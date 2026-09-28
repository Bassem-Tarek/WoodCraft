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

"""Skirting — the plinth that closes the gap under an assembled kitchen.

Deliberately NOT part of the Countertop command: a worktop is referenced off the
WALL behind the cabinets, a plinth off the cabinet FRONTS, and forcing one dialog to
ask for both references made neither read clearly.

What you select
---------------
    Front faces        one per run — the front face of an end panel, or of any
                       cabinet in that run. This is the reference: its plane is the
                       front of the run and its normal says which way the run faces.
                       Picking it rather than inferring it is what lets a run be at
                       any angle and removes the guesswork over which end of a
                       symmetric side panel is the front.
    Run side panels    the side panels of the END cabinets of each run. They only
                       supply two things: how far the run reaches along its own
                       direction, and how high the underside of the carcass sits.
    Island side panels the same, for a cabinet block that gets skirting all the way
                       ROUND rather than across the front. Separate input because
                       an island is a different shape of answer, not a different
                       setting — and one command run can do both.
    Ground             optional. Any planar face or construction plane; its height
                       is the floor. Left empty, the floor is Z = 0.

and two numbers: the board's `thickness`, and its `setback` from the front of the
cabinets — the toe recess.

What it builds
--------------
One component per run ('Skirting', 'Skirting 2' …). Inside it, one sub-component per
physical length of board, because a 4.5 m run is not one piece: anything longer than
skirting_geom.MAX_PIECE_CM (3 m) is divided into equal lengths, and each piece is its
own component so the cut list counts it as a real part rather than as one impossible
board.

Corners are MITRED. The maths lives in commands/skirting_geom.py, which offsets the
run lines and intersects them, so an L, a U and a closed island loop all come out of
one construction — and the material at a corner is counted once, not twice.

Shapes it handles
-----------------
A straight run is one segment. An L or a U is two or three segments that meet, found
by intersecting their front lines near their ends and chained automatically, so you
select all the end panels and all the front faces in one go. A galley — two parallel
runs facing each other — deliberately does NOT chain: parallel lines never meet, so
each gets its own skirting. An island is a closed loop of four mitred sides.
"""

import os

import adsk.core
import adsk.fusion

from .. import entity_memo
from .. import ui_helpers
from .. import wc_attrs
from .. import part_names
from .. import skirting_geom as geom
from ...lib import fusionAddInUtils as futil
from ... import config

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_skirting'
CMD_NAME = 'Skirting'
CMD_Description = (
    'Builds the plinth under an assembled kitchen. Pick the front face of each run '
    'and the side panels of its end cabinets; the skirting runs from the underside '
    'of the carcass to the floor, set back from the fronts by the amount you give. '
    'Corners are mitred, runs longer than 3 m are split into equal boards, and an '
    'island gets skirting all the way round.'
)
IS_PROMOTED = True

PANEL_ID = config.KITCHEN_PANEL_ID
PANEL_NAME = config.KITCHEN_PANEL_NAME

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

RUNS_ID = 'sk_runs'
RETURNS_ID = 'sk_returns'
ISLAND_ID = 'sk_island'
FRONTS_ID = 'sk_fronts'
GROUND_ID = 'sk_ground'
THICKNESS_ID = 'sk_thickness'
SETBACK_ID = 'sk_setback'

DEFAULT_THICKNESS_CM = 1.8      # 18 mm board
DEFAULT_SETBACK_CM = 5.0        # 50 mm toe recess

# A front face has to be near-vertical: its normal must be near-horizontal. A plinth
# referenced off a worktop or a floor is always a mis-pick, and catching it here
# gives a clear message instead of a nonsense board. ~3° of slop.
FRONT_NORMAL_Z_TOL = 0.05

# How far a side panel's own front may sit from the picked front plane and still
# count as part of that run. Generous enough for a panel set back behind a door or
# an applied end, tight enough that the far side of a galley never matches.
RUN_MATCH_TOL_CM = 30.0

# Two cabinets belong to the same run when they face the same way and their fronts
# lie in the same plane. 5 cm of slop absorbs a cabinet set fractionally proud of
# its neighbour without merging a run with the one behind it.
SAME_RUN_TOL_CM = 5.0

# Preview colours: the boards in wood amber, the returns in orange so an end wrap
# is obviously an end wrap before you commit to it.
PREVIEW_COLOR = (217, 162, 27, 255)
RETURN_COLOR = (240, 106, 32, 255)

