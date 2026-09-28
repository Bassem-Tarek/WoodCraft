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

"""Shared component collection for WoodCraft output commands (Cut List, BOM, Inspect).

Components are collected by their WoodCraft *category* attribute (config.WC_CATEGORY
— 'panel' or 'hardware'); there is no geometry guessing, so panels and purchased
items never get mixed up. Collection walks the occurrence tree (root.occurrences →
childOccurrences) so it reaches items inside referenced cabinets, records ONE
instance per occurrence (so the piece count is real), and remembers each item's
immediate parent assembly (the cabinet it lives in) so reports can tell apart
same-named panels — every cabinet has a "Left Panel".
"""

import copy
import json
import os
import time

import adsk.core
import adsk.fusion

from .. import config
from . import wc_attrs
from . import entity_memo
from . import nesting
from . import sheets_store
from . import settings_store


def panel_material(component):
    """Native Fusion material name for a panel, or '' if none is set.

    Prefers the material of the component's single body (the usual case for a
    panel — a material dragged onto the body wins over the component default),
    then the component-level material. The Cut List matches panels to stock
    sheets by this name, so it must reflect what the user assigned in Fusion.
    """
    try:
        bodies = component.bRepBodies
        if bodies.count == 1:
            mat = bodies.item(0).material
            if mat:
                return mat.name
    except Exception:
        pass
    try:
        mat = component.material
        if mat:
            return mat.name
    except Exception:
        pass
    return ''


def panel_appearance(component):
    """Name of the Fusion appearance the component displays, or '' if none.

    Mirrors panel_material: a single body's (effective) appearance wins — that is
    what the viewport shows and what a melamine/veneer colour is dragged onto —
    falling back to the appearance bound to the component's physical material.
    The appearance is how decor/colour is told apart when several panels share
    one physical material (e.g. all 'MDF', coloured by appearance)."""
    try:
        bodies = component.bRepBodies
        if bodies.count == 1:
            ap = bodies.item(0).appearance
            if ap:
                return ap.name
    except Exception:
        pass
    try:
        mat = component.material
        if mat and mat.appearance:
            return mat.appearance.name
    except Exception:
        pass
    return ''


def occurrence_appearance(occ, override, fallback):
    """Name of the appearance THIS occurrence displays, or `fallback`.

    panel_appearance() asks the component in its own document, which misses
    occurrence-level overrides — what Fusion records when an appearance is
    dragged onto a cabinet in the browser, and what Appearance Config's themes
    and per-cabinet custom finish paint (occ.appearance = …). The body PROXY
    (occ.bRepBodies) reports the appearance in this occurrence's assembly
    context, resolved through Fusion's own face/body/occurrence/material
    precedence — what the viewport actually shows. `override` is the nearest
    occ.appearance override on the path down from the root (innermost wins),
    carried by the walk for the nodes that have no single body to ask —
    assemblies and multi-body parts."""
    try:
        bodies = occ.bRepBodies
        if bodies.count == 1:
            ap = bodies.item(0).appearance
            if ap:
                return ap.name
    except Exception:
        pass
    return override or fallback


def occurrence_override_name(occ):
    """The occurrence's own appearance override name, or None when unpainted."""
    try:
        ap = occ.appearance
        if ap:
            return ap.name
    except Exception:
        pass
    return None


def _bbox_ext_mm(bb):
    """Bounding-box extents in millimetres, sorted largest first."""
    ext = [(bb.maxPoint.x - bb.minPoint.x) * 10.0,
           (bb.maxPoint.y - bb.minPoint.y) * 10.0,
           (bb.maxPoint.z - bb.minPoint.z) * 10.0]
    ext.sort(reverse=True)
    return ext


def _sheet_metal_dims_mm(component):
    """Unfolded (L, W, T) in millimetres for a sheet-metal component, or None for
    a regular part.

    A bent/curved sheet-metal part wraps around its bounding box, so the box's
    smallest extent is the bend envelope, not the sheet thickness (a curved 18 mm
    panel reads as 780 mm thick). Prefer the flat pattern's body — the true
    unfolded blank, which is also the size the cut list must nest (arc length,
    not chord). When no flat pattern has been created yet, fall back to the
    component's sheet-metal rule for T with the folded bounding box for L/W
    (approximate: the folded extents undersell the blank)."""
    try:
        if not any(b.isSheetMetal for b in component.bRepBodies):
            return None
    except Exception:
        return None
    try:
        fp = component.flatPattern
        if fp:
            ext = _bbox_ext_mm(fp.flatBody.boundingBox)
            return (ext[0], ext[1], ext[2])
    except Exception:
        pass
    try:
        rule = component.activeSheetMetalRule
        if rule:
            ext = _bbox_ext_mm(component.boundingBox)
            return (ext[0], ext[1], rule.thickness.value * 10.0)
    except Exception:
        pass
    return None


def _own_bodies_ext_mm(component):
    """Extents (mm, largest first) of the union of the component's OWN bodies'
    bounding boxes, or None when it has no bodies. Component.boundingBox spans
    child occurrences too, and Fusion computes it by touching every body in the
    whole subtree — seconds per call on an assembly — so measurement must stay
    on the component's own (cheap, precomputed) body boxes."""
    try:
        bodies = component.bRepBodies
        if bodies.count == 0:
            return None
        mins = [float('inf')] * 3
        maxs = [float('-inf')] * 3
        measured = 0
        for i in range(bodies.count):
            body = bodies.item(i)
            # Match Component.boundingBox: hidden bodies (alternate-position /
            # tool leftovers) don't count toward a part's size.
            if not body.isVisible:
                continue
            bb = body.boundingBox
            lo, hi = bb.minPoint, bb.maxPoint
            # A degenerate zero-size body (e.g. the leftover of a failed feature)
            # is a stray point that would inflate the union across the distance
            # to the real body — Fusion's own Component.boundingBox skips these.
            if max(hi.x - lo.x, hi.y - lo.y, hi.z - lo.z) < 1e-3:   # < 0.01 mm
                continue
            measured += 1
            for axis, (lo_v, hi_v) in enumerate(((lo.x, hi.x), (lo.y, hi.y), (lo.z, hi.z))):
                mins[axis] = min(mins[axis], lo_v)
                maxs[axis] = max(maxs[axis], hi_v)
        if not measured:
            return None
        ext = sorted(((maxs[a] - mins[a]) * 10.0 for a in range(3)), reverse=True)
        return ext
    except Exception:
        return None


