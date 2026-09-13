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

"""Shelf-pin line boring — geometry core + pluggable boring rules.

A *rule* knows how to turn a panel plus a few scalars into shelf-pin holes. Rules
are a strategy registry (``RULES``): the Line Boring command shows them in a
dropdown and delegates all the math here, so adding a rule later is one class plus
one registry entry — no command changes. Today there is one rule, **Emaar**.

This module is deliberately separate from the Fusion command (commands/lineBoring):
- ``frame()`` derives a panel's local axes/extents from its inner face.
- each rule exposes ``preview_points()`` (numeric hole centres, for the live
  custom-graphics preview) and ``build_plan()`` (the symbolic, parameter-driven
  recipe the command turns into a live-parametric feature tree, plus a numeric
  ``all_points`` list for the guaranteed-correct explicit fallback).

The Fusion feature creation itself lives in entry.py; this module only computes,
which keeps the geometry self-contained and the rules easy to extend.

All scalars here are in Fusion's internal length unit, the CENTIMETRE. Rule
defaults are authored in millimetres (cabinetmaking's working unit) and converted
at the edges.
"""

import adsk.core
import adsk.fusion

app = adsk.core.Application.get()

# Unit helpers. Fusion internal length = cm; the trade works in mm.
MM = 0.1                 # 1 mm expressed in cm
EPS = 1e-6               # general direction/length tolerance (cm)
EPS_TIE = 1e-3           # axis-alignment tie-break for height inference

# User-parameter namespace for the live-parametric build. Prefixed so the knobs
# never collide with the user's own design parameters and read as a group.
PFX = 'wc_lb_'


# ---------------------------------------------------------------------------
# Small vector helpers (Fusion Vector3D/Point3D are mutable; copy before scaling)
# ---------------------------------------------------------------------------
def _scaled(vec: adsk.core.Vector3D, s: float) -> adsk.core.Vector3D:
    v = vec.copy()
    v.scaleBy(s)
    return v


def _orient_toward(vec: adsk.core.Vector3D, ref: adsk.core.Vector3D) -> bool:
    """Flip ``vec`` in place so it points the same general way as ``ref``. Returns
    False if the two are (near) perpendicular, i.e. ref can't decide the sign."""
    d = vec.dotProduct(ref)
    if d < -EPS:
        vec.scaleBy(-1.0)
        return True
    return d > EPS


def _mm(cm: float) -> float:
    return cm / MM


# ---------------------------------------------------------------------------
# Panel frame
# ---------------------------------------------------------------------------
class Frame:
    """A panel's local coordinate frame, derived from its inner face. Vectors are
    unit Vector3D in model space; scalars are centimetres."""

    def __init__(self, origin, height_dir, depth_dir, normal, bore_dir,
                 height, depth, thickness, ambiguous):
        self.origin = origin            # bottom + back corner of the inner face
        self.height_dir = height_dir    # points up the panel
        self.depth_dir = depth_dir      # points toward the front edge
        self.normal = normal            # outward face normal
        self.bore_dir = bore_dir        # into the panel (= -normal)
        self.height = height            # H along height_dir
        self.depth = depth              # along depth_dir
        self.thickness = thickness      # panel thickness along normal
        self.ambiguous = ambiguous      # orientation couldn't be inferred from +Z

    def point(self, h: float, d: float) -> adsk.core.Point3D:
        """Model point at height offset ``h`` (from the bottom edge) and depth
        offset ``d`` (from the back edge); lies on the inner face plane."""
        p = self.origin.copy()
        p.translateBy(_scaled(self.height_dir, h))
        p.translateBy(_scaled(self.depth_dir, d))
        return p


def _longest_edge_dir(face) -> adsk.core.Vector3D:
    """Direction of the longest linear edge of ``face`` (height fallback when the
    panel isn't upright so world +Z can't pick the vertical axis)."""
    best = None
    best_len = -1.0
    for edge in face.edges:
        try:
            a = edge.startVertex.geometry
            b = edge.endVertex.geometry
        except Exception:
            continue
        v = a.vectorTo(b)
        if v.length > best_len:
            best_len = v.length
            best = v
    if best is None:
        best = adsk.core.Vector3D.create(0, 0, 1)
    best.normalize()
    return best


