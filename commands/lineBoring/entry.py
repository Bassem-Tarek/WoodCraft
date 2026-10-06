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

"""Line Boring — drill shelf-pin holes into a panel from a chosen rule.

Pick the inner face(s) of a side/gable panel, pick the bottom and top panels the
holes are split between, choose a boring *rule* and a few numbers (shelf count,
setbacks, hole size), and the command bores the shelf-pin hole pattern. The
geometry and the rule catalogue live in commands/boring.py; this file is just the
Fusion command around them.

The default rule is **Emaar**: three-hole sets (a middle hole plus one a pitch
above and below) in two columns set in from the front and back edges. The sets are
placed so the SHELF FACES divide the clear opening between the bottom and top
panels into N+1 equal gaps — shelf thickness and the pin rise (a shelf rests on the
pin, so it sits above the hole centre) are taken out of the arithmetic first, which
is what makes the gap under the first shelf match the gap over the last one. Leave the top/bottom selection empty and the side panel's own ends are used
instead (the old whole-panel behaviour).

Live-parametric, fully defined build: per panel the command creates wc_lb_* user
parameters and a feature tree driven by them:
  LB Opening sketch (bottom/top datums + driven opening dimension, hidden)
  -> LB Holes sketch (6 seed points, each locked by two driving dimensions)
  -> seed HoleFeature -> rectangular pattern.
Every datum is the picked faces intersected with the sketch plane or the side
panel's projected edges, so both sketches are fully constrained and moving a
panel, changing a thickness or resizing the cabinet reflows the holes. If the live
build fails it rolls back and drills the same holes in a fixed (but still fully
dimensioned) sketch, and tells the user which panels that happened to.
"""

import os

import adsk.core
import adsk.fusion

from .. import ui_helpers
from .. import boring
from ...lib import fusionAddInUtils as futil
from ... import config

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_lineBoring'
CMD_NAME = 'Line Boring'
CMD_Description = (
    'Bore shelf-pin holes into a panel from a chosen rule (e.g. Emaar): 3-hole sets '
    'split evenly between the bottom and top panels, in two columns, built '
    'live-parametric.'
)
IS_PROMOTED = True

PANEL_ID = config.CABINET_PANEL_ID
PANEL_NAME = config.CABINET_PANEL_NAME

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

# Input ids.
FACES_ID = 'lb_faces'
BACK_PANEL_ID = 'lb_back_panel'
SPAN_ID = 'lb_span'
FRONT_EDGE_ID = 'lb_front_edge'
N_ID = 'lb_n'
FRONT_ID = 'lb_front'
BACK_ID = 'lb_back'
PITCH_ID = 'lb_pitch'
DIA_ID = 'lb_dia'
SHELF_ID = 'lb_shelf'
RISE_ID = 'lb_rise'
DEPTH_ID = 'lb_depth'

PREVIEW_SEG_CM = 0.4   # length of each preview "drill mark" along the bore direction

local_handlers = []
_graphics_group = None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def start():
    cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_Description, ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)

    panel = ui_helpers.get_panel(PANEL_ID, PANEL_NAME)
    control = panel.controls.addCommand(cmd_def)
    control.isPromoted = IS_PROMOTED


def stop():
    ui_helpers.remove_command(PANEL_ID, CMD_ID)


def command_created(args: adsk.core.CommandCreatedEventArgs):
    futil.log(f'{CMD_NAME} Command Created Event')

    # Reset state before building inputs. Keep this body non-throwing: the command
    # template swallows exceptions raised in command_created, which would silently
    # leave execute/inputChanged unregistered (dialog opens but does nothing).
    global _graphics_group
    _graphics_group = None

    inputs = args.command.commandInputs
    length_units = app.activeProduct.unitsManager.defaultLengthUnits
    defaults = boring.EmaarRule.DEFAULTS

    faces = inputs.addSelectionInput(FACES_ID, 'Panel face(s)', 'Pick the inner face of each side panel to bore')
    faces.addSelectionFilter('PlanarFaces')
    faces.setSelectionLimits(1, 0)

    back_panel = inputs.addSelectionInput(
        BACK_PANEL_ID, 'Back panel face',
        'Pick the back panel\'s front face — the back column tracks this face (projected into the sketch)')
    back_panel.addSelectionFilter('PlanarFaces')
    back_panel.setSelectionLimits(0, 1)

    span = inputs.addSelectionInput(
        SPAN_ID, 'Bottom / top panels',
        'Pick the bottom panel\'s TOP face and the top panel\'s BOTTOM face — the '
        'holes are split evenly between them; leave empty to use the side panel\'s '
        'own ends')
    span.addSelectionFilter('PlanarFaces')
    span.setSelectionLimits(0, 2)

    front_edge = inputs.addSelectionInput(
        FRONT_EDGE_ID, 'Front edge (optional)',
        'Pick the panel\'s front edge for the front column to track; leave empty to auto-detect it')
    front_edge.addSelectionFilter('LinearEdges')
    front_edge.setSelectionLimits(0, 1)

    inputs.addIntegerSpinnerCommandInput(N_ID, 'Shelves (N)', 1, 200, 1, int(defaults['n']))

    inputs.addValueInput(FRONT_ID, 'Front setback', length_units,
                         adsk.core.ValueInput.createByReal(defaults['front']))
    inputs.addValueInput(BACK_ID, 'Back setback', length_units,
                         adsk.core.ValueInput.createByReal(defaults['back']))
    inputs.addValueInput(PITCH_ID, 'Set pitch', length_units,
                         adsk.core.ValueInput.createByReal(defaults['pitch']))
    inputs.addValueInput(DIA_ID, 'Hole diameter', length_units,
                         adsk.core.ValueInput.createByReal(defaults['dia']))
    inputs.addValueInput(DEPTH_ID, 'Hole depth', length_units,
                         adsk.core.ValueInput.createByReal(defaults['depth']))
    inputs.addValueInput(SHELF_ID, 'Shelf thickness', length_units,
                         adsk.core.ValueInput.createByReal(defaults['shelf']))
    inputs.addValueInput(RISE_ID, 'Pin rise', length_units,
                         adsk.core.ValueInput.createByReal(defaults['rise']))

    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.executePreview, command_preview, local_handlers=local_handlers)
    futil.add_handler(args.command.validateInputs, command_validate_input, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)


