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

"""Countertop — build the worktop over an assembled run of cabinets.

Pick the **wall face(s)** the kitchen runs along and the **side panels of the
cabinets at each end** of those runs, give the cabinet depth and the slab
thickness, and the command drops a worktop on top of the cabinets:

  - the back edge sits flush on the wall face;
  - the ends line up with the outer faces of the selected side panels;
  - the depth is your cabinet depth plus an overhang (20 mm by default), so the
    worktop stands slightly proud of the doors;
  - the underside sits at the top of the tallest selected side panel, so the slab
    lands on the carcasses without you measuring the plinth + carcass height.

Tick **Backsplash** to add an upstand as well: it runs the full length of each
run, hard against the wall, standing on the worktop, with its own thickness and
height.

One wall face = one run = one component with one body, named "Countertop" (or
"Countertop 1..N" for an L- or U-shaped kitchen), plus a "Backsplash N" beside
it. Each is tagged with the **countertop** category, not `panel`: a worktop is
priced by area and edgebanded exactly like a sheet good, but it is bought as a
slab or a cut length rather than nested out of a stock sheet, so it belongs on
the BOM and not on a nesting diagram. Set its Fusion material and it prices
itself from the Sheets library like anything else.

Which side panels belong to which run is worked out from geometry (see
commands/countertop_geom.py), so an L-shaped kitchen can be done in one go:
select both walls and all four end panels. Three things then keep the corner
honest:

  - a run is **extended** to any picked wall its axis runs into, because the last
    cabinet stops short of the corner but a worktop does not;
  - it is then **clipped to those same wall planes**, so it ends exactly on the
    line where the walls meet and never crosses one;
  - where the finished runs still overlap, the command **Combine-cuts** the later
    run with the earlier one (keeping the tool), so the corner is solid once,
    not twice.

Extend-then-clip in that order matters: extending alone would overshoot,
clipping alone would leave the run short of the corner — and a short run cannot
cut away all of its neighbour, which is what leaves a strip beside the wall.

A clipped run is no longer a rectangle, so the builder profiles an arbitrary
polygon rather than four fixed corners.

The maths lives in commands/countertop_geom.py — pure Python, no Fusion API.
This module is only the dialog, the selection plumbing and the extrude.
"""

import os

import adsk.core
import adsk.fusion

from .. import entity_memo
from .. import ui_helpers
from .. import wc_attrs
from .. import countertop_geom as geom
from ...lib import fusionAddInUtils as futil
from ... import config

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_countertop'
CMD_NAME = 'Countertop'
CMD_Description = (
    'Build the kitchen worktop over assembled cabinets. Pick the wall face(s) and '
    'the side panels of the cabinets at each end of the run, set the cabinet depth '
    'and slab thickness, and a slab is created on top of the carcasses — back edge '
    'on the wall, ends flush with the end panels, depth = cabinet depth + overhang.'
)
IS_PROMOTED = True

PANEL_ID = config.KITCHEN_PANEL_ID
PANEL_NAME = config.KITCHEN_PANEL_NAME

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

WALLS_ID = 'ct_walls'
SIDES_ID = 'ct_sides'
DEPTH_ID = 'ct_depth'
OVERHANG_ID = 'ct_overhang'
THICKNESS_ID = 'ct_thickness'
BACKSPLASH_ID = 'ct_backsplash'
BS_THICKNESS_ID = 'ct_bs_thickness'
BS_HEIGHT_ID = 'ct_bs_height'

# Trade defaults, in Fusion's internal unit (cm). 560 mm carcass depth + 20 mm
# overhang = a 580 mm worktop, and 40 mm is a typical post-formed slab. A
# backsplash is usually the same board on edge — 18 mm — and 100 mm tall.
DEFAULT_DEPTH_CM = 56.0
DEFAULT_OVERHANG_CM = 2.0
DEFAULT_THICKNESS_CM = 4.0
DEFAULT_BS_THICKNESS_CM = 1.8
DEFAULT_BS_HEIGHT_CM = 10.0