def panel_dims_mm(component):
    """Sorted (L, W, T) in millimetres, or None. Flat parts measure straight off
    their own bodies' bounding boxes; sheet-metal parts use their flat pattern /
    rule so bent panels report the real sheet thickness and unfolded blank size.
    A component with no bodies of its own (e.g. a priced hardware pack whose
    geometry lives in children) falls back to Component.boundingBox — expensive,
    so callers must not measure plain assemblies (BOM assembly rows skip this)."""
    dims = _sheet_metal_dims_mm(component)
    if dims:
        return dims
    ext = _own_bodies_ext_mm(component)
    if ext is None:
        try:
            ext = _bbox_ext_mm(component.boundingBox)
        except Exception:
            return None
    return (ext[0], ext[1], ext[2])


def assembly_dims_mm(component):
    """(Width, Height, Depth) in millimetres for a cabinet ASSEMBLY, or None.

    A parametric cabinet is inserted as a referenced design whose own user
    parameters carry the cabinet's size — read Width/Height/Depth (case-
    insensitive) from `component.parentDesign.userParameters`. Only trusted when
    `component` IS that design's root component (i.e. the inserted cabinet
    itself): for a local sub-assembly the parentDesign is the whole kitchen, and
    its design-wide parameters would stamp every cabinet with the same size.
    Falls back to Component.boundingBox extents sorted largest-first — expensive
    (walks the whole subtree), so callers should cache per unique component."""
    try:
        owner = component.parentDesign
        if owner.rootComponent == component:
            vals = {}
            for p in owner.userParameters:
                n = p.name.strip().lower()
                if n in ('width', 'height', 'depth') and n not in vals:
                    vals[n] = p.value * 10.0        # internal cm → mm
            if len(vals) == 3:
                return (vals['width'], vals['height'], vals['depth'])
    except Exception:
        pass
    try:
        ext = _bbox_ext_mm(component.boundingBox)
        if ext[0] > 0:
            return (ext[0], ext[1], ext[2])
    except Exception:
        pass
    return None


def _fmt_code_mm(value):
    """A millimetre value as a compact code segment: whole numbers without the
    decimal ('600'), everything else to one decimal ('599.5')."""
    r = round(value, 1)
    return str(int(r)) if r == int(r) else str(r)


def component_surface_area_m2(component):
    """Total surface area of the component's OWN bodies in m² — the sum of
    BRepBody.area (cm² internally, ×1e-4), which is the 'Area' Fusion shows in
    the component's Properties. All faces count: both big faces AND the edges,
    so this is the paint/finish coverage figure, unlike the L × W footprint the
    costing and nesting use. 0.0 when the component has no bodies or a body
    can't be measured (callers fall back to L × W)."""
    total = 0.0
    try:
        bodies = component.bRepBodies
        for i in range(bodies.count):
            try:
                total += bodies.item(i).area
            except Exception:
                pass
    except Exception:
        return 0.0
    return total * 1e-4


def node_code(part_number, width_mm, material, appearance):
    """The user's part-coding string: 'part number-width-material-appearance',
    dash-joined with empty segments skipped (no leading/doubled dashes when a
    piece has no part number or appearance)."""
    segments = [part_number or '',
                _fmt_code_mm(width_mm) if width_mm and width_mm > 0 else '',
                material or '',
                appearance or '']
    return '-'.join(s for s in segments if s)


def looks_like_panel(dims, min_t=3.0, max_t=40.0, min_ratio=4.0):
    """Geometry heuristic: a thin slab whose thickness is in sheet range and far
    smaller than its width/length (so it reads as sheet stock, not hardware).

    NOT used by collection (which is strictly category-driven) — reserved for an
    optional "auto-detect flat panels" helper in the Set Type command."""
    if not dims:
        return False
    L, W, T = dims
    if T < min_t or T > max_t or W <= 0:
        return False
    return (W / T) >= min_ratio


# ---------------------------------------------------------------------------
# Edgebanding (WC_EDGEBAND face attributes → per-component band lengths)
# ---------------------------------------------------------------------------
def face_edgeband_length_mm(face, thickness_mm=None):
    """Banding length ONE face needs, in millimetres: face area ÷ panel thickness.
    Exact for a rectangular edge face (T × L) and arc-true for a curved edge (a
    band follows the surface, so a bent front needs its arc length — the bounding
    box would undersell it). When the thickness is unknown, fall back to the
    face's largest bounding-box extent (right for straight edges only)."""
    try:
        area_mm2 = face.area * 100.0        # Fusion areas are cm²
    except Exception:
        return 0.0
    if thickness_mm and thickness_mm > 0:
        return area_mm2 / thickness_mm
    try:
        bb = face.boundingBox
        return max(bb.maxPoint.x - bb.minPoint.x,
                   bb.maxPoint.y - bb.minPoint.y,
                   bb.maxPoint.z - bb.minPoint.z) * 10.0
    except Exception:
        return 0.0


def component_edgebands(component):
    """{band name: total banding length mm} over this component's tagged faces —
    ONE instance's worth (callers multiply by occurrence quantity). Empty dict
    when nothing is tagged. Reads the WC_EDGEBAND face attributes the Edgeband
    command stamped; lengths divide each face's area by the panel thickness so
    curved edges count their real arc length."""
    dims = panel_dims_mm(component)
    thickness = dims[2] if dims else None
    out = {}
    try:
        bodies = component.bRepBodies
    except Exception:
        return out
    for bi in range(bodies.count):
        try:
            faces = bodies.item(bi).faces
        except Exception:
            continue
        for fi in range(faces.count):
            face = faces.item(fi)
            band = wc_attrs.get_edgeband(face)
            if not band:
                continue
            length = face_edgeband_length_mm(face, thickness)
            if length > 0:
                out[band] = out.get(band, 0.0) + length
    return out