def frame(face, up=None, front_refs=None, back_ref_point=None) -> Frame:
    """Derive a panel's local frame from its (inner) planar face.

    height_dir = the ``up`` reference projected into the face plane (the in-plane
    "up"); if the panel is lying flat, fall back to its longest edge. depth_dir is
    perpendicular to height; its FRONT direction is decided by ``back_ref_point``
    (point away from the picked back panel) when given, else by the first
    ``front_refs`` reference that isn't in-plane-perpendicular. Extents come from an
    oriented bounding box aligned to those axes; thickness from the body's extent
    along the normal.

    ``up`` / ``front_refs`` / ``back_ref_point`` are all in the FACE's own space
    (world for a selection proxy, component space for a native face inside a rotated
    occurrence) so the frame is consistent with the geometry it is derived from —
    see _ref_axes / _to_native_point in entry.py."""
    measure = app.measureManager   # MeasureManager hangs off Application, not Design

    if up is None:
        up = adsk.core.Vector3D.create(0, 0, 1)
    if front_refs is None:
        front_refs = [adsk.core.Vector3D.create(0, 1, 0), adsk.core.Vector3D.create(1, 0, 0)]

    _, normal = face.evaluator.getNormalAtPoint(face.pointOnFace)
    normal.normalize()

    height_dir = up.copy()
    height_dir.subtract(_scaled(normal, up.dotProduct(normal)))   # project onto plane
    ambiguous = False
    if height_dir.length < EPS_TIE:
        # Near-horizontal face (panel lying flat): up can't pick the vertical axis.
        ambiguous = True
        height_dir = _longest_edge_dir(face)
        # Strip any out-of-plane component so the axis lies in the face.
        height_dir.subtract(_scaled(normal, height_dir.dotProduct(normal)))
    height_dir.normalize()

    depth_dir = normal.crossProduct(height_dir)
    depth_dir.normalize()

    # Extents + centre are independent of the depth_dir SIGN, so measure first and
    # decide which way is "front" afterwards.
    face_obb = measure.getOrientedBoundingBox(face, height_dir, depth_dir)
    height = face_obb.length          # along height_dir (1st arg)
    depth = face_obb.width            # along depth_dir (2nd arg)
    center = face_obb.centerPoint

    body_obb = measure.getOrientedBoundingBox(face.body, height_dir, depth_dir)
    thickness = body_obb.height       # along the normal (cross of the two args)

    # Orient depth_dir toward the FRONT.
    if back_ref_point is not None:
        to_back = center.vectorTo(back_ref_point)
        along = depth_dir.dotProduct(to_back)
        if abs(along) < EPS_TIE:
            ambiguous = True              # back panel doesn't lie along the depth axis
        elif along > 0:
            depth_dir.scaleBy(-1.0)       # depth_dir pointed toward the back panel — flip to front
    elif not any(_orient_toward(depth_dir, ref) for ref in front_refs):
        ambiguous = True                  # front/back couldn't be inferred

    origin = center.copy()
    origin.translateBy(_scaled(height_dir, -height / 2.0))
    origin.translateBy(_scaled(depth_dir, -depth / 2.0))

    bore_dir = _scaled(normal, -1.0)

    return Frame(origin, height_dir, depth_dir, normal, bore_dir,
                 height, depth, thickness, ambiguous)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def clear_gap(bottom: float, top: float, n: int, shelf: float = 0.0) -> float:
    """The equal CLEAR gap between consecutive shelf FACES.

    The opening holds N shelves of thickness ``shelf`` plus N+1 clear gaps, so
    gap = (opening - N * shelf) / (N + 1). With shelf = 0 this is the old
    centre-to-centre spacing."""
    return (top - bottom - n * shelf) / (n + 1)