COMPONENT_NAME = 'Countertop'
BACKSPLASH_NAME = 'Backsplash'

PREVIEW_COLOR = (80, 200, 255, 255)     # the same cyan Shelf Creator uses
BACKSPLASH_COLOR = (255, 190, 80, 255)  # amber, so the upstand reads apart from the slab
PREVIEW_WEIGHT = 3

local_handlers = []
_graphics_group = None


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

    global _graphics_group
    _graphics_group = None
    _box_memo.clear()

    inputs = args.command.commandInputs
    length_units = app.activeProduct.unitsManager.defaultLengthUnits

    walls = inputs.addSelectionInput(
        WALLS_ID, 'Wall faces', 'Pick the wall face each run of cabinets sits against')
    walls.addSelectionFilter('PlanarFaces')
    walls.setSelectionLimits(1, 0)
    walls.tooltip = ('One slab per face. For an L-shaped kitchen pick both walls — '
                     'each run only uses the side panels standing in front of it.')

    sides = inputs.addSelectionInput(
        SIDES_ID, 'End side panels',
        'Pick the side panels of the cabinets at each end of the run(s)')
    sides.addSelectionFilter('Occurrences')
    sides.addSelectionFilter('SolidBodies')
    sides.setSelectionLimits(2, 0)
    sides.tooltip = ('These set the length of each run and — via their top face — '
                     'the height the worktop sits at. Two per run (one at each '
                     'end); picking the intermediate ones too does no harm.')

    depth = inputs.addValueInput(
        DEPTH_ID, 'Cabinet depth', length_units,
        adsk.core.ValueInput.createByReal(DEFAULT_DEPTH_CM))
    depth.tooltip = 'Depth of the carcasses, measured from the wall.'

    overhang = inputs.addValueInput(
        OVERHANG_ID, 'Overhang', length_units,
        adsk.core.ValueInput.createByReal(DEFAULT_OVERHANG_CM))
    overhang.tooltip = ('Added to the cabinet depth so the worktop stands proud of '
                        'the doors. 20 mm is the usual kitchen figure.')

    thickness = inputs.addValueInput(
        THICKNESS_ID, 'Countertop thickness', length_units,
        adsk.core.ValueInput.createByReal(DEFAULT_THICKNESS_CM))
    thickness.tooltip = 'Thickness of the slab, extruded upward from the top of the cabinets.'

    backsplash = inputs.addBoolValueInput(BACKSPLASH_ID, 'Backsplash', True, '', False)
    backsplash.tooltip = ('Add an upstand along the wall, standing on the worktop '
                          'and running the full length of each run.')

    bs_thickness = inputs.addValueInput(
        BS_THICKNESS_ID, 'Backsplash thickness', length_units,
        adsk.core.ValueInput.createByReal(DEFAULT_BS_THICKNESS_CM))
    bs_thickness.tooltip = 'How far the backsplash projects from the wall.'
    bs_thickness.isVisible = False

    bs_height = inputs.addValueInput(
        BS_HEIGHT_ID, 'Backsplash height', length_units,
        adsk.core.ValueInput.createByReal(DEFAULT_BS_HEIGHT_CM))
    bs_height.tooltip = 'How far the backsplash rises above the worktop surface.'
    bs_height.isVisible = False

    futil.add_handler(args.command.inputChanged, command_input_changed, local_handlers=local_handlers)
    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.executePreview, command_preview, local_handlers=local_handlers)
    futil.add_handler(args.command.validateInputs, command_validate_input, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)


# ---------------------------------------------------------------------------
# Reading the selections
# ---------------------------------------------------------------------------
def _as_box(bounding_box):
    return (bounding_box.minPoint.x, bounding_box.minPoint.y, bounding_box.minPoint.z,
            bounding_box.maxPoint.x, bounding_box.maxPoint.y, bounding_box.maxPoint.z)