def design_band_faces(design):
    """{component name: [(band name, BRepFace), ...]} for every WC_EDGEBAND-tagged
    face in `design`, from ONE findAttributes sweep — or None when the sweep isn't
    available (the caller then falls back to component_edgebands' per-face scan).

    This inverts the lookup: component_edgebands asks every face of a component
    for its attribute (one API round-trip per face — a bored panel or a hinge has
    hundreds), while findAttributes returns just the tagged faces in one call, so
    untagged components cost nothing. Attributes live in the design that OWNS the
    component, so for referenced (inserted) cabinets call this on the component's
    parentDesign, not the assembly design. Orphaned attributes (face deleted)
    are skipped."""
    out = {}
    try:
        # findAttributes returns a plain list of Attribute (an AttributeVector) —
        # NOT an API collection: it has no .count/.item(), only iteration/len().
        for attr in design.findAttributes(config.WC_GROUP, config.WC_EDGEBAND):
            try:
                face = attr.parent
                if face is None:    # entity deleted; attribute is orphaned
                    continue
                comp_name = face.body.parentComponent.name
                out.setdefault(comp_name, []).append((attr.value, face))
            except Exception:
                continue
    except Exception:
        return None
    return out


def _cylinder_face_is_convex(face):
    """True when a cylindrical face bulges OUTWARD (a rounded panel edge/corner —
    bandable), False when it curves inward (a drilled hole, hinge cup, dowel bore —
    never banded). Convex ⇔ the surface normal points away from the cylinder axis.
    Non-cylindrical faces return True (no opinion)."""
    try:
        cyl = adsk.core.Cylinder.cast(face.geometry)
        if not cyl:
            return True
        pt = face.pointOnFace
        ok, normal = face.evaluator.getNormalAtPoint(pt)
        if not ok:
            return True
        # Radial direction at pt: its offset from the axis, minus the axial part.
        radial = cyl.origin.vectorTo(pt)
        axis = cyl.axis.copy()
        axis.normalize()
        along = axis.copy()
        along.scaleBy(radial.dotProduct(axis))
        radial.subtract(along)
        return radial.dotProduct(normal) >= 0
    except Exception:
        return True


# A face counts as EDGE-LIKE when its strip width (2·area ÷ perimeter — exact
# short side for a long rectangle) is at most this multiple of the panel
# thickness; anything wider is a broad face. 1.6 tolerates bbox slop and
# slightly proud bands while keeping 40 mm rails' broad faces broad.
_EDGE_WIDTH_FACTOR = 1.6


def bandable_faces(component):
    """The faces of `component` that can take edgebanding — its thickness-side
    ('edge') faces, found geometrically per body:

    - Every face gets a strip width = 2·area ÷ perimeter (for a long rectangle
      that IS the short side). Width ≤ 1.6 × panel thickness ⇒ edge-like;
      wider ⇒ a broad face (a slab skin). Width — not top-2-by-area — because a
      curved panel's outer skin is often SPLIT at a surface seam into two faces,
      and picking the two largest would orphan the end strip that only touches
      one half.
    - Bandable = an edge-like face adjacent (shared edge) to at least TWO
      distinct broad faces: the border strip between the skins. Groove/dado
      walls touch at most one skin, so joinery stays out.
    - Drilled holes are rejected two ways: a blind hole touches one skin
      (adjacency fails); a through hole is a concave cylinder
      (_cylinder_face_is_convex fails). A rounded corner/edge is convex and
      stays bandable.

    Returns NATIVE BRepFace objects, in body/face order; [] when nothing fits
    (incl. components with no measurable thickness)."""
    out = []
    dims = panel_dims_mm(component)
    if not dims or dims[2] <= 0:
        return out
    edge_cap_cm = _EDGE_WIDTH_FACTOR * dims[2] / 10.0
    try:
        bodies = component.bRepBodies
    except Exception:
        return out
    for bi in range(bodies.count):
        body = bodies.item(bi)
        faces = [body.faces.item(i) for i in range(body.faces.count)]
        if len(faces) < 3:      # a slab needs 2 broad skins + at least 1 edge
            continue
        broad_ids = set()
        for f in faces:
            try:
                perimeter = sum(f.edges.item(i).length for i in range(f.edges.count))
                if perimeter > 0 and (2.0 * f.area / perimeter) > edge_cap_cm:
                    broad_ids.add(f.tempId)
            except Exception:
                continue
        if len(broad_ids) < 2:  # no two skins — not slab-like (cleats, dowels…)
            continue
        for face in faces:
            if face.tempId in broad_ids:
                continue
            adjacent = set()
            try:
                edges = face.edges
                for ei in range(edges.count):
                    edge_faces = edges.item(ei).faces
                    for fi in range(edge_faces.count):
                        adjacent.add(edge_faces.item(fi).tempId)
            except Exception:
                continue
            adjacent.discard(face.tempId)
            if len(adjacent & broad_ids) < 2:
                continue
            if not _cylinder_face_is_convex(face):
                continue
            out.append(face)
    return out


def _priced_hardware(component):
    """True when this component counts as ONE priced purchased unit whose price
    covers everything inside it: hardware, with its own cost, bought as a pack.
    In 'separate' purchase mode the component is translucent — its parts are
    bought individually, so any stored pack cost is ignored and reports sum the
    children instead."""
    return (wc_attrs.is_hardware(component)
            and wc_attrs.get_cost(component) > 0
            and wc_attrs.get_purchase_mode(component) == config.WC_PURCHASE_PACK)


def _make_instance(occ_or_comp, comp, dims, category, parent='', profile=None):
    L, W, T = dims
    if profile is None:
        profile = {}
    if 'material' not in profile:
        profile['material'] = panel_material(comp)
    if 'cost' not in profile:
        # Only a pack-priced unit bills its own cost; a separate-mode assembly's
        # stored pack price must not ALSO be billed next to its children's.
        priced = profile.get('priced')
        if priced is None:
            priced = _priced_hardware(comp)
        profile['cost'] = wc_attrs.get_cost(comp) if priced else 0.0
    if 'comp_name' not in profile:
        profile['comp_name'] = comp.name
    return {
        'name': getattr(occ_or_comp, 'name', profile['comp_name']),
        'comp_name': profile['comp_name'],
        'parent': parent,
        'category': category,
        'cost': profile['cost'],
        'L': L, 'W': W, 'T': T,
        'material': profile['material'],
        'component': comp,
        'occurrence': occ_or_comp,
    }


