# WoodCraft — a Fusion add-in for cabinetmaking.
# Repair Line Boring core: bring line boring made by older WoodCraft versions
# (or drawn by hand) in line with the current Line Boring tool, WITHOUT
# recreating the holes. Driven by commands/repairLineBoring.
#
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

"""Repair the shelf-pin line boring in the active cabinet.

Run from the "Repair Line Boring" button (Cabinet Builder panel) on an open
cabinet. For every shelf-pin boring it finds, it rebuilds the SKETCH the
holes sit on the way the current Line Boring tool builds it, and leaves the Hole
and Pattern FEATURES themselves in place — so joints, shelf mounts and anything
else attached to those holes keep their references.

What a repaired boring looks like (same as a fresh Line Boring):
  * hidden "LB Bottom/Top/Back" planes extending the panels the holes are spaced
    between (panels rarely touch the side panel);
  * a hidden "LB Opening" sketch measuring the opening between them;
  * the hole sketch, renamed "LB Holes ...", fully defined: every hole point is
    dimensioned from the bottom datum (shelf-face rule, live from the opening)
    and from the back / front datum; no projected edges that break when a
    configuration changes the panel's shape;
  * a pattern (if the boring has one) driven by the same live spacing.

How it finds what you originally picked
  Older versions don't record the bottom / top panels, so the opening is taken
  as the NEAREST panels directly below and above the holes (horizontal panel
  faces next to the side panel; the side panel's own ends only when nothing else
  is there). Shelves resting on this panel's pins are adjustable and never
  count as a boundary. The back datum is the nearest panel face behind the back
  column (else the side panel's own back). Front/back setbacks are kept as
  measured. Holes are rebuilt to the current shelf-face rule; if that moves any
  of them more than MATCH_TOL_MM (hand-placed or old evenly-spaced layouts),
  the script asks first, and No leaves that boring exactly as it is.

It never saves. Check the result, then save — or Undo (the whole repair is
one undo step when run from the button), or close without saving.
"""

import re
import traceback

import adsk.core
import adsk.fusion

from . import boring as _boring
from .lineBoring import entry as _lb

SCRIPT_NAME = 'Repair Line Boring'
MATCH_TOL_MM = 2.0         # holes moving more than this -> ask first; within it = 'matched'
PIN_DIA_MAX_CM = 0.8       # holes wider than this are not shelf-pin borings
REPAIRED_ATTR = ('WoodCraft', 'lineBoringRepaired')

app = adsk.core.Application.get()
ui = app.userInterface


def repair_active_design(confirm=True, log=None):
    """Repair every line boring in the active design. Returns the report lines."""
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        ui.messageBox('Open a cabinet design first.', SCRIPT_NAME)
        return []
    lb, boring = _woodcraft_modules()

    report = []
    say = log or (lambda s: None)
    borings = _find_borings(design, boring)
    if not borings:
        ui.messageBox('No shelf-pin line boring found in this design.', SCRIPT_NAME)
        return []

    if confirm:
        names = '\n'.join(f'  - {b["comp"].name}: {b["n_total"]} shelf set(s)'
                          f'{" + pattern" if b["pattern"] else ""}' for b in borings)
        ans = ui.messageBox(
            f'Found {len(borings)} line boring(s):\n{names}\n\nRebuild their sketches to '
            'the current Line Boring method? The holes themselves are kept, so joints '
            'to them stay. Nothing is saved.',
            SCRIPT_NAME, adsk.core.MessageBoxButtonTypes.YesNoButtonType)
        if ans != adsk.core.DialogResults.DialogYes:
            return []

    for b in borings:
        label = f'{b["comp"].name} ({b["sketch_name"]})'
        try:
            msg = _repair_one(design, lb, boring, b, ask=_ask_move if confirm else None)
            report.append(f'OK    {label}: {msg}')
        except _Skip as sk:
            report.append(f'SKIP  {label}: {sk}')
        except Exception as ex:
            report.append(f'FAIL  {label}: {ex}')
            say(traceback.format_exc())
        finally:
            try:
                design.timeline.moveToEnd()
            except Exception:
                pass
        say(report[-1])

    _delete_unused_lb_params(design)
    if confirm:
        failed = any(r.startswith('FAIL') for r in report)
        tail = ('\n\nSOME BORINGS FAILED and may be half-repaired: close this design '
                'WITHOUT SAVING (or Undo).' if failed else
                '\n\nCheck the cabinet, then save (or Undo to put it back).')
        ui.messageBox('\n'.join(report) + tail, SCRIPT_NAME)
    return report