def _occurrence_boxes(occurrence, depth=0):
    """One box per solid body inside an occurrence, in WORLD coordinates.

    Measured from the bodies rather than from Occurrence.boundingBox: bodies
    reached through an occurrence are proxies, so their bounding boxes are
    unambiguously in the assembly's (root) frame — the frame the geometry module
    works in. Recurses, because a "side panel" picked in the browser may be a
    small sub-assembly rather than a leaf.
    """
    boxes = []
    try:
        bodies = occurrence.bRepBodies
        for i in range(bodies.count):
            body = bodies.item(i)
            if body.isSolid:
                boxes.append(_as_box(body.boundingBox))
    except Exception:
        pass
    if depth < 4:
        try:
            children = occurrence.childOccurrences
            for i in range(children.count):
                boxes.extend(_occurrence_boxes(children.item(i), depth + 1))
        except Exception:
            pass
    if not boxes:
        # Nothing measurable from bodies — fall back to the occurrence's own box.
        try:
            boxes.append(_as_box(occurrence.boundingBox))
        except Exception:
            pass
    return boxes


# Measured boxes per selected entity, kept for the life of the dialog so a
# preview redraw (every input change) doesn't re-walk the same cabinets.
_box_memo = entity_memo.EntityMemo()


def _world_boxes(entity):
    """World-space bounding boxes for one selected side panel (memoised)."""
    return _box_memo.get(entity, _measure_world_boxes)


def _measure_world_boxes(entity):
    try:
        if entity.objectType == adsk.fusion.Occurrence.classType():
            return _occurrence_boxes(adsk.fusion.Occurrence.cast(entity))
        if entity.objectType == adsk.fusion.BRepBody.classType():
            return [_as_box(adsk.fusion.BRepBody.cast(entity).boundingBox)]
    except Exception:
        pass
    return []


def _wall_frame(face):
    """(origin_xy, normal_xyz) in world coordinates for a picked wall face."""
    try:
        point = face.pointOnFace
        ok, normal = face.evaluator.getNormalAtPoint(point)
        if not ok:
            return None
        return ((point.x, point.y), (normal.x, normal.y, normal.z))
    except Exception:
        return None


def _read_inputs(inputs):
    """(plans, spec, rejected) from the dialog's current state.

    `spec` carries the numbers: depth_total, thickness, and the backsplash
    settings (thickness/height are 0 when the box is unticked, so every consumer
    can just test for a positive number).

    Shared by the preview and the execute so what you see is exactly what gets
    built. Resolves every selection up front: creating a component invalidates
    the live selection list, and the next selection(i) then throws "invalid
    argument index" (the trap Set Type documents).
    """
    walls_input: adsk.core.SelectionCommandInput = inputs.itemById(WALLS_ID)
    sides_input: adsk.core.SelectionCommandInput = inputs.itemById(SIDES_ID)

    walls = [walls_input.selection(i).entity for i in range(walls_input.selectionCount)]
    boxes = []
    for i in range(sides_input.selectionCount):
        boxes.extend(_world_boxes(sides_input.selection(i).entity))

    wants_backsplash = bool(inputs.itemById(BACKSPLASH_ID).value)
    spec = {
        'depth_total': inputs.itemById(DEPTH_ID).value + inputs.itemById(OVERHANG_ID).value,
        'thickness': inputs.itemById(THICKNESS_ID).value,
        'bs_thickness': inputs.itemById(BS_THICKNESS_ID).value if wants_backsplash else 0.0,
        'bs_height': inputs.itemById(BS_HEIGHT_ID).value if wants_backsplash else 0.0,
    }

    # Every wall is resolved first, so each run can be clipped against all the
    # OTHERS — that is what stops a run at the corner instead of letting it
    # carry on through the adjoining wall.
    frames = [f for f in (_wall_frame(wall) for wall in walls) if f]
    rejected = len(walls) - len(frames)

    plans = []
    for index, frame in enumerate(frames):
        others = [f for j, f in enumerate(frames) if j != index]
        plan = geom.plan_run(frame[0], frame[1], boxes, spec['depth_total'], others)
        if plan is None:
            rejected += 1
        else:
            plans.append(plan)
    return plans, spec, rejected