# ---------------------------------------------------------------------------
# Helpers shared by preview + execute
# ---------------------------------------------------------------------------
def _read_params(inputs: adsk.core.CommandInputs) -> dict:
    return {
        'n': inputs.itemById(N_ID).value,
        'front': inputs.itemById(FRONT_ID).value,
        'back': inputs.itemById(BACK_ID).value,
        'pitch': inputs.itemById(PITCH_ID).value,
        'dia': inputs.itemById(DIA_ID).value,
        'depth': inputs.itemById(DEPTH_ID).value,
        'shelf': inputs.itemById(SHELF_ID).value,
        'rise': inputs.itemById(RISE_ID).value,
    }


def _face_world_point(ent):
    """A world-space point ON a picked planar face. A point on the face is enough:
    projected onto one of the side panel's axes it gives that face's position along
    it, which is all these datums need."""
    try:
        return ent.centroid                      # BRepFace.centroid lies on the face
    except Exception:
        bb = ent.boundingBox
        return adsk.core.Point3D.create(
            (bb.minPoint.x + bb.maxPoint.x) / 2.0,
            (bb.minPoint.y + bb.maxPoint.y) / 2.0,
            (bb.minPoint.z + bb.maxPoint.z) / 2.0)


def _back_ref_world(inputs: adsk.core.CommandInputs):
    """A world-space point on the picked back panel's front face (or None) — the
    datum the back hole column is measured forward of, so a recessed back is
    measured correctly (it also orients front/back)."""
    sel = inputs.itemById(BACK_PANEL_ID)
    if sel.selectionCount == 0:
        return None
    return _face_world_point(sel.selection(0).entity)


def _span_selection(inputs: adsk.core.CommandInputs):
    """The picked bottom/top panel faces as ``[(face, world point), ...]`` (0-2, in
    pick order — ordering along the panel happens in _span_ends)."""
    sel = inputs.itemById(SPAN_ID)
    return [(sel.selection(i).entity, _face_world_point(sel.selection(i).entity))
            for i in range(sel.selectionCount)]


def _span_native(panel_face, span_sel):
    """``span_sel`` with its points mapped into ``panel_face``'s native space, for
    the native-space frame used by validation and the explicit fallback."""
    return [(face, _to_native_point(panel_face, pt)) for face, pt in span_sel]


def _span_ends(fr, span_sel):
    """Order the picked span faces along the side panel's height and return
    ``(bottom, top)``, each ``(height offset, face, point)`` or None. One picked
    face is taken as the bottom or the top by which half of the side panel it lies
    in, so the other end falls back to the side panel's own edge."""
    items = sorted(
        ((fr.origin.vectorTo(pt).dotProduct(fr.height_dir), face, pt)
         for face, pt in span_sel if pt is not None),
        key=lambda item: item[0])   # key: BRepFace has no ordering for tie-breaks
    if not items:
        return None, None
    if len(items) == 1:
        return (items[0], None) if items[0][0] < fr.height / 2.0 else (None, items[0])
    return items[0], items[-1]


def _span_heights(fr, span_sel):
    """``(bottom_h, top_h)`` height offsets for the rule; None where not picked."""
    bottom, top = _span_ends(fr, span_sel)
    return (bottom[0] if bottom else None, top[0] if top else None)


def _to_native_point(face, world_pt):
    """Map a world point into ``face``'s native (component) space, so it can be
    compared against a frame built from the native face inside an occurrence."""
    if world_pt is None:
        return None
    occ = face.assemblyContext
    if not occ:
        return world_pt
    inv = occ.transform.copy()
    if inv.invert():
        p = world_pt.copy()
        p.transformBy(inv)
        return p
    return world_pt


def _back_depth(fr, back_point):
    """The back panel's depth position along a side panel's depth axis (distance
    from the panel's back edge/origin), or None. This is the datum the back column
    is measured forward of."""
    if back_point is None:
        return None
    return fr.origin.vectorTo(back_point).dotProduct(fr.depth_dir)


def _translated(pt: adsk.core.Point3D, vec: adsk.core.Vector3D, dist: float) -> adsk.core.Point3D:
    out = pt.copy()
    v = vec.copy()
    v.scaleBy(dist)
    out.translateBy(v)
    return out