class _Skip(Exception):
    pass


def _ask_move(b, inf):
    """Confirm a repair that moves holes noticeably (hand-placed / old layouts)."""
    why = ('were placed by hand (they follow no spacing rule)' if inf['rule'].startswith('none')
           else 'follow the old evenly-spaced-centres rule')
    ans = ui.messageBox(
        f'{b["comp"].name} ({b["sketch_name"]}): these {b["n_total"]} hole set(s) {why}.\n\n'
        f'Between {_owner(inf["bottom"])} and {_owner(inf["top"])} the Line Boring rule '
        f'puts them up to {inf["score_mm"]:.0f} mm away from where they are now.\n\n'
        'Yes = move them to the tool\'s spacing (anything jointed to the holes, e.g. '
        'shelf mounts, moves with them; check that the shelf itself follows).\n'
        'No = leave this boring as it is.',
        SCRIPT_NAME, adsk.core.MessageBoxButtonTypes.YesNoButtonType)
    return ans == adsk.core.DialogResults.DialogYes


def _woodcraft_modules():
    """The Line Boring command module and boring core this repair builds with."""
    return _lb, _boring


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _find_borings(design, boring):
    """Every HoleFeature that looks like shelf-pin line boring: positioned by sketch
    points on a side panel face, in two columns of 3-hole sets."""
    patterns = _pattern_map(design)
    found = []
    for comp in design.allComponents:
        if comp == design.rootComponent:
            continue
        for hole in comp.features.holeFeatures:
            try:
                spd = adsk.fusion.SketchPointsHolePositionDefinition.cast(hole.holePositionDefinition)
                if not spd or hole.holeDiameter.value > PIN_DIA_MAX_CM:
                    continue
                pts = [spd.sketchPoints.item(i) for i in range(spd.sketchPoints.count)]
                if len(pts) < 6 or len(pts) % 6:
                    continue
                sketch = pts[0].parentSketch
                try:
                    if sketch.attributes.itemByName(*REPAIRED_ATTR):
                        continue
                except Exception:
                    pass
                proxy = _side_face(design, comp, pts)
                if proxy is None:
                    continue
                fr = boring.frame(proxy)
                sets = _analyse_points(fr, pts)
                if sets is None:
                    continue
                pat, pat_dir, two_dir = patterns.get((comp.name, hole.name), (None, None, False))
                qty = 1
                if pat is not None:
                    if two_dir:
                        continue                      # 2-direction pattern: not ours
                    qty = int(round(pat.quantityOne.value))
                found.append({
                    'comp': comp, 'hole': hole, 'sketch': sketch,
                    'hole_name': hole.name, 'pattern_name': pat.name if pat is not None else None,
                    'sketch_name': sketch.name, 'proxy': proxy, 'pattern': pat,
                    'pattern_dir': pat_dir,
                    'sets_in_sketch': len(sets['mids']), 'qty': qty,
                    'n_total': len(sets['mids']) * qty,
                    'mids': ([sets['mids'][0] + k * pat.distanceOne.value for k in range(qty)]
                             if pat is not None else list(sets['mids'])),
                })
            except Exception:
                continue
    # Every shelf-set height on each side panel, so an ADJUSTABLE shelf (one that
    # rests on any of these pins) is never mistaken for a panel bounding an opening.
    by_comp = {}
    for b in found:
        by_comp.setdefault(b['comp'].name, []).extend(b['mids'])
    for b in found:
        b['panel_mids'] = by_comp[b['comp'].name]
    return found


def _pattern_map(design):
    """(component name, hole name) -> (pattern, direction-entity token). Entity
    tokens are NOT stable strings (the same entity can report a different token
    later), so features are keyed by name here. Reading a pattern's
    input and direction entities needs the timeline rolled back to it."""
    result = {}
    dirs = {}
    tl = design.timeline
    try:
        for comp in design.allComponents:
            for pf in comp.features.rectangularPatternFeatures:
                try:
                    pf.timelineObject.rollTo(True)
                    ents = pf.inputEntities
                    try:
                        d1 = pf.directionOneEntity
                        d1_tok = d1.entityToken if d1 else None
                    except Exception:
                        d1_tok = None
                    try:   # a second direction actually in use (qty2 alone lies)
                        two_dir = pf.directionTwoEntity is not None and pf.quantityTwo.value > 1.5
                    except Exception:
                        two_dir = False
                    for i in range(ents.count):
                        h = adsk.fusion.HoleFeature.cast(ents.item(i))
                        if h:
                            key = (comp.name, h.name)
                            result[key] = pf
                            dirs[key] = (d1_tok, two_dir)
                except Exception:
                    pass
    finally:
        tl.moveToEnd()
    # Tokens are stable, but the objects were read rolled back: re-fetch them.
    fresh = {}
    for tok, pf in result.items():
        try:
            fresh[tok] = (design.findEntityByToken(pf.entityToken)[0],) + dirs.get(tok, (None, False))
        except Exception:
            fresh[tok] = (pf,) + dirs.get(tok, (None, False))
    return fresh


