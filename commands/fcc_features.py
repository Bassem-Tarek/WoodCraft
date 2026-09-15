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

"""Fusion B-Rep -> flat panel features, for the FCC export.

Reads a panel component and answers the three questions the machine asks that
Fusion has no word for:

  * which way is the panel lying    -> `panel_frame`
  * where are the holes, and into which face do they go  -> `holes`
  * where are the grooves           -> `slots`

Everything comes back in the panel's own frame, in millimetres:
`u` along its length, `v` along its width, `w` up through its thickness with
0 at Face 6 (bottom) and T at Face 5 (top). `commands/fcc_writer.py` turns that
into sheet coordinates once the nester has placed the part.

The frame is the component-space bounding box of the component's own visible
bodies, so it is independent of how the cabinet is oriented in the assembly — a
side panel standing on edge in the kitchen reads exactly like the same panel
lying flat, which is what a flat-panel machine wants. It assumes the panel is
modelled axis-aligned in its OWN component (true of anything Carcass Maker,
Shelf Creator or Convert Panel produced); a panel modelled at an angle inside
its component is reported by `frame_is_reliable` so the caller can warn rather
than emit silently wrong coordinates.
"""

import math

import adsk.core
import adsk.fusion

try:
    from . import wc_attrs
except ImportError:     # imported standalone (tests, probe scripts)
    wc_attrs = None


WC_GROUP = 'WoodCraft'
WC_EDGEBAND = 'edgeband'

MM = 10.0           # Fusion works in cm; the machine file is millimetres
EPS_MM = 0.05       # coincidence tolerance for "a hole opens on this face"
AXIS_TOL = 1e-3     # how parallel an axis must be to count as aligned
MIN_GROOVE_RATIO = 1.8   # a groove floor is at least this much longer than wide

FACE_YMAX, FACE_YMIN, FACE_XMAX, FACE_XMIN, FACE_TOP, FACE_BOTTOM = 1, 2, 3, 4, 5, 6


def face_band_name(face):
    """The edgeband tagged on a face, or None. Goes through wc_attrs inside the
    add-in and reads the attribute directly when this module is imported on its
    own, so probes and tests need no package context."""
    if wc_attrs is not None:
        return wc_attrs.get_edgeband(face)
    try:
        attr = face.attributes.itemByName(WC_GROUP, WC_EDGEBAND)
        return attr.value if attr else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Frame
# ---------------------------------------------------------------------------
class PanelFrame:
    """The panel's own flat-part coordinate system.

    `axes` maps (u, v, w) onto component-space axis indices; `flip` reverses v
    when the permutation would otherwise be left-handed, which would mirror the
    panel and put every hole on the wrong side of it."""

    def __init__(self, lo, hi, axes, flip):
        self.lo, self.hi = lo, hi
        self.axes = axes                     # (u_axis, v_axis, w_axis)
        self.flip = flip
        self.length = (hi[axes[0]] - lo[axes[0]]) * MM
        self.width = (hi[axes[1]] - lo[axes[1]]) * MM
        self.thickness = (hi[axes[2]] - lo[axes[2]]) * MM

    def point(self, pt):
        """A component-space Point3D as (u, v, w) millimetres."""
        coords = (pt.x, pt.y, pt.z)
        au, av, aw = self.axes
        u = (coords[au] - self.lo[au]) * MM
        v = (coords[av] - self.lo[av]) * MM
        w = (coords[aw] - self.lo[aw]) * MM
        if self.flip:
            v = self.width - v
        return u, v, w

    def direction(self, vec):
        """A component-space Vector3D as (du, dv, dw), same handedness fix."""
        coords = (vec.x, vec.y, vec.z)
        au, av, aw = self.axes
        dv = coords[av]
        return coords[au], (-dv if self.flip else dv), coords[aw]

    def local_axis(self, vec):
        """'u', 'v', 'w' when `vec` is parallel to one frame axis, else None."""
        du, dv, dw = (abs(c) for c in self.direction(vec))
        biggest = max(du, dv, dw)
        if biggest <= 0:
            return None
        du, dv, dw = du / biggest, dv / biggest, dw / biggest
        if dv < AXIS_TOL and dw < AXIS_TOL:
            return 'u'
        if du < AXIS_TOL and dw < AXIS_TOL:
            return 'v'
        if du < AXIS_TOL and dv < AXIS_TOL:
            return 'w'
        return None


