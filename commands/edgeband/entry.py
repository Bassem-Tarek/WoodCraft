# WoodCraft — a Fusion add-in for cabinetmaking.
# Copyright (C) 2026 Abdelrahman Youssry
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <https://www.gnu.org/licenses/>.

"""Edgeband — tag panel edge faces with an edgeband type for the BOM.

Pick the faces to band (a panel's thin edge faces — the 18 mm-wide strips) and
choose an edgeband from the Sheets library's band catalogue; each face is stamped
with a WC_EDGEBAND attribute holding the band's NAME. The BOM then sums the
tagged lengths per band type across the whole design (face area ÷ panel
thickness, so a curved edge counts its true arc length) and prices the metres
from the catalogue's cost per metre. Nothing but the name is stored on the face —
cost lives in the library, so re-pricing a band re-prices every past design.

Tagging also TINTS the face in the viewport: a design-local appearance named
after the band (colour = the band's library colour) is applied as a face
override, so banded edges read at a glance. Removing the tag clears the
override. The tint is cosmetic — the BOM reads only the attribute.

Bands are edited in the Sheets palette (same sheets.json file as the stock
sheets). The last dropdown entry removes an existing tag instead.
"""

import os

import adsk.core
import adsk.fusion

from .. import ui_helpers
from .. import wc_attrs
from .. import panels
from .. import sheets_store
from ...lib import fusionAddInUtils as futil
from ... import config

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_edgeband'
CMD_NAME = 'Edgeband'
CMD_Description = (
    'Tag the selected panel edge faces with an edgeband type from the Sheets '
    'library. The BOM totals the tagged edge length per band type and prices it '
    'at the library\'s cost per metre. Pick the remove entry to strip a tag.'
)
IS_PROMOTED = True

PANEL_ID = config.CABINET_PANEL_ID
PANEL_NAME = config.CABINET_PANEL_NAME

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

SEL_ID = 'eb_faces'
BAND_ID = 'eb_band'
SCOPE_ID = 'eb_scope'
EXPOSED_ID = 'eb_exposed'
DETECT_ID = 'eb_detect'
STATUS_ID = 'eb_status'

# "Only exposed edges" probes each edge face from points just OUTSIDE it (along
# the face normal); a probe landing INSIDE another panel body means the face is
# butted against it (a joint). Probing — rather than minimum distance — is what
# keeps corner-line contact (two exposed front edges meeting at an arris) from
# being mistaken for a joint. Fusion's internal unit is cm.
PROBE_OFFSET_CM = 0.02    # 0.2 mm outside the face

REMOVE_LABEL = '— Remove edgeband —'

# Copied from an Appearance-library paint to make each band's tint (newer and
# older Fusion library names).
APPEARANCE_LIBS = ('Fusion Appearance Library', 'Fusion 360 Appearance Library')
BASE_APPEARANCE = 'Paint - Enamel Glossy (Yellow)'

# Rebuilt on every dialog open from the Sheets library: [(label, band dict|None)].
# None = the remove entry. Module state is safe: one command dialog at a time.
band_choices = []

# Auto-detected face proxies that could NOT be added to the selection input
# (Fusion only lets addSelection reach the ACTIVE input, and focus handoff can
# fail). They are banded on OK anyway; cleared on every Detect and on destroy.
detected_stash = []

local_handlers = []


def start():
    cmd_def = ui.commandDefinitions.itemById(CMD_ID)
    if not cmd_def:
        cmd_def = ui.commandDefinitions.addButtonDefinition(
            CMD_ID, CMD_NAME, CMD_Description, ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)

    panel = ui_helpers.get_panel(PANEL_ID, PANEL_NAME)
    control = panel.controls.addCommand(cmd_def)
    control.isPromoted = IS_PROMOTED


def stop():
    ui_helpers.remove_command(PANEL_ID, CMD_ID)


def _band_label(band):
    """Dropdown label: name + the catalogue's size when present, e.g.
    'PVC Oak 2 mm  (22 × 2 mm)'."""
    width = band.get('width') or 0
    thickness = band.get('thickness') or 0
    if width and thickness:
        return f"{band['name']}  ({width:g} × {thickness:g} mm)"
    return band['name']


