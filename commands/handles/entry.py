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

"""Handles — fit one handle model to every cabinet in the kitchen, and swap it.

Pick a handle from the dropdown, press OK, and every locator point in every
cabinet's "Handles" sketch gets that handle. Pick a different one later and the old
handles come out first, so the dropdown behaves like a choice rather than an
accumulation — which is the whole point: a kitchen has ONE handle model, and
changing your mind should not mean deleting nine occurrences by hand.

Where the handles come from
---------------------------
The `Handles` folder of the `config.HARDWARE_PROJECT_NAME` cloud project — the same
library the Insert Hardware command reads, so a handle added to the project appears
here with no code change.

Where they go
-------------
Into the KITCHEN assembly's root component, not inside the cabinets. A handle is not
part of the cabinet's own design — the same cabinet takes a different handle in the
next kitchen — so it is placed alongside the cabinets and jointed to their sketches.
That also means swapping the handle never touches a cabinet component, so nothing
propagates back into the library.

The placement maths is the EmaarHandlesInsertion script's, which is already proven
against this library:

  - A marker is DRAWN geometry only. Fronts arrive in these sketches as projected
    edges, and projected geometry is never a marker — that is what stops a handle
    landing on an outline corner. Of the drawn vertices, the marker is the one that
    touches nothing else.
  - The handle runs along the edge of the front its marker springs from — up a door,
    across a drawer — and is stood upright when it comes out vertical.
  - It is centred on its point, then slid back along its own length if centring
    would run it off the end of that front, leaving HANDLE_EDGE_CLEARANCE_MM.
  - Nothing about the handle model is assumed; it is measured.

Each handle is RIGID-JOINTED to its marker with an ordinary joint — joint origin,
flip, angle, offsets — exactly as the script does it, so nothing is positioned by
transform and the handle is genuinely attached rather than floating at a coordinate.
The cabinets here are LINKED, though, so their own sketch points are read-only and
cannot carry a joint origin; each cabinet gets a native anchor of our own instead.
See the Jointing section.

What is different here is the SCOPE: the script fits nine handles in one cabinet
document; this fits one handle to every marker in a whole kitchen, which can easily
be forty of them across a dozen linked cabinets. Scope is what shapes the code —
the script could afford to measure and correct each handle in turn, and at kitchen
scale that costs minutes. So the work that is the same for every handle is done
once and only once:

  - the handle FILE is inserted once and copied thereafter (HandleProfile,
    _add_handle);
  - the handle MODEL is measured once, into numbers that are local to it and so
    true of every copy (profile_handle);
  - the way Fusion ORIENTS this handle on a front is calibrated once, and expressed
    in the anchor plane's own basis so it carries to cabinets facing any direction
    (Calibration, calibrate);
  - each cabinet's front FACE is found once and then settles which way is out,
    hosts the anchor sketch, and answers the containment probe.

Every handle after the first is then created already flipped, turned and slid into
place, in one shot, with no timeline edits — and the first handle on each cabinet is
measured against the prediction (check_fit), so the arithmetic is still checked
rather than trusted. A cabinet that fails the check falls back to the slow
measure-and-nudge loop (refine).
"""

import math
import os
import time

import adsk.core
import adsk.fusion

from . import configs
from .. import config_table
from .. import ui_helpers
from .. import wc_attrs
from ...lib import fusionAddInUtils as futil
from ... import config

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_handles'
CMD_NAME = 'Fit Handles'
CMD_Description = (
    'Fits one handle model to every cabinet in the kitchen, using each cabinet\'s '
    'own "Handles" sketch. The handles are placed in the kitchen assembly rather '
    'than inside the cabinets. Choosing a different handle removes the ones already '
    'placed and fits the new one.'
)
IS_PROMOTED = True

PANEL_ID = config.KITCHEN_PANEL_ID
PANEL_NAME = config.KITCHEN_PANEL_NAME

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

HANDLE_ID = 'hd_handle'
INFO_ID = 'hd_info'

# The folder in the hardware project, and the sketch inside each cabinet that
# carries the locator points. Both are alias lists rather than one name, tried in
# preference order and matched with case, spacing and punctuation ignored — a
# cabinet drawn with a sketch called "handles", "Handle" or "Handle Points" is
# fitted like any other instead of being silently skipped. The first entry is
# what these should be called, and what the user is told to look for.
HANDLE_FOLDER_NAMES = ('Handles', 'Handle', 'Handles Library', 'Handle Library',
                       'Hardware Handles', 'Pulls')
HANDLE_SKETCH_NAMES = ('Handles', 'Handle', 'Handle Points', 'Handle Point',
                       'Handles Sketch', 'Handle Sketch', 'Handle Locators',
                       'Handle Positions', 'Handle Position')

HANDLE_FOLDER_NAME = HANDLE_FOLDER_NAMES[0]
HANDLE_SKETCH_NAME = HANDLE_SKETCH_NAMES[0]

# Marks an occurrence as one this command placed, so a later run can find and remove
# it. Read off the model would be guesswork — a kitchen legitimately contains other
# hardware — and a name match would break the moment a handle file is renamed.
WC_HANDLE_TAG = 'placedHandle'

HANDLE_EDGE_CLEARANCE_MM = 10.0
HANDLE_KEEP_INSIDE_FRONT = True
HANDLE_PROFILE_FLIP = False

FLAT_TOL = 0.02          # how close to the mounting plane a face has to be (cm)
POINT_TOL = 0.01         # how far a sketch point may sit off a curve and still count

# Fusion does NOT recompute the document while a command's execute handler is
# running, so a timeline roll-back/roll-forward inside it does not take effect
# until the command has closed. Every measurement taken after one therefore reads
# the ROLLED-BACK position — which is the handle still sitting at the origin. That
# is what sent handles metres away from their markers: the centring loop measured
# zero every pass and kept adding the same correction. So execute does nothing but
# record the choice and fire this event; the fitting runs from the event handler,
# after the command has closed, in exactly the context the original script ran in.
RUN_EVENT_ID = 'WoodCraftFitHandlesRun'

local_handlers = []
_event_handlers = []     # custom-event handlers, kept alive for the add-in's life
_run_event = None
_pending = None          # (label, DataFile) handed from execute to the event
_files = []              # [(label, DataFile)] for the dropdown, index-aligned


class _RunHandler(adsk.core.CustomEventHandler):
    """Runs the fitting once the command has closed and the timeline is live."""

    def notify(self, args):
        global _pending
        job, _pending = _pending, None
        if not job:
            return
        try:
            _fit_handles(job[0], job[1])
        except Exception:
            futil.handle_error(CMD_NAME)