# Attributes stamped on every board, so a later run of the command can recover the
# line it was built from and mitre against it instead of crashing into it.
WC_SKIRT_LINE = 'skirtLine'
WC_SKIRT_SPEC = 'skirtSpec'

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


# ---------------------------------------------------------------------------
# Reading the selection
# ---------------------------------------------------------------------------
def _as_box(bounding_box):
    return (bounding_box.minPoint.x, bounding_box.minPoint.y, bounding_box.minPoint.z,
            bounding_box.maxPoint.x, bounding_box.maxPoint.y, bounding_box.maxPoint.z)


def _occurrence_boxes(occurrence, depth=0):
    """One box per solid body inside an occurrence, in WORLD coordinates.

    From the bodies rather than Occurrence.boundingBox: bodies reached through an
    occurrence are proxies, so their boxes are unambiguously in the assembly frame —
    the frame the geometry module works in. Recurses, because a "side panel" picked
    in the browser may be a small sub-assembly rather than a leaf. Same helper the
    Countertop command uses, for the same reason."""
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
        try:
            boxes.append(_as_box(occurrence.boundingBox))
        except Exception:
            pass
    return boxes


# Measured boxes per selected entity, kept for the life of the dialog so a
# preview redraw (every input change) doesn't re-walk the same cabinets.
_box_memo = entity_memo.EntityMemo()
_class_memo = entity_memo.EntityMemo()


def _world_boxes(entity):
    """World-space bounding boxes for one selection (memoised for the dialog)."""
    return _box_memo.get(entity, _measure_world_boxes)


def _measure_world_boxes(entity):
    """World-space bounding boxes for one selected side panel."""
    try:
        if entity.objectType == adsk.fusion.Occurrence.classType():
            return _occurrence_boxes(adsk.fusion.Occurrence.cast(entity))
        if entity.objectType == adsk.fusion.BRepBody.classType():
            return [_as_box(adsk.fusion.BRepBody.cast(entity).boundingBox)]
    except Exception:
        pass
    return []


def _collect_boxes(selection_input):
    boxes = []
    for i in range(selection_input.selectionCount):
        boxes.extend(_world_boxes(selection_input.selection(i).entity))
    return boxes


def _front_frame(face):
    """(origin_xy, outward_normal_xy) in world coordinates for a picked front face.

    The normal is flattened to horizontal and normalised. Its SIGN is taken as
    given — it points out of the solid, which for a cabinet front is out into the
    room, which is exactly the direction the skirting sets back from."""
    try:
        point = face.pointOnFace
        ok, normal = face.evaluator.getNormalAtPoint(point)
        if not ok:
            return None
    except Exception:
        return None
    if abs(normal.z) > FRONT_NORMAL_Z_TOL:
        return None                      # near-horizontal face: not a cabinet front
    flat = geom.normalize((normal.x, normal.y))
    if geom.length(flat) < 0.5:
        return None
    return (point.x, point.y), flat


def _ground_z(selection_input, default=0.0):
    """Floor height from the optional ground pick, else `default`."""
    if not selection_input or selection_input.selectionCount == 0:
        return default
    entity = selection_input.selection(0).entity
    try:
        if entity.objectType == adsk.fusion.ConstructionPlane.classType():
            return adsk.fusion.ConstructionPlane.cast(entity).geometry.origin.z
    except Exception:
        pass
    try:
        return entity.pointOnFace.z
    except Exception:
        pass
    try:
        return entity.boundingBox.minPoint.z
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Turning the selection into segments
# ---------------------------------------------------------------------------
def _box_corners_xy(box):
    minx, miny, _minz, maxx, maxy, _maxz = box
    return ((minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy))


def _front_distance(box, normal):
    """How far the FRONT of this panel reaches along `normal`."""
    return max(geom.dot(corner, normal) for corner in _box_corners_xy(box))