def command_created(args: adsk.core.CommandCreatedEventArgs):
    futil.log(f'{CMD_NAME} Command Created Event')
    inputs = args.command.commandInputs

    sel = inputs.addSelectionInput(
        SEL_ID, 'Edge faces',
        'Select the panel edge faces to band (the thin thickness-side strips), '
        'or use Auto-detect below')
    sel.addSelectionFilter('Faces')
    # Minimum 0, NOT 1: Fusion gates OK on selection minimums before our
    # validateInputs ever runs, and auto-detected faces may live only in
    # detected_stash (when addSelection refuses them) — validate handles the
    # "needs at least one face somewhere" rule instead.
    sel.setSelectionLimits(0, 0)

    # --- Auto-detect group: pick a cabinet (or nothing = whole design), click
    # Detect, and every panel edge face under it is added to the selection.
    group = inputs.addGroupCommandInput('eb_auto_group', 'Auto-detect')
    group.isExpanded = True
    gi = group.children

    scope = gi.addSelectionInput(
        SCOPE_ID, 'Cabinet / scope',
        'Limit detection to these assemblies or components. Leave empty to scan '
        'every panel in the design.')
    scope.addSelectionFilter('Occurrences')
    scope.setSelectionLimits(0, 0)

    exposed = gi.addBoolValueInput(EXPOSED_ID, 'Only exposed edges', True, '', True)
    exposed.tooltip = ('Skip edge faces that touch another panel (butt joints — '
                       'gables, backs). Off = collect every edge face.')

    detect = gi.addBoolValueInput(DETECT_ID, 'Detect edge faces', False, '', False)
    detect.text = 'Detect'
    detect.tooltip = ('Find the edge (thickness-side) faces of every panel in the '
                      'scope and add them to the selection. Drilled holes are '
                      'skipped; rounded corners are included. You can still '
                      'Ctrl-click to drop faces you don\'t want banded.')

    status = gi.addTextBoxCommandInput(STATUS_ID, '', '', 2, True)
    status.isFullWidth = True

    global band_choices
    band_choices = [(_band_label(b), b)
                    for b in sheets_store.load()['edgebands']]
    band_choices.append((REMOVE_LABEL, None))

    dd = inputs.addDropDownCommandInput(
        BAND_ID, 'Edgeband', adsk.core.DropDownStyles.TextListDropDownStyle)
    for i, (label, _value) in enumerate(band_choices):
        dd.listItems.add(label, i == 0)
    dd.tooltip = ('Band types come from the Sheets palette (Edgebands section), '
                  'where each carries its cost per metre for the BOM.')

    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.inputChanged, command_input_changed,
                      local_handlers=local_handlers)
    futil.add_handler(args.command.validateInputs, command_validate_input,
                      local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)


def _selected_band(inputs):
    """The chosen band dict, or None for the remove entry."""
    dd = inputs.itemById(BAND_ID)
    idx = dd.selectedItem.index if dd and dd.selectedItem else 0
    return band_choices[idx][1] if 0 <= idx < len(band_choices) else None


# ---------------------------------------------------------------------------
# Viewport tint (face appearance override in the band's library colour)
# ---------------------------------------------------------------------------
def _hex_to_rgb(hex_str):
    """(r, g, b) from '#RRGGBB', falling back to the library's default tan."""
    try:
        s = str(hex_str or '').lstrip('#')
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except (ValueError, IndexError):
        return 201, 168, 106      # sheets_store.DEFAULT_COLOR

def _band_appearance(design, band):
    """Get-or-create this band's design-local appearance ('WoodCraft Edgeband —
    <name>'), with its colour synced to the band's CURRENT library colour. None
    when no base appearance could be found (tint is then skipped — the tag still
    works; the tint is cosmetic only)."""
    name = f'WoodCraft Edgeband — {band["name"]}'
    appearance = None
    try:
        appearance = design.appearances.itemByName(name)
    except Exception:
        pass
    if not appearance:
        base = None
        for lib_name in APPEARANCE_LIBS:
            try:
                lib = app.materialLibraries.itemByName(lib_name)
                if lib:
                    base = lib.appearances.itemByName(BASE_APPEARANCE)
                    if base:
                        break
            except Exception:
                continue
        if not base:
            futil.log(f'{CMD_NAME}: no base appearance found — skipping face tint')
            return None
        try:
            appearance = design.appearances.addByCopy(base, name)
        except Exception:
            return None
    try:
        r, g, b = _hex_to_rgb(band.get('color'))
        prop = appearance.appearanceProperties.itemByName('Color')
        if prop:
            prop.value = adsk.core.Color.create(r, g, b, 255)
    except Exception:
        pass    # wrong colour is still a visible tint — keep going
    return appearance