def _arm_event():
    """Register the run event and attach a live handler, replacing any before it.

    Called at start-up AND again before every fire. A custom event registration
    can be left stale — the add-in reloaded without a clean stop, or two copies of
    the module each registering the same id — and a stale one accepts a fire and
    dispatches it to nobody. That failure mode is invisible: the dialog closes, no
    error appears, and no handles are fitted. Re-arming costs microseconds."""
    global _run_event
    try:
        app.unregisterCustomEvent(RUN_EVENT_ID)
    except Exception:
        pass
    _run_event = app.registerCustomEvent(RUN_EVENT_ID)
    handler = _RunHandler()
    _run_event.add(handler)
    _event_handlers.append(handler)
    del _event_handlers[:-2]     # the live one, and the one before it


def start():
    cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_Description, ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)
    _arm_event()

    panel = ui_helpers.get_panel(PANEL_ID, PANEL_NAME)
    control = panel.controls.addCommand(cmd_def)
    control.isPromoted = IS_PROMOTED


def stop():
    global _run_event
    _run_event = None
    _event_handlers.clear()
    try:
        app.unregisterCustomEvent(RUN_EVENT_ID)
    except Exception:
        pass
    ui_helpers.remove_command(PANEL_ID, CMD_ID)


# ---------------------------------------------------------------------------
# Small vector helpers
# ---------------------------------------------------------------------------
def vec(x, y, z):
    return adsk.core.Vector3D.create(x, y, z)


def unit(v):
    v = v.copy()
    v.normalize()
    return v


def scaled(v, s):
    v = v.copy()
    v.scaleBy(s)
    return v


def project(v, normal):
    d = v.dotProduct(normal)
    return vec(v.x - d * normal.x, v.y - d * normal.y, v.z - d * normal.z)


def basis(i, sign=1.0):
    return vec(sign if i == 0 else 0.0, sign if i == 1 else 0.0, sign if i == 2 else 0.0)


def rotate_local(occurrence, local_vector):
    """Direction of a component-local vector in world space."""
    m = occurrence.transform2.asArray()
    return unit(vec(
        m[0] * local_vector.x + m[1] * local_vector.y + m[2] * local_vector.z,
        m[4] * local_vector.x + m[5] * local_vector.y + m[6] * local_vector.z,
        m[8] * local_vector.x + m[9] * local_vector.y + m[10] * local_vector.z))


def wait(cycles=60):
    """Let Fusion finish loading an inserted external component.

    Bounding boxes read straight after addByInsert come back as zeros or as a
    partially-oriented box, so every measurement below is taken only after pumping
    the event loop. Straight from the script, and for the same reason."""
    for _ in range(cycles):
        adsk.doEvents()


def all_bodies(occurrence):
    out = [occurrence.bRepBodies.item(i) for i in range(occurrence.bRepBodies.count)]
    for i in range(occurrence.childOccurrences.count):
        out.extend(all_bodies(occurrence.childOccurrences.item(i)))
    return out


def point_to_segment_distance(p, a, b):
    ab = vec(b.x - a.x, b.y - a.y, b.z - a.z)
    ap = vec(p.x - a.x, p.y - a.y, p.z - a.z)
    denom = ab.dotProduct(ab)
    if denom < 1e-12:
        return ap.length
    t = max(0.0, min(1.0, ap.dotProduct(ab) / denom))
    closest = adsk.core.Point3D.create(a.x + ab.x * t, a.y + ab.y * t, a.z + ab.z * t)
    return p.distanceTo(closest)


def span_along(points, direction):
    values = [p.x * direction.x + p.y * direction.y + p.z * direction.z for p in points]
    return (min(values), max(values)) if values else None


def box_span(box, direction):
    """How far a bounding box reaches along `direction`, as (low, high).

    The extreme of a box along any direction is one of its corners, and for an
    axis-aligned box each axis contributes independently — so this is the sum of
    the per-axis extremes rather than eight dot products."""
    low = high = 0.0
    for lo, hi, d in ((box.minPoint.x, box.maxPoint.x, direction.x),
                      (box.minPoint.y, box.maxPoint.y, direction.y),
                      (box.minPoint.z, box.maxPoint.z, direction.z)):
        a, b = lo * d, hi * d
        low += min(a, b)
        high += max(a, b)
    return low, high


# ---------------------------------------------------------------------------
# The handle library
# ---------------------------------------------------------------------------
def _hardware_project():
    target = config.HARDWARE_PROJECT_NAME.strip().lower()
    try:
        hubs = app.data.dataHubs
        for h in range(hubs.count):
            projects = hubs.item(h).dataProjects
            for p in range(projects.count):
                if projects.item(p).name.strip().lower() == target:
                    return projects.item(p)
    except Exception:
        futil.handle_error('Fit Handles: reading the hardware project')
    return None


def _handle_files():
    """[(label, DataFile)] from the project's Handles folder, sorted by name."""
    project = _hardware_project()
    if project is None:
        return []
    try:
        folders = project.rootFolder.dataFolders
        by_name = {}
        for i in range(folders.count):
            by_name.setdefault(folders.item(i).name, folders.item(i))
        chosen = config_table.pick_name(list(by_name), HANDLE_FOLDER_NAMES)
        if chosen is None:
            return []
        folder = by_name[chosen]
        files = folder.dataFiles
        rows = []
        for i in range(files.count):
            data_file = files.item(i)
            rows.append((data_file.name, data_file))
        rows.sort(key=lambda row: row[0].lower())
        return rows
    except Exception:
        futil.handle_error('Fit Handles: listing the Handles folder')
        return []


# ---------------------------------------------------------------------------
# Reading a cabinet's Handles sketch, in ASSEMBLY coordinates
# ---------------------------------------------------------------------------
def _sketch_in_context(occurrence):
    """The occurrence's own "Handles" sketch, as a proxy in the assembly.

    createForAssemblyContext is the whole trick: the component's sketch is in the
    CABINET's coordinates, and a kitchen has the same cabinet component placed in
    several different spots. Reached through the occurrence, every point's
    worldGeometry comes back where that particular cabinet actually stands.

    Which sketch is the handles sketch is settled by HANDLE_SKETCH_NAMES rather
    than by one exact name: the whole component is offered up at once so the
    preferred name always wins where it exists, and an alias is only reached for
    when it does not."""
    try:
        sketches = occurrence.component.sketches
    except Exception:
        return None
    by_name = {}
    for i in range(sketches.count):
        sketch = sketches.item(i)
        try:
            by_name.setdefault(sketch.name, sketch)
        except Exception:
            continue
    chosen = config_table.pick_name(list(by_name), HANDLE_SKETCH_NAMES)
    if chosen is None:
        return None
    sketch = by_name[chosen]
    try:
        return sketch.createForAssemblyContext(occurrence)
    except Exception:
        return sketch


def _cabinets_with_sketches(root):
    """[(occurrence, sketch proxy)] — every cabinet carrying a Handles sketch."""
    found = []

    def walk(occurrence, depth=0):
        sketch = _sketch_in_context(occurrence)
        if sketch is not None:
            found.append((occurrence, sketch))
        if depth < 3:
            try:
                children = occurrence.childOccurrences
                for i in range(children.count):
                    walk(children.item(i), depth + 1)
            except Exception:
                pass

    for i in range(root.occurrences.count):
        walk(root.occurrences.item(i))
    return found