def _assign_boxes(boxes, frames):
    """{frame index: [box]} — each panel joins the run whose front plane its own
    front sits closest to.

    Distance along that run's normal, not straight-line distance: a panel at the far
    end of a long run is metres away from the picked face but sits in the same
    plane, which is what actually identifies it. Panels that match nothing within
    RUN_MATCH_TOL_CM are reported rather than silently attached to the nearest run."""
    assigned = {i: [] for i in range(len(frames))}
    orphans = []
    for box in boxes:
        corners = _box_corners_xy(box)
        candidates = []
        for i, (origin, normal) in enumerate(frames):
            plane_d = geom.dot(origin, normal)
            gap = abs(_front_distance(box, normal) - plane_d)
            if gap > RUN_MATCH_TOL_CM:
                continue

            # Plane distance alone is ambiguous at a corner: the side panel of the
            # corner cabinet genuinely lies in BOTH runs' front planes, so both gaps
            # are zero and the first frame would always win. Break the tie on shape.
            # A side panel is THIN across its run and DEEP along the run's normal
            # (it is the gable, ~18 mm by ~600 mm), so the frame it belongs to is
            # the one it is deep along.
            along_u = geom.left_normal(normal)
            ext_n = (max(geom.dot(c, normal) for c in corners)
                     - min(geom.dot(c, normal) for c in corners))
            ext_u = (max(geom.dot(c, along_u) for c in corners)
                     - min(geom.dot(c, along_u) for c in corners))
            candidates.append((0 if ext_n >= ext_u else 1, gap, i))

        if not candidates:
            orphans.append(box)
        else:
            candidates.sort()
            assigned[candidates[0][2]].append(box)
    return assigned, orphans


def _run_segment(frame, boxes, setback):
    """(Segment, bottom_z) for one run, or (None, None) if it can't be measured."""
    origin, normal = frame
    if not boxes:
        return None, None
    u = geom.left_normal(normal)

    spans = [geom.dot(corner, u) for box in boxes for corner in _box_corners_xy(box)]
    s_min, s_max = min(spans), max(spans)
    if s_max - s_min < geom.MM:
        return None, None
    bottom_z = min(box[2] for box in boxes)

    # (u, normal) is an orthonormal basis, so a point is just u·s + normal·d. Build
    # the ends from ABSOLUTE projections rather than by stepping along u from some
    # base point: the spans above are absolute (dot(corner, u)), and adding them to
    # a base that already has its own u-component would shift the whole run by that
    # component — which is how a run starting at x=0 ended up starting at x=150.
    d_outer = geom.dot(origin, normal) - setback

    def point(s):
        return geom.add(geom.scale(u, s), geom.scale(normal, d_outer))

    segment = geom.Segment(point(s_min), point(s_max), geom.scale(normal, -1.0))
    return segment, bottom_z


def _island_footprint(boxes, frames):
    """Four plan corners for an island, oriented by a matching front face if there
    is one and axis-aligned otherwise.

    Orienting off a front face matters for an island set at an angle to the world
    axes: a world-axis bounding box would wrap it in a rectangle that is neither its
    size nor its direction."""
    if not boxes:
        return None

    frame = None
    best_gap = None
    for candidate in frames:
        origin, normal = candidate
        plane_d = geom.dot(origin, normal)
        gap = min(abs(_front_distance(box, normal) - plane_d) for box in boxes)
        if best_gap is None or gap < best_gap:
            frame, best_gap = candidate, gap
    if frame is None or best_gap is None or best_gap > RUN_MATCH_TOL_CM:
        # No usable front face: fall back to the world axes.
        minx = min(b[0] for b in boxes)
        miny = min(b[1] for b in boxes)
        maxx = max(b[3] for b in boxes)
        maxy = max(b[4] for b in boxes)
        return [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)]

    _origin, normal = frame
    u = geom.left_normal(normal)
    us = [geom.dot(c, u) for box in boxes for c in _box_corners_xy(box)]
    ns = [geom.dot(c, normal) for box in boxes for c in _box_corners_xy(box)]
    u_lo, u_hi, n_lo, n_hi = min(us), max(us), min(ns), max(ns)

    def point(su, sn):
        return geom.add(geom.scale(u, su), geom.scale(normal, sn))

    return [point(u_lo, n_lo), point(u_hi, n_lo), point(u_hi, n_hi), point(u_lo, n_hi)]


# ---------------------------------------------------------------------------
# Working out which way a cabinet faces, without being told
# ---------------------------------------------------------------------------
def _classified_boxes(occurrence, depth=0):
    """Memoised for the dialog at the top level (see _measure_classified)."""
    if depth == 0:
        return _class_memo.get(occurrence, _measure_classified)
    return _measure_classified(occurrence, depth)


def _measure_classified(occurrence, depth=0):
    """[(group, box)] for every solid inside a cabinet, group from part_names.

    'group' is 'carcass', 'door' or None — the same classification Set Finish uses,
    which is what lets this command find the cabinet's FRONT without the user
    pointing at it: the doors are on the front, by definition."""
    out = []
    try:
        component = occurrence.component
        group = part_names.group_for(component)
        comp_bodies = component.bRepBodies
        occ_bodies = occurrence.bRepBodies
        comp_count, occ_count = comp_bodies.count, occ_bodies.count
        for i in range(comp_count):
            body = occ_bodies.item(i) if i < occ_count else comp_bodies.item(i)
            if body.isSolid:
                out.append((group, _as_box(body.boundingBox)))
    except Exception:
        pass
    if depth < 4:
        try:
            children = occurrence.childOccurrences
            for i in range(children.count):
                out.extend(_measure_classified(children.item(i), depth + 1))
        except Exception:
            pass
    return out