def _native_face(entity):
    """Resolve a selected face to its NATIVE BRepFace. A face picked on an
    occurrence is a proxy; writing the attribute on the native face makes the
    band per-component (every occurrence of the panel gets it — matching how the
    BOM multiplies one instance's banding by the quantity)."""
    face = adsk.fusion.BRepFace.cast(entity)
    if not face:
        return None
    try:
        return face.nativeObject or face
    except Exception:
        return face


# ---------------------------------------------------------------------------
# Auto-detect (one-shot Detect button — never reacts to the selection input
# itself, per the selection-reentrancy rule)
# ---------------------------------------------------------------------------
def _panel_occurrences(design, scope_occs):
    """[(occurrence|None, component)] for every classified panel under the scope
    occurrences (or the whole design when scope is empty). Occurrences are root-
    context proxies, so their faces/bodies proxy into world space. None stands
    for the root component itself (a part document).

    Countertops count too (wc_attrs.is_sheet_like, not is_panel): a worktop's
    exposed front edge is banded exactly like a panel's, even though the slab
    itself is never nested."""
    result = []
    seen = set()    # occurrence fullPathName / root marker — guards double scope picks

    def add(occ, comp):
        key = getattr(occ, 'fullPathName', None) or '<root>'
        if key in seen:
            return
        seen.add(key)
        result.append((occ, comp))

    def walk(occ):
        comp = occ.component
        if wc_attrs.is_sheet_like(comp):
            add(occ, comp)
        try:
            children = occ.childOccurrences
        except Exception:
            return
        for i in range(children.count):
            walk(children.item(i))

    if scope_occs:
        for occ in scope_occs:
            walk(occ)
    else:
        root = design.rootComponent
        if wc_attrs.is_sheet_like(root):
            add(None, root)
        occs = root.occurrences
        for i in range(occs.count):
            walk(occs.item(i))
    return result


def _face_proxy(face, occ):
    """`face` as seen in the root context (world space), or the native face for
    a root-component panel."""
    if occ is None:
        return face
    try:
        return face.createForAssemblyContext(occ) or face
    except Exception:
        return face


def _box(bb):
    """A BoundingBox3D as a plain (x0, y0, z0, x1, y1, z1) tuple — read once, so
    the joint test below compares floats instead of making six API calls per
    face/body pair."""
    lo, hi = bb.minPoint, bb.maxPoint
    return (lo.x, lo.y, lo.z, hi.x, hi.y, hi.z)


def _boxes_overlap(a, b, tol_cm):
    return (a[0] - tol_cm <= b[3] and b[0] - tol_cm <= a[3]
            and a[1] - tol_cm <= b[4] and b[1] - tol_cm <= a[4]
            and a[2] - tol_cm <= b[5] and b[2] - tol_cm <= a[5])


def _bbox_overlaps(a, b, tol_cm):
    return (a.minPoint.x - tol_cm <= b.maxPoint.x and b.minPoint.x - tol_cm <= a.maxPoint.x
            and a.minPoint.y - tol_cm <= b.maxPoint.y and b.minPoint.y - tol_cm <= a.maxPoint.y
            and a.minPoint.z - tol_cm <= b.maxPoint.z and b.minPoint.z - tol_cm <= a.maxPoint.z)


def _outward_probe_points(face):
    """A few world-space points hovering PROBE_OFFSET_CM outside `face` (sampled
    across its parametric range, offset along the outward normal — Fusion face
    normals point out of the solid). Empty list when the evaluator won't play."""
    pts = []
    try:
        ev = face.evaluator
        rng = ev.parametricRange()
        if not rng:
            return pts
        u0, v0 = rng.minPoint.x, rng.minPoint.y
        u1, v1 = rng.maxPoint.x, rng.maxPoint.y
        for fu, fv in ((0.5, 0.5), (0.25, 0.5), (0.75, 0.5), (0.5, 0.25), (0.5, 0.75)):
            param = adsk.core.Point2D.create(u0 + (u1 - u0) * fu, v0 + (v1 - v0) * fv)
            try:
                if not ev.isParameterOnFace(param):
                    continue    # hole/trim in the face — sample elsewhere
            except Exception:
                pass
            ok, pt = ev.getPointAtParameter(param)
            if not ok:
                continue
            ok, normal = ev.getNormalAtPoint(pt)
            if not ok:
                continue
            normal.scaleBy(PROBE_OFFSET_CM)
            pt.translateBy(normal)
            pts.append(pt)
    except Exception:
        pass
    return pts