def _pieces(plans, spec):
    """Every solid the command will create, in build order.

    A flat list of dicts — `corners`, `z` (underside), `height`, `name`, `kind`
    — so the slabs and the backsplashes go through exactly the same preview,
    build and overlap-cut paths.
    """
    pieces = []
    multiple = len(plans) > 1
    for index, plan in enumerate(plans):
        suffix = f' {index + 1}' if multiple else ''
        pieces.append({'kind': 'slab',
                       'name': f'{COMPONENT_NAME}{suffix}',
                       'corners': plan['corners'],
                       'z': plan['z'],
                       'height': spec['thickness'],
                       'plan': plan})
        if spec['bs_thickness'] > 0 and spec['bs_height'] > 0:
            corners = geom.backsplash_corners(plan, spec['bs_thickness'])
            if corners:
                pieces.append({'kind': 'backsplash',
                               'name': f'{BACKSPLASH_NAME}{suffix}',
                               'corners': corners,
                               # Stands ON the worktop, so it starts at the
                               # slab's top face.
                               'z': plan['z'] + spec['thickness'],
                               'height': spec['bs_height'],
                               'plan': plan})
    return pieces


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def command_input_changed(args: adsk.core.InputChangedEventArgs):
    """Show the backsplash numbers only when the box is ticked.

    Deliberately reacts ONLY to the checkbox: mutating any input while a
    SELECTION is changing clears the in-progress pick, which would collapse a
    multi-panel selection down to one (the reentrancy trap Set Type documents).
    """
    if args.input.id != BACKSPLASH_ID:
        return
    wanted = bool(args.inputs.itemById(BACKSPLASH_ID).value)
    args.inputs.itemById(BS_THICKNESS_ID).isVisible = wanted
    args.inputs.itemById(BS_HEIGHT_ID).isVisible = wanted


def command_validate_input(args: adsk.core.ValidateInputsEventArgs):
    inputs = args.inputs
    walls = inputs.itemById(WALLS_ID)
    sides = inputs.itemById(SIDES_ID)
    valid = bool(
        walls and walls.selectionCount >= 1
        and sides and sides.selectionCount >= 2
        and inputs.itemById(DEPTH_ID).value > 0
        and inputs.itemById(THICKNESS_ID).value > 0
        and inputs.itemById(DEPTH_ID).value + inputs.itemById(OVERHANG_ID).value > 0)
    if valid and inputs.itemById(BACKSPLASH_ID).value:
        valid = (inputs.itemById(BS_THICKNESS_ID).value > 0
                 and inputs.itemById(BS_HEIGHT_ID).value > 0)
    args.areInputsValid = valid


def command_preview(args: adsk.core.CommandEventArgs):
    """Draw each planned slab as a wireframe box, with its length labelled.

    Custom graphics rather than real geometry: it is instant, and it means a bad
    pick shows up on screen instead of as unwanted components in the browser.
    """
    global _graphics_group
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        root = design.rootComponent

        _clear_preview()

        plans, spec, _rejected = _read_inputs(args.command.commandInputs)
        pieces = _pieces(plans, spec)
        if not pieces:
            app.activeViewport.refresh()
            return

        group = root.customGraphicsGroups.add()
        slab_color = adsk.fusion.CustomGraphicsSolidColorEffect.create(
            adsk.core.Color.create(*PREVIEW_COLOR))
        splash_color = adsk.fusion.CustomGraphicsSolidColorEffect.create(
            adsk.core.Color.create(*BACKSPLASH_COLOR))

        lines = entity_memo.LineBatch()
        for piece in pieces:
            color = slab_color if piece['kind'] == 'slab' else splash_color
            low = piece['z']
            high = low + piece['height']
            corners = piece['corners']

            # The two rectangles, plus the four verticals joining them.
            for z in (low, high):
                lines.strip(color,
                            [(x, y, z) for x, y in corners] + [(corners[0][0], corners[0][1], z)])
            for x, y in corners:
                lines.strip(color, [(x, y, low), (x, y, high)])

            if piece['kind'] != 'slab':
                continue
            # Label the run length over the middle of the slab, so a sliver is
            # unmissable before you commit to it. Centroid rather than a fixed
            # corner pair: a run clipped by a wall is not a rectangle.
            mid = adsk.core.Point3D.create(
                sum(c[0] for c in corners) / len(corners),
                sum(c[1] for c in corners) / len(corners), high)
            transform = adsk.core.Matrix3D.create()
            transform.translation = mid.asVector()
            text = group.addText(f'{piece["plan"]["length"] * 10:.0f} mm', 'Arial', 2.0, transform)
            text.billBoarding = adsk.fusion.CustomGraphicsBillBoard.create(mid)
            text.color = color

        lines.draw(group, PREVIEW_WEIGHT)
        _graphics_group = group
        app.activeViewport.refresh()
    except Exception:
        futil.handle_error('Countertop: preview')