def sketch_curves(sketch):
    return [sketch.sketchCurves.item(i) for i in range(sketch.sketchCurves.count)]


def curve_ends(curve):
    out = []
    for attribute in ('startSketchPoint', 'endSketchPoint'):
        try:
            p = getattr(curve, attribute)
        except Exception:
            p = None
        if p is not None:
            out.append(p)
    return out


def curve_length(curve):
    try:
        evaluator = curve.worldGeometry.evaluator
        ok, low, high = evaluator.getParameterExtents()
        if ok:
            ok, length = evaluator.getLengthAtParameter(low, high)
            if ok:
                return length
    except Exception:
        pass
    ends = curve_ends(curve)
    if len(ends) == 2:
        return ends[0].worldGeometry.distanceTo(ends[1].worldGeometry)
    return 0.0


def distance_to_curve(point, curve):
    line = adsk.fusion.SketchLine.cast(curve)
    if line:
        return point_to_segment_distance(point, line.startSketchPoint.worldGeometry,
                                         line.endSketchPoint.worldGeometry)
    try:
        evaluator = curve.worldGeometry.evaluator
        ok, low, high = evaluator.getParameterExtents()
        if not ok:
            return 1.0e9
        closest = 1.0e9
        for i in range(49):
            ok, p = evaluator.getPointAtParameter(low + (high - low) * i / 48.0)
            if ok:
                closest = min(closest, point.distanceTo(p))
        return closest
    except Exception:
        return 1.0e9


def curve_direction(curve):
    ends = curve_ends(curve)
    if len(ends) != 2:
        return None
    a, b = ends[0].worldGeometry, ends[1].worldGeometry
    return vec(b.x - a.x, b.y - a.y, b.z - a.z)


def find_locator_points(sketch):
    """([(point, own curves)], reference curves, note) — the markers in one sketch.

    Only DRAWN geometry can be a marker, and of those only a vertex touching nothing
    else. Covers both conventions in the library without configuring anything: a
    line offset in from an edge (the free end is the point) and an 80x80 corner
    square (the corner not lying on an edge)."""
    curves = sketch_curves(sketch)
    reference = [c for c in curves if c.isReference]
    drawn = [c for c in curves if not c.isReference]
    if not drawn:
        return [], reference, 'holds nothing but projected geometry'

    owners = {}
    for curve in drawn:
        for p in curve_ends(curve):
            entry = owners.setdefault(p.entityToken, [p, []])
            entry[1].append(curve)

    found = []
    for point, own in owners.values():
        w = point.worldGeometry
        if any(distance_to_curve(w, curve) < POINT_TOL
               for curve in curves if not any(curve is o for o in own)):
            continue
        found.append((point, own))

    note = '' if found else 'no free drawn vertex — nothing to hang a handle off'
    return found, reference, note


def sketch_normal(sketch):
    """The Handles sketch's own plane normal, in assembly coordinates."""
    return unit(sketch.transform.getAsCoordinateSystem()[3])


def _in_sketch_plane(face, normal, at_plane):
    """Is this face a planar face lying in the sketch's own plane?"""
    plane = adsk.core.Plane.cast(face.geometry)
    if plane is None:
        return False
    if abs(abs(plane.normal.dotProduct(normal)) - 1.0) > 0.001:
        return False
    origin = plane.origin
    here = origin.x * normal.x + origin.y * normal.y + origin.z * normal.z
    return abs(here - at_plane) <= FLAT_TOL


def front_face(root, occurrence, sketch, marker):
    """The cabinet face the Handles sketch was drawn on, or None.

    Three ways of asking, cheapest first, because this runs once per cabinet and a
    kitchen has a lot of cabinets:

      1. the sketch's own reference plane — instant, and right most of the time;
      2. failing that (on a linked cabinet the call sometimes throws an
         InternalValidationError deep inside Fusion), ask Fusion what BRep sits at
         the marker. The marker lies ON the front, so the front is in the answer;
      3. failing even that, the exhaustive search: every planar face of every body
         in the cabinet that lies in the sketch's plane.

    The last one is what this used to do always, and on a cabinet of 358 bodies it
    cost well over a second — most of it spent reading bounding boxes one at a
    time. It is now the rare case rather than the rule."""
    try:
        face = adsk.fusion.BRepFace.cast(sketch.referencePlane)
        if face is not None:
            return face
    except Exception:
        pass

    normal = sketch_normal(sketch)
    origin = sketch.transform.getAsCoordinateSystem()[0]
    at_plane = origin.x * normal.x + origin.y * normal.y + origin.z * normal.z

    try:
        hits = root.findBRepUsingPoint(
            marker, adsk.fusion.BRepEntityTypes.BRepFaceEntityType, 0.05, False)
        best, best_area = None, -1.0
        for i in range(hits.count):
            face = hits.item(i)
            if _in_sketch_plane(face, normal, at_plane) and face.area > best_area:
                best_area, best = face.area, face
        if best is not None:
            return best
    except Exception:
        pass

    best, best_area = None, -1.0
    for body in all_bodies(occurrence):
        # A cabinet can hold several hundred bodies and only a handful of them
        # come anywhere near the front. Rejecting a body by its bounding box is
        # cheaper than opening its face list.
        try:
            span = box_span(body.boundingBox, normal)
        except Exception:
            continue
        if span[0] - FLAT_TOL > at_plane or span[1] + FLAT_TOL < at_plane:
            continue
        try:
            faces = body.faces
        except Exception:
            continue
        for i in range(faces.count):
            face = faces.item(i)
            if _in_sketch_plane(face, normal, at_plane) and face.area > best_area:
                best_area, best = face.area, face
    return best


def _inside_solid(bodies, point):
    inside = adsk.fusion.PointContainment.PointInsidePointContainment
    for body in bodies:
        # A bounding-box rejection costs almost nothing; a pointContainment call
        # on every body in a cabinet costs real time.
        try:
            if not body.boundingBox.contains(point):
                continue
            if body.pointContainment(point) == inside:
                return True
        except Exception:
            continue
    return False