def _touches_another_panel(face_proxy, own_body_token, panel_bodies):
    """True when this (world-space) face lies against any OTHER panel body — a
    butt joint, so the edge isn't exposed. Probes points just outside the face:
    inside a neighbour ⇒ joint. Corner-line contact (two exposed edges meeting
    at an arris) probes into empty air, so it correctly stays bandable.
    Bounding boxes prefilter which bodies pay for containment tests — and when no
    other panel is even near the face, the probe points are never computed."""
    try:
        fbb = _box(face_proxy.boundingBox)
    except Exception:
        return False
    near = [body for token, body, box in panel_bodies
            if token != own_body_token and box is not None
            and _boxes_overlap(fbb, box, 2 * PROBE_OFFSET_CM)]
    if not near:
        return False
    probes = _outward_probe_points(face_proxy)
    if not probes:
        return False
    inside = adsk.fusion.PointContainment.PointInsidePointContainment
    for body in near:
        try:
            for pt in probes:
                if body.pointContainment(pt) == inside:
                    return True
        except Exception:
            continue
    return False


def _detect_edge_faces(inputs):
    """Fill the faces selection with every bandable panel edge face in the scope.
    Returns a status line for the dialog."""
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        return 'No active design.'
    sel = inputs.itemById(SEL_ID)
    scope_input = inputs.itemById(SCOPE_ID)
    exposed_only = inputs.itemById(EXPOSED_ID).value

    # Snapshot the scope BEFORE touching the faces selection: addSelection on one
    # input can invalidate another input's live selection list.
    scope_occs = []
    for i in range(scope_input.selectionCount):
        ent = scope_input.selection(i).entity
        occ = adsk.fusion.Occurrence.cast(ent)
        if occ:
            scope_occs.append(occ)

    instances = _panel_occurrences(design, scope_occs)
    if not instances:
        return ('No classified panels found in the scope. Panels are tagged by '
                'Carcass Maker / Shelf Creator or by hand with Set Type.')

    # World-space (proxy) bodies of every panel in scope — the joint test set.
    panel_bodies = []
    for occ, comp in instances:
        try:
            bodies = comp.bRepBodies
            for bi in range(bodies.count):
                body = bodies.item(bi)
                proxy = body if occ is None else (body.createForAssemblyContext(occ) or body)
                try:
                    box = _box(proxy.boundingBox)
                except Exception:
                    box = None
                panel_bodies.append((f'{getattr(occ, "fullPathName", "<root>")}#{bi}',
                                     proxy, box))
        except Exception:
            continue

    # addSelection only works on the ACTIVE selection input — after picking the
    # scope, THAT input has focus and every add would silently return False.
    # Grab focus and flush the UI queue so the change lands before we add.
    try:
        sel.hasFocus = True
        adsk.doEvents()
    except Exception:
        pass

    global detected_stash
    detected_stash = []
    found = 0
    added = 0
    skipped_joints = 0
    # The same cabinet placed several times shares its components: find each
    # component's bandable faces (and which body owns them) once, not per placement.
    face_cache = {}
    for occ, comp in instances:
        own_prefix = f'{getattr(occ, "fullPathName", "<root>")}#'
        try:
            comp_key = comp.id
        except Exception:
            comp_key = None
        # Ids are only a bucket (distinct components can share one — see Set
        # Finish's _ComponentSet); equality decides.
        faces = None
        bucket = face_cache.setdefault(comp_key, []) if comp_key else []
        for other, other_faces in bucket:
            try:
                if other == comp:
                    faces = other_faces
                    break
            except Exception:
                continue
        if faces is None:
            faces = [(face, _body_index(comp, face))
                     for face in panels.bandable_faces(comp)]
            bucket.append((comp, faces))
        for face, body_index in faces:
            found += 1
            proxy = _face_proxy(face, occ)
            own_token = own_prefix + str(body_index)
            if exposed_only and _touches_another_panel(proxy, own_token, panel_bodies):
                skipped_joints += 1
                continue
            try:
                if sel.addSelection(proxy):
                    added += 1
                else:
                    detected_stash.append(proxy)
            except Exception:
                detected_stash.append(proxy)

    if not found:
        return (f'No edge faces found on {len(instances)} panel(s) — are their '
                f'bodies thin slabs with two broad faces?')
    msg = f'Added {added} edge face(s) from {len(instances)} panel(s).'
    if detected_stash:
        # Selection UI refused them, but the tags don't need it: OK bands these.
        msg = (f'Detected {added + len(detected_stash)} edge face(s) from '
               f'{len(instances)} panel(s); {len(detected_stash)} could not be '
               f'shown in the selection but WILL be banded on OK.')
    if skipped_joints:
        msg += f' Skipped {skipped_joints} touching another panel (joints).'
    if added and not detected_stash:
        msg += ' Ctrl-click to drop any you don\'t want.'
    return msg