def _union(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), min(b[2] for b in boxes),
            max(b[3] for b in boxes), max(b[4] for b in boxes), max(b[5] for b in boxes))


def _axis_snap(vector):
    """The dominant world axis of a horizontal vector, as a unit direction.

    Snapped rather than used raw because every measurement here comes from
    axis-aligned bounding boxes: a box already cannot describe a rotated cabinet, so
    pretending to recover an arbitrary angle from one would be false precision. A
    run at an angle is exactly the case the optional Front faces input exists for."""
    x, y = vector
    if abs(x) >= abs(y):
        return (1.0 if x >= 0 else -1.0, 0.0)
    return (0.0, 1.0 if y >= 0 else -1.0)


def _cabinet_frame(occurrence):
    """(origin, outward normal, carcass bottom z) for one cabinet, or None.

    The front is found from the cabinet's own doors: a door sits on the front face,
    so the direction from the carcass's centre to the doors' centre IS the way the
    cabinet faces. The front PLANE is then the outer face of those doors, because
    that is the surface a toe recess is measured back from.

    Returns None when a cabinet has no recognisable front — an open shelf unit, or
    one whose parts are named in some other vocabulary. Those are reported and the
    user can pin them with a Front face pick instead of the command guessing."""
    rows = _classified_boxes(occurrence)
    if not rows:
        return None
    doors = [box for group, box in rows if group == 'door']
    carcass = [box for group, box in rows if group == 'carcass'] or [b for _g, b in rows]
    if not doors or not carcass:
        return None

    whole = _union([b for _g, b in rows])
    door_box = _union(doors)
    centre = ((whole[0] + whole[3]) * 0.5, (whole[1] + whole[4]) * 0.5)
    door_centre = ((door_box[0] + door_box[3]) * 0.5, (door_box[1] + door_box[4]) * 0.5)

    offset = geom.sub(door_centre, centre)
    if geom.length(offset) < geom.MM:
        return None
    normal = _axis_snap(offset)

    front_d = max(geom.dot(c, normal) for c in _box_corners_xy(door_box))
    origin = geom.scale(normal, front_d)
    bottom_z = min(b[2] for b in carcass)
    return origin, normal, bottom_z


def _auto_frames(occurrences):
    """Group cabinets into runs by the way they face and where their fronts sit.

    [(frame, [box], bottom_z)]. Two cabinets join the same run when their normals
    agree and their front planes coincide within SAME_RUN_TOL_CM — which is exactly
    what "the same run of cabinets" means, and needs no picking at all."""
    runs = []
    unknown = []
    for occurrence in occurrences:
        frame = _cabinet_frame(occurrence)
        if frame is None:
            unknown.append(occurrence)
            continue
        origin, normal, bottom_z = frame
        plane_d = geom.dot(origin, normal)
        boxes = _world_boxes(occurrence)

        for run in runs:
            if (geom.dot(run['normal'], normal) > 0.99
                    and abs(run['d'] - plane_d) <= SAME_RUN_TOL_CM):
                run['boxes'].extend(boxes)
                run['bottom'] = min(run['bottom'], bottom_z)
                break
        else:
            runs.append({'normal': normal, 'd': plane_d, 'boxes': list(boxes),
                         'bottom': bottom_z})
    return runs, unknown


# ---------------------------------------------------------------------------
# Returned ends
# ---------------------------------------------------------------------------
def _return_segment(boxes, frame, setback):
    """The piece that wraps an exposed end and runs back under the side panel.

    A run against a wall stops square; a run with a walkway beside it turns the
    corner, and the plinth continues under the gable to the back of the cabinet —
    the dark band that carries on around the end in a finished kitchen.

    Flush with the panel's OUTSIDE face, not set back like the front: the setback is
    a toe recess, and there is no toe against the side of a cabinet. It runs from
    the front line back to the back of the panel."""
    origin, normal = frame
    u = geom.left_normal(normal)
    corners = [c for box in boxes for c in _box_corners_xy(box)]
    if not corners:
        return None

    us = [geom.dot(c, u) for c in corners]
    ns = [geom.dot(c, normal) for c in corners]
    u_lo, u_hi = min(us), max(us)
    d_front = geom.dot(origin, normal) - setback
    d_back = min(ns)
    if d_front - d_back < geom.MM:
        return None
    return (u_lo, u_hi, d_front, d_back, u, normal)