def _visible_body_box(component):
    """Component-space bounds of the component's own visible bodies, as
    ((minx, miny, minz), (maxx, maxy, maxz)) in cm, or None.

    Mirrors panels._own_bodies_ext_mm: hidden bodies and degenerate leftovers
    are skipped, exactly as Fusion's own Component.boundingBox does."""
    try:
        bodies = component.bRepBodies
    except Exception:
        return None
    lo = [float('inf')] * 3
    hi = [float('-inf')] * 3
    measured = 0
    for i in range(bodies.count):
        body = bodies.item(i)
        try:
            if not body.isVisible:
                continue
            box = body.boundingBox
            a, b = box.minPoint, box.maxPoint
            if max(b.x - a.x, b.y - a.y, b.z - a.z) < 1e-3:
                continue
            measured += 1
            for axis, (low, high) in enumerate(((a.x, b.x), (a.y, b.y), (a.z, b.z))):
                lo[axis] = min(lo[axis], low)
                hi[axis] = max(hi[axis], high)
        except Exception:
            continue
    if not measured:
        return None
    return tuple(lo), tuple(hi)


def panel_frame(component):
    """The panel's flat-part frame, or None when it has no measurable bodies.

    Thickness is the smallest extent, length the largest, width what's left."""
    box = _visible_body_box(component)
    if not box:
        return None
    lo, hi = box
    extents = [hi[a] - lo[a] for a in range(3)]
    order = sorted(range(3), key=lambda a: extents[a], reverse=True)
    u_axis, v_axis, w_axis = order[0], order[1], order[2]
    if extents[w_axis] <= 0 or extents[v_axis] <= 0:
        return None
    # A permutation of the component axes is right-handed only when it is an
    # even permutation of (x, y, z); flip v to fix an odd one.
    even = (u_axis, v_axis, w_axis) in ((0, 1, 2), (1, 2, 0), (2, 0, 1))
    return PanelFrame(lo, hi, (u_axis, v_axis, w_axis), flip=not even)


def frame_is_reliable(component, frame, tol_mm=0.2):
    """True when the panel really is axis-aligned in its own component — i.e.
    its two big faces are planar and square to the thickness axis.

    A panel modelled at an angle still produces a frame (its bounding box), but
    every feature coordinate inside it would be wrong, so the exporter warns
    instead of shipping it."""
    if not frame:
        return False
    try:
        bodies = component.bRepBodies
    except Exception:
        return False
    skins = 0
    for bi in range(bodies.count):
        body = bodies.item(bi)
        try:
            if not body.isVisible:
                continue
            faces = body.faces
        except Exception:
            continue
        for fi in range(faces.count):
            face = faces.item(fi)
            plane = adsk.core.Plane.cast(face.geometry)
            if not plane:
                continue
            if frame.local_axis(plane.normal) != 'w':
                continue
            _, _, w = frame.point(plane.origin)
            if w <= tol_mm or w >= frame.thickness - tol_mm:
                skins += 1
    return skins >= 2


# ---------------------------------------------------------------------------
# Holes
# ---------------------------------------------------------------------------
def _is_bore(face, cylinder):
    """True when a cylindrical face curves INWARD — a drilled hole rather than a
    rounded corner or a dowel. Convex ⇔ the normal points away from the axis."""
    try:
        point = face.pointOnFace
        ok, normal = face.evaluator.getNormalAtPoint(point)
        if not ok:
            return False
        radial = cylinder.origin.vectorTo(point)
        axis = cylinder.axis.copy()
        axis.normalize()
        along = axis.copy()
        along.scaleBy(radial.dotProduct(axis))
        radial.subtract(along)
        return radial.dotProduct(normal) < 0
    except Exception:
        return False


def _axial_span(face, frame, axis_name):
    """(min, max) of the face along the frame axis the hole is drilled on, mm."""
    try:
        box = face.boundingBox
    except Exception:
        return None
    lo = frame.point(box.minPoint)
    hi = frame.point(box.maxPoint)
    index = {'u': 0, 'v': 1, 'w': 2}[axis_name]
    return min(lo[index], hi[index]), max(lo[index], hi[index])


def _merge_spans(spans, gap=0.5):
    """Collapse overlapping/touching (min, max) intervals, leaving genuinely
    separate ones apart.

    Two bores drilled into OPPOSITE ends of a panel are coaxial and the same
    diameter, so they look identical to a key built from axis, position and
    size — merging those blindly turns a pair of 25 mm dowel holes into one
    464 mm bore straight through the part. Only faces whose spans actually
    meet belong to the same hole (a bore split at a surface seam, or crossing
    a groove)."""
    ordered = sorted(spans)
    merged = []
    for low, high in ordered:
        if merged and low - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], high)
        else:
            merged.append([low, high])
    return [(low, high) for low, high in merged]