def out_direction(occurrence, sketch, target, face=None):
    """Which way the handle must point — out of the cabinet, not into it.

    The sketch's plane gives the axis; which END of it is "out" is settled by
    asking the solid: step a few millimetres off the marker each way and see which
    side has cabinet in it. The handle goes on the empty side. When the front face
    is known, only its own body needs asking, which is two calls rather than a walk
    of every body in the cabinet.

    This replaces guessing from the cabinet's bounding-box centre, which is what
    put handles on the inside — a marker near the middle of a tall unit, or on a
    door that sits proud of the carcass, gives that heuristic almost nothing."""
    normal = sketch_normal(sketch)

    probe = 0.5     # cm off the face — clear of the surface, inside any panel
    ahead = adsk.core.Point3D.create(target.x + normal.x * probe,
                                     target.y + normal.y * probe,
                                     target.z + normal.z * probe)
    behind = adsk.core.Point3D.create(target.x - normal.x * probe,
                                      target.y - normal.y * probe,
                                      target.z - normal.z * probe)
    try:
        bodies = [face.body] if face is not None else all_bodies(occurrence)
        solid_ahead = _inside_solid(bodies, ahead)
        solid_behind = _inside_solid(bodies, behind)
        if solid_ahead == solid_behind and face is not None:
            bodies = all_bodies(occurrence)      # the face's own body was no help
            solid_ahead = _inside_solid(bodies, ahead)
            solid_behind = _inside_solid(bodies, behind)
        if solid_ahead != solid_behind:
            return scaled(normal, -1.0) if solid_ahead else normal
    except Exception:
        pass

    # Nothing conclusive (marker off the panel, or an open frame): fall back to
    # pointing away from the middle of the cabinet.
    box = occurrence.boundingBox
    centre = adsk.core.Point3D.create(
        (box.minPoint.x + box.maxPoint.x) / 2.0,
        (box.minPoint.y + box.maxPoint.y) / 2.0,
        (box.minPoint.z + box.maxPoint.z) / 2.0)
    away = vec(target.x - centre.x, target.y - centre.y, target.z - centre.z)
    if away.length > 1e-6 and away.dotProduct(normal) < 0:
        normal.scaleBy(-1.0)
    return normal


def plane_directions(outward):
    up = project(vec(0.0, 0.0, 1.0), outward)
    if up.length < 1e-6:
        up = project(vec(1.0, 0.0, 0.0), outward)
    up.normalize()
    across = outward.crossProduct(up)
    across.normalize()
    return up, across


def order_points(found, up, across):
    def key(item):
        w = item[0].worldGeometry
        v = vec(w.x, w.y, w.z)
        return (-v.dotProduct(up), v.dotProduct(across))
    return sorted(found, key=key)


def outline_edge(point, own_curves, reference_curves):
    """The longest projected edge this marker springs from."""
    best, best_length = None, -1.0
    for curve in own_curves:
        for end in curve_ends(curve):
            if end.entityToken == point.entityToken:
                continue
            w = end.worldGeometry
            for reference in reference_curves:
                if distance_to_curve(w, reference) < POINT_TOL:
                    length = curve_length(reference)
                    if length > best_length:
                        best, best_length = reference, length
    return best


def marker_direction(point, own_curves, outward):
    curve = max(own_curves, key=curve_length)
    tail = None
    for end in curve_ends(curve):
        if end.entityToken != point.entityToken:
            tail = end.worldGeometry
    if tail is None:
        return None
    head = point.worldGeometry
    along = project(vec(head.x - tail.x, head.y - tail.y, head.z - tail.z), outward)
    if along.length < 1e-6:
        return None
    along.normalize()
    return along


def handle_direction(point, own_curves, reference_curves, outward, up, across):
    """Along the edge of the front the marker springs from; upright if vertical."""
    wanted = None
    edge = outline_edge(point, own_curves, reference_curves)
    if edge is not None:
        along = project(curve_direction(edge) or vec(0, 0, 0), outward)
        if along.length > 1e-6:
            along.normalize()
            wanted = along
    if wanted is None:
        along = marker_direction(point, own_curves, outward)
        if along is None:
            wanted = up.copy()
        else:
            wanted = outward.crossProduct(along)
            wanted.normalize()
    if wanted.dotProduct(up) < -0.001:
        wanted.scaleBy(-1.0)
    if HANDLE_PROFILE_FLIP:
        wanted.scaleBy(-1.0)
    return wanted


def outline_points(curves):
    out = []
    for curve in curves:
        sampled = False
        try:
            evaluator = curve.worldGeometry.evaluator
            ok, low, high = evaluator.getParameterExtents()
            if ok:
                for i in range(9):
                    ok, p = evaluator.getPointAtParameter(low + (high - low) * i / 8.0)
                    if ok:
                        out.append(p)
                sampled = True
        except Exception:
            pass
        if not sampled:
            out.extend(end.worldGeometry for end in curve_ends(curve))
    return out


def curve_world_ends(curve):
    return [e.worldGeometry for e in curve_ends(curve)]


def touching_reference(point, own_curves, reference_curves):
    out = []
    for curve in own_curves:
        for end in curve_ends(curve):
            if end.entityToken == point.entityToken:
                continue
            w = end.worldGeometry
            for reference in reference_curves:
                if any(reference is r for r in out):
                    continue
                if distance_to_curve(w, reference) < POINT_TOL:
                    out.append(reference)
    return out


def reference_loop(seeds, reference_curves):
    """The one outline the marker's edges belong to.

    Rebuilt by walking edge to edge wherever their ends meet. The 2 mm gap the
    library leaves between adjacent fronts is what stops one front's loop bleeding
    into the next — which is why the extent is measured per front, so a handle on an
    upper door can never be slid down into the gap beneath it."""
    loop = list(seeds)
    growing = True
    while growing:
        growing = False
        for candidate in reference_curves:
            if any(candidate is c for c in loop):
                continue
            ends = curve_world_ends(candidate)
            if not ends:
                continue
            for member in loop:
                if any(a.distanceTo(b) < POINT_TOL
                       for a in ends for b in curve_world_ends(member)):
                    loop.append(candidate)
                    growing = True
                    break
    return loop


def front_extent(point, own_curves, reference_curves, wanted):
    seeds = touching_reference(point, own_curves, reference_curves)
    if not seeds:
        return None
    return span_along(outline_points(reference_loop(seeds, reference_curves)), wanted)


def occurrence_span(occurrence, direction):
    return box_span(occurrence.boundingBox, direction)


class HandleProfile:
    """Everything about a handle MODEL, measured once and reused for every copy.

    All of it is local to the handle's own coordinates, so it is identical for
    every occurrence of the component — which is what lets a whole kitchen be
    fitted from a single measurement."""

    __slots__ = ('long_axis', 'depth_axis', 'body_sign',
                 'body_index', 'face_index', 'half', 'delta')

    def __init__(self, long_axis, depth_axis, body_sign, body_index, face_index,
                 half, delta):
        self.long_axis = long_axis
        self.depth_axis = depth_axis
        self.body_sign = body_sign
        self.body_index = body_index      # which body carries the mounting face
        self.face_index = face_index      # and which face of it
        self.half = half                  # half the handle's length
        # How far the handle's CENTRE sits from its mounting face's centre point,
        # along its own length. The joint lands the mounting face's centre on the
        # marker, so the handle ends up this far past it — and knowing that in
        # advance is what removes the measure-and-correct loop.
        self.delta = delta


def mount_face_for(occurrence, profile):
    """This copy's own mounting face, by the position recorded in the profile."""
    bodies = all_bodies(occurrence)
    if profile.body_index >= len(bodies):
        return None
    faces = bodies[profile.body_index].faces
    if profile.face_index >= faces.count:
        return None
    return faces.item(profile.face_index)