def _component_profile(comp):
    """The per-COMPONENT facts the collector needs, read once. A cabinet placed
    ten times shares its components, so every occurrence after the first reuses
    these instead of re-reading attributes and re-measuring bodies."""
    return {'category': wc_attrs.get_category(comp),
            'priced': _priced_hardware(comp)}


def design_panel_materials(design, instances=None):
    """Sorted, distinct Fusion material names found on panels in `design` — the
    exact strings Cut List matches against. Reused by the Sheets palette (to offer
    real names) and Cut List. Empty list if no design / none found."""
    if design is None:
        return []
    found = set()
    try:
        for it in (instances if instances is not None else collect_panel_instances(design)):
            mat = (it.get('material') or '').strip()
            if mat:
                found.add(mat)
    except Exception:
        pass
    return sorted(found)


def design_panel_groups(design, instances=None):
    """[{'material','thickness','count'}] for the design's panels, grouped by
    (material name, thickness mm). Lets the Sheets palette show/offer the exact
    (name, thickness) combinations present in the design. Sorted by name, then
    thickness descending."""
    if design is None:
        return []
    groups = {}
    try:
        for it in (instances if instances is not None else collect_panel_instances(design)):
            mat = (it.get('material') or '').strip() or 'Unassigned'
            t = round(it['T'], 1)
            key = (mat, t)
            groups[key] = groups.get(key, 0) + 1
    except Exception:
        pass
    out = [{'material': k[0], 'thickness': k[1], 'count': v} for k, v in groups.items()]
    out.sort(key=lambda g: (g['material'].lower(), -g['thickness']))
    return out


def collect_instances(design, root=None, categories=None, root_name=''):
    """List of classified-component instances (one dict per physical piece /
    occurrence) under `root` (defaults to the whole design). Each dict has
    name/comp_name/parent/category/cost/L/W/T (mm)/material/component/occurrence.

    A component is included ONLY if it carries a WoodCraft category — no geometry
    guessing — so panels and purchased items stay cleanly separated. `categories`
    optionally restricts the result to an iterable of category values (e.g.
    {config.WC_CAT_PANEL}); None returns every classified component.

    `parent` is the name of the item's immediate parent assembly component (the
    cabinet it belongs to), or '' for an item at the top level. The tree is walked
    explicitly (root.occurrences → childOccurrences) rather than via the flattened
    allOccurrences so each item can be attributed to its real parent regardless of
    the assembly context. `root_name` seeds the parent for `root`'s own children —
    pass a scoped occurrence's component name so items collected under a single
    selected cabinet are still labelled with that cabinet."""
    if design is None:
        return []
    root = root or design.rootComponent
    wanted = set(categories) if categories else None
    instances = []
    memo = entity_memo.EntityMemo()

    def consider(owner, comp, parent, profile):
        category = profile['category']
        if category is None or (wanted is not None and category not in wanted):
            return
        if 'dims' not in profile:
            profile['dims'] = panel_dims_mm(comp)
        dims = profile['dims']
        if dims is None:
            # A sheet good (panel or worktop) needs a measurable size; a purchased
            # item is still counted.
            if category in config.WC_SHEET_LIKE:
                return
            dims = (0.0, 0.0, 0.0)
        instances.append(_make_instance(owner, comp, dims, category, parent, profile))

    def walk(occ, parent):
        comp = occ.component
        profile = memo.get(comp, _component_profile)
        consider(occ, comp, parent, profile)
        # A hardware component with its OWN price is a purchased unit: whatever
        # is inside it is already covered by that price, so descending would
        # double-count its children (e.g. a Minifix assembly priced as a whole
        # vs. its screw + cam priced individually — only one level may count).
        if profile['priced']:
            return
        # Descend with THIS occurrence's component name as its children's parent.
        try:
            children = occ.childOccurrences
        except Exception:
            return
        if 'comp_name' not in profile:
            profile['comp_name'] = comp.name
        child_parent = profile['comp_name']
        for i in range(children.count):
            walk(children.item(i), child_parent)

    # The root component itself — covers scope set to a single leaf item. (For the
    # whole design the root is the assembly, which is unclassified, so this is a
    # no-op there.)
    root_profile = _component_profile(root)
    consider(root, root, root_name, root_profile)

    if not root_profile['priced']:
        occs = root.occurrences
        for i in range(occs.count):
            walk(occs.item(i), root_name)

    return instances


def collect_panel_instances(design, root=None, root_name=''):
    """Panels only — thin wrapper over collect_instances() for the cut list / nest
    and other panel-centric callers.

    Strictly WC_CAT_PANEL, so COUNTERTOPS are deliberately excluded: a worktop is
    bought as a slab or a cut length, not nested out of a stock sheet, and letting
    one into the nest would demand a 40 mm "sheet" in the library and blow the
    yield figures. Countertops still reach the BOM through collect_instances()."""
    return collect_instances(design, root=root, categories={config.WC_CAT_PANEL},
                             root_name=root_name)


# ---------------------------------------------------------------------------
# Shared grouping / labelling (used by Cut List and BOM)
# ---------------------------------------------------------------------------
def instance_label(it):
    """Item name qualified by its parent assembly (cabinet), e.g.
    'Base Cabinet / Left Panel', so identical names across cabinets stay distinct.
    Falls back to the bare name for items at the top level (no parent)."""
    parent = (it.get('parent') or '').strip()
    name = it.get('comp_name') or ''
    return f'{parent} / {name}' if parent else name


def group_by_material_thickness(instances):
    """Ordered list of {key, material, thickness, items} grouped by (material name,
    thickness mm). 'Unassigned' stands in for items with no Fusion material. Sorted
    by material name, then thickness descending."""
    groups = {}
    order = []
    for it in instances:
        material = (it.get('material') or '').strip() or 'Unassigned'
        t = round(it['T'], 1)
        key = (material.lower(), t)
        if key not in groups:
            groups[key] = {'key': key, 'material': material, 'thickness': t, 'items': []}
            order.append(key)
        groups[key]['items'].append(it)
    order.sort(key=lambda k: (k[0], -k[1]))
    return [groups[k] for k in order]