def _side_face(design, comp, pts):
    """The assembly-context planar face of ``comp`` the hole points sit on."""
    world = [p.worldGeometry for p in pts]
    occs = design.rootComponent.allOccurrencesByComponent(comp)
    for oi in range(occs.count):
        occ = occs.item(oi)
        for body in comp.bRepBodies:
            for face in body.faces:
                if face.geometry.surfaceType != adsk.core.SurfaceTypes.PlaneSurfaceType:
                    continue
                proxy = face.createForAssemblyContext(occ)
                plane = proxy.geometry
                n = plane.normal
                if all(abs(plane.origin.vectorTo(w).dotProduct(n)) < 1e-3 for w in world):
                    best = proxy
                    # largest coplanar face (the panel's inner face, not a groove)
                    for f2 in body.faces:
                        if f2 is face or f2.geometry.surfaceType != adsk.core.SurfaceTypes.PlaneSurfaceType:
                            continue
                        p2 = f2.createForAssemblyContext(occ)
                        g2 = p2.geometry
                        if (abs(g2.normal.dotProduct(n)) > 0.9999
                                and abs(plane.origin.vectorTo(g2.origin).dotProduct(n)) < 1e-3
                                and p2.area > best.area):
                            best = p2
                    return best
    return None


def _hd(fr, p):
    v = fr.origin.vectorTo(p.worldGeometry)
    return v.dotProduct(fr.height_dir), v.dotProduct(fr.depth_dir)