def set_centers(bottom: float, top: float, n: int, shelf: float = 0.0, rise: float = 0.0):
    """Emaar even-interior rule: N set centres placed so the shelf FACES divide the
    clear opening into N+1 equal gaps.

    A shelf does not sit at its hole centre — it rests on the pin, so its underside
    lands ``rise`` above the centre (``rise`` = the pin's supporting height above the
    hole axis, half the hole diameter for a plain shelf pin) and its top face
    ``shelf`` higher again. Spacing the CENTRES evenly therefore leaves the bottom
    gap larger than the top one by exactly (rise + shelf). Spacing the FACES evenly
    instead gives, with g = clear_gap():

        centre_1   = bottom + g - rise          (shelf underside lands at bottom + g)
        centre_k+1 = centre_k + g + shelf       (a gap plus the shelf it sits under)

    so every gap — bottom panel to first shelf, shelf to shelf, last shelf to top
    panel — measures g. ``bottom``/``top`` are height offsets from the side panel's
    bottom edge; shelf = rise = 0 reproduces the old evenly-spaced-centres rule."""
    gap = clear_gap(bottom, top, n, shelf)
    first = bottom + gap - rise
    return [first + k * (gap + shelf) for k in range(n)]


class EmaarRule:
    """3-hole sets (middle ± pitch), evenly split between the bottom and top
    panels, in two columns.

    The SHELF FACES divide the clear opening into N+1 equal gaps: the opening runs
    from the picked bottom panel's top face to the picked top panel's bottom face
    (``p['bottom_h']`` / ``p['top_h']``, height offsets from the side panel's bottom
    edge), falling back to the side panel's own ends when they aren't picked. Shelf
    thickness (``p['shelf']``) and the pin rise (``p['rise']``, how far the shelf's
    underside sits above the hole centre) are taken out of the arithmetic, so the
    usable gaps come out equal rather than the hole centres — see set_centers().
    Each set is three holes on a vertical line: a middle hole at the nominal centre,
    plus one ``pitch`` above and one below. Two vertical columns sit ``front``/
    ``back`` in from the panel's front and back edges."""

    name = 'emaar'
    display = 'Emaar'

    # Defaults in centimetres (authored as the mm values the trade uses).
    DEFAULTS = {
        'n': 4,
        'front': 10.75,      # 107.5 mm in from the front edge
        'back': 8.25,       # 82.5 mm in from the back edge
        'pitch': 6.4,      # 64 mm System-32 pitch
        'dia': 0.5,        # 5 mm pin hole
        'depth': 1.2,      # 12 mm blind depth
        'shelf': 1.8,      # 18 mm shelf
        'rise': 0.5,       # 5 mm — measured off the Emaar 19557 shelf mount
                           #   (shelf underside 1115.5 mm over a 1110.5 mm hole centre)
    }

    # ---- column geometry (shared by preview + plan) ----
    @staticmethod
    def _column_depths(fr: Frame, p: dict):
        """Depth offsets (from the back edge = origin) of the back and front columns.

        The BACK column is measured forward of the picked BACK PANEL when one was
        supplied (``p['back_depth']`` = the back panel's depth position along the
        side panel's depth axis, so a recessed back is handled correctly); with no
        back panel it falls back to the side panel's own back edge (depth 0). The
        FRONT column is measured in from the side panel's front edge."""
        base = p.get('back_depth') or 0.0
        back_d = base + p['back']
        front_d = fr.depth - p['front']
        return back_d, front_d

    # ---- vertical span (shared by preview + plan) ----
    @staticmethod
    def _span(fr: Frame, p: dict):
        """Height offsets (from the side panel's bottom edge = frame origin) of the
        two datums the hole sets are split between: the picked BOTTOM panel's top
        face and the picked TOP panel's bottom face (``p['bottom_h']`` /
        ``p['top_h']``). Either falls back to the side panel's own end when that
        panel wasn't picked, which reproduces the old whole-panel spacing."""
        bottom = p.get('bottom_h')
        top = p.get('top_h')
        if bottom is None:
            bottom = 0.0
        if top is None:
            top = fr.height
        return bottom, top

    # ---- numeric hole centres on the inner face (preview + build) ----
    def preview_points(self, fr: Frame, p: dict):
        pts = []
        back_d, front_d = self._column_depths(fr, p)
        bottom_h, top_h = self._span(fr, p)
        for c in set_centers(bottom_h, top_h, int(p['n']),
                             p.get('shelf', 0.0), p.get('rise', 0.0)):
            for h in (c - p['pitch'], c, c + p['pitch']):
                for d in (back_d, front_d):
                    pts.append(fr.point(h, d))
        return pts

    # ---- validation (raises ValueError with a user-facing message) ----
    def validate(self, fr: Frame, p: dict):
        n = int(p['n'])
        if n < 1:
            raise ValueError('Shelf count must be at least 1.')
        if p['pitch'] <= 0 or p['dia'] <= 0 or p['depth'] <= 0:
            raise ValueError('Pitch, hole diameter and hole depth must all be positive.')
        if p['front'] < 0 or p['back'] < 0:
            raise ValueError('Setbacks cannot be negative.')
        if p.get('shelf', 0.0) < 0 or p.get('rise', 0.0) < 0:
            raise ValueError('Shelf thickness and pin rise cannot be negative.')
        radius = p['dia'] / 2.0
        back_d, front_d = self._column_depths(fr, p)
        if front_d - back_d < p['dia']:
            raise ValueError(
                'The two hole columns end up less than one hole diameter apart '
                f'(panel depth {_mm(fr.depth):.0f} mm). Adjust the setbacks or back panel.')
        if back_d < radius or front_d > fr.depth - radius:
            raise ValueError('A hole column falls within its own radius of the panel '
                             'edge — adjust the setbacks.')
        bottom_h, top_h = self._span(fr, p)
        span = top_h - bottom_h
        if span <= EPS:
            raise ValueError(
                'The bottom and top panels do not enclose an opening on this side '
                'panel — pick the bottom panel\'s TOP face and the top panel\'s '
                'BOTTOM face.')
        shelf = p.get('shelf', 0.0)
        rise = p.get('rise', 0.0)
        gap = clear_gap(bottom_h, top_h, n, shelf)
        if gap <= 0:
            raise ValueError(
                f'{n} shelves of {_mm(shelf):.0f} mm leave no room in a '
                f'{_mm(span):.0f} mm opening. Reduce the shelf count or thickness.')
        centers = set_centers(bottom_h, top_h, n, shelf, rise)
        # The outermost satellite of the bottom set sits (centre_1 - pitch) up from
        # the opening's floor, and of the top set (pitch) above centre_N; both must
        # clear their panel by a hole radius.
        if (centers[0] - p['pitch']) - bottom_h <= radius:
            raise ValueError(
                f'Opening too short for this many shelves ({_mm(gap):.0f} mm clear per '
                'gap): the bottom set\'s lowest hole would run into the bottom panel. '
                'Reduce the shelf count or pitch.')
        if top_h - (centers[-1] + p['pitch']) <= radius:
            raise ValueError(
                f'Opening too short for this many shelves ({_mm(gap):.0f} mm clear per '
                'gap): the top set\'s highest hole would run into the top panel. '
                'Reduce the shelf count or pitch.')
        lowest = centers[0] - p['pitch']
        highest = centers[-1] + p['pitch']
        if lowest < radius or highest > fr.height - radius:
            raise ValueError(
                'The hole pattern runs off the end of the side panel — check the '
                'faces picked for the bottom and top panels.')
        if fr.thickness > EPS and p['depth'] >= fr.thickness:
            raise ValueError(
                f'Hole depth ({_mm(p["depth"]):.0f} mm) reaches through the panel '
                f'({_mm(fr.thickness):.0f} mm thick). Reduce the hole depth.')

    # ---- build recipe: a live-parametric plan + an explicit fallback ----
    def build_plan(self, fr: Frame, p: dict) -> dict:
        """Two recipes for the same holes:

        - ``all_points``: every hole centre as an explicit model point (the robust
          fallback — placed directly, no pattern, so nothing can march off-panel).
        - the parametric recipe: a seed FIRST SET of BOTH columns (6 holes) whose
          depth is dimensioned to ASSOCIATIVE datums the command projects into the
          sketch — the back column off the intersected back-panel face, the front
          column off the projected front edge — so the holes follow those references
          when the back panel thickness or panel depth changes. A single height
          rectangular pattern (qty N, spacing H/(N+1)) replicates the set up the
          panel. Heights are measured off the BOTTOM datum (the picked bottom
          panel's top face, or the side panel's bottom edge) and the spacing is
          driven by a live reference dimension across the opening, so moving or
          re-sizing the bottom/top panels reflows the holes. Only the opening height
          is baked numerically when that reference dimension can't be made; the
          knobs (N, pitch, dia, depth, front, back) are wc_lb_* user parameters.
          Each ``seed`` entry tags its column so the command picks the right datum.
        """
        n = int(p['n'])
        back_d, front_d = self._column_depths(fr, p)
        bottom_h, top_h = self._span(fr, p)
        span = top_h - bottom_h
        shelf = p.get('shelf', 0.0)
        rise = p.get('rise', 0.0)

        params = [
            (f'{PFX}N',     str(n),                      '',   'Line boring: shelves per column (sets)'),
            (f'{PFX}pitch', f'{_mm(p["pitch"]):.4f} mm', 'mm', 'Line boring: 3-hole set pitch'),
            (f'{PFX}dia',   f'{_mm(p["dia"]):.4f} mm',   'mm', 'Line boring: hole diameter'),
            (f'{PFX}depth', f'{_mm(p["depth"]):.4f} mm', 'mm', 'Line boring: blind hole depth'),
            (f'{PFX}front', f'{_mm(p["front"]):.4f} mm', 'mm', 'Line boring: front-edge setback'),
            (f'{PFX}back',  f'{_mm(p["back"]):.4f} mm',  'mm', 'Line boring: back-panel setback'),
            (f'{PFX}shelf', f'{_mm(shelf):.4f} mm',       'mm', 'Line boring: shelf thickness (taken out of the gaps)'),
            (f'{PFX}rise',  f'{_mm(rise):.4f} mm',        'mm', 'Line boring: shelf underside above the hole centre (pin rise)'),
        ]

        # Seed = first set (k=1) of BOTH columns: 6 holes. Each tags its column (so
        # the command dimensions depth against the matching datum) and its height
        # variant ('mid'/'up'/'low'); the command builds the height expression from
        # a live opening-height token, so spacing tracks the bottom/top panels.
        c1 = set_centers(bottom_h, top_h, n, shelf, rise)[0]
        pitch = p['pitch']
        seed = []
        for h_off, variant in ((c1 - pitch, 'low'), (c1, 'mid'), (c1 + pitch, 'up')):
            seed.append({'variant': variant, 'col': 'back',  'pt': fr.point(h_off, back_d)})
            seed.append({'variant': variant, 'col': 'front', 'pt': fr.point(h_off, front_d)})

        return {
            'all_points': self.preview_points(fr, p),
            'dia_cm': p['dia'],
            'depth_cm': p['depth'],
            'params': params,
            'seed': seed,
            'back_expr': f'{PFX}back',     # offset from the intersected back-panel line
            'front_expr': f'{PFX}front',   # offset from the projected front edge
            'hole': (f'{PFX}dia', f'{PFX}depth'),
            'qty_expr': f'{PFX}N',
            'span_mm': f'{_mm(span):.4f} mm',     # baked fallback if no live opening token
            'gap_cm': clear_gap(bottom_h, top_h, n, shelf),   # equal clear gap (preview label)
        }


# Strategy registry. Order = dropdown order; first entry is the default.
RULES = [EmaarRule()]
RULES_BY_NAME = {r.name: r for r in RULES}