def measure_handle(occurrence):
    """(long axis, depth axis, body sign, mounting face) while it sits at the origin.

    Measured rather than assumed: every handle in this library happens to be
    modelled with its length on local X and its mounting plane at Y = 0, but a
    library grows and one that isn't would silently land sideways."""
    box = occurrence.boundingBox
    lo = [box.minPoint.x, box.minPoint.y, box.minPoint.z]
    hi = [box.maxPoint.x, box.maxPoint.y, box.maxPoint.z]
    size = [hi[i] - lo[i] for i in range(3)]

    long_axis = size.index(max(size))
    others = [i for i in range(3) if i != long_axis]

    depth_axis = None
    for i in others:
        if abs(lo[i]) < FLAT_TOL or abs(hi[i]) < FLAT_TOL:
            depth_axis = i
            break
    if depth_axis is None:
        depth_axis = max(others, key=lambda i: size[i])
    body_sign = -1.0 if abs(hi[depth_axis]) < abs(lo[depth_axis]) else 1.0

    mount_face, best_area = None, -1.0
    for body in all_bodies(occurrence):
        for i in range(body.faces.count):
            face = body.faces.item(i)
            if adsk.core.Plane.cast(face.geometry) is None:
                continue
            fb = face.boundingBox
            flo = [fb.minPoint.x, fb.minPoint.y, fb.minPoint.z][depth_axis]
            fhi = [fb.maxPoint.x, fb.maxPoint.y, fb.maxPoint.z][depth_axis]
            if abs(flo) < FLAT_TOL and abs(fhi) < FLAT_TOL and face.area > best_area:
                best_area, mount_face = face.area, face
    if mount_face is None:
        return None
    return long_axis, depth_axis, body_sign, mount_face


def profile_handle(occurrence):
    """Measure a handle model once. Returns a HandleProfile, or None."""
    measured = measure_handle(occurrence)
    if measured is None:
        return None
    long_axis, depth_axis, body_sign, mount_face = measured

    body_index, face_index = -1, -1
    for bi, body in enumerate(all_bodies(occurrence)):
        for fi in range(body.faces.count):
            if body.faces.item(fi) == mount_face:
                body_index, face_index = bi, fi
                break
        if body_index >= 0:
            break
    if body_index < 0:
        return None

    box = occurrence.boundingBox
    lo = [box.minPoint.x, box.minPoint.y, box.minPoint.z][long_axis]
    hi = [box.maxPoint.x, box.maxPoint.y, box.maxPoint.z][long_axis]
    fb = mount_face.boundingBox
    flo = [fb.minPoint.x, fb.minPoint.y, fb.minPoint.z][long_axis]
    fhi = [fb.maxPoint.x, fb.maxPoint.y, fb.maxPoint.z][long_axis]

    half = (hi - lo) / 2.0
    delta = (lo + hi) / 2.0 - (flo + fhi) / 2.0
    return HandleProfile(long_axis, depth_axis, body_sign,
                         body_index, face_index, half, delta)


# ---------------------------------------------------------------------------
# Jointing
# ---------------------------------------------------------------------------
# The original script's method, unchanged in substance: drop a joint origin on the
# marker, join the handle's mounting face to it with a rigid joint, then turn and
# slide the handle with the joint's own flip / angle / offset parameters. Nothing is
# positioned by transform, so nothing has to be captured and nothing can be lost.
#
# The one adaptation a kitchen forces: the cabinets are LINKED, so their sketch
# points are read-only and Fusion refuses to build a joint origin on one. So each
# cabinet gets a native anchor of our own — a construction plane offset zero from
# the face the Handles sketch was drawn on, and a native sketch on that plane with
# one point per marker. Because the plane comes from the cabinet's own front face,
# the joint origin's primary axis lands on the front's normal, which is precisely
# the orientation the script gets for free from the sketch's plane. (A bare
# construction point carries no orientation at all — that is what laid every handle
# flat in the previous attempt.)


def native_anchor(root, occurrence, face):
    """A native sketch lying on this cabinet's front face, or None.

    One per cabinet: every marker on that cabinet becomes a point in it. The sketch
    is added straight onto the cabinet's own front face — a linked face is read-only
    but perfectly good to sketch on, and the sketch itself belongs to the kitchen,
    so its points can carry joint origins where the cabinet's own cannot. Sketching
    on the face directly rather than on a construction plane offset from it saves a
    feature per cabinet and, more to the point, one less thing to clean up later."""
    if face is None:
        return None
    try:
        anchor = root.sketches.add(face)
    except Exception as exc:
        futil.log(f'Fit Handles: no anchor on {occurrence.name}: {exc}')
        return None
    try:
        anchor.name = f'Handle anchors - {occurrence.name}'
        anchor.isLightBulbOn = False
    except Exception:
        pass
    wc_attrs.set_value(anchor, WC_HANDLE_TAG, 'anchor')
    return anchor


def anchor_point(anchor, world_point):
    """A native sketch point on the anchor sketch, coincident with the marker."""
    local = anchor.modelToSketchSpace(world_point)
    return anchor.sketchPoints.add(local)


def edit_joint_origin(design, origin, offsets):
    """offsets is a 3-tuple of absolute values for offsetX / offsetY / offsetZ."""
    rolled = False
    try:
        origin.timelineObject.rollTo(True)
        rolled = True
    except Exception:
        pass
    wait(20)
    origin.offsetX.value = offsets[0]
    origin.offsetY.value = offsets[1]
    origin.offsetZ.value = offsets[2]
    if rolled:
        design.timeline.moveToEnd()
    wait(60)


class Calibration:
    """How Fusion orients THIS handle model on an anchor plane.

    Two unknowns, and neither can be reasoned out from the API's documentation
    with any confidence: whether the joint has to be flipped for the handle to
    stand out of the front rather than sink into it, and which way the handle's
    length points once jointed at angle zero. Both are settled ONCE, by building a
    probe joint and measuring it — after which every remaining handle in the
    kitchen can be created already correct, with no timeline edits at all.

    The reference direction is stored in the anchor plane's OWN basis, not in world
    coordinates, so a calibration taken on one cabinet transfers to a cabinet
    facing a completely different way."""

    __slots__ = ('flip', 'ref_secondary', 'ref_third')

    def __init__(self, flip, ref_secondary, ref_third):
        self.flip = flip
        self.ref_secondary = ref_secondary
        self.ref_third = ref_third

    def reference(self, axes):
        """The angle-zero direction of the handle's length, on this anchor."""
        secondary, third, _primary = axes
        return unit(vec(
            secondary.x * self.ref_secondary + third.x * self.ref_third,
            secondary.y * self.ref_secondary + third.y * self.ref_third,
            secondary.z * self.ref_secondary + third.z * self.ref_third))