def _build_return(run_span, panel_boxes, frame, setback):
    """A Segment for a returned end, oriented away from the run it belongs to."""
    made = _return_segment(panel_boxes, frame, setback)
    if made is None:
        return None
    u_lo, u_hi, d_front, d_back, u, normal = made
    s_min, s_max = run_span

    # Which end of the run is this? The outer face is the panel side facing away
    # from the run's middle — that is the face the return has to be flush with.
    middle = (s_min + s_max) * 0.5
    if (u_lo + u_hi) * 0.5 <= middle:
        outer_u = u_lo
        inward = u                     # the cabinet is on the +u side
    else:
        outer_u = u_hi
        inward = geom.scale(u, -1.0)

    def point(d):
        return geom.add(geom.scale(u, outer_u), geom.scale(normal, d))

    return geom.Segment(point(d_front), point(d_back), inward)


# ---------------------------------------------------------------------------
# Planning — one pass, shared by the preview and the build
# ---------------------------------------------------------------------------
def _plan(inputs):
    """([{'chain', 'groups', 'bottom', 'closed', 'label'}], [note]).

    Deliberately the ONLY place the selection is turned into geometry, so the
    wireframe you see while picking and the boards you get on OK cannot drift
    apart — they are the same numbers."""
    notes = []
    thickness = inputs.itemById(THICKNESS_ID).value
    setback = inputs.itemById(SETBACK_ID).value
    ground_z = _ground_z(inputs.itemById(GROUND_ID))

    picked_frames, bad_faces = [], 0
    fronts_input = inputs.itemById(FRONTS_ID)
    for i in range(fronts_input.selectionCount):
        frame = _front_frame(fronts_input.selection(i).entity)
        if frame is None:
            bad_faces += 1
        else:
            picked_frames.append(frame)
    if bad_faces:
        notes.append(f'{bad_faces} picked face(s) were not vertical and were ignored.')

    runs_input = inputs.itemById(RUNS_ID)
    selections = [runs_input.selection(i).entity for i in range(runs_input.selectionCount)]

    runs = []
    if picked_frames:
        # An explicit pick wins: the user is overriding, usually because a run is at
        # an angle or has no doors to read.
        boxes = []
        for entity in selections:
            boxes.extend(_world_boxes(entity))
        assigned, orphans = _assign_boxes(boxes, picked_frames)
        for index, frame in enumerate(picked_frames):
            if assigned[index]:
                runs.append({'normal': frame[1], 'd': geom.dot(frame[0], frame[1]),
                             'boxes': assigned[index],
                             'bottom': min(b[2] for b in assigned[index])})
        if orphans:
            notes.append(f'{len(orphans)} selection(s) matched no picked front face '
                         f'and were ignored.')
    else:
        occurrences = [e for e in selections
                       if e.objectType == adsk.fusion.Occurrence.classType()]
        if len(occurrences) < len(selections):
            notes.append('Front faces are worked out from each cabinet\'s doors, so '
                         'select CABINETS here (or pick Front faces yourself).')
        runs, unknown = _auto_frames(occurrences)
        if unknown:
            notes.append(f'{len(unknown)} cabinet(s) had no recognisable front and '
                         f'were skipped — pick a Front face for those.')

    # ---- turn each run into a segment ----
    segments, bottoms, spans = [], {}, {}
    for run in runs:
        frame = (geom.scale(run['normal'], run['d']), run['normal'])
        segment, _bottom = _run_segment(frame, run['boxes'], setback)
        if segment is None:
            continue
        segments.append(segment)
        bottoms[id(segment)] = run['bottom']
        u = geom.left_normal(run['normal'])
        corners = [c for box in run['boxes'] for c in _box_corners_xy(box)]
        spans[id(segment)] = (min(geom.dot(c, u) for c in corners),
                              max(geom.dot(c, u) for c in corners))
        run['segment'] = segment
        run['frame'] = frame

    # ---- returned ends ----
    returns_input = inputs.itemById(RETURNS_ID)
    for i in range(returns_input.selectionCount):
        boxes = _world_boxes(returns_input.selection(i).entity)
        if not boxes:
            continue
        host, best = None, None
        for run in runs:
            if 'segment' not in run:
                continue
            gap = abs(_front_distance(boxes[0], run['normal']) - run['d'])
            if best is None or gap < best:
                host, best = run, gap
        if host is None or best is None or best > RUN_MATCH_TOL_CM:
            notes.append('A return end matched no run and was ignored.')
            continue
        segment = _build_return(spans[id(host['segment'])], boxes, host['frame'], setback)
        if segment is None:
            continue
        segment.label = 'return'
        segments.append(segment)
        # A wrapped end reaches the same underside as the run it belongs to: it is
        # the same plinth turning a corner, not a separate height.
        bottoms[id(segment)] = host['bottom']

    plans = []
    for chain in geom.chain_segments(segments):
        bottom = min(bottoms.get(id(seg), 0.0) for seg in chain)
        if bottom - ground_z <= geom.MM:
            notes.append('A run sits at or below the ground, so it was skipped.')
            continue
        plans.append({'chain': chain,
                      'groups': geom.plan_chain(chain, thickness, closed=False),
                      'bottom': bottom, 'ground': ground_z,
                      'closed': False, 'label': 'Skirting'})

    # ---- island ----
    island_boxes = _collect_boxes(inputs.itemById(ISLAND_ID))
    if island_boxes:
        frames = picked_frames or [(geom.scale(r['normal'], r['d']), r['normal'])
                                   for r in runs]
        corners = _island_footprint(island_boxes, frames)
        segs = geom.island_segments(corners, setback) if corners else []
        bottom = min(box[2] for box in island_boxes)
        if segs and bottom - ground_z > geom.MM:
            plans.append({'chain': segs,
                          'groups': geom.plan_chain(segs, thickness, closed=True),
                          'bottom': bottom, 'ground': ground_z,
                          'closed': True, 'label': 'Island Skirting'})
    return plans, notes


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------
def _unique_name(root, base):
    """'Skirting', then 'Skirting 2', 'Skirting 3' … so a second command run doesn't
    collide with the first."""
    existing = set()
    try:
        occs = root.occurrences
        for i in range(occs.count):
            existing.add(occs.item(i).component.name)
    except Exception:
        pass
    if base not in existing:
        return base
    index = 2
    while f'{base} {index}' in existing:
        index += 1
    return f'{base} {index}'