# ---------------------------------------------------------------------------
# Hierarchical BOM tree (used by the BOM palette / Excel export)
# ---------------------------------------------------------------------------
# Component.partNumber on a cloud-referenced component is a BLOCKING data-service
# fetch — measured ~0.6–1 s per referenced component even when it SUCCEEDS, and
# ~30 s + RuntimeError with a bad connection. A kitchen has hundreds of unique
# components, so uncapped reads freeze the BOM for minutes. Strategy:
#   - CACHE every value read (keyed like proto_cache) for the whole session,
#     re-reading only after a TTL so an edited part number still shows up.
#   - Spend at most a fixed BUDGET of wall-clock per build on new reads; the
#     rest come back cached-or-blank and fill in on the next build/Refresh
#     (progressive fill beats an all-or-nothing freeze).
#   - A RAISED read means the service is unreachable — stop asking for a while.
# A stale cached value is always preferred over blank.
_PART_NUMBER_RETRY_AFTER = 0.0
_PART_NUMBER_COOLDOWN_S = 300.0
_PART_NUMBER_TTL_S = 600.0
_PART_NUMBER_BUDGET_S = 8.0
_PART_NUMBER_REVALIDATE_S = 2.0     # budget slice for stale-but-known values
_PART_NUMBER_SPENT = 0.0        # this build's read time; reset by build_tree
_part_number_cache = {}         # comp key -> (value, read_at)
_part_number_cache_loaded = False


# The cache is persisted next to the sheet library (part_numbers.json) so a new
# Fusion session starts with every previously seen part number instead of
# re-paying ~0.8 s per component: stale values display immediately and are
# re-validated gradually (TTL + budget above). Keys flatten the (root design
# name, component name) tuple with a unit separator.
def _pn_cache_path():
    return os.path.join(sheets_store.library_dir(), 'part_numbers.json')


def _pn_cache_load():
    global _part_number_cache_loaded
    if _part_number_cache_loaded:
        return
    _part_number_cache_loaded = True
    try:
        with open(_pn_cache_path(), 'r', encoding='utf-8') as f:
            raw = json.load(f)
        for flat_key, entry in raw.items():
            root, sep, name = flat_key.partition('')
            if sep and isinstance(entry, list) and len(entry) == 2:
                _part_number_cache[(root, name)] = (str(entry[0]), float(entry[1]))
    except Exception:
        pass


def _pn_cache_save():
    try:
        os.makedirs(sheets_store.library_dir(), exist_ok=True)
        raw = {f'{k[0]}{k[1]}': [v, ts]
               for k, (v, ts) in _part_number_cache.items()}
        with open(_pn_cache_path(), 'w', encoding='utf-8') as f:
            json.dump(raw, f)
    except Exception:
        pass


def _component_part_number(component, key=None):
    """Native Fusion component part number (read/write property), or the best
    known value: fresh cache hit → cached; over budget / in failure cooldown /
    offline → last cached value (possibly stale) or ''.

    UNKNOWN components may use the full read budget (their value is missing
    outright); STALE-but-known ones only the smaller revalidation slice — so a
    routine refresh with a fully-populated cache costs at most ~2 s extra while
    an edited part number still catches up within the TTL."""
    global _PART_NUMBER_RETRY_AFTER, _PART_NUMBER_SPENT
    now = time.time()
    cached = _part_number_cache.get(key) if key else None
    if cached and (now - cached[1]) < _PART_NUMBER_TTL_S:
        return cached[0]
    fallback = cached[0] if cached else ''
    budget = _PART_NUMBER_REVALIDATE_S if cached else _PART_NUMBER_BUDGET_S
    if now < _PART_NUMBER_RETRY_AFTER or _PART_NUMBER_SPENT >= budget:
        return fallback
    try:
        if adsk.core.Application.get().isOffLine:
            return fallback
    except Exception:
        pass
    started = time.time()
    try:
        value = component.partNumber or ''
        _PART_NUMBER_SPENT += time.time() - started
        if key:
            _part_number_cache[key] = (value, time.time())
        return value
    except Exception:
        _PART_NUMBER_SPENT += time.time() - started
        _PART_NUMBER_RETRY_AFTER = time.time() + _PART_NUMBER_COOLDOWN_S
        return fallback


def _node_type(component, has_children):
    """BOM type label from the WoodCraft category, falling back to the structure:
    a component with children is an Assembly, an unclassified leaf is a Part."""
    category = wc_attrs.get_category(component)
    if category == config.WC_CAT_PANEL:
        return 'Panel'
    if category == config.WC_CAT_HARDWARE:
        return 'Hardware'
    if category == config.WC_CAT_COUNTERTOP:
        return 'Countertop'
    return 'Assembly' if has_children else 'Part'


# BOM row types costed by area from the Sheets library — the node-level mirror of
# config.WC_SHEET_LIKE, since the cost walk sees `type` labels, not categories.
SHEET_LIKE_TYPES = ('Panel', 'Countertop')