def anchor_axes(root, anchor):
    """(secondary, third, primary) for joint origins on this anchor sketch.

    A joint origin takes its orientation from the plane its point sits on, so
    every origin on one anchor shares these. Read once per cabinet off a throwaway
    origin on the sketch's own origin point — cheaper than creating each real
    origin twice just to see which way its offsets will run."""
    probe = root.jointOrigins.add(root.jointOrigins.createInput(
        adsk.fusion.JointGeometry.createByPoint(anchor.originPoint)))
    axes = (probe.secondaryAxisVector.copy(),
            probe.thirdAxisVector.copy(),
            probe.primaryAxisVector.copy())
    probe.deleteMe()
    return axes


def _offset_values(axes, displacement):
    """offsetX / offsetY / offsetZ that shift a joint origin by `displacement`.

    offsetX runs along the secondary axis, offsetY along the third and offsetZ
    along the primary one — measured on a live joint, not assumed."""
    secondary, third, primary = axes
    return (adsk.core.ValueInput.createByReal(displacement.dotProduct(secondary)),
            adsk.core.ValueInput.createByReal(displacement.dotProduct(third)),
            adsk.core.ValueInput.createByReal(displacement.dotProduct(primary)))


def build_joint(root, occurrence, profile, marker_point, axes, name,
                flipped=False, angle=0.0, displacement=None):
    """Create a joint origin and a rigid joint, already in their final state.

    The whole speed story is here. `JointInput` carries isFlipped and angle, and
    `JointOriginInput` carries offsetX/Y/Z, so a handle can be created turned and
    slid exactly where it belongs. Setting any of those AFTER the fact means
    rolling the timeline back onto the feature and forward again — about a third
    of a second each, several times per handle, on top of a full recompute of
    everything downstream."""
    origin_input = root.jointOrigins.createInput(
        adsk.fusion.JointGeometry.createByPoint(marker_point))
    if displacement is not None:
        x, y, z = _offset_values(axes, displacement)
        origin_input.offsetX = x
        origin_input.offsetY = y
        origin_input.offsetZ = z
    joint_origin = root.jointOrigins.add(origin_input)
    try:
        joint_origin.name = name
        joint_origin.isLightBulbOn = False
    except Exception:
        pass
    wc_attrs.set_value(joint_origin, WC_HANDLE_TAG, name)

    face = mount_face_for(occurrence, profile)
    if face is None:
        joint_origin.deleteMe()
        raise RuntimeError('lost track of the handle mounting face')
    geometry = adsk.fusion.JointGeometry.createByPlanarFace(
        face, None, adsk.fusion.JointKeyPointTypes.CenterKeyPoint)
    joint_input = root.joints.createInput(geometry, joint_origin)
    joint_input.setAsRigidJointMotion()
    joint_input.isFlipped = flipped
    if abs(angle) > 1e-9:
        joint_input.angle = adsk.core.ValueInput.createByReal(angle)
    joint = root.joints.add(joint_input)
    try:
        joint.name = name
    except Exception:
        pass
    wc_attrs.set_value(joint, WC_HANDLE_TAG, name)
    return joint_origin, joint


def calibrate(root, occurrence, profile, marker_point, axes, outward):
    """Build a probe joint, learn flip and the angle-zero direction, tear it down."""
    body_local = basis(profile.depth_axis, profile.body_sign)
    joint_origin, joint = build_joint(root, occurrence, profile, marker_point,
                                      axes, 'Handle calibration')
    wait(20)
    flip = rotate_local(occurrence, body_local).dotProduct(outward) < 0
    if flip:
        joint.deleteMe()
        joint_origin.deleteMe()
        joint_origin, joint = build_joint(root, occurrence, profile, marker_point,
                                          axes, 'Handle calibration', flipped=True)
        wait(20)

    reference = rotate_local(occurrence, basis(profile.long_axis))
    secondary, third, _primary = axes
    joint.deleteMe()
    joint_origin.deleteMe()
    wait(20)
    return Calibration(flip, reference.dotProduct(secondary),
                       reference.dotProduct(third))


def aim(target_point, wanted, profile, limits):
    """Where the handle's centre should end up along `wanted`, and by how much it
    had to be pulled back off the edge of the front. No measurement needed: the
    handle's own length is known from its profile."""
    target = target_point.worldGeometry
    at_target = (target.x * wanted.x + target.y * wanted.y + target.z * wanted.z)
    clearance = HANDLE_EDGE_CLEARANCE_MM / 10.0

    desired, too_long = at_target, False
    if HANDLE_KEEP_INSIDE_FRONT and limits:
        inner_low = limits[0] + clearance + profile.half
        inner_high = limits[1] - clearance - profile.half
        if inner_low <= inner_high:
            desired = min(max(desired, inner_low), inner_high)
        else:
            too_long = True
    return at_target, desired, too_long


def fit_handle(root, occurrence, profile, marker_point, axes, calibration,
               wanted, outward, name, limits=None):
    """Place and joint one handle in a single shot. Returns (origin, joint, info).

    `info` is (problems, note, desired) — desired being where the centre was aimed,
    so the caller can check the result without recomputing anything."""
    at_target, desired, too_long = aim(marker_point, wanted, profile, limits)

    # Turn: the signed angle from where the handle's length would point at angle
    # zero round to where it is wanted, taken about the front's normal.
    reference = calibration.reference(axes)
    angle = math.atan2(reference.crossProduct(wanted).dotProduct(outward),
                       reference.dotProduct(wanted))

    # Slide: the joint puts the mounting face's centre on the marker, which leaves
    # the handle `delta` further along its own length. Correct for that and for the
    # pull-back in one go.
    slide = desired - (at_target + profile.delta)
    displacement = scaled(wanted, slide)

    joint_origin, joint = build_joint(root, occurrence, profile, marker_point, axes,
                                      name, flipped=calibration.flip, angle=angle,
                                      displacement=displacement)
    wait(20)

    note = ''
    if not too_long and abs(desired - at_target) > 0.05:
        note = f'pulled back {abs(desired - at_target) * 10.0:.0f} mm to clear the edge'
    problems = ['too long to fit this front with a '
                f'{HANDLE_EDGE_CLEARANCE_MM:.0f} mm gap - left centred and '
                'overhanging'] if too_long else []
    return joint_origin, joint, (problems, note, desired)


def check_fit(occurrence, profile, wanted, outward, desired):
    """What is wrong with a placed handle, measured off the result. '' if nothing.

    Run on the first handle of every cabinet. The fast path above predicts the
    outcome rather than measuring it, and a prediction that is never checked is a
    guess — this is what turns it back into a measurement."""
    problems = []
    if rotate_local(occurrence, basis(profile.long_axis)).dotProduct(wanted) < 0.99:
        problems.append('not lined up with its point')
    if rotate_local(occurrence, basis(profile.depth_axis, profile.body_sign)) \
            .dotProduct(outward) < 0.99:
        problems.append('facing the wrong way')
    span = occurrence_span(occurrence, wanted)
    if span and abs((span[0] + span[1]) / 2.0 - desired) > 0.05:
        problems.append('not centred on its point')
    return ', '.join(problems)


