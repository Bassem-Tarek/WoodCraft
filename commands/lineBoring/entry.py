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
above and below), the set centres dividing the CLEAR OPENING between the bottom
and top panels into N+1 equal gaps, in two columns set in from the front and back
edges. Leave the top/bottom selection empty and the side panel's own ends are used
instead (the old whole-panel behaviour).

Live-parametric build: the command creates wc_lb_* user parameters and a feature
tree (sketch -> seed HoleFeature -> rectangular pattern) driven by them, so editing
the parameters reflows the holes. The opening is associative too — the bottom and
top datums are the picked panels' faces intersected with the sketch plane, and the
spacing is driven by a reference dimension across them, so moving a panel or
changing its thickness redistributes the holes. If any step of the parametric
build fails, it rolls back and falls back to a plain (numeric) HoleFeature over
every computed point, so the command always produces correct holes.
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
            label = f'{len(pts)} holes  |  {(t - b) / boring.MM:.0f} mm opening'
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
    # Per-panel geometry checks (depth/height-dependent) stay in EmaarRule.validate,
    # surfaced as a messageBox on OK; here we only gate the cheap, panel-independent ones.
    args.areInputsValid = faces_ok and n_ok and sizes_ok and setbacks_ok


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
            _bore_one(comp, face, native, back_proxy, front_edge, bp_world, span_sel, params)
            bored += 1
        except ValueError as ve:
            ui.messageBox(str(ve), CMD_NAME)
        except Exception:
            futil.handle_error(f'{CMD_NAME}: failed to bore a panel')

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
    fy = adsk.core.Vector3D.create(0, 1, 0)
    fx = adsk.core.Vector3D.create(1, 0, 0)
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
    compare that normal to the into-material bore direction and flip when they agree.
    Guarded because the property name varies across API versions."""
    try:
        sk_normal = sketch.xDirection.crossProduct(sketch.yDirection)
        sk_normal.normalize()
        hole_input.isDefaultDirectionFlipped = sk_normal.dotProduct(fr.bore_dir) > 0
    except Exception:
        pass


def _bore_one(comp, proxy_face, native_face, back_proxy, front_edge, bp_world, span_sel, params):
    """Try the live-parametric build (associative datums + seed + height pattern); if
    any step throws, roll back the partial features and fall back to explicit holes
    on the native face, which always build correctly. Either way the panel is bored."""
    created = []
    try:
        _build_parametric(comp, proxy_face, back_proxy, front_edge, bp_world, span_sel,
                          params, created)
    except Exception:
        for feat in reversed(created):
            try:
                feat.deleteMe()
            except Exception:
                pass
        futil.log(f'{CMD_NAME}: parametric build failed; using explicit holes', force_console=True)
        _build_explicit(comp, proxy_face, native_face, bp_world, span_sel, params)


def _build_explicit(comp, proxy_face, native_face, bp_world, span_sel, params):
    """Robust fallback: drill every computed centre with one HoleFeature at fixed
    positions on the native face — no pattern, no cross-component refs, so it always
    builds. Not reflow-on-edit (the parametric path provides that)."""
    up, front_refs = _ref_axes(proxy_face)
    bp_native = _to_native_point(proxy_face, bp_world)
    fr = boring.frame(native_face, up=up, front_refs=front_refs, back_ref_point=bp_native)
    p = dict(params)
    p['back_depth'] = _back_depth(fr, bp_native)
    p['bottom_h'], p['top_h'] = _span_heights(fr, _span_native(proxy_face, span_sel))
    plan = boring.RULES[0].build_plan(fr, p)

    sketch = comp.sketches.add(native_face)
    point_coll = adsk.core.ObjectCollection.create()
    for model_pt in plan['all_points']:
        point_coll.add(sketch.sketchPoints.add(sketch.modelToSketchSpace(model_pt)))

    holes = comp.features.holeFeatures
    hin = holes.createSimpleInput(adsk.core.ValueInput.createByReal(plan['dia_cm']))
    hin.setPositionBySketchPoints(point_coll)
    hin.setDistanceExtent(adsk.core.ValueInput.createByReal(plan['depth_cm']))
    _set_bore_direction(hin, sketch, fr)
    holes.add(hin)


def _build_parametric(comp, proxy_face, back_proxy, front_edge, bp_world, span_sel,
                      params, created):
    """Live-parametric build, Shelf-Creator style: the sketch is created on the side
    panel face in ASSEMBLY context, so it can reference other components. Datums are
    real, associative geometry — the back column is dimensioned off the back-panel
    face INTERSECTED with the sketch plane, the front column off the (projected)
    front edge, and heights off the BOTTOM PANEL's face (also intersected), with the
    spacing driven by a reference dimension up to the TOP PANEL's face. A single
    height rectangular pattern (qty N) replicates the seed set up the opening. So
    changing a panel's thickness, or moving the bottom/top panels, moves and
    redistributes the holes with the references."""
    if back_proxy is None:
        raise RuntimeError('Back panel face is required for the parametric build.')

    # World frame from the assembly-context proxy face (matches the sketch space).
    fr = boring.frame(proxy_face, back_ref_point=bp_world)
    p = dict(params)
    p['back_depth'] = _back_depth(fr, bp_world)
    bottom_ref, top_ref = _span_ends(fr, span_sel)
    p['bottom_h'] = bottom_ref[0] if bottom_ref else None
    p['top_h'] = top_ref[0] if top_ref else None
    plan = boring.RULES[0].build_plan(fr, p)

    design = adsk.fusion.Design.cast(app.activeProduct)
    _ensure_params(design, plan['params'])

    sketch = comp.sketches.add(proxy_face)
    created.append(sketch)

    # Associative datums. The bottom datum is the picked bottom panel's face where it
    # crosses this sketch plane (so the first set tracks that panel); with no bottom
    # panel picked it falls back to the side panel's own bottom edge. A picked face
    # that fails to intersect raises, which drops the whole panel to the explicit
    # fallback rather than silently boring a different (panel-based) spacing.
    if bottom_ref is not None:
        bottom_line = _intersect_line(sketch, bottom_ref[1])
        if bottom_line is None:
            raise RuntimeError('The bottom panel face does not cross the side panel sketch plane.')
    else:
        bottom_edge = _find_edge(proxy_face, fr, along=fr.depth_dir, minimize=fr.height_dir,
                                 min_len=fr.depth * 0.5)
        if bottom_edge is None:
            raise RuntimeError('Could not identify the panel bottom edge.')
        bottom_line = _project_line(sketch, bottom_edge)

    # Top datum: the picked top panel's face, else the side panel's own top edge.
    if top_ref is not None:
        top_line = _intersect_line(sketch, top_ref[1])
        if top_line is None:
            raise RuntimeError('The top panel face does not cross the side panel sketch plane.')
    else:
        top_edge = _find_edge(proxy_face, fr, along=fr.depth_dir, minimize=_neg(fr.height_dir),
                              min_len=fr.depth * 0.5)
        top_line = _project_line(sketch, top_edge) if top_edge is not None else None

    back_line = _intersect_line(sketch, back_proxy)   # where the back panel crosses this plane
    if back_line is None:
        raise RuntimeError('The back panel face does not cross the panel sketch plane.')

    front_ref = front_edge if front_edge is not None else _find_edge(
        proxy_face, fr, along=fr.height_dir, minimize=_neg(fr.depth_dir), min_len=fr.height * 0.5)
    if front_ref is None:
        raise RuntimeError('Could not identify the panel front edge (pick one).')
    front_line = _project_line(sketch, front_ref)

    # Deterministic +height direction for the pattern (drawn, not read off an edge).
    up_line = _dir_line(sketch, fr, fr.height_dir, fr.height)

    dims = sketch.sketchDimensions

    # Live opening token: a DRIVEN reference dimension from the bottom datum to the
    # top datum measures the CLEAR OPENING between the two panels, so spacing =
    # opening/(N+1) tracks them — move a panel, change its thickness or drive it from
    # the user's own parameters and the holes redistribute. Falls back to a baked
    # number if the dimension can't be made.
    h_token = f'({plan["span_mm"]})'
    if top_line is not None:
        try:
            h_ref = dims.addOffsetDimension(
                bottom_line, top_line,
                sketch.modelToSketchSpace(fr.point(fr.height / 2.0, fr.depth / 2.0)), False)
            hp = h_ref.parameter
            # Give the auto-named (d###) reference dimension a clear, unique name +
            # comment so it reads as "the opening that drives the hole spacing".
            try:
                hp.name = _unique_param_name(design, _safe_name(f'{boring.PFX}H_{comp.name}'))
                hp.comment = ('Line boring: clear opening between the bottom and top '
                              'panels (drives shelf-pin spacing)')
            except Exception:
                pass
            h_token = hp.name
        except Exception:
            h_token = f'({plan["span_mm"]})'

    spacing_expr = f'({h_token} / ({boring.PFX}N + 1))'
    height_exprs = {
        'mid': spacing_expr,
        'up': f'{spacing_expr} + {boring.PFX}pitch',
        'low': f'{spacing_expr} - {boring.PFX}pitch',
    }

    # Seed = first set of BOTH columns; heights measured up from the bottom datum,
    # depth dimensioned to the matching column datum.
    seed_points = []
    for item in plan['seed']:
        sp = sketch.sketchPoints.add(sketch.modelToSketchSpace(item['pt']))
        seed_points.append(sp)
        text_pt = sketch.modelToSketchSpace(item['pt'])
        h_dim = dims.addOffsetDimension(bottom_line, sp, text_pt)
        h_dim.parameter.expression = height_exprs[item['variant']]
        datum = back_line if item['col'] == 'back' else front_line
        off_expr = plan['back_expr'] if item['col'] == 'back' else plan['front_expr']
        d_dim = dims.addOffsetDimension(datum, sp, text_pt)
        d_dim.parameter.expression = off_expr

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

    # Height pattern only: replicate the seed set up the opening (qty N, spacing
    # opening/(N+1) — the SAME live spacing_expr used for the seed, so the whole
    # column redistributes when the bottom/top panels move).
    pattern_ent = adsk.core.ObjectCollection.create()
    pattern_ent.add(hole_feat)
    patterns = comp.features.rectangularPatternFeatures
    pin = patterns.createInput(
        pattern_ent, up_line,
        adsk.core.ValueInput.createByString(plan['qty_expr']),
        adsk.core.ValueInput.createByString(spacing_expr),
        adsk.fusion.PatternDistanceType.SpacingPatternDistanceType)
    pattern_feat = patterns.add(pin)
    created.append(pattern_feat)


def _ensure_params(design, specs):
    """Create or update the wc_lb_* user parameters that drive the holes."""
    ups = design.userParameters
    for name, expr, units, comment in specs:
        existing = ups.itemByName(name)
        if existing:
            existing.expression = expr
        else:
            ups.add(name, adsk.core.ValueInput.createByString(expr), units, comment)


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


def _dir_line(sketch, fr, vec, length):
    """A construction line from the frame origin along ``vec`` (length cm). Both
    endpoints are computed, so the line's direction in model space is exactly +vec —
    a deterministic direction reference for the rectangular pattern."""
    start = sketch.modelToSketchSpace(fr.origin)
    end = sketch.modelToSketchSpace(_translated(fr.origin, vec, length))
    line = sketch.sketchCurves.sketchLines.addByTwoPoints(start, end)
    line.isConstruction = True
    return line


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