def build_tree(design, root=None):
    """Hierarchical bill of materials. Returns a list of top-level nodes; each node:
    {name, type, material, appearance, part_number, code, L, W, T (mm), qty,
     surface_m2, unit_cost, cost, cost_kind, children:[...]}. Leaf dims are sorted
    extents; surface_m2 is Fusion's Properties 'Area' (all faces, sheet-like
    leaves only — the paint-coverage figure); Assembly dims are the cabinet's
    Width × Height × Depth (see assembly_dims_mm).
    `code` is the part-coding string (see node_code).

    Walks the occurrence tree (occurrences -> childOccurrences) so the structure
    mirrors the browser, and groups identical sibling components into ONE node whose
    `qty` is the count within that parent (standard indented-BOM quantities). Every
    component is included — assemblies (cabinets), classified panels/hardware, and
    unclassified parts alike — so the structure is complete regardless of tagging.

    `appearance` is resolved PER OCCURRENCE, in assembly context — so a finish
    dragged onto one cabinet in the browser, or painted by Appearance Config's
    themes / per-cabinet custom finish (all occurrence-level overrides), shows
    up in the BOM exactly as the viewport displays it. Two instances of the
    same cabinet wearing different finishes split into separate qty rows.

    Each node also carries `edgebands`: [{name, length_mm, cost_per_m, cost}] for
    ONE instance — the summed lengths of its WC_EDGEBAND-tagged faces, priced from
    the Sheets library's band catalogue (cost_per_m/cost None when the band is
    unpriced or no longer in the library). Empty list when nothing is tagged.

    Costing (`unit_cost` = one instance, `cost` = unit_cost × qty, both None when
    unpriced; `cost_kind` says where the number came from):
      'set'      hardware with its own WC_COST — a purchased unit. Its descendants
                 are re-marked 'absorbed' (cost None): the parent price covers them,
                 counting both would double-bill (e.g. a priced Minifix assembly
                 vs. its individually-priced screw + cam).
      'est'      panel — raw area × the sheet library's average cost/m² for its
                 (material, thickness), plus the global waste factor (Settings).
      'rollup'   assembly / unpriced hardware — the sum of its children's costs.
      'absorbed' inside a priced purchased unit (unit_cost kept for reference).
      None       nothing priced anywhere below."""
    if design is None:
        return []
    root = root or design.rootComponent

    # Fresh part-number read budget for this build (values cached across builds
    # and sessions — see _pn_cache_load/save).
    global _PART_NUMBER_SPENT
    _PART_NUMBER_SPENT = 0.0
    _pn_cache_load()
    pn_cache_size = len(_part_number_cache)

    library = sheets_store.load()
    materials = library['materials']
    band_catalogue = library['edgebands']
    waste_mult = 1.0 + settings_store.get_waste_percent() / 100.0
    rate_cache = {}   # (material lower, thickness rounded) -> rate or None

    def rate_for(material_name, thickness):
        key = (str(material_name).strip().lower(), round(thickness, 1))
        if key not in rate_cache:
            m = sheets_store.find_material(materials, material_name, thickness)
            rate_cache[key] = sheets_store.cost_rate_per_m2(m)
        return rate_cache[key]

    # One findAttributes sweep per distinct owning design (the assembly itself,
    # plus each referenced cabinet's source design) replaces the old per-face
    # attribute scan — the scan asked EVERY face of every component for its tag
    # (a line-bored panel or a hinge is hundreds of API calls), which is what
    # made the BOM crawl on multi-cabinet kitchens. Keyed like proto_cache, by
    # the owning design's root-component name. A design whose sweep failed maps
    # to None → those components fall back to the per-face scan.
    band_face_cache = {}

    def banded_faces_for(component):
        """[(band name, face), ...] for `component`'s tagged faces via the sweep,
        [] when untagged, or None when the sweep isn't available for its design."""
        try:
            owner = component.parentDesign
            dkey = owner.rootComponent.name
        except Exception:
            return None
        if dkey not in band_face_cache:
            band_face_cache[dkey] = design_band_faces(owner)
        faces_by_comp = band_face_cache[dkey]
        if faces_by_comp is None:
            return None
        return faces_by_comp.get(component.name, [])

    def edgeband_rows(component, thickness):
        """[{name, length_mm, cost_per_m, cost}] for ONE instance of `component`,
        priced from the band catalogue. The waste factor applies like it does to
        panels (banding has offcuts at every edge)."""
        rows = []
        banded = banded_faces_for(component)
        if banded is None:
            lengths = component_edgebands(component)
        else:
            lengths = {}
            for name, face in banded:
                length = face_edgeband_length_mm(face, thickness)
                if length > 0:
                    lengths[name] = lengths.get(name, 0.0) + length
        for name, length_mm in sorted(lengths.items()):
            rate = sheets_store.edgeband_cost_per_m(
                sheets_store.find_edgeband(band_catalogue, name))
            cost = length_mm / 1000.0 * rate * waste_mult if rate is not None else None
            rows.append({'name': name, 'length_mm': round(length_mm, 1),
                         'cost_per_m': rate, 'cost': cost})
        return rows

    def absorb(nodes):
        for n in nodes:
            n['cost'] = None
            n['cost_kind'] = 'absorbed'
            absorb(n['children'])

    def cost_for(component, node, children):
        """(unit_cost, cost_kind) for one instance of `component`."""
        if _priced_hardware(component):
            absorb(children)
            return wc_attrs.get_cost(component), 'set'
        if node['type'] in SHEET_LIKE_TYPES:
            rate = rate_for(node['material'], node['T'])
            if rate is None or node['L'] <= 0:
                return None, None
            area_m2 = node['L'] * node['W'] / 1e6
            return area_m2 * rate * waste_mult, 'est'
        rolled = [c['cost'] for c in children if c['cost'] is not None]
        if rolled:
            return sum(rolled), 'rollup'
        return None, None

    # One leaf-data build per unique component: the same cabinet dropped in 10
    # times used to re-measure its dims / materials / per-face edgeband
    # attributes 10 times over, which is what froze Fusion on big models. Keyed
    # by (source document, component name) — component names are unique within
    # a document, but two inserted library cabinets can each contain a "Left
    # Side". Appearance is deliberately NOT trusted from this cache: a dragged-
    # on finish or Appearance Config's custom finish is an occurrence-level
    # override, so the same component can display a different appearance per
    # instance — nodes are therefore built per OCCURRENCE (cheap: cache hit +
    # one proxy appearance read), and identical siblings regroup afterwards.
    proto_cache = {}

    def _comp_key(component):
        # parentDesign.rootComponent stays in memory; Document-level properties
        # can force a (network) load of a referenced design, so avoid them here.
        try:
            return (component.parentDesign.rootComponent.name, component.name)
        except Exception:
            return None

    def build_proto(component, key=None):
        """The occurrence-independent (and expensive) fields of a node —
        everything but children, appearance, code and costs."""
        has_children = False
        try:
            has_children = component.occurrences.count > 0
        except Exception:
            pass
        ntype = _node_type(component, has_children)
        if ntype == 'Assembly':
            # A cabinet's size comes from its design's Width/Height/Depth user
            # parameters (bounding box as a last resort), shown as W × H × D —
            # not sorted extents. No banding on assemblies.
            dims = assembly_dims_mm(component) or (0.0, 0.0, 0.0)
            bands = []
        else:
            dims = panel_dims_mm(component) or (0.0, 0.0, 0.0)
            bands = edgeband_rows(component, dims[2] if dims[2] > 0 else None)
        return {
            'name': component.name,
            'type': ntype,
            'material': panel_material(component),
            'appearance': panel_appearance(component),
            'part_number': _component_part_number(component, key),
            'L': dims[0], 'W': dims[1], 'T': dims[2],
            'qty': 1,
            'edgebands': bands,
            # Fusion's Properties 'Area' (all faces) — what the appearance/paint
            # coverage figures use. Only measured on sheet-like leaves.
            'surface_m2': (component_surface_area_m2(component)
                           if ntype in SHEET_LIKE_TYPES else 0.0),
        }

    def proto_for(component):
        key = _comp_key(component)
        proto = proto_cache.get(key) if key else None
        if proto is None:
            proto = build_proto(component, key)
            if key:
                proto_cache[key] = proto
        return proto

    def finish_node(node, component):
        # The coding string's width: a cabinet's is its Width parameter (the
        # first dims slot); a panel's is its W (second-largest extent).
        node['code'] = node_code(node['part_number'],
                                 node['L'] if node['type'] == 'Assembly' else node['W'],
                                 node['material'], node['appearance'])
        unit, kind = cost_for(component, node, node['children'])
        node['unit_cost'] = unit
        node['cost'] = unit
        node['cost_kind'] = kind
        return node

    def node_for_occ(occ, component, override):
        node = copy.deepcopy(proto_for(component))
        node['children'] = build_level(occ.childOccurrences, override)
        # Only sheet-like leaves (Panel/Countertop) get the occurrence-resolved
        # appearance — that's what a dragged-on finish or Appearance Config
        # paints. Hardware is usually multi-body (proxy read falls through to
        # `override`), and most hardware has no override of its own, so it
        # would otherwise inherit whatever cabinet-paint override sits above
        # it — Fusion's real rendering behaviour, but meaningless for a screw
        # or hinge and it was fragmenting hardware rows across cabinets that
        # merely differ in ambient paint. Assemblies keep their own (constant)
        # reading too — nothing paints a cabinet row itself.
        if node['type'] in SHEET_LIKE_TYPES:
            node['appearance'] = occurrence_appearance(occ, override,
                                                       node['appearance'])
        return finish_node(node, component)

    def _occ_component(occ):
        # Occurrence.component can transiently throw InternalValidationError.
        for _ in range(3):
            try:
                return occ.component
            except Exception:
                pass
        return None

    def _app_sig(node):
        """Displayed-appearance fingerprint of a node's subtree — what tells
        apart two instances of the same cabinet wearing different finishes."""
        return (node['appearance'],
                tuple(_app_sig(c) for c in node['children']))

    def build_level(occ_collection, override=None):
        # Aggregate identical sibling components into one node carrying a
        # quantity, using component IDENTITY (==) — the way Fusion's own
        # ExtractBOM sample does (entityToken is NOT a reliable grouping key:
        # distinct components can collide on it, which dropped a sibling) —
        # but only while the instances LOOK identical too: appearance resolves
        # per occurrence, so the same cabinet painted two ways yields two rows.
        # `override` is the nearest ancestor occ.appearance on this path.
        groups = []   # [{'comp', 'node', 'sig', 'qty'}], in first-seen order
        for i in range(occ_collection.count):
            occ = occ_collection.item(i)
            comp = _occ_component(occ)
            if comp is None:
                continue
            node = node_for_occ(occ, comp,
                                occurrence_override_name(occ) or override)
            sig = _app_sig(node)
            for g in groups:
                if g['comp'] == comp and g['sig'] == sig:
                    g['qty'] += 1
                    break
            else:
                groups.append({'comp': comp, 'node': node, 'sig': sig, 'qty': 1})
        nodes = []
        for g in groups:
            node = g['node']
            node['qty'] = g['qty']
            node['cost'] = (node['unit_cost'] * g['qty']
                            if node['unit_cost'] is not None else None)
            nodes.append(node)
        return nodes

    nodes = build_level(root.occurrences)
    # A PART document (or a scope narrowed to a leaf) has no occurrences to walk —
    # the root component IS the item. Emit it as the single node so a lone dowel
    # or bracket still gets a BOM. Assemblies keep the root out (its children are
    # the top-level rows), same as before. No occurrence exists for a root, so
    # its appearance is the component's own — nothing can override it.
    if not nodes:
        has_content = False
        try:
            has_content = root.bRepBodies.count > 0
        except Exception:
            pass
        if has_content or wc_attrs.get_category(root):
            node = copy.deepcopy(proto_for(root))
            node['children'] = []
            nodes = [finish_node(node, root)]
    _number_tree(nodes)
    # Persist any part numbers this build managed to read (or re-validate) so
    # the next session starts warm instead of re-fetching from the data service.
    if len(_part_number_cache) != pn_cache_size or _PART_NUMBER_SPENT > 0:
        _pn_cache_save()
    return nodes