def _build_piece(parent_comp, name, polygon, ground_z, height):
    """One board: a sub-component holding one extruded body."""
    occ = parent_comp.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    comp.name = name
    # A plinth is a sheet good like any other panel — it should be cut and nested.
    wc_attrs.set_category(comp, config.WC_CAT_PANEL)

    planes = comp.constructionPlanes
    plane_input = planes.createInput()
    plane_input.setByOffset(comp.xYConstructionPlane,
                            adsk.core.ValueInput.createByReal(ground_z))
    sketch = comp.sketches.add(planes.add(plane_input))

    points = [sketch.modelToSketchSpace(adsk.core.Point3D.create(x, y, ground_z))
              for x, y in polygon]
    if len(points) < 3:
        raise RuntimeError(f'{name}: outline has fewer than three corners.')

    lines = sketch.sketchCurves.sketchLines
    first = lines.addByTwoPoints(points[0], points[1])
    previous = first
    for point in points[2:]:
        previous = lines.addByTwoPoints(previous.endSketchPoint, point)
    lines.addByTwoPoints(previous.endSketchPoint, first.startSketchPoint)

    if sketch.profiles.count == 0:
        raise RuntimeError(f'{name}: the outline did not close into a profile.')

    # Extrude UP to the underside of the carcass. The plane is an offset of XY so
    # its normal is +Z, but derive the sign rather than trusting that.
    normal = sketch.xDirection.crossProduct(sketch.yDirection)
    sign = 1.0 if normal.z >= 0 else -1.0

    extrudes = comp.features.extrudeFeatures
    ext_input = extrudes.createInput(sketch.profiles.item(0),
                                     adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    ext_input.setDistanceExtent(False, adsk.core.ValueInput.createByReal(sign * height))
    feature = extrudes.add(ext_input)
    try:
        feature.bodies.item(0).name = name
    except Exception:
        pass
    return comp


def _stamp(component, segment, thickness, ground_z, bottom_z):
    """Record the line this board was built from, on the board itself.

    Recovering a plinth's centreline from its solid afterwards is guesswork; storing
    it is exact. Nothing reads these yet — they are here so a later run of the
    command can find an existing run and mitre into it rather than crashing through
    it, without having to reverse-engineer geometry."""
    try:
        wc_attrs.set_value(component, WC_SKIRT_LINE,
                           f'{segment.p0[0]:.6f},{segment.p0[1]:.6f},'
                           f'{segment.p1[0]:.6f},{segment.p1[1]:.6f},'
                           f'{segment.m[0]:.6f},{segment.m[1]:.6f}')
        wc_attrs.set_value(component, WC_SKIRT_SPEC,
                           f'{thickness:.6f},{ground_z:.6f},{bottom_z:.6f}')
    except Exception:
        pass


def _build_run(root, label, groups, segments, ground_z, bottom_z, thickness=0.0):
    """One run component holding one sub-component per board. Returns piece count.

    Refuses a non-positive height rather than handing Fusion a zero-distance
    extrude, which fails with a bare "Some input argument is invalid" and gives the
    user nothing to go on. command_execute screens for this too; keeping the guard
    here as well means no future caller can reintroduce it."""
    height = bottom_z - ground_z
    if height <= geom.MM:
        futil.log(f'Skirting: {label} has no height '
                  f'(underside {bottom_z:.2f} cm, ground {ground_z:.2f} cm) — skipped')
        return 0

    occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    comp.name = _unique_name(root, label)

    made = 0
    for segment, group in zip(segments, groups):
        for polygon in group:
            made += 1
            piece = _build_piece(comp, f'{comp.name} Piece {made}', polygon,
                                 ground_z, height)
            _stamp(piece, segment, thickness, ground_z, bottom_z)
    if made == 0:
        try:
            occ.deleteMe()
        except Exception:
            pass
    return made


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------
def command_created(args: adsk.core.CommandCreatedEventArgs):
    futil.log(f'{CMD_NAME} Command Created Event')
    _box_memo.clear()
    _class_memo.clear()
    inputs = args.command.commandInputs

    runs = inputs.addSelectionInput(
        RUNS_ID, 'Cabinets', 'Select the cabinets that need skirting')
    runs.addSelectionFilter('Occurrences')
    runs.addSelectionFilter('SolidBodies')
    runs.setSelectionLimits(0, 0)
    runs.tooltip = ('Select the cabinets — a whole run at a time is fine. Which way '
                    'each one faces is read from its own doors, and cabinets whose '
                    'fronts line up are grouped into one run, so an L or a U needs '
                    'no extra picking.')

    returns = inputs.addSelectionInput(
        RETURNS_ID, 'Wrap these ends', 'Side panels whose end the skirting turns around')
    returns.addSelectionFilter('Occurrences')
    returns.addSelectionFilter('SolidBodies')
    returns.setSelectionLimits(0, 0)
    returns.tooltip = ('Pick the exposed side panel at the end of a run and the '
                       'skirting turns the corner and runs back underneath it. '
                       'Leave an end out and it stops square — which is what you '
                       'want where the run meets a wall.')

    island = inputs.addSelectionInput(
        ISLAND_ID, 'Island', 'Island cabinets — skirted all the way round')
    island.addSelectionFilter('Occurrences')
    island.addSelectionFilter('SolidBodies')
    island.setSelectionLimits(0, 0)
    island.tooltip = 'Leave empty if there is no island.'

    fronts = inputs.addSelectionInput(
        FRONTS_ID, 'Front faces (optional)', 'Only needed to override the automatic front')
    fronts.addSelectionFilter('PlanarFaces')
    fronts.setSelectionLimits(0, 0)
    fronts.tooltip = ('Leave empty unless the automatic answer is wrong. Pick one '
                      'vertical face per run to say where its front is — needed for '
                      'a run at an angle to the world axes, or one with no doors to '
                      'read. Picking any face here switches the whole command to '
                      'manual, so pick one for EVERY run if you pick at all.')

    ground = inputs.addSelectionInput(
        GROUND_ID, 'Ground (optional)', 'A face or plane at floor level')
    ground.addSelectionFilter('PlanarFaces')
    try:
        ground.addSelectionFilter('ConstructionPlanes')
    except Exception:
        futil.log('Skirting: ConstructionPlanes selection filter unavailable')
    ground.setSelectionLimits(0, 1)
    ground.tooltip = 'Left empty, the floor is Z = 0.'

    length_units = app.activeProduct.unitsManager.defaultLengthUnits
    thickness = inputs.addValueInput(
        THICKNESS_ID, 'Thickness', length_units,
        adsk.core.ValueInput.createByReal(DEFAULT_THICKNESS_CM))
    thickness.tooltip = 'Board thickness. The skirting thickens backward, under the cabinets.'

    setback = inputs.addValueInput(
        SETBACK_ID, 'Setback from front', length_units,
        adsk.core.ValueInput.createByReal(DEFAULT_SETBACK_CM))
    setback.tooltip = ('The toe recess: how far the skirting sits behind the fronts. '
                       'A wrapped end is flush with its panel instead — there is no '
                       'toe against the side of a cabinet.')

    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.executePreview, command_preview, local_handlers=local_handlers)
    futil.add_handler(args.command.validateInputs, command_validate_input, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)