def _add_strip(group, color, points):
    """One open polyline through `points` (world cm)."""
    flat = []
    for x, y, z in points:
        flat.extend((x, y, z))
    coords = adsk.fusion.CustomGraphicsCoordinates.create(flat)
    line = group.addLines(coords, [], True)
    line.weight = PREVIEW_WEIGHT
    line.color = color
    return line


def _clear_preview():
    global _graphics_group
    if _graphics_group:
        try:
            _graphics_group.deleteMe()
        except Exception:
            pass
        _graphics_group = None


def command_execute(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Execute Event')
    try:
        plans, spec, rejected = _read_inputs(args.command.commandInputs)
        _build(plans, spec, rejected)
    except Exception:
        futil.handle_error('Countertop: build failed', show_message_box=True)


def _build(plans, spec, rejected):
    if not plans:
        ui.messageBox(
            'No countertop could be built.\n\n'
            'Check that the picked faces are vertical WALL faces (not the floor or '
            'a cabinet top) and that the side panels stand in front of them.')
        return

    design = adsk.fusion.Design.cast(app.activeProduct)
    root = design.rootComponent

    pieces = _pieces(plans, spec)
    for piece in pieces:
        piece['body'] = _build_piece(root, piece)

    cuts = _cut_overlaps(root, pieces)
    ui.messageBox(_summary(plans, pieces, spec, cuts, rejected))


def _summary(plans, pieces, spec, cuts, rejected):
    slabs = [p for p in pieces if p['kind'] == 'slab']
    splashes = [p for p in pieces if p['kind'] == 'backsplash']

    lines = [f'Created {len(slabs)} countertop run(s), '
             f'{spec["depth_total"] * 10:.0f} mm deep × '
             f'{spec["thickness"] * 10:.0f} mm thick.']
    for piece in slabs:
        plan = piece['plan']
        lines.append(f'  • {piece["name"]}: {plan["length"] * 10:.0f} mm long, '
                     f'underside at {plan["z"] * 10:.0f} mm '
                     f'(ends set by {plan["used"]} of {plan["total"]} panels)')
    if splashes:
        lines.append(f'\nPlus {len(splashes)} backsplash(es), '
                     f'{spec["bs_thickness"] * 10:.0f} mm thick × '
                     f'{spec["bs_height"] * 10:.0f} mm tall.')
    if cuts:
        lines.append(f'\nTrimmed {cuts} overlapping piece(s) at the corner(s) — the '
                     f'first run was kept whole and used as the cutting tool.')
    if rejected:
        lines.append(f'\n{rejected} picked face(s) produced nothing — not a vertical '
                     f'wall, or no side panels standing in front of them.')
    return '\n'.join(lines)


def _build_piece(root, piece):
    """One solid: a component holding one extruded body. Returns the body as a
    proxy in the ROOT context, which is the form Combine needs."""
    occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    comp.name = piece['name']
    # Its own category, not 'panel' — costed and banded like a sheet good, but
    # kept out of the cut list and the nest (see config.WC_SHEET_LIKE).
    wc_attrs.set_category(comp, config.WC_CAT_COUNTERTOP)

    # Sketch on a construction plane at the piece's underside, so the extrude is
    # a plain positive distance and the solid starts exactly there.
    planes = comp.constructionPlanes
    plane_input = planes.createInput()
    plane_input.setByOffset(comp.xYConstructionPlane,
                            adsk.core.ValueInput.createByReal(piece['z']))
    sketch = comp.sketches.add(planes.add(plane_input))

    # The new component sits at the origin, so its model space IS world space;
    # modelToSketchSpace still does the mapping rather than assuming the sketch's
    # axes line up with X/Y.
    points = [sketch.modelToSketchSpace(adsk.core.Point3D.create(x, y, piece['z']))
              for x, y in piece['corners']]
    if len(points) < 3:
        raise RuntimeError(f'{piece["name"]}: outline has fewer than three corners.')

    # Any number of sides — a run clipped by a wall is no longer a rectangle.
    # Each segment reuses the previous end point so the loop really closes;
    # coincident endpoints alone would leave Fusion without a profile.
    lines = sketch.sketchCurves.sketchLines
    first = lines.addByTwoPoints(points[0], points[1])
    previous = first
    for point in points[2:]:
        previous = lines.addByTwoPoints(previous.endSketchPoint, point)
    lines.addByTwoPoints(previous.endSketchPoint, first.startSketchPoint)

    if sketch.profiles.count == 0:
        raise RuntimeError(f'{piece["name"]}: the outline did not close into a profile.')
    profile = sketch.profiles.item(0)

    # Extrude UP. The plane is an offset of XY so its normal is +Z, but derive
    # the sign rather than trusting that.
    normal = sketch.xDirection.crossProduct(sketch.yDirection)
    sign = 1.0 if normal.z >= 0 else -1.0

    extrudes = comp.features.extrudeFeatures
    ext_input = extrudes.createInput(profile, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    ext_input.setDistanceExtent(False, adsk.core.ValueInput.createByReal(sign * piece['height']))
    feature = extrudes.add(ext_input)

    body = feature.bodies.item(0)
    try:
        body.name = piece['name']
    except Exception:
        pass
    # Combine works across components only on bodies expressed in the assembly
    # context, so hand back the proxy rather than the component-space body.
    try:
        return body.createForAssemblyContext(occ)
    except Exception:
        return body


def _cut_overlaps(root, pieces):
    """Trim the coincident material where two runs meet at an inside corner.

    Two rectangular runs on an L- or U-shaped kitchen overlap over roughly
    depth × depth. Left alone that is duplicated solid: it looks wrong, and the
    cut list would bill the corner twice. So each overlapping pair is resolved
    with a Combine **cut**, the EARLIER run acting as the tool.

    `isKeepToolBodies = True` — the tool is a real worktop that must survive the
    operation, unlike Trim's disposable grown copies. The result is one whole
    run plus one notched run that butts cleanly against it.

    Backsplashes are trimmed the same way, so the upstands meet in the corner
    without doubling up too.
    """
    combines = root.features.combineFeatures
    cuts = 0
    for keep, cut in geom.overlapping_pairs(pieces):
        tool, target = pieces[keep].get('body'), pieces[cut].get('body')
        if not tool or not target:
            continue
        try:
            tools = adsk.core.ObjectCollection.create()
            tools.add(tool)
            combine_input = combines.createInput(target, tools)
            combine_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
            combine_input.isKeepToolBodies = True
            combines.add(combine_input)
            cuts += 1
        except Exception as error:
            futil.log(f'Countertop: corner cut failed for "{pieces[cut]["name"]}": {error}')
    return cuts


def command_destroy(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Destroy Event')
    global local_handlers
    _clear_preview()
    _box_memo.clear()
    app.activeViewport.refresh()
    local_handlers = []