# ---------------------------------------------------------------------------
# Preview: a red "drill mark" into the panel at each hole centre.
# ---------------------------------------------------------------------------
def command_preview(args: adsk.core.CommandEventArgs):
    global _graphics_group
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        root = design.rootComponent
        inputs = args.command.commandInputs

        if _graphics_group:
            try:
                _graphics_group.deleteMe()
            except Exception:
                pass
            _graphics_group = None

        faces_input: adsk.core.SelectionCommandInput = inputs.itemById(FACES_ID)
        if faces_input.selectionCount == 0:
            app.activeViewport.refresh()
            return

        params = _read_params(inputs)
        rule = boring.RULES[0]
        bp_world = _back_ref_world(inputs)   # preview uses world-space proxy faces
        span_sel = _span_selection(inputs)

        group = root.customGraphicsGroups.add()
        red = adsk.fusion.CustomGraphicsSolidColorEffect.create(adsk.core.Color.create(239, 68, 68, 255))
        label_color = adsk.fusion.CustomGraphicsSolidColorEffect.create(adsk.core.Color.create(255, 200, 0, 255))

        for i in range(faces_input.selectionCount):
            face = faces_input.selection(i).entity
            try:
                fr = boring.frame(face, back_ref_point=bp_world)
                p = dict(params)
                p['back_depth'] = _back_depth(fr, bp_world)   # preview is all world-space
                p['bottom_h'], p['top_h'] = _span_heights(fr, span_sel)
                pts = rule.preview_points(fr, p)
            except Exception:
                continue
            if not pts:
                continue

            coords = []
            idx = []
            for pt in pts:
                tip = _translated(pt, fr.bore_dir, PREVIEW_SEG_CM)
                base = len(coords) // 3
                coords += [pt.x, pt.y, pt.z, tip.x, tip.y, tip.z]
                idx += [base, base + 1]
            cg = adsk.fusion.CustomGraphicsCoordinates.create(coords)
            line = group.addLines(cg, idx, False)
            line.color = red
            line.weight = 4

            # Count + opening label floated off the face centre; warn when orientation
            # was guessed. The opening is what the sets are split between, so showing
            # it makes a mis-picked top/bottom face obvious before OK.
            b = p['bottom_h'] if p['bottom_h'] is not None else 0.0
            t = p['top_h'] if p['top_h'] is not None else fr.height
            gap = boring.clear_gap(b, t, int(p['n']), p['shelf'])
            label = (f'{len(pts)} holes  |  {(t - b) / boring.MM:.0f} mm opening  |  '
                     f'{int(p["n"]) + 1} x {gap / boring.MM:.1f} mm clear')
            if fr.ambiguous:
                label += '  (orientation guessed — pick the back panel face)'
            label_pt = _translated(fr.point(fr.height / 2.0, fr.depth / 2.0), fr.normal, 1.0)
            transform = adsk.core.Matrix3D.create()
            transform.translation = label_pt.asVector()
            text = group.addText(label, 'Arial', 1.6, transform)
            text.billBoarding = adsk.fusion.CustomGraphicsBillBoard.create(label_pt)
            text.color = label_color

        _graphics_group = group
        app.activeViewport.refresh()
    except Exception:
        futil.handle_error('Line Boring: preview')


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------
def command_validate_input(args: adsk.core.ValidateInputsEventArgs):
    inputs = args.inputs
    faces_ok = inputs.itemById(FACES_ID).selectionCount >= 1
    n_ok = inputs.itemById(N_ID).value >= 1
    sizes_ok = (inputs.itemById(DIA_ID).value > 0
                and inputs.itemById(DEPTH_ID).value > 0
                and inputs.itemById(PITCH_ID).value > 0)
    setbacks_ok = inputs.itemById(FRONT_ID).value >= 0 and inputs.itemById(BACK_ID).value >= 0
    shelf_ok = inputs.itemById(SHELF_ID).value >= 0 and inputs.itemById(RISE_ID).value >= 0
    # Per-panel geometry checks (depth/height-dependent) stay in EmaarRule.validate,
    # surfaced as a messageBox on OK; here we only gate the cheap, panel-independent ones.
    args.areInputsValid = faces_ok and n_ok and sizes_ok and setbacks_ok and shelf_ok


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------
def command_execute(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Execute Event')
    inputs = args.command.commandInputs

    # Snapshot every selection and value BEFORE any document write — the first
    # feature invalidates the live selection list and face proxies.
    faces_input: adsk.core.SelectionCommandInput = inputs.itemById(FACES_ID)
    faces = [faces_input.selection(i).entity for i in range(faces_input.selectionCount)]
    back_input: adsk.core.SelectionCommandInput = inputs.itemById(BACK_PANEL_ID)
    back_proxy = back_input.selection(0).entity if back_input.selectionCount else None
    front_input: adsk.core.SelectionCommandInput = inputs.itemById(FRONT_EDGE_ID)
    front_edge = front_input.selection(0).entity if front_input.selectionCount else None
    span_sel = _span_selection(inputs)           # [(face, world point)] for bottom/top
    rule = boring.RULES[0]
    params = _read_params(inputs)
    bp_world = _back_ref_world(inputs)           # world point on the back panel face

    bored = 0
    fallbacks = []   # (panel name, reason) for panels that could not be built live-parametric
    for face in faces:
        try:
            native = face.nativeObject if face.nativeObject else face
            comp = native.body.parentComponent
            # Validate against a native-space frame (cheap, geometry-only).
            up, front_refs = _ref_axes(face)
            bp_native = _to_native_point(face, bp_world)
            fr_native = boring.frame(native, up=up, front_refs=front_refs, back_ref_point=bp_native)
            p_native = dict(params)
            p_native['back_depth'] = _back_depth(fr_native, bp_native)
            p_native['bottom_h'], p_native['top_h'] = _span_heights(
                fr_native, _span_native(face, span_sel))
            rule.validate(fr_native, p_native)   # raises ValueError with a message
            reason = _bore_one(comp, face, native, back_proxy, front_edge, bp_world, span_sel, params)
            if reason:
                fallbacks.append((comp.name, reason))
            bored += 1
        except ValueError as ve:
            ui.messageBox(str(ve), CMD_NAME)
        except Exception:
            futil.handle_error(f'{CMD_NAME}: failed to bore a panel')

    if fallbacks:
        # Never fall back silently: the user needs to know these holes will NOT
        # follow later model changes.
        lines = '\n'.join(f'• {name}: {why}' for name, why in fallbacks)
        ui.messageBox(
            'The holes were drilled, but on these panels they are not fully linked to '
            'the model and may not follow later size changes:\n\n'
            f'{lines}', CMD_NAME)

    if bored == 0 and faces:
        ui.messageBox('No panels were bored — see the messages above.', CMD_NAME)


def _ref_axes(face):
    """World up / front-reference vectors expressed in the FACE's own coordinate
    space. For a face selected inside an occurrence, the native geometry lives in
    component space, so the world axes are mapped through the inverse occurrence
    transform; for a root-level face there is no occurrence and they stay the world
    axes. This keeps boring.frame()'s orientation consistent with the native
    geometry it measures, even for a rotated occurrence."""
    up = adsk.core.Vector3D.create(0, 0, 1)
    # Front faces -Y (Fusion's Front view convention) — see boring.frame().
    fy = adsk.core.Vector3D.create(0, -1, 0)
    fx = adsk.core.Vector3D.create(-1, 0, 0)
    occ = face.assemblyContext
    if occ:
        inv = occ.transform.copy()
        if inv.invert():
            up.transformBy(inv)
            fy.transformBy(inv)
            fx.transformBy(inv)
    return up, [fy, fx]


def _set_bore_direction(hole_input, sketch, fr):
    """Make the hole drill INTO the panel material regardless of how the sketch
    plane ends up oriented. A cabinet's two gables have mirror-opposite — and often
    topologically reversed — faces, so a fixed flip is correct for one and wrong for
    the other. The hole's default direction is opposite the sketch's own normal, so
    compare that normal to the into-material bore direction and reverse the hole
    when they agree. (The property is ``isDefaultDirection``; the API silently
    accepts misspelt attributes, so a wrong name here fails without any error.)"""
    sk_normal = sketch.xDirection.crossProduct(sketch.yDirection)
    sk_normal.normalize()
    hole_input.isDefaultDirection = not (sk_normal.dotProduct(fr.bore_dir) > 0)


def _bore_one(comp, proxy_face, native_face, back_proxy, front_edge, bp_world, span_sel, params):
    """Try the live-parametric build (associative datums + seed + height pattern); if
    any step throws, roll back the partial features and fall back to explicit holes
    on the native face, which always build correctly. Either way the panel is bored.

    Returns None for a fully live build, else a note on what is NOT live."""
    created = []
    try:
        return _build_parametric(comp, proxy_face, back_proxy, front_edge, bp_world,
                                 span_sel, params, created)
    except Exception as ex:
        # Undo in reverse: features/sketches first, then the wc_lb_* user parameters
        # this run created (left behind they'd show up as orphaned _2 parameters).
        design = adsk.fusion.Design.cast(app.activeProduct)
        for item in reversed(created):
            try:
                if isinstance(item, tuple) and item[0] == 'param':
                    up = design.userParameters.itemByName(item[1])
                    if up:
                        up.deleteMe()
                else:
                    item.deleteMe()
            except Exception:
                pass
        # Second pass: a parameter can refuse deletion until the features that
        # used it are fully gone.
        for item in created:
            if isinstance(item, tuple) and item[0] == 'param':
                try:
                    up = design.userParameters.itemByName(item[1])
                    if up:
                        up.deleteMe()
                except Exception:
                    pass
        reason = (str(ex) or ex.__class__.__name__) + ' — holes placed at fixed positions'
        futil.log(f'{CMD_NAME}: parametric build failed ({reason}); using explicit holes',
                  force_console=True)
        _build_explicit(comp, proxy_face, native_face, bp_world, span_sel, params)
        return reason


def _build_explicit(comp, proxy_face, native_face, bp_world, span_sel, params):
    """Robust fallback: drill every computed centre with one HoleFeature on the
    native face — no pattern, no cross-component refs, so it always builds. The
    sketch is still FULLY DEFINED: each point is dimensioned off the side panel's
    own projected bottom edge and its back (or front) edge, so the holes stay put
    relative to the panel. It does not redistribute when the opening changes (the
    parametric path provides that)."""
    up, front_refs = _ref_axes(proxy_face)
    bp_native = _to_native_point(proxy_face, bp_world)
    fr = boring.frame(native_face, up=up, front_refs=front_refs, back_ref_point=bp_native)
    p = dict(params)
    p['back_depth'] = _back_depth(fr, bp_native)
    p['bottom_h'], p['top_h'] = _span_heights(fr, _span_native(proxy_face, span_sel))
    plan = boring.RULES[0].build_plan(fr, p)

    sketch = comp.sketches.add(native_face)
    _name_sketch(sketch, f'LB Holes (fixed) {comp.name}')

    # Panel-edge datums (projected = fixed reference geometry). Any that can't be
    # found just leaves those points un-dimensioned rather than failing the bore.
    def _proj(edge):
        try:
            return _project_line(sketch, edge) if edge is not None else None
        except Exception:
            return None

    bottom_line = _proj(_find_edge(native_face, fr, along=fr.depth_dir, minimize=fr.height_dir,
                                   min_len=fr.depth * 0.5))
    back_line = _proj(_find_edge(native_face, fr, along=fr.height_dir, minimize=fr.depth_dir,
                                 min_len=fr.height * 0.5))
    front_line = _proj(_find_edge(native_face, fr, along=fr.height_dir,
                                  minimize=_neg(fr.depth_dir), min_len=fr.height * 0.5))

    dims = sketch.sketchDimensions
    point_coll = adsk.core.ObjectCollection.create()
    for model_pt in plan['all_points']:
        sk_pt = sketch.modelToSketchSpace(model_pt)
        sp = sketch.sketchPoints.add(sk_pt)
        point_coll.add(sp)
        v = fr.origin.vectorTo(model_pt)
        h = v.dotProduct(fr.height_dir)
        d = v.dotProduct(fr.depth_dir)
        is_back = d < fr.depth / 2.0
        datum = back_line if is_back else front_line
        d_val = d if is_back else fr.depth - d
        try:
            if bottom_line is not None:
                dims.addOffsetDimension(bottom_line, sp, sk_pt).parameter.value = h
            if datum is not None:
                dims.addOffsetDimension(datum, sp, sk_pt).parameter.value = d_val
        except Exception:
            pass   # the point is already at the right place; only its lock is lost

    holes = comp.features.holeFeatures
    hin = holes.createSimpleInput(adsk.core.ValueInput.createByReal(plan['dia_cm']))
    hin.setPositionBySketchPoints(point_coll)
    hin.setDistanceExtent(adsk.core.ValueInput.createByReal(plan['depth_cm']))
    _set_bore_direction(hin, sketch, fr)
    holes.add(hin)


def _name_sketch(sketch, name):
    try:
        sketch.name = name
    except Exception:
        pass


def _clear_auto_projection(sketch):
    """Delete what Fusion auto-projects when a sketch is created on a face (every
    edge of that face, if the user has "Auto project edges on reference" on). None
    of it is used, and those edges are exactly the references that go missing when
    a configuration changes the panel's shape, flooding the sketch with warnings."""
    try:
        origin = sketch.originPoint
    except Exception:
        origin = None
    for coll in (sketch.sketchCurves, sketch.sketchPoints):
        for ent in [coll.item(i) for i in range(coll.count)]:
            try:
                if ent.isReference and ent != origin:
                    ent.deleteMe()
            except Exception:
                pass


def _panel_end_face(proxy_face, fr, direction):
    """The side panel's own planar end face facing ``direction`` (its bottom, top,
    back or front end), in the same assembly context as ``proxy_face``. Of the
    faces facing that way, the outermost; ties go to the largest (an end split by
    a cut-out keeps its biggest piece)."""
    best, best_key = None, None
    try:
        faces = proxy_face.body.faces
    except Exception:
        return None
    for f in faces:
        try:
            if f.geometry.surfaceType != adsk.core.SurfaceTypes.PlaneSurfaceType:
                continue
            ok, n = f.evaluator.getNormalAtPoint(f.pointOnFace)
            if not ok or n.dotProduct(direction) < 0.999:
                continue
            pos = fr.origin.vectorTo(f.pointOnFace).dotProduct(direction)
            key = (round(pos, 4), f.area)
            if best_key is None or key > best_key:
                best, best_key = f, key
        except Exception:
            continue
    return best


def _end_segment(sketch, proxy_face, fr, direction):
    """The side panel's own bottom (``direction`` = down) or top end face
    intersected with the sketch plane: a line running the panel's full depth, back
    corner to front corner. It references the FACE, which configurations leave
    whole, so it survives rows where the panel's front or back is cut differently."""
    face = _panel_end_face(proxy_face, fr, direction)
    if face is None:
        return None
    try:
        return _intersect_line(sketch, face)
    except Exception:
        return None


def _corner_line(sketch, proxy_face, fr, front=True):
    """A construction line marking the side panel's front (or back) extent, fully
    defined from its top and bottom END segments (see _end_segment).

    It starts at the OUTERMOST of the two end corners (front: furthest forward)
    and runs perpendicular to that end segment until it meets the other end
    segment's line. So a recess cut into one corner only (e.g. a plinth/Gola
    notch at the bottom-front in some configuration rows) can't tilt it — the
    column stays vertical and keeps its setback from the panel's real front.
    Before, the line joined the two corners directly and went slanted whenever
    one of them was notched."""
    bot = _end_segment(sketch, proxy_face, fr, _neg(fr.height_dir))
    top = _end_segment(sketch, proxy_face, fr, fr.height_dir)
    if bot is None or top is None:
        return None

    def depth(sp):
        return fr.origin.vectorTo(sp.worldGeometry).dotProduct(fr.depth_dir)

    def corner(line):
        ends = (line.startSketchPoint, line.endSketchPoint)
        return max(ends, key=depth) if front else min(ends, key=depth)

    cb, ct = corner(bot), corner(top)
    if front:
        use_top = depth(ct) >= depth(cb) - 1e-6
    else:
        use_top = depth(ct) <= depth(cb) + 1e-6
    anchor, anchor_seg, other_seg = (ct, top, bot) if use_top else (cb, bot, top)

    try:
        # Far end placed straight across at the other end's height, then pinned
        # there by constraints (perpendicular + on the other end's line).
        a_w = anchor.worldGeometry
        o_w = other_seg.startSketchPoint.worldGeometry
        dh = fr.origin.vectorTo(o_w).dotProduct(fr.height_dir) - \
            fr.origin.vectorTo(a_w).dotProduct(fr.height_dir)
        far = a_w.copy()
        far.translateBy(_scaled_vec(fr.height_dir, dh))
        line = sketch.sketchCurves.sketchLines.addByTwoPoints(anchor, sketch.modelToSketchSpace(far))
        line.isConstruction = True
        gc = sketch.geometricConstraints
        gc.addPerpendicular(line, anchor_seg)
        gc.addCoincident(line.endSketchPoint, other_seg)
        return line
    except Exception:
        return None


def _scaled_vec(vec, s):
    v = vec.copy()
    v.scaleBy(s)
    return v


def _bottom_datum(sketch, proxy_face, fr, bottom_ref, picked=False):
    """The bottom datum line in ``sketch``: ``bottom_ref`` (the picked bottom panel's
    face, or the hidden plane extending it — see _datum_source) where it meets the
    sketch plane, else the side panel's own (projected) bottom edge. All of these
    are fixed, associative reference geometry."""
    if bottom_ref is not None:
        line = _face_datum_line(sketch, bottom_ref, fr, fr.depth_dir)
        if line is not None:
            return line
        if picked:   # never silently swap a picked panel for the side panel's end
            raise RuntimeError('could not reference the bottom panel face')
    seg = _end_segment(sketch, proxy_face, fr, _neg(fr.height_dir))
    if seg is not None:
        return seg
    edge = _find_edge(proxy_face, fr, along=fr.depth_dir, minimize=fr.height_dir,
                      min_len=fr.depth * 0.5)
    if edge is None:
        raise RuntimeError('could not find the side panel\'s bottom edge')
    return _project_line(sketch, edge)


def _top_datum(sketch, proxy_face, fr, top_ref, picked=False):
    """The top datum line: ``top_ref`` (the picked top panel's face or its extending
    plane) where it meets the sketch plane, else the side panel's own top edge
    (projected). None only when neither can be found."""
    if top_ref is not None:
        line = _face_datum_line(sketch, top_ref, fr, fr.depth_dir)
        if line is not None:
            return line
        if picked:
            raise RuntimeError('could not reference the top panel face')
    seg = _end_segment(sketch, proxy_face, fr, fr.height_dir)
    if seg is not None:
        return seg
    edge = _find_edge(proxy_face, fr, along=fr.depth_dir, minimize=_neg(fr.height_dir),
                      min_len=fr.depth * 0.5)
    return _project_line(sketch, edge) if edge is not None else None


def _build_parametric(comp, proxy_face, back_proxy, front_edge, bp_world, span_sel,
                      params, created):
    """Live-parametric, FULLY DEFINED build. Two sketches on the side panel face,
    both in assembly context so they can reference the other panels:

    1. ``LB Opening`` — the bottom and top datums (the picked bottom/top panels'
       faces intersected with the sketch plane, or the side panel's own ends) and a
       DRIVEN dimension across them. Its parameter (wc_lb_H_<panel>) is the live
       clear opening. Kept in its own hidden sketch so the hole sketch holds only
       the holes and their locks (older Fusion builds also only accepted driven
       parameters in expressions outside their own sketch).
    2. ``LB Holes`` — the seed set of both columns. Every point carries two driving
       dimensions: height off the bottom datum (an expression of wc_lb_H, N, shelf,
       rise, pitch) and depth off the back datum (back panel face, else the side
       panel's back edge) or the front datum (picked/auto-detected front edge). The
       pattern-direction line joins two seed points, so it adds no free geometry.
       Datums are all intersected/projected (fixed) geometry, so the sketch is fully
       constrained.

    A HoleFeature over the 6 seed points and a rectangular pattern (qty N, spacing
    gap + shelf) complete it. Moving a panel, changing a thickness, or resizing the
    cabinet moves the datums, the opening parameter follows, and the holes reflow."""
    # World frame from the assembly-context proxy face (matches the sketch space).
    fr = boring.frame(proxy_face, back_ref_point=bp_world)
    p = dict(params)
    p['back_depth'] = _back_depth(fr, bp_world)
    bottom_ref, top_ref = _span_ends(fr, span_sel)
    p['bottom_h'] = bottom_ref[0] if bottom_ref else None
    p['top_h'] = top_ref[0] if top_ref else None

    design = adsk.fusion.Design.cast(app.activeProduct)
    # Each Line Boring instance gets its own wc_lb_* parameters (name-suffixed per
    # component + occurrence), so a second run can't clobber the parameters an
    # earlier instance's sketch dimensions, hole and pattern still reference.
    suffix = _unique_instance_suffix(design, _safe_name(comp.name))
    plan = boring.RULES[0].build_plan(fr, p, suffix=suffix)
    created.extend(('param', name) for name in _ensure_params(design, plan['params']))

    # ---- Datum sources --------------------------------------------------------
    # Panels rarely touch the side panel (a shelf or back stops short of it), so a
    # picked face that doesn't cross the sketch plane is EXTENDED: a hidden
    # construction plane offset 0 from it, created here so it sits before the
    # sketches in the timeline. Both sketches then intersect that plane.
    #
    # Never an EDGE: configurations change panel topology (e.g. Gola rows vs plain
    # rows split the side panel's front edge into pieces), and a projected edge
    # that doesn't exist in another row breaks the sketch. Faces -> planes survive.
    # With nothing picked, the side panel's own end faces are used the same way.
    # The side panel's OWN extents (its front, and its back/bottom/top when no
    # panel is picked) come from its top and bottom END faces intersected with the
    # sketch plane — see _end_segment / _corner_line. Those end faces stay whole in
    # every configuration, whereas its front face is cut up differently per row.
    def src(face, name):
        return _datum_source(comp, face, fr, f'{name} {suffix}', created) if face is not None else None

    bottom_src = src(bottom_ref[1], 'LB Bottom') if bottom_ref is not None else None
    top_src = src(top_ref[1], 'LB Top') if top_ref is not None else None
    back_src = src(back_proxy, 'LB Back') if back_proxy is not None else None

    # ---- Sketch 1: the live opening -------------------------------------------
    baked_h = f'({plan["span_mm"]})'
    h_token = baked_h
    open_sk = comp.sketches.add(proxy_face)
    created.append(open_sk)
    _name_sketch(open_sk, f'LB Opening {suffix}')
    _clear_auto_projection(open_sk)
    o_bottom = _bottom_datum(open_sk, proxy_face, fr, bottom_src, bottom_ref is not None)
    o_top = _top_datum(open_sk, proxy_face, fr, top_src, top_ref is not None)
    if o_top is not None:
        try:
            h_ref = open_sk.sketchDimensions.addOffsetDimension(
                o_bottom, o_top,
                open_sk.modelToSketchSpace(fr.point(fr.height / 2.0, fr.depth / 2.0)), False)
            hp = h_ref.parameter
            try:
                hp.name = _unique_param_name(design, f'{boring.PFX}H_{suffix}')
                hp.comment = ('Line boring: clear opening between the bottom and top '
                              'panels (drives shelf-pin spacing)')
            except Exception:
                pass
            h_token = hp.name
        except Exception:
            h_token = baked_h
    try:
        open_sk.isVisible = False
    except Exception:
        pass

    # ---- Sketch 2: the seed holes ---------------------------------------------
    sketch = comp.sketches.add(proxy_face)
    created.append(sketch)
    _name_sketch(sketch, f'LB Holes {suffix}')
    _clear_auto_projection(sketch)

    bottom_line = _bottom_datum(sketch, proxy_face, fr, bottom_src, bottom_ref is not None)

    back_line = (_face_datum_line(sketch, back_src, fr, fr.height_dir)
                 if back_src is not None else None)
    if back_line is None and back_proxy is not None:
        raise RuntimeError('could not reference the back panel face')
    if back_line is None:
        back_line = _corner_line(sketch, proxy_face, fr, front=False)
    if back_line is None:
        # No back panel picked: the back column is measured from the side panel's
        # own back edge (matches the preview, where back_depth is 0).
        back_edge = _find_edge(proxy_face, fr, along=fr.height_dir, minimize=fr.depth_dir,
                               min_len=fr.height * 0.5)
        if back_edge is None:
            raise RuntimeError('could not find the side panel\'s back edge (pick the back panel)')
        back_line = _project_line(sketch, back_edge)

    front_line = _corner_line(sketch, proxy_face, fr, front=True) if front_edge is None else None
    if front_line is None:
        # A picked front edge (the user's explicit choice), else last-resort edge.
        front_ref = front_edge if front_edge is not None else _find_edge(
            proxy_face, fr, along=fr.height_dir, minimize=_neg(fr.depth_dir),
            min_len=fr.height * 0.5)
        if front_ref is None:
            raise RuntimeError('could not find the side panel\'s front edge (pick one)')
        front_line = _project_line(sketch, front_ref)

    n_expr, shelf_expr, rise_expr, pitch_expr = (
        plan['n_expr'], plan['shelf_expr'], plan['rise_expr'], plan['pitch_expr'])

    def exprs(h):
        # gap = (opening - N*shelf)/(N+1); the first set sits a gap up from the
        # bottom datum minus the pin rise (the shelf rests above its hole centre);
        # each following set is one gap plus one shelf higher.
        gap = f'(({h} - {n_expr} * {shelf_expr}) / ({n_expr} + 1))'
        first = f'({gap} - {rise_expr})'
        return ({'mid': first,
                 'up': f'{first} + {pitch_expr}',
                 'low': f'{first} - {pitch_expr}'},
                f'({gap} + {shelf_expr})')

    height_exprs, step_expr = exprs(h_token)

    dims = sketch.sketchDimensions
    seed_points = []
    by_key = {}
    for item in plan['seed']:
        text_pt = sketch.modelToSketchSpace(item['pt'])
        sp = sketch.sketchPoints.add(text_pt)
        seed_points.append(sp)
        by_key[(item['col'], item['variant'])] = sp
        h_dim = dims.addOffsetDimension(bottom_line, sp, text_pt)
        try:
            h_dim.parameter.expression = height_exprs[item['variant']]
        except Exception:
            if h_token == baked_h:
                raise
            # This Fusion build won't take the driven opening here: bake the
            # opening for the whole panel (still fully defined, still follows the
            # bottom/back/front panels, just no longer redistributes) and say so.
            futil.log(f'{CMD_NAME}: opening parameter rejected; baking the opening',
                      force_console=True)
            h_token = baked_h
            height_exprs, step_expr = exprs(h_token)
            h_dim.parameter.expression = height_exprs[item['variant']]
        datum = back_line if item['col'] == 'back' else front_line
        off_expr = plan['back_expr'] if item['col'] == 'back' else plan['front_expr']
        d_dim = dims.addOffsetDimension(datum, sp, text_pt)
        d_dim.parameter.expression = off_expr

    # Pattern direction: a construction line from the back column's LOW seed hole to
    # its UP seed hole. Both ends are already-dimensioned seed points, so the line
    # adds no degrees of freedom and always points up the panel.
    up_line = sketch.sketchCurves.sketchLines.addByTwoPoints(
        by_key[('back', 'low')], by_key[('back', 'up')])
    up_line.isConstruction = True

    # One HoleFeature over the 6 seed points.
    dia_expr, depth_expr = plan['hole']
    point_coll = adsk.core.ObjectCollection.create()
    for sp in seed_points:
        point_coll.add(sp)
    holes = comp.features.holeFeatures
    hin = holes.createSimpleInput(adsk.core.ValueInput.createByString(dia_expr))
    hin.setPositionBySketchPoints(point_coll)
    hin.setDistanceExtent(adsk.core.ValueInput.createByString(depth_expr))
    _set_bore_direction(hin, sketch, fr)
    hole_feat = holes.add(hin)
    created.append(hole_feat)

    # Height pattern: qty N at (gap + shelf), from the same live expressions.
    # One shelf needs no pattern — the seed set IS the boring — and Fusion
    # rejects a pattern of quantity 1 ("no pattern instances"), which used to
    # sink the whole live build into the fixed fallback.
    if int(p['n']) > 1:
        pattern_ent = adsk.core.ObjectCollection.create()
        pattern_ent.add(hole_feat)
        patterns = comp.features.rectangularPatternFeatures
        pin = patterns.createInput(
            pattern_ent, up_line,
            adsk.core.ValueInput.createByString(plan['qty_expr']),
            adsk.core.ValueInput.createByString(step_expr),
            adsk.fusion.PatternDistanceType.SpacingPatternDistanceType)
        n_before = comp.features.rectangularPatternFeatures.count
        try:
            pattern_feat = patterns.add(pin)
        except Exception:
            # A failed add can still leave the (broken) feature in the timeline;
            # make sure the rollback removes it too.
            if comp.features.rectangularPatternFeatures.count > n_before:
                created.append(comp.features.rectangularPatternFeatures.item(
                    comp.features.rectangularPatternFeatures.count - 1))
            raise
        created.append(pattern_feat)

    if not sketch.isFullyConstrained:
        futil.log(f'{CMD_NAME}: hole sketch for {comp.name} is not fully constrained',
                  force_console=True)
    if h_token == baked_h:
        return ('the bottom-to-top opening could not be measured live, so the holes '
                'follow the panels but will not re-spread if the opening height changes')
    return None


def _ensure_params(design, specs):
    """Create or update the wc_lb_* user parameters that drive the holes. Returns
    the names it newly created (so a failed build can remove them again)."""
    ups = design.userParameters
    added = []
    for name, expr, units, comment in specs:
        existing = ups.itemByName(name)
        if existing:
            existing.expression = expr
        else:
            ups.add(name, adsk.core.ValueInput.createByString(expr), units, comment)
            added.append(name)
    return added


def _safe_name(raw: str) -> str:
    """Sanitise text into a valid Fusion parameter identifier (alnum/underscore,
    not starting with a digit)."""
    cleaned = ''.join(ch if (ch.isalnum() or ch == '_') else '_' for ch in raw)
    if not cleaned:
        cleaned = f'{boring.PFX}H'
    if cleaned[0].isdigit():
        cleaned = '_' + cleaned
    return cleaned


def _unique_param_name(design, base: str) -> str:
    """`base` if free, else base_2, base_3, ... — so each panel's height reference
    dimension gets its own name instead of clobbering another's."""
    existing = set()
    allp = design.allParameters
    for i in range(allp.count):
        try:
            existing.add(allp.item(i).name)
        except Exception:
            pass
    if base not in existing:
        return base
    i = 2
    while f'{base}_{i}' in existing:
        i += 1
    return f'{base}_{i}'


def _unique_instance_suffix(design, base: str) -> str:
    """A suffix for THIS Line Boring invocation's wc_lb_* parameter group, distinct
    from any earlier instance's. ``base`` alone (the component name) is never itself
    a parameter name — only ``wc_lb_N_<base>`` etc. are — so collision has to be
    checked against a composed name, not the bare base, or a repeat run on the same
    panel would keep computing the same (already-used) suffix and clobber the
    earlier instance's parameters again."""
    existing = set()
    allp = design.allParameters
    for i in range(allp.count):
        try:
            existing.add(allp.item(i).name)
        except Exception:
            pass
    candidate = base
    i = 2
    # Check the opening (H_) name too: an older run may have left a wc_lb_H_<panel>
    # behind, which would otherwise push this run's opening to a mismatched _2 name.
    while (f'{boring.PFX}N_{candidate}' in existing
           or f'{boring.PFX}H_{candidate}' in existing):
        candidate = f'{base}_{i}'
        i += 1
    return candidate


def _find_edge(face, fr, along: adsk.core.Vector3D, minimize: adsk.core.Vector3D, min_len: float):
    """The full-length linear edge of ``face`` parallel to ``along`` whose midpoint
    sits lowest along ``minimize`` (panel-relative). Short edges are ignored; ties
    on projection prefer the longer edge."""
    best = None
    best_key = None
    for edge in face.edges:
        try:
            a = edge.startVertex.geometry
            b = edge.endVertex.geometry
        except Exception:
            continue
        v = a.vectorTo(b)
        length = v.length
        if length < min_len:
            continue
        v.normalize()
        if abs(v.dotProduct(along)) < 0.999:
            continue
        mid = adsk.core.Point3D.create((a.x + b.x) / 2.0, (a.y + b.y) / 2.0, (a.z + b.z) / 2.0)
        proj = fr.origin.vectorTo(mid).dotProduct(minimize)
        key = (round(proj, 6), -length)
        if best_key is None or key < best_key:
            best_key = key
            best = edge
    return best


def _project_line(sketch: adsk.fusion.Sketch, edge) -> adsk.fusion.SketchLine:
    projected = sketch.project(edge)
    for i in range(projected.count):
        ent = projected.item(i)
        if ent.objectType == adsk.fusion.SketchLine.classType():
            return ent
    raise RuntimeError('Projecting a panel edge did not yield a line.')


def _intersect_line(sketch: adsk.fusion.Sketch, face):
    """The (associative) sketch line where ``face`` crosses the sketch plane — the
    same technique Shelf Creator uses. Returns None if the face doesn't cross it."""
    created = sketch.intersectWithSketchPlane([face])
    for ent in _iter_vector(created):
        if ent.objectType == adsk.fusion.SketchLine.classType():
            return ent
    return None


def _face_crosses_plane(face, fr, tol=1e-3):
    """True when ``face`` genuinely passes through the side panel's sketch plane
    (vertices on both sides of it), so intersecting the face itself gives a line.
    A face that stops short of the plane, or only touches it along an edge, does
    not count — those are extended with a plane instead."""
    ds = []
    for v in face.vertices:
        ds.append(fr.origin.vectorTo(v.geometry).dotProduct(fr.normal))
    return bool(ds) and min(ds) < -tol and max(ds) > tol


def _datum_source(comp, face, fr, name, created):
    """What a datum line is taken from: the picked ``face`` itself when it crosses
    the side panel, else a hidden construction plane offset 0 from it — the face
    extended to infinity, so it always meets the sketch plane, and linked to the
    face so it follows that panel when the model changes."""
    if _face_crosses_plane(face, fr):
        return face
    planes = comp.constructionPlanes
    pin = planes.createInput()
    pin.setByOffset(face, adsk.core.ValueInput.createByReal(0.0))
    plane = planes.add(pin)
    created.append(plane)
    try:
        plane.name = name
    except Exception:
        pass
    try:
        plane.isLightBulbOn = False
    except Exception:
        pass
    return plane


def _face_datum_line(sketch: adsk.fusion.Sketch, source, fr, along):
    """A fixed, associative sketch line where ``source`` — a bottom/top/back panel
    face, or the construction plane extending one (see _datum_source) — meets the
    sketch plane. Intersection first; a plane that won't intersect is projected
    (a plane perpendicular to the sketch projects to a line); a face as a last
    resort projects its edge nearest the side panel running ``along`` it."""
    try:
        line = _intersect_line(sketch, source)
        if line is not None:
            return line
    except Exception:
        pass
    if source.objectType == adsk.fusion.ConstructionPlane.classType():
        try:
            for ent in _iter_vector(sketch.project2([source], True)):
                if ent.objectType == adsk.fusion.SketchLine.classType():
                    return ent
        except Exception:
            pass
        return None
    edge = _nearest_parallel_edge(source, fr, along)
    if edge is None:
        return None
    try:
        return _project_line(sketch, edge)
    except Exception:
        return None


def _nearest_parallel_edge(face, fr, along):
    """The linear edge of ``face`` parallel to ``along`` that lies closest to the
    side panel's plane (ties prefer the longer edge). Edges pointing at the side
    panel would project to a point, so they are skipped."""
    best, best_key = None, None
    for edge in face.edges:
        try:
            a = edge.startVertex.geometry
            b = edge.endVertex.geometry
        except Exception:
            continue
        v = a.vectorTo(b)
        length = v.length
        if length < 1e-4:
            continue
        v.normalize()
        if abs(v.dotProduct(along)) < 0.999:
            continue
        mid = adsk.core.Point3D.create((a.x + b.x) / 2.0, (a.y + b.y) / 2.0, (a.z + b.z) / 2.0)
        dist = abs(fr.origin.vectorTo(mid).dotProduct(fr.normal))
        key = (round(dist, 6), -length)
        if best_key is None or key < best_key:
            best, best_key = edge, key
    return best


def _iter_vector(vec):
    """Iterate a Fusion return collection whose count attribute varies
    (ObjectCollection uses .count, SketchEntityVector uses .length)."""
    count = None
    for attr in ('count', 'length'):
        if hasattr(vec, attr):
            count = getattr(vec, attr)
            break
    if count is not None:
        for i in range(count):
            yield vec.item(i)
        return
    for item in vec:
        yield item


def _neg(vec: adsk.core.Vector3D) -> adsk.core.Vector3D:
    v = vec.copy()
    v.scaleBy(-1.0)
    return v


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
def command_destroy(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Destroy Event')
    global local_handlers, _graphics_group
    if _graphics_group:
        try:
            _graphics_group.deleteMe()
        except Exception:
            pass
        _graphics_group = None
        app.activeViewport.refresh()
    local_handlers = []