def _clear_preview():
    global _graphics_group
    try:
        if _graphics_group and _graphics_group.isValid:
            _graphics_group.deleteMe()
    except Exception:
        pass
    _graphics_group = None


def _add_strip(group, colour, points):
    flat = []
    for x, y, z in points:
        flat.extend((x, y, z))
    coords = adsk.fusion.CustomGraphicsCoordinates.create(flat)
    line = group.addLines(coords, [], True)
    line.color = colour
    return line


def command_preview(args: adsk.core.CommandEventArgs):
    """Draw every planned board as a wireframe box while you pick.

    Custom graphics rather than real geometry: instant, and a bad pick shows up on
    screen instead of as unwanted components in the browser. Returns are drawn in a
    different colour so a wrapped end is unmistakable before you commit."""
    global _graphics_group
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            return
        _clear_preview()

        plans, _notes = _plan(args.command.commandInputs)
        if not plans:
            app.activeViewport.refresh()
            return

        group = design.rootComponent.customGraphicsGroups.add()
        board = adsk.fusion.CustomGraphicsSolidColorEffect.create(
            adsk.core.Color.create(*PREVIEW_COLOR))
        wrap = adsk.fusion.CustomGraphicsSolidColorEffect.create(
            adsk.core.Color.create(*RETURN_COLOR))

        lines = entity_memo.LineBatch()
        for plan in plans:
            low, high = plan['ground'], plan['bottom']
            for segment, pieces in zip(plan['chain'], plan['groups']):
                colour = wrap if getattr(segment, 'label', '') == 'return' else board
                for polygon in pieces:
                    for z in (low, high):
                        lines.strip(colour,
                                    [(x, y, z) for x, y in polygon]
                                    + [(polygon[0][0], polygon[0][1], z)])
                    for x, y in polygon:
                        lines.strip(colour, [(x, y, low), (x, y, high)])

        lines.draw(group)
        _graphics_group = group
        app.activeViewport.refresh()
    except Exception:
        futil.handle_error('Skirting preview')