def _split_collinear(groups):
    """One entry per real hole: each collinear group's faces are merged into as
    many separate bores as their spans describe."""
    out = []
    for item in groups:
        for span in _merge_spans(item['spans']):
            out.append({'axis': item['axis'], 'centre': item['centre'],
                        'diameter': item['diameter'], 'span': span})
    return out


def holes(component, frame, min_diameter=2.0, max_diameter=60.0):
    """Every drilled hole in the panel, as writer-ready dicts:

        {'type': 1|2, 'face': 1-6, 'u', 'v', 'z'?, 'diameter', 'depth'}

    Type 2 is vertical (into Face 5 or 6) and Type 1 horizontal (into an edge,
    carrying `z`, the height of the hole's axis above Face 6).

    Cylindrical faces that belong to one bore — a hole split at a surface seam,
    or a hole crossing a groove — are merged so the machine gets one instruction
    rather than three; coaxial bores whose spans do not meet stay separate, so a
    pair of dowel holes drilled into opposite ends of a panel is reported as two
    holes and not as one bore through the part."""
    found = {}
    try:
        bodies = component.bRepBodies
    except Exception:
        return []
    for bi in range(bodies.count):
        body = bodies.item(bi)
        try:
            if not body.isVisible:
                continue
            faces = body.faces
        except Exception:
            continue
        for fi in range(faces.count):
            face = faces.item(fi)
            cylinder = adsk.core.Cylinder.cast(face.geometry)
            if not cylinder or not _is_bore(face, cylinder):
                continue
            diameter = cylinder.radius * 2.0 * MM
            if diameter < min_diameter or diameter > max_diameter:
                continue
            axis_name = frame.local_axis(cylinder.axis)
            if axis_name is None:
                continue        # a bore at a compound angle — not a flat-panel hole
            span = _axial_span(face, frame, axis_name)
            if not span:
                continue
            centre = frame.point(cylinder.origin)
            index = {'u': 0, 'v': 1, 'w': 2}[axis_name]
            across = tuple(round(c, 2) for i, c in enumerate(centre) if i != index)
            key = (axis_name, across, round(diameter, 2))
            found.setdefault(key, {'axis': axis_name, 'centre': centre,
                                   'diameter': diameter, 'spans': []})
            found[key]['spans'].append(span)

    out = []
    thickness = frame.thickness
    for item in _split_collinear(found.values()):
        low, high = item['span']
        depth = high - low
        if depth <= 0:
            continue
        axis_name = item['axis']
        cu, cv, cw = item['centre']

        if axis_name == 'w':
            # Vertical. It is drilled from whichever skin it breaks through; a
            # hole that reaches both is a through hole, drilled from the top.
            opens_top = high >= thickness - EPS_MM
            opens_bottom = low <= EPS_MM
            if not opens_top and not opens_bottom:
                continue            # a pocket inside the panel — nothing to drill
            face_no = FACE_TOP if opens_top else FACE_BOTTOM
            out.append({'type': 2, 'face': face_no,
                        'u': round(cu, 3), 'v': round(cv, 3),
                        'diameter': round(item['diameter'], 3),
                        'depth': round(depth, 3),
                        'through': opens_top and opens_bottom})
            continue

        # Horizontal: it enters through one of the four edges.
        if axis_name == 'u':
            if low <= EPS_MM:
                face_no, u, v = FACE_XMIN, 0.0, cv
            elif high >= frame.length - EPS_MM:
                face_no, u, v = FACE_XMAX, frame.length, cv
            else:
                continue
        else:
            if low <= EPS_MM:
                face_no, u, v = FACE_YMIN, cu, 0.0
            elif high >= frame.width - EPS_MM:
                face_no, u, v = FACE_YMAX, cu, frame.width
            else:
                continue
        out.append({'type': 1, 'face': face_no,
                    'u': round(u, 3), 'v': round(v, 3), 'z': round(cw, 3),
                    'diameter': round(item['diameter'], 3),
                    'depth': round(depth, 3)})

    out.sort(key=lambda h: (h['type'], h['face'], h['u'], h['v']))
    for i, hole in enumerate(out, start=1):
        hole['id'] = i
    return out