def _body_index(component, face):
    """Index of the body owning `face` within its component (joins the face to
    the panel_bodies token scheme)."""
    try:
        owner = face.body
        bodies = component.bRepBodies
        for bi in range(bodies.count):
            if bodies.item(bi) == owner:
                return bi
    except Exception:
        pass
    return 0


def command_input_changed(args: adsk.core.InputChangedEventArgs):
    # ONLY the Detect button — deliberately no reaction to selection changes.
    # The value guard skips the event fired by our own reset back to False.
    if args.input.id != DETECT_ID or not args.input.value:
        return
    inputs = args.inputs
    try:
        status = _detect_edge_faces(inputs)
    except Exception as e:
        futil.handle_error(f'{CMD_NAME} auto-detect')
        status = f'Auto-detect failed: {e}'
    box = inputs.itemById(STATUS_ID)
    if box:
        box.text = status
    args.input.value = False    # reset the one-shot button


def command_validate_input(args: adsk.core.ValidateInputsEventArgs):
    sel = args.inputs.itemById(SEL_ID)
    args.areInputsValid = bool((sel and sel.selectionCount > 0) or detected_stash)


def command_execute(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Execute Event')
    inputs = args.command.commandInputs
    sel: adsk.core.SelectionCommandInput = inputs.itemById(SEL_ID)
    band = _selected_band(inputs)
    band_name = band['name'] if band else None

    # Snapshot every selected face BEFORE writing: adding an attribute mutates the
    # document, which invalidates the selection input's list mid-loop (see the
    # Set Type command / project memory). Auto-detected faces the selection UI
    # refused (detected_stash) are banded too — de-duped against the selection
    # by (component, body, face) identity.
    faces = []
    seen = set()

    def face_key(face):
        try:
            return (face.body.parentComponent.name, face.body.name, face.tempId)
        except Exception:
            return id(face)

    for i in range(sel.selectionCount):
        face = _native_face(sel.selection(i).entity)
        if face and face_key(face) not in seen:
            seen.add(face_key(face))
            faces.append(face)
    for proxy in detected_stash:
        face = _native_face(proxy)
        if face and face_key(face) not in seen:
            seen.add(face_key(face))
            faces.append(face)

    # One appearance per band, resolved once against the active design.
    appearance = None
    if band is not None:
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design:
                appearance = _band_appearance(design, band)
        except Exception:
            appearance = None

    done = 0
    skipped = 0
    length_mm = 0.0
    for face in faces:
        if band_name is None:
            if wc_attrs.remove_edgeband(face):
                done += 1
                try:
                    face.appearance = None      # drop the tint override too
                except Exception:
                    pass
            continue
        if not wc_attrs.set_edgeband(face, band_name):
            skipped += 1
            futil.log(f'{CMD_NAME}: could not tag a face (referenced/read-only?)')
            continue
        done += 1
        if appearance is not None:
            try:
                face.appearance = appearance    # tint the edge in the band colour
            except Exception:
                pass
        try:
            comp = face.body.parentComponent
            dims = panels.panel_dims_mm(comp)
            length_mm += panels.face_edgeband_length_mm(face, dims[2] if dims else None)
        except Exception:
            pass    # length is only for the message; the tag itself succeeded

    if band_name is None:
        msg = f'Removed the edgeband tag (and tint) from {done} face(s).'
    else:
        msg = (f'Tagged {done} face(s) with "{band_name}" — '
               f'≈ {length_mm / 1000.0:.2f} m of banding.\n'
               f'The BOM totals and prices all tagged edges per band type.')
    if skipped:
        msg += (f'\n{skipped} face(s) could not be tagged — likely a referenced/'
                f'read-only component. Open its source design to band those.')
    ui.messageBox(msg)


def command_destroy(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Destroy Event')
    global local_handlers, detected_stash
    local_handlers = []
    detected_stash = []