def command_validate_input(args: adsk.core.ValidateInputsEventArgs):
    inputs = args.inputs
    has_something = (inputs.itemById(RUNS_ID).selectionCount > 0
                     or inputs.itemById(ISLAND_ID).selectionCount > 0)
    args.areInputsValid = (has_something
                           and inputs.itemById(THICKNESS_ID).value > 0
                           and inputs.itemById(SETBACK_ID).value >= 0)


def command_execute(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Execute Event')
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        ui.messageBox('Skirting needs an open design.')
        return
    _clear_preview()

    inputs = args.command.commandInputs
    thickness = inputs.itemById(THICKNESS_ID).value

    # Plan everything BEFORE creating a single component: building invalidates the
    # live selection lists, and the plan reads them.
    plans, notes = _plan(inputs)

    root = design.rootComponent
    total_runs, total_pieces = 0, 0
    for plan in plans:
        made = _build_run(root, plan['label'], plan['groups'], plan['chain'],
                          plan['ground'], plan['bottom'], thickness)
        if made:
            total_runs += 1
            total_pieces += made

    ui.messageBox(_summary(total_runs, total_pieces,
                           plans[0]['ground'] if plans else 0.0, notes))


def _summary(total_runs, total_pieces, ground_z, notes):
    """The message shown after a build. (Execute called this before it existed,
    so every successful build ended in a NameError instead of a report.)"""
    if total_runs:
        lines = [f'Built {total_runs} skirting run(s) from {total_pieces} board(s), '
                 f'standing on Z = {ground_z * 10.0:.0f} mm.']
    else:
        lines = ['No skirting was built.',
                 'Select the cabinets (or an island), and check the thickness and '
                 'setback.']
    if notes:
        lines.append('')
        lines.extend(notes)
    return '\n'.join(lines)


def command_destroy(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Destroy Event')
    global local_handlers
    _clear_preview()
    _box_memo.clear()
    _class_memo.clear()
    local_handlers = []