# ---------------------------------------------------------------------------
# Grooves
# ---------------------------------------------------------------------------
def slots(component, frame, tol_mm=0.2):
    """Straight grooves cut into either big face, as writer-ready dicts:

        {'face': 5|6, 'u1', 'v1', 'u2', 'v2', 'width', 'depth'}

    A groove is found by its FLOOR: a planar face parallel to the skins, lying
    strictly between them, longer than it is wide. The floor's outward normal
    points into the removed material, which is what says whether the groove was
    cut from the top or the bottom. The centreline runs down the middle of the
    long axis, so the machine's tool centre follows it with no offset —
    `tool_offset` is written as 0.

    Deliberately conservative: it reports straight, axis-aligned grooves and
    stays silent about shaped pockets, stopped dados that curve, and rebates
    that break out through an edge, rather than guessing at them."""
    out = []
    try:
        bodies = component.bRepBodies
    except Exception:
        return out
    for bi in range(bodies.count):
        body = bodies.item(bi)
        try:
            if not body.isVisible:
                continue
            faces = body.faces
        except Exception:
            continue
        for fi in range(faces.count):
            face = faces.item(fi)
            plane = adsk.core.Plane.cast(face.geometry)
            if not plane or frame.local_axis(plane.normal) != 'w':
                continue
            _, _, floor = frame.point(plane.origin)
            if floor <= tol_mm or floor >= frame.thickness - tol_mm:
                continue            # that's a skin, not a groove floor
            try:
                box = face.boundingBox
            except Exception:
                continue
            lo, hi = frame.point(box.minPoint), frame.point(box.maxPoint)
            u0, u1 = min(lo[0], hi[0]), max(lo[0], hi[0])
            v0, v1 = min(lo[1], hi[1]), max(lo[1], hi[1])
            du, dv = u1 - u0, v1 - v0
            if du <= 0 or dv <= 0:
                continue
            longer, shorter = max(du, dv), min(du, dv)
            if shorter <= 0 or longer / shorter < MIN_GROOVE_RATIO:
                continue            # a square pocket, not a groove

            _, _, normal_w = frame.direction(plane.normal)
            if normal_w > 0:
                face_no, depth = FACE_TOP, frame.thickness - floor
            else:
                face_no, depth = FACE_BOTTOM, floor

            if du >= dv:            # runs along the length
                mid = (v0 + v1) / 2.0
                start, end = (u0, mid), (u1, mid)
            else:
                mid = (u0 + u1) / 2.0
                start, end = (mid, v0), (mid, v1)
            out.append({'face': face_no,
                        'u1': round(start[0], 3), 'v1': round(start[1], 3),
                        'u2': round(end[0], 3), 'v2': round(end[1], 3),
                        'width': round(shorter, 3), 'depth': round(depth, 3),
                        'tool_offset': 0})

    out.sort(key=lambda s: (s['face'], s['u1'], s['v1']))
    for i, slot in enumerate(out, start=1):
        slot['id'] = i
    return out


# ---------------------------------------------------------------------------
# Edgebanding
# ---------------------------------------------------------------------------
def banded_faces(component, frame, tagged=None, tol_mm=1.0):
    """{face 1-4: edgeband name} from the WC_EDGEBAND face tags the Edgeband
    command stamped.

    Each tagged face is matched to the edge it sits on by its centre: an edge
    face lies on u = 0, u = L, v = 0 or v = W. `tagged` optionally supplies the
    [(band name, BRepFace)] list from panels.design_band_faces so a whole design
    costs one findAttributes sweep instead of a per-face scan.

    Where two tagged faces land on the same edge (a split or mitred edge), the
    first wins and the caller sees one band per edge, which is all the file can
    carry."""
    out = {}
    pairs = tagged
    if pairs is None:
        pairs = []
        try:
            bodies = component.bRepBodies
            for bi in range(bodies.count):
                faces = bodies.item(bi).faces
                for fi in range(faces.count):
                    face = faces.item(fi)
                    name = face_band_name(face)
                    if name:
                        pairs.append((name, face))
        except Exception:
            return out

    for name, face in pairs:
        try:
            box = face.boundingBox
        except Exception:
            continue
        lo, hi = frame.point(box.minPoint), frame.point(box.maxPoint)
        cu, cv = (lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0
        distances = ((abs(cu), FACE_XMIN), (abs(frame.length - cu), FACE_XMAX),
                     (abs(cv), FACE_YMIN), (abs(frame.width - cv), FACE_YMAX))
        distance, face_no = min(distances)
        if distance > tol_mm + frame.thickness:
            continue                # not on an edge at all — ignore the tag
        out.setdefault(face_no, name)
    return out