def refine(design, occurrence, joint_origin, axes, wanted, desired):
    """Slow fallback: measure the handle and nudge the joint origin until it sits
    where it should. Only used when check_fit rejects a cabinet's first handle —
    every timeline edit in here costs a full recompute of the design."""
    offsets = [0.0, 0.0, 0.0]
    for _ in range(3):
        span = occurrence_span(occurrence, wanted)
        if not span:
            return
        residual = (span[0] + span[1]) / 2.0 - desired
        if abs(residual) < 0.005:
            return
        shift = scaled(wanted, -residual)
        offsets = [offsets[i] + shift.dotProduct(axes[i]) for i in range(3)]
        edit_joint_origin(design, joint_origin,
                          (offsets[0], offsets[1], offsets[2]))


# ---------------------------------------------------------------------------
# Removing what is already there
# ---------------------------------------------------------------------------
def _remove_placed(root):
    """Delete every handle this command placed, with its joints and anchors.

    Found by the attribute stamped at placement, not by name: a kitchen legitimately
    contains other hardware, and a name match would break the moment a handle file
    is renamed in the library.

    Collected first and deleted in ONE call. deleteEntities takes the whole set at
    once and works out the dependencies itself, where deleting item by item makes
    Fusion recompute after every single one — the difference is seconds on a
    kitchen's worth of handles."""
    doomed = adsk.core.ObjectCollection.create()
    handles = 0

    # asBuiltJoints and constructionPoints are swept purely to clear anything an
    # earlier version of this command left behind.
    for collection in (root.asBuiltJoints, root.joints, root.jointOrigins,
                       root.sketches, root.constructionPlanes,
                       root.constructionPoints):
        for i in range(collection.count):
            item = collection.item(i)
            try:
                if wc_attrs.get_value(item, WC_HANDLE_TAG):
                    doomed.add(item)
            except Exception:
                continue

    for i in range(root.occurrences.count):
        occurrence = root.occurrences.item(i)
        try:
            tagged = (wc_attrs.get_value(occurrence, WC_HANDLE_TAG)
                      or wc_attrs.get_value(occurrence.component, WC_HANDLE_TAG))
        except Exception:
            tagged = None
        if tagged:
            doomed.add(occurrence)
            handles += 1

    if doomed.count == 0:
        return 0
    design = adsk.fusion.Design.cast(app.activeProduct)
    try:
        design.deleteEntities(doomed)
        return handles
    except Exception:
        futil.log('Fit Handles: batch delete failed, falling back to one at a time')

    handles = 0
    for i in range(doomed.count - 1, -1, -1):
        item = doomed.item(i)
        try:
            is_occurrence = adsk.fusion.Occurrence.cast(item) is not None
            item.deleteMe()
            if is_occurrence:
                handles += 1
        except Exception:
            continue
    return handles


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------
def command_created(args: adsk.core.CommandCreatedEventArgs):
    futil.log(f'{CMD_NAME} Command Created Event')
    inputs = args.command.commandInputs

    global _files
    _files = _handle_files()

    dropdown = inputs.addDropDownCommandInput(
        HANDLE_ID, 'Handle', adsk.core.DropDownStyles.TextListDropDownStyle)
    if _files:
        for i, (label, _data_file) in enumerate(_files):
            dropdown.listItems.add(label, i == 0)
    else:
        dropdown.listItems.add('No handles found', True)
    dropdown.tooltip = (f'From the "{HANDLE_FOLDER_NAME}" folder of the '
                        f'"{config.HARDWARE_PROJECT_NAME}" project.')

    message = (f'Fits the chosen handle to every locator point in every cabinet\'s '
               f'"{HANDLE_SKETCH_NAME}" sketch. Handles already fitted by this '
               f'command are removed first, so this swaps rather than stacks.')
    if not _files:
        message = (f'No handles found. Check that the project '
                   f'"{config.HARDWARE_PROJECT_NAME}" has a "{HANDLE_FOLDER_NAME}" '
                   f'folder and that you are signed in.')
    info = inputs.addTextBoxCommandInput(INFO_ID, '', message, 3, True)
    info.isFullWidth = True

    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.validateInputs, command_validate_input, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)


def command_validate_input(args: adsk.core.ValidateInputsEventArgs):
    args.areInputsValid = bool(_files)


def command_execute(args: adsk.core.CommandEventArgs):
    """Record the choice and hand it to the custom event — see RUN_EVENT_ID."""
    futil.log(f'{CMD_NAME} Command Execute Event')
    global _pending
    if not adsk.fusion.Design.cast(app.activeProduct):
        ui.messageBox('Fit Handles needs an open design.')
        return

    dropdown = args.command.commandInputs.itemById(HANDLE_ID)
    item = dropdown.selectedItem if dropdown else None
    index = item.index if item else 0
    if not _files or index >= len(_files):
        ui.messageBox('No handle selected.')
        return

    _pending = _files[index]
    _arm_event()
    app.fireCustomEvent(RUN_EVENT_ID)