def _number_tree(nodes, prefix=''):
    """Stamp standard indented-BOM item numbers on every node ('no'): top level
    1, 2, 3…; children 1.1, 1.2…; grandchildren 1.1.1… Position-derived, so the
    palette and the Excel export always agree."""
    for i, n in enumerate(nodes, 1):
        n['no'] = f'{prefix}.{i}' if prefix else str(i)
        _number_tree(n['children'], n['no'])


def estimate_panel_unit_cost(material_name, thickness, L_mm, W_mm,
                             materials=None, waste_mult=None):
    """Sheet-derived estimated cost of ONE panel — raw area × the material's
    average sheet cost/m² × the waste factor — or None when the Sheets library
    has no priced sheet for (material_name, thickness). Pass `materials` /
    `waste_mult` when calling in a loop to avoid re-reading the stores."""
    if materials is None:
        materials = sheets_store.load()['materials']
    if waste_mult is None:
        waste_mult = 1.0 + settings_store.get_waste_percent() / 100.0
    rate = sheets_store.cost_rate_per_m2(
        sheets_store.find_material(materials, material_name, thickness))
    if rate is None or L_mm <= 0:
        return None
    return L_mm * W_mm / 1e6 * rate * waste_mult


def tree_cost_totals(nodes):
    """Bill split for a build_tree() result:
    {'panels_est', 'hardware', 'edgeband', 'grand', 'unpriced_panels',
     'edgebands': [{name, length_mm, cost|None}],
     'appearances': [{name, area_m2, count}]} — panels_est is the sheet-derived
    estimate, hardware the entered purchase costs, edgeband the priced banding,
    grand their sum; unpriced_panels counts physical panels no rate could be found
    for (so a low total can't silently mean 'panels missing from the bill'). The
    edgebands list totals every tagged band BY TYPE across the design — an unpriced
    band still reports its length with cost None, so the metres to buy are always
    complete even when the library has no price yet. The appearances list totals
    the ACTUAL surface area of every sheet-like piece BY APPEARANCE across the
    design (node surface_m2 — Fusion's Properties 'Area', all faces including
    edges, so it sizes paint/finish coverage; L × W is the fallback when a body
    couldn't be measured), with `count` the number of pieces.

    Node costs are relative to ONE instance of their parent, so the walk carries
    the multiplier of enclosing quantities (2 cabinets × 4 screws = 8 screws)."""
    totals = {'panels_est': 0.0, 'hardware': 0.0, 'edgeband': 0.0, 'grand': 0.0,
              'unpriced_panels': 0, 'edgebands': [], 'appearances': []}
    bands = {}   # name -> {'name', 'length_mm', 'cost', 'priced'}
    apps = {}    # appearance name -> {'name', 'area_m2', 'count'}

    def walk(ns, mult):
        for n in ns:
            eff_qty = mult * n['qty']
            kind = n.get('cost_kind')
            if kind == 'set':
                totals['hardware'] += n['unit_cost'] * eff_qty
                continue    # descendants (and their banding) are in this price
            for b in n.get('edgebands') or []:
                agg = bands.setdefault(b['name'], {'name': b['name'], 'length_mm': 0.0,
                                                   'cost': 0.0, 'priced': True})
                agg['length_mm'] += b['length_mm'] * eff_qty
                if b['cost'] is not None:
                    agg['cost'] += b['cost'] * eff_qty
                else:
                    agg['priced'] = False
            if n['type'] in SHEET_LIKE_TYPES and n.get('appearance'):
                area = n.get('surface_m2') or 0.0
                if area <= 0 and n['L'] > 0:
                    area = n['L'] * n['W'] / 1e6
                if area > 0:
                    agg = apps.setdefault(n['appearance'],
                                          {'name': n['appearance'], 'area_m2': 0.0,
                                           'count': 0})
                    agg['area_m2'] += area * eff_qty
                    agg['count'] += eff_qty
            if kind == 'est':
                totals['panels_est'] += n['unit_cost'] * eff_qty
            elif n['type'] in SHEET_LIKE_TYPES and kind is None:
                totals['unpriced_panels'] += eff_qty
            walk(n['children'], eff_qty)

    walk(nodes, 1)
    for name in sorted(bands, key=str.lower):
        agg = bands[name]
        cost = agg['cost'] if agg['priced'] else None
        totals['edgebands'].append({'name': name,
                                    'length_mm': round(agg['length_mm'], 1),
                                    'cost': cost})
        if cost:
            totals['edgeband'] += cost
    for name in sorted(apps, key=str.lower):
        agg = apps[name]
        totals['appearances'].append({'name': name,
                                      'area_m2': round(agg['area_m2'], 2),
                                      'count': agg['count']})
    totals['grand'] = totals['panels_est'] + totals['hardware'] + totals['edgeband']
    return totals