def _analyse_points(fr, pts):
    """Split hole points into two depth columns of 3-hole sets. Returns
    {'mids': [set mid heights], 'pitch': p, 'cols': (back_d, front_d),
    'tags': {point index: (set_index, variant, column)}} or None if not line boring."""
    hd = [(_hd(fr, p) + (i,)) for i, p in enumerate(pts)]
    ds = [d for _, d, _ in hd]
    split = (min(ds) + max(ds)) / 2.0
    if max(ds) - min(ds) < 2.0:
        return None
    cols = {'back': [x for x in hd if x[1] < split], 'front': [x for x in hd if x[1] >= split]}
    if len(cols['back']) != len(cols['front']):
        return None
    mids_by_col, pitch, tags = {}, None, {}
    for col, items in cols.items():
        items.sort(key=lambda x: x[0])
        mids = []
        for k in range(0, len(items), 3):
            trip = items[k:k + 3]
            p1 = trip[1][0] - trip[0][0]
            p2 = trip[2][0] - trip[1][0]
            if abs(p1 - p2) > 0.05 or not (2.0 < p1 < 12.0):
                return None
            pitch = p1 if pitch is None else pitch
            if abs(pitch - p1) > 0.05:
                return None
            mids.append(trip[1][0])
            for var, it in zip(('low', 'mid', 'up'), trip):
                tags[it[2]] = (k // 3, var, col)
        mids_by_col[col] = mids
    if any(abs(a - b) > 0.05 for a, b in zip(mids_by_col['back'], mids_by_col['front'])):
        return None
    return {'mids': mids_by_col['back'], 'pitch': pitch,
            'cols': (sum(d for _, d, _ in cols['back']) / len(cols['back']),
                     sum(d for _, d, _ in cols['front']) / len(cols['front'])),
            'tags': tags}


# ---------------------------------------------------------------------------
# Inferring what the user picked
# ---------------------------------------------------------------------------
def _near_faces(design, comp, fr, max_gap=2.0):
    """Planar faces of OTHER bodies lying against the side panel's inner face
    (within ``max_gap`` cm of it, on the cabinet side)."""
    out = []
    for occ in design.rootComponent.allOccurrences:
        if occ.component == comp or occ.bRepBodies.count == 0:
            continue
        try:
            bb = occ.boundingBox
            corners = [adsk.core.Point3D.create(x, y, z)
                       for x in (bb.minPoint.x, bb.maxPoint.x)
                       for y in (bb.minPoint.y, bb.maxPoint.y)
                       for z in (bb.minPoint.z, bb.maxPoint.z)]
            s = [fr.origin.vectorTo(c).dotProduct(fr.normal) for c in corners]
            if min(s) > max_gap or max(s) < -0.5:
                continue
        except Exception:
            continue
        for body in occ.bRepBodies:
            for f in body.faces:
                try:
                    if f.geometry.surfaceType != adsk.core.SurfaceTypes.PlaneSurfaceType:
                        continue
                    vs = [fr.origin.vectorTo(v.geometry) for v in f.vertices]
                    s = [v.dotProduct(fr.normal) for v in vs]
                    if min(s) > max_gap or max(s) < -0.5:
                        continue
                    out.append((f, vs))
                except Exception:
                    continue
    return out


def _infer(design, boring, b, fr, sets, n_total, step_obs, shelf, rise):
    """Pick the bottom/top faces (or panel ends) whose rule spacing reproduces the
    existing holes, and the back face behind the back column."""
    r = 0.25
    obs = [sets['mids'][0] + k * step_obs for k in range(n_total)] if b['pattern'] else sets['mids']
    pitch = sets['pitch']
    lowest, highest = obs[0] - pitch, obs[-1] + pitch
    back_d, front_d = sets['cols']
    faces = _near_faces(design, b['comp'], fr)

    bottoms, tops, backs = [(None, 0.0)], [(None, fr.height)], []
    for f, vs in faces:
        n = f.evaluator.getNormalAtPoint(f.pointOnFace)[1]
        hs = [v.dotProduct(fr.height_dir) for v in vs]
        ds = [v.dotProduct(fr.depth_dir) for v in vs]
        nh, nd = n.dotProduct(fr.height_dir), n.dotProduct(fr.depth_dir)
        if abs(nh) > 0.999 and min(ds) <= back_d + 0.5 and max(ds) >= front_d - 0.5:
            h = sum(hs) / len(hs)
            if nh > 0 and h < lowest - r:
                bottoms.append((f, h))
            elif nh < 0 and h > highest + r:
                tops.append((f, h))
        elif nd > 0.999 and min(hs) <= lowest and max(hs) >= highest:
            d = sum(ds) / len(ds)
            if d < back_d - r:
                backs.append((f, d))
    # The opening is bounded by the NEAREST panels directly below and above the
    # holes — never one hidden behind another panel (e.g. the side panel's own end
    # behind the Bottom Panel), whatever spacing it would happen to fit. Shelves
    # that rest on pins of this panel are adjustable, not boundaries: skip them.
    mids = b.get('panel_mids') or obs
    bottoms = [x for x in sorted(bottoms, key=lambda x: -x[1]) if not _on_pins(x[0], fr, mids)]
    tops = [x for x in sorted(tops, key=lambda x: x[1]) if not _on_pins(x[0], fr, mids)]
    bf, bh = bottoms[0]
    tf, th = tops[0]
    back = max(backs, key=lambda x: x[1]) if backs else (None, 0.0)
    if boring.clear_gap(bh, th, n_total, shelf) <= 0:
        raise _Skip('the opening around these holes is too small for '
                    f'{n_total} shelf set(s) — run Line Boring on this panel instead')

    pred = boring.set_centers(bh, th, n_total, shelf, rise)
    moved = max(abs(a - o) for a, o in zip(pred, obs)) * 10.0
    # Which rule (if any) the old layout followed — for the report only; the
    # rebuild always uses the current shelf-face rule.
    legacy = boring.set_centers(bh, th, n_total, 0.0, 0.0)
    legacy_off = max(abs(a - o) for a, o in zip(legacy, obs)) * 10.0
    if moved <= MATCH_TOL_MM:
        rule = 'current'
    elif legacy_off <= MATCH_TOL_MM:
        rule = 'legacy'
    else:
        rule = 'none (hand-placed)'
    return {'bottom': bf, 'bottom_h': bh, 'top': tf, 'top_h': th,
            'rule': rule, 'score_mm': moved, 'forced': moved > MATCH_TOL_MM,
            'back': back[0], 'back_d': back[1], 'obs': obs}


def _on_pins(face, fr, mids, reach=1.0):
    """True when ``face`` belongs to a shelf resting on shelf pins of this panel:
    its body's underside sits just above (0..``reach`` cm) one of the set heights."""
    if face is None:
        return False
    try:
        hs = [fr.origin.vectorTo(v.geometry).dotProduct(fr.height_dir) for v in face.body.vertices]
    except Exception:
        return False
    if not hs:
        return False
    under = min(hs)
    return any(0.0 <= under - m <= reach for m in mids)


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------
def _refresh(design, b):
    """Re-read a boring's live objects just before repairing it. Repairing an
    earlier boring on the same panel recomputes that panel, which invalidates any
    face / feature objects captured during discovery."""
    comp = b['comp']
    hole = comp.features.holeFeatures.itemByName(b['hole_name'])
    if hole is None:
        raise RuntimeError('its hole feature could not be found again')
    spd = adsk.fusion.SketchPointsHolePositionDefinition.cast(hole.holePositionDefinition)
    pts = [spd.sketchPoints.item(i) for i in range(spd.sketchPoints.count)]
    proxy = _side_face(design, comp, pts)
    if proxy is None:
        raise RuntimeError('its side panel face could not be found again')
    b['hole'], b['sketch'], b['proxy'] = hole, pts[0].parentSketch, proxy
    if b['pattern_name']:
        b['pattern'] = comp.features.rectangularPatternFeatures.itemByName(b['pattern_name'])


def _repair_one(design, lb, boring, b, ask=None):
    defaults = boring.EmaarRule.DEFAULTS
    _refresh(design, b)
    comp, hole, sketch, pat = b['comp'], b['hole'], b['sketch'], b['pattern']
    spd = adsk.fusion.SketchPointsHolePositionDefinition.cast(hole.holePositionDefinition)
    pts = [spd.sketchPoints.item(i) for i in range(spd.sketchPoints.count)]

    # --- read the existing boring --------------------------------------------
    fr0 = boring.frame(b['proxy'])
    sets0 = _analyse_points(fr0, pts)
    step_obs = pat.distanceOne.value if pat else 0.0
    shelf = _param_value(design, sketch, pat, 'shelf', defaults['shelf'])
    rise = _param_value(design, sketch, pat, 'rise', defaults['rise'])
    inf = _infer(design, boring, b, fr0, sets0, b['n_total'], step_obs, shelf, rise)
    if inf['forced'] and ask is not None and not ask(b, inf):
        raise _Skip(f'holes are {inf["score_mm"]:.0f} mm off the tool\'s spacing for '
                    f'{_owner(inf["bottom"])} -> {_owner(inf["top"])}; left as they are')

    # Final frame, oriented by the back face when there is one (as the tool does).
    back_pt = lb._face_world_point(inf['back']) if inf['back'] is not None else None
    fr = boring.frame(b['proxy'], back_ref_point=back_pt)
    sets = _analyse_points(fr, pts)
    if sets is None:
        raise _Skip('hole layout not recognised in the panel frame')
    back_d, front_d = sets['cols']
    back_base = 0.0 if back_pt is None else fr.origin.vectorTo(back_pt).dotProduct(fr.depth_dir)

    p = {
        'n': b['n_total'] if not pat else b['qty'],     # pattern: N = pattern qty
        'front': fr.depth - front_d,                     # keep the measured setbacks
        'back': back_d - back_base,
        'pitch': sets['pitch'],
        'dia': hole.holeDiameter.value,
        'depth': _hole_depth(hole, defaults['depth']),
        'shelf': shelf, 'rise': rise,
        'back_depth': back_base if back_pt is not None else None,
        'bottom_h': inf['bottom_h'] if inf['bottom'] is not None else None,
        'top_h': inf['top_h'] if inf['top'] is not None else None,
    }
    n_total = b['n_total']
    suffix = lb._unique_instance_suffix(design, lb._safe_name(comp.name))
    plan = boring.RULES[0].build_plan(fr, dict(p, n=n_total), suffix=suffix)
    params = [list(x) for x in plan['params']]
    if pat:   # pattern borings keep N = pattern quantity (sets_in_sketch is 1)
        params[0][1] = str(b['qty'])
    old_names = _wc_names_used(sketch, pat)
    lb._ensure_params(design, [tuple(x) for x in params])

    tokens = {
        'sketch': sketch.entityToken, 'hole': hole.entityToken,
        'pattern': pat.entityToken if pat else None,
        'proxy': b['proxy'].entityToken,
        'bottom': inf['bottom'].entityToken if inf['bottom'] is not None else None,
        'top': inf['top'].entityToken if inf['top'] is not None else None,
        'back': inf['back'].entityToken if inf['back'] is not None else None,
        'points': [pt.entityToken for pt in pts],
    }
    tags = sets['tags']                      # by index into pts / tokens['points']
    find = lambda tok: design.findEntityByToken(tok)[0] if tok else None

    # --- 0. re-point the hole / pattern at the new parameters, at the timeline
    # end (they can't be reached once it is rolled back to the sketch). The
    # pattern spacing is baked for now — the live expression needs the opening
    # parameter created below. Old expressions are kept to undo a failed attempt.
    saved = {'dia': hole.holeDiameter.expression}
    hole.holeDiameter.expression = plan['hole'][0]
    try:
        saved['depth'] = hole.extentDefinition.distance.expression
        hole.extentDefinition.distance.expression = plan['hole'][1]
    except Exception:
        pass
    if pat:
        saved['qty'] = pat.quantityOne.expression
        saved['dist'] = pat.distanceOne.expression
        pat.quantityOne.expression = plan['qty_expr']
        pat.distanceOne.expression = f'{pat.distanceOne.value * 10.0:.4f} mm'

    def restore():
        h = find(tokens['hole'])
        _try(lambda: setattr(h.holeDiameter, 'expression', saved['dia']))
        if 'depth' in saved:
            _try(lambda: setattr(h.extentDefinition.distance, 'expression', saved['depth']))
        if tokens['pattern']:
            pf = find(tokens['pattern'])
            _try(lambda: setattr(pf.quantityOne, 'expression', saved['qty']))
            _try(lambda: setattr(pf.distanceOne, 'expression', saved['dist']))

    # --- 1. datums + LB Opening BEFORE the hole sketch (timeline order) ------
    tl = design.timeline
    sketch.timelineObject.rollTo(True)
    proxy = find(tokens['proxy'])
    created = []
    new_param_names = [x[0] for x in params]
    try:
        h_token = _build_opening(design, lb, boring, comp, fr, proxy, suffix, tokens, find, created)
    except Exception:
        # Nothing in the hole sketch has been touched yet: undo cleanly.
        for item in reversed(created):
            _try(item.deleteMe)
        tl.moveToEnd()
        restore()
        for name in new_param_names:
            up = design.userParameters.itemByName(name)
            if up:
                _try(up.deleteMe)
        raise
    bottom_src, back_src = created_srcs(created, tokens)

    # --- 2. rebuild the hole sketch in place ---------------------------------
    # With the marker rolled back the sketch can't be looked up by token; it is
    # the timeline item right after the marker.
    tl.item(tl.markerPosition).rollTo(False)
    sketch = find(tokens['sketch'])
    # Objects captured while rolled further back may be stale: fetch them again.
    proxy = find(tokens['proxy'])
    bottom_src = find(bottom_src.entityToken) if bottom_src is not None else None
    back_src = find(back_src.entityToken) if back_src is not None else None
    hole_pts = [find(t) for t in tokens['points']]
    is_hole = lambda sp: any(sp == hp for hp in hole_pts)   # object identity, not tokens

    n_expr, shelf_expr, rise_expr, pitch_expr = (
        plan['n_expr'], plan['shelf_expr'], plan['rise_expr'], plan['pitch_expr'])
    gap = f'(({h_token} - {n_expr} * {shelf_expr}) / ({n_expr} + 1))'
    first = f'({gap} - {rise_expr})'
    step = f'({gap} + {shelf_expr})'
    offs = {'low': f' - {pitch_expr}', 'mid': '', 'up': f' + {pitch_expr}'}

    dir_line = None
    if b['pattern_dir']:
        try:
            d1 = adsk.fusion.SketchLine.cast(find(b['pattern_dir']))
            if d1 and d1.parentSketch == sketch:
                dir_line = d1
        except Exception:
            dir_line = None

    for d in [sketch.sketchDimensions.item(i) for i in range(sketch.sketchDimensions.count)]:
        _try(d.deleteMe)
    for c in [sketch.geometricConstraints.item(i) for i in range(sketch.geometricConstraints.count)]:
        _try(c.deleteMe)
    for c in [sketch.sketchCurves.item(i) for i in range(sketch.sketchCurves.count)]:
        if dir_line is not None and c == dir_line:
            continue
        line = adsk.fusion.SketchLine.cast(c)
        ends = [line.startSketchPoint, line.endSketchPoint] if line else []
        if any(is_hole(e) for e in ends):
            # Never delete a line that ends on a hole point (that could take the
            # point — and the hole — with it). Keep it as construction; a free far
            # end is fixed so the sketch stays fully defined.
            c.isConstruction = True
            for sp_end in (line.startSketchPoint, line.endSketchPoint):
                if not is_hole(sp_end):
                    _try(lambda: setattr(sp_end, 'isFixed', True))
            continue
        _try(c.deleteMe)
    origin = sketch.originPoint
    for sp in [sketch.sketchPoints.item(i) for i in range(sketch.sketchPoints.count)]:
        if is_hole(sp) or sp == origin:
            continue
        if sp.connectedEntities is None or sp.connectedEntities.count == 0:
            _try(sp.deleteMe)
    if not all(hp.isValid for hp in hole_pts):
        raise RuntimeError('a hole point was lost while cleaning the sketch — CLOSE THIS '
                           'DESIGN WITHOUT SAVING')
    for sp in hole_pts:
        _try(lambda: setattr(sp, 'isFixed', False))

    bottom_line = lb._bottom_datum(sketch, proxy, fr, bottom_src, tokens['bottom'] is not None)
    back_line = lb._face_datum_line(sketch, back_src, fr, fr.height_dir) if back_src else None
    if back_line is None:
        back_line = lb._corner_line(sketch, proxy, fr, front=False)
    front_line = lb._corner_line(sketch, proxy, fr, front=True)
    if bottom_line is None or back_line is None or front_line is None:
        raise RuntimeError('could not build the sketch datums')

    dims = sketch.sketchDimensions
    by_key = {}
    for idx, sp in enumerate(hole_pts):
        k, var, col = tags[idx]
        by_key[(k, var, col)] = sp
        txt = sp.geometry
        h_expr = f'{first}{f" + {k} * {step}" if k else ""}{offs[var]}'
        dims.addOffsetDimension(bottom_line, sp, txt).parameter.expression = h_expr
        datum = back_line if col == 'back' else front_line
        dims.addOffsetDimension(datum, sp, txt).parameter.expression = (
            plan['back_expr'] if col == 'back' else plan['front_expr'])

    if dir_line is not None:
        low, up = by_key.get((0, 'low', 'back')), by_key.get((0, 'up', 'back'))
        gc = sketch.geometricConstraints
        if low is not None and up is not None:
            if dir_line.startSketchPoint != low:
                gc.addCoincident(dir_line.startSketchPoint, low)
            if dir_line.endSketchPoint != up:
                gc.addCoincident(dir_line.endSketchPoint, up)

    lb._name_sketch(sketch, f'LB Holes {suffix}')
    try:
        sketch.attributes.add(REPAIRED_ATTR[0], REPAIRED_ATTR[1], suffix)
    except Exception:
        pass
    fully = sketch.isFullyConstrained
    design.timeline.moveToEnd()
    if tokens['pattern']:
        find(tokens['pattern']).distanceOne.expression = step     # now live

    _remove_old_opening(design, comp, old_names, suffix)

    # --- 3. report -------------------------------------------------------------
    sk = find(tokens['sketch'])
    health = [x.timelineObject.healthState for x in (sk, find(tokens['hole'])) if x]
    if tokens['pattern']:
        health.append(find(tokens['pattern']).timelineObject.healthState)
    rule_n = n_total
    new_mids = boring.set_centers(p['bottom_h'] if p['bottom_h'] is not None else 0.0,
                                  p['top_h'] if p['top_h'] is not None else fr.height,
                                  rule_n, shelf, rise)
    moved = max(abs(a - o) for a, o in zip(new_mids, inf['obs'])) * 10.0
    where = (f'{_owner(inf["bottom"])} -> {_owner(inf["top"])}, back: {_owner(inf["back"], "panel back")}')
    ok = fully and all(h == adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState for h in health)
    layout = ('old layout was hand-placed (matched no rule) and was moved to the tool\'s spacing'
              if inf['rule'].startswith('none') else f'old layout matched the {inf["rule"]} rule')
    return (f'{"" if ok else "CHECK: "}{n_total} set(s), opening {where}; {layout}; '
            f'holes moved up to {moved:.1f} mm; sketch '
            f'{"fully defined" if fully else "NOT fully defined"}')


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _build_opening(design, lb, boring, comp, fr, proxy, suffix, tokens, find, created):
    """Datum planes + the hidden LB Opening sketch, created while the timeline is
    rolled back to just before the hole sketch. Returns the opening parameter's
    name. Every created object is appended to ``created``, tagged for reuse."""
    def src(key, name):
        tok = tokens[key]
        if not tok:
            return None
        before = len(created)
        ent = lb._datum_source(comp, find(tok), fr, f'{name} {suffix}', created)
        if len(created) == before:          # the face itself (it crosses the panel)
            created.append(_Tag(ent, key))
        else:
            created[-1] = _Tag(created[-1], key)
        return ent

    bottom_src = src('bottom', 'LB Bottom')
    top_src = src('top', 'LB Top')
    src('back', 'LB Back')

    open_sk = comp.sketches.add(proxy)
    created.append(open_sk)
    lb._name_sketch(open_sk, f'LB Opening {suffix}')
    lb._clear_auto_projection(open_sk)
    o_bottom = lb._bottom_datum(open_sk, proxy, fr, bottom_src, tokens['bottom'] is not None)
    o_top = lb._top_datum(open_sk, proxy, fr, top_src, tokens['top'] is not None)
    if o_bottom is None or o_top is None:
        raise RuntimeError('could not build the opening datums')
    h_dim = open_sk.sketchDimensions.addOffsetDimension(
        o_bottom, o_top, open_sk.modelToSketchSpace(fr.point(fr.height / 2.0, fr.depth / 2.0)), False)
    hp = h_dim.parameter
    hp.name = lb._unique_param_name(design, f'{boring.PFX}H_{suffix}')
    hp.comment = 'Line boring: clear opening between the bottom and top panels (drives shelf-pin spacing)'
    open_sk.isVisible = False
    return hp.name


class _Tag:
    """A created datum source remembered with its role ('bottom'/'top'/'back');
    deleteMe only removes things this script created (never a picked face)."""
    def __init__(self, ent, role):
        self.ent, self.role = ent, role

    def deleteMe(self):
        if adsk.fusion.ConstructionPlane.cast(self.ent):
            self.ent.deleteMe()


def created_srcs(created, tokens):
    """(bottom source, back source) entities from the tagged ``created`` list."""
    got = {c.role: c.ent for c in created if isinstance(c, _Tag)}
    return got.get('bottom'), got.get('back')


def _try(fn):
    try:
        fn()
    except Exception:
        pass


def _owner(face, none_text='panel end'):
    if face is None:
        return none_text
    try:
        return face.assemblyContext.name if face.assemblyContext else face.body.parentComponent.name
    except Exception:
        return 'face'


def _hole_depth(hole, default):
    try:
        return hole.extentDefinition.distance.value
    except Exception:
        return default


_WC_RE = re.compile(r'\bwc_lb_[A-Za-z0-9_]+\b')


def _wc_names_used(sketch, pat):
    names = set()
    for i in range(sketch.sketchDimensions.count):
        try:
            names.update(_WC_RE.findall(sketch.sketchDimensions.item(i).parameter.expression))
        except Exception:
            pass
    if pat:
        for v in (pat.quantityOne, pat.distanceOne):
            try:
                names.update(_WC_RE.findall(v.expression))
            except Exception:
                pass
    return names


def _param_value(design, sketch, pat, word, default):
    """An existing boring's shelf/rise value (wc_lb_<word>[_suffix]) if it had one."""
    for name in _wc_names_used(sketch, pat):
        if name == f'wc_lb_{word}' or name.startswith(f'wc_lb_{word}_'):
            p = design.allParameters.itemByName(name)
            if p:
                return p.value
    return default


def _remove_old_opening(design, comp, old_names, new_suffix):
    """Remove the replaced boring's own LB Opening sketch and LB planes (only those
    tagged with the OLD parameter suffix — nothing references them any more)."""
    old_suffixes = set()
    for n in old_names:
        for word in ('N', 'H', 'pitch', 'shelf', 'rise', 'front', 'back', 'dia', 'depth'):
            pre = f'wc_lb_{word}_'
            if n.startswith(pre):
                old_suffixes.add(n[len(pre):])
    old_suffixes.discard(new_suffix)
    for suf in old_suffixes:
        for sk in [comp.sketches.item(i) for i in range(comp.sketches.count)]:
            if sk.name == f'LB Opening {suf}':
                _try(sk.deleteMe)
        for pl in [comp.constructionPlanes.item(i) for i in range(comp.constructionPlanes.count)]:
            if pl.name in (f'LB Bottom {suf}', f'LB Top {suf}', f'LB Back {suf}', f'LB Front {suf}'):
                _try(pl.deleteMe)


def _delete_unused_lb_params(design):
    """Drop wc_lb_* user parameters nothing references any more."""
    ups = design.userParameters
    for p in [ups.item(i) for i in range(ups.count)]:
        if not p.name.startswith('wc_lb_'):
            continue
        try:
            if p.dependentParameters.count == 0 and p.isDeletable:
                p.deleteMe()
        except Exception:
            pass