def _fit_handles(label, data_file):
    """Fit one handle to every marker in the assembly. Runs OUTSIDE the command.

    Built to take a whole kitchen rather than a cabinet. Three things make that
    bearable, and all three are about not repeating work:

      * the handle file is INSERTED once and copied thereafter — addByInsert costs
        about a second and a half every time, addExistingComponent about fifty
        milliseconds, and both give an occurrence of the same component;
      * the handle model is MEASURED once, into a HandleProfile whose numbers are
        local to the handle and therefore true of every copy;
      * the way Fusion orients this handle on an anchor is CALIBRATED once, so
        every joint after the first is created already flipped, turned and slid
        into place instead of being corrected afterwards. Correcting a joint means
        rolling the timeline onto it and back, which recomputes the design.

    Prediction replaces measurement in the inner loop, so the first handle on every
    cabinet is checked against the model; if the check fails, that cabinet falls
    back to measuring and nudging."""
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        return
    root = design.rootComponent
    started = time.perf_counter()

    removed = _remove_placed(root)
    wait(20)

    # A Gola profile is machined into the cabinet, not fitted to it, so the
    # choice of handle is first a choice of CONFIGURATION. Every base and tall
    # cabinet is switched to the matching Handles theme before anything is
    # placed — and when the choice IS Gola, that switch is the whole job.
    want_gola = configs.wants_gola(label)
    switched, unchanged, notes, problems = configs.apply_theme(
        app, root, want_gola, futil.log)

    # Activating the library documents to read their tables leaves these stale.
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        return
    root = design.rootComponent
    wait(20)

    if want_gola:
        _report_gola(label, switched, unchanged, removed, notes, problems, started)
        return

    cabinets = _cabinets_with_sketches(root)
    if not cabinets:
        ui.messageBox(f'No cabinet in this assembly has a sketch called '
                      f'"{HANDLE_SKETCH_NAME}" (or anything reading as it).\n\n'
                      f'{removed} previously fitted handle(s) were removed.')
        return

    identity = adsk.core.Matrix3D.create()
    # `problems` already carries anything the configuration switch reported —
    # keep it rather than starting a fresh list, or those get lost.
    placed, skipped, refined = 0, 0, 0
    profile = None                 # measured off the first copy, reused thereafter
    calibration = None             # learned once, checked on every cabinet
    template = None                # the component every later copy is made from

    for occurrence, sketch in cabinets:
        found, reference, note = find_locator_points(sketch)
        if not found:
            skipped += 1
            if note:
                futil.log(f'Fit Handles: {occurrence.name}: {note}')
            continue

        # The front face is found once and then does three jobs: it settles which
        # way is out, it hosts the anchor sketch, and its own body answers the
        # containment probe. Finding it again for each of those was costing more
        # than everything else this loop does.
        marker_world = found[0][0].worldGeometry
        face = front_face(root, occurrence, sketch, marker_world)
        outward = out_direction(occurrence, sketch, marker_world, face)
        up, across = plane_directions(outward)
        points = order_points(found, up, across)

        # One native anchor per cabinet. The cabinets are linked, so their own
        # sketch points cannot carry a joint origin; a sketch of ours on their
        # front face gives points that can, oriented the same way.
        anchor = native_anchor(root, occurrence, face)
        if anchor is None:
            skipped += 1
            problems.append(f'{occurrence.name}: could not build an anchor on the '
                            f'face the "{HANDLE_SKETCH_NAME}" sketch was drawn on')
            continue
        try:
            axes = anchor_axes(root, anchor)
        except Exception as exc:
            skipped += 1
            problems.append(f'{occurrence.name}: could not read the anchor ({exc})')
            continue

        first_on_cabinet = True
        for number, (target_point, own_curves) in enumerate(points, start=1):
            wanted = handle_direction(target_point, own_curves, reference,
                                      outward, up, across)
            limits = front_extent(target_point, own_curves, reference, wanted)
            where = f'{occurrence.name} point {number}'

            try:
                marker = anchor_point(anchor, target_point.worldGeometry)
            except Exception as exc:
                problems.append(f'{where}: could not anchor the marker ({exc})')
                continue

            try:
                handle_occ = _add_handle(root, data_file, template, identity)
            except Exception as exc:
                problems.append(f'{where}: could not insert the handle ({exc})')
                continue
            wait(20)
            wc_attrs.set_value(handle_occ, WC_HANDLE_TAG, label)
            if template is None:
                template = handle_occ.component
                wc_attrs.set_category(template, config.WC_CAT_HARDWARE)

            if profile is None:
                profile = profile_handle(handle_occ)
                if profile is None:
                    problems.append(f'{label} has no flat mounting face')
                    return _report(label, placed, len(cabinets) - skipped, removed,
                                   skipped, refined, problems, started)

            name = f'{label} - {occurrence.name} {number}'
            if calibration is None:
                try:
                    calibration = calibrate(root, handle_occ, profile, marker,
                                            axes, outward)
                except Exception as exc:
                    problems.append(f'{where}: could not calibrate ({exc})')
                    continue

            try:
                joint_origin, _joint, info = fit_handle(
                    root, handle_occ, profile, marker, axes, calibration,
                    wanted, outward, name, limits)
            except Exception as exc:
                problems.append(f'{where}: {exc}')
                continue

            trouble, note_text, desired = info[0], info[1], info[2]

            # The fast path predicts rather than measures, so check the prediction
            # once per cabinet. A cabinet whose front is modelled unusually falls
            # back to measuring and nudging, which is slow but always right.
            if first_on_cabinet:
                first_on_cabinet = False
                wrong = check_fit(handle_occ, profile, wanted, outward, desired)
                if wrong:
                    refine(design, handle_occ, joint_origin, axes, wanted, desired)
                    refined += 1
                    wrong = check_fit(handle_occ, profile, wanted, outward, desired)
                    if wrong:
                        trouble = list(trouble) + [wrong]

            placed += 1
            if trouble:
                problems.append(f'{where}: ' + ', '.join(trouble))
            elif note_text:
                futil.log(f'Fit Handles: {where} {note_text}')

    _report(label, placed, len(cabinets) - skipped, removed, skipped, refined,
            problems, started, switched, unchanged, notes)


def _add_handle(root, data_file, template, matrix):
    """Insert the handle file, or copy the one already inserted.

    addByInsert has to reach out to the library file and takes well over a second
    every time; every handle after the first is an occurrence of the component that
    first insert brought in, which costs about fifty milliseconds. Both are linked
    occurrences of the same component, so there is no difference in the result."""
    if template is not None:
        return root.occurrences.addExistingComponent(template, matrix)
    return root.occurrences.addByInsert(data_file, matrix, True)


def _report_gola(label, switched, unchanged, removed, notes, problems, started):
    """Gola is a configuration, not a part — so there is nothing to place."""
    lines = [f'"{label}" is a Gola profile, so it was configured into the '
             f'cabinets rather than placed as hardware.',
             '',
             f'{switched} cabinet(s) switched to "{configs.GOLA_VALUE}" '
             f'in {time.perf_counter() - started:.0f} s.']
    if unchanged:
        lines.append(f'{unchanged} were already configured for it.')
    if removed:
        lines.append(f'{removed} separately fitted handle(s) were removed.')
    lines.extend(_extra(notes, problems))
    ui.messageBox('\n'.join(lines), CMD_NAME)


def _extra(notes, problems):
    lines = []
    for note in notes:
        lines.append(note)
    if problems:
        lines.append('')
        lines.append('Problems:')
        lines.extend(f'  {p}' for p in problems[:10])
        if len(problems) > 10:
            lines.append(f'  … and {len(problems) - 10} more '
                         f'(see the Text Command window)')
    return lines


def _report(label, placed, cabinets, removed, skipped, refined, problems, started,
            switched=0, unchanged=0, notes=()):
    lines = [f'{placed} × "{label}" fitted across {cabinets} cabinet(s) '
             f'in {time.perf_counter() - started:.0f} s.']
    if switched:
        lines.append(f'{switched} cabinet(s) switched to '
                     f'"{configs.OTHER_VALUE}" first.')
    if unchanged:
        lines.append(f'{unchanged} were already configured for it.')
    for note in notes:
        lines.append(note)
    if removed:
        lines.append(f'{removed} previously fitted handle(s) removed first.')
    if skipped:
        lines.append(f'{skipped} cabinet(s) had a "{HANDLE_SKETCH_NAME}" sketch with '
                     f'no usable marker and were skipped.')
    if refined:
        lines.append(f'{refined} cabinet(s) needed the slow measured fit.')
    if problems:
        lines.append('')
        lines.append('Problems:')
        lines.extend(f'  {p}' for p in problems[:10])
        if len(problems) > 10:
            lines.append(f'  … and {len(problems) - 10} more (see the Text Command window)')
    ui.messageBox('\n'.join(lines), CMD_NAME)


def command_destroy(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Destroy Event')
    global local_handlers
    local_handlers = []