def tree_sheet_counts(nodes):
    """Stock sheets to buy per (material, thickness) — the Cut List's nesting run
    over a build_tree() result's Panel pieces. Walks the tree with enclosing
    quantities multiplied through (2 cabinets × 2 sides = 4 rects), groups like
    group_by_material_thickness, and packs each group onto its material's
    primary stock sheet (the Cut List's default choice) with that sheet's
    gap / trim / rotation via nesting.pack — so the count matches a Cut List
    report run at its defaults. Countertops are excluded, same as the Cut List
    (bought as slabs, not nested out of stock sheets).

    Returns [{material, thickness, pieces, matched, num_sheets, sheet_length,
    sheet_width, unplaced, cost}] sorted by material name then thickness
    descending. An unmatched group (no stock material/sheet in the Sheets
    library) still reports its piece count with num_sheets 0, so missing stock
    is visible rather than silently dropped. cost is num_sheets × the sheet's
    purchase cost (0 when unpriced) — informational; the billed BOM keeps its
    area-based panel estimate."""
    groups = {}
    order = []

    def walk(ns, mult):
        for n in ns:
            eff = mult * n['qty']
            if n['type'] == 'Panel' and n['L'] > 0:
                material = (n.get('material') or '').strip() or 'Unassigned'
                t = round(n['T'], 1)
                key = (material.lower(), t)
                if key not in groups:
                    groups[key] = {'material': material, 'thickness': t, 'rects': []}
                    order.append(key)
                g = groups[key]
                for _ in range(int(eff)):
                    g['rects'].append({'id': len(g['rects']), 'label': n['name'],
                                       'w': n['L'], 'h': n['W']})
            walk(n['children'], eff)

    walk(nodes, 1)
    order.sort(key=lambda k: (k[0], -k[1]))

    materials = sheets_store.load()['materials']
    out = []
    for key in order:
        g = groups[key]
        mat = sheets_store.find_material(materials, g['material'], g['thickness'])
        stock = (mat.get('sheets') or []) if mat else []
        sheet = stock[0] if stock else None
        row = {'material': g['material'], 'thickness': g['thickness'],
               'pieces': len(g['rects']), 'matched': sheet is not None,
               'num_sheets': 0, 'sheet_length': 0.0, 'sheet_width': 0.0,
               'unplaced': 0, 'cost': 0.0}
        if sheet:
            gap = max(0.0, float(sheet.get('separation') or 0.0))
            trim = max(0.0, float(sheet.get('trim') or 0.0))
            allow_rot = sheets_store.rotation_allows_rotation(sheet.get('rotation'))
            result = nesting.pack(g['rects'], sheet['length'], sheet['width'],
                                  gap, trim, allow_rot)
            row.update({'num_sheets': len(result['sheets']),
                        'sheet_length': sheet['length'],
                        'sheet_width': sheet['width'],
                        'unplaced': len(result['unplaced']),
                        'cost': (len(result['sheets'])
                                 * float(sheet.get('cost', 0.0) or 0.0))})
        out.append(row)
    return out


def flatten_tree(nodes, level=0):
    """Depth-first (node, level) pairs from build_tree() output — for tabular export
    (Excel outline levels) and flat rendering."""
    out = []
    for n in nodes:
        out.append((n, level))
        out.extend(flatten_tree(n['children'], level + 1))
    return out
