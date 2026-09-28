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

"""Engine for the Appearance Config command: scan, classify, build, verify.

Ports a workflow proven by hand on 7+ cabinets and an 18-cabinet kitchen
(4864 cells) into add-in code. The moving parts:

  scan()               walk the tree, classify every candidate occurrence
                       (attribute role → category → name keywords), descend
                       zero-body wrappers, apply user overrides, resolve
                       'front' roles by the single-door rule. Read-only.
  appearance_status()  which scheme appearances the design already has and
                       where the missing ones could come from. Read-only.
  apply_plan()         the builder AND the fix pass (one code path — the sweep
                       is the builder: it adds whatever columns/rows are
                       missing and fills whatever cells are empty or wrong).
  verify()             read-only cell sweep → empty / wrong counts.
  preview()/restore()  point the active configuration at a chosen appearance
                       row for a visual check, then put it back.
  open_source_document()  last-resort cloud fetch of the appearance source
                       doc, with a time-budget breaker (cloud calls BLOCK when
                       offline — same trap as Component.partNumber).

Rules baked in (do not renegotiate — they encode real Fusion API bugs and
user decisions):
  * The ROOT cell of every row stays EMPTY in an assembly: the root is the
    whole kitchen, and an appearance there tints appliances, sink, window
    glass and the room shell.
  * Never create or touch a table named like 'Shelves' (suppress columns are
    UI-only; an API-built Shelves table is always incomplete).
  * Appearances are matched by their LEADING CODE, never the full name
    (library copies are UPPERCASE and one is mojibaked).
  * Column titles are leaf occurrence names and repeat across cabinets, so
    the column↔occurrence mapping is persisted as a JSON attribute on the
    root component (config.WC_APPEARANCE_COLS) and re-derived by ordered
    alignment for tables built before this command existed.
"""

import json
import os
import re
import time

import adsk
import adsk.core
import adsk.fusion

from . import config_tables_store
from . import wc_attrs
from .. import config

ROLE_DOOR = config.WC_ROLE_DOOR
ROLE_FRONT = config.WC_ROLE_FRONT
ROLE_CARCASS = config.WC_ROLE_CARCASS
ROLE_SKIP = config.WC_ROLE_SKIP
# col_info-only marker: a column whose part was DELIBERATELY excluded — the
# builder empties its cells and deletes it so the part stops following the
# theme (it keeps its current look; the original appearance is gone anyway).
ROLE_RETIRED = 'retired'

# How many bodies a child-less top-level occurrence may have before it is
# flagged as a FLATTENED cabinet (one solid per part but no occurrence tree —
# cannot be split into door vs carcass by columns).
FLATTENED_BODY_COUNT = 4

_WRAPPER_DEPTH = 3        # zero-body wrapper descent limit (Door:1 → Right Door:1)
PATH_SEP = ' > '

# An exclude keyword only fires on SMALL top-level occurrences. 'BC_S2_SINK:1'
# is a cabinet that HOLDS a sink (36+ children), not a sink; real appliances
# and trim measure tiny ('kitchen sink:1' has 2 children, the stove 1, fillers
# 0) — verified against the VILLA-C kitchen. Anything with a real occurrence
# tree gets classified, whatever its name says.
EXCLUDE_MAX_CHILDREN = 5


def key_of(path):
    return PATH_SEP.join(path)


def _comp(occ):
    """occ.component, or None when the proxy is broken — the getter itself can
    raise InternalValidationError on damaged/unresolved occurrences (seen live
    on a real kitchen). Callers fall back to name-keyword classification."""
    try:
        return occ.component
    except Exception:
        return None


# Two DIFFERENT components that share a name produce occurrences with the
# IDENTICAL name ('Custom 1:1' twice — Fusion numbers instances per component,
# not per name; seen live on a real kitchen). Everything keyed by occurrence
# name breaks: grouping merges the two cabinets, itemByName always returns the
# first one. So sibling duplicates get a ' #N' display suffix throughout the
# plan, and lookups strip it / count through matches.
_DUP_RE = re.compile(r'^(.*) #(\d+)$')


def _base(name):
    """The raw occurrence name behind a display name ('Custom 1:1 #2' → 'Custom 1:1')."""
    m = _DUP_RE.match(str(name))
    return m.group(1) if m else str(name)


def _unique_names(collection):
    """[(occ, display_name)] for a sibling collection — first holder of a name
    keeps it bare, later different-component twins get ' #2', ' #3', …"""
    counts, out = {}, []
    try:
        for occ in collection:
            n = occ.name
            counts[n] = counts.get(n, 0) + 1
            out.append((occ, n if counts[n] == 1 else f'{n} #{counts[n]}'))
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Appearance lookup / sourcing
# ---------------------------------------------------------------------------
def find_appearance(container, code):
    """First appearance in `container` (design.appearances or a library's
    .appearances) whose name starts with `code`, case-insensitive, or None."""
    want = str(code or '').strip().lower()
    if not want:
        return None
    try:
        for i in range(container.count):
            a = container.item(i)
            if a and a.name.strip().lower().startswith(want):
                return a
    except Exception:
        pass
    return None


def appearance_status(design, profile):
    """Read-only report: {'have': [...], 'missing': [...], 'libraryOk': bool,
    'openDocs': [...]} — missing = codes not in the design; openDocs = other
    open documents that hold at least one missing code (copy candidates)."""
    app = adsk.core.Application.get()
    needed = config_tables_store.needed_appearances(profile)
    have, missing = [], []
    for e in needed:
        (have if find_appearance(design.appearances, e['code']) else missing).append(e)
    lib = str(profile.get('appearance_library') or '')
    status = {
        'have': [e['name'] for e in have],
        'missing': [e['name'] for e in missing],
        'libraryPath': lib,
        'libraryOk': bool(lib) and os.path.isfile(lib),
        'openDocs': [],
    }
    if missing:
        for i in range(app.documents.count):
            try:
                doc = app.documents.item(i)
                if doc == app.activeDocument:
                    continue
                src = adsk.fusion.Design.cast(
                    doc.products.itemByProductType('DesignProductType'))
                if src and any(find_appearance(src.appearances, e['code'])
                               for e in missing):
                    status['openDocs'].append(doc.name)
            except Exception:
                continue
    return status


def ensure_appearances(design, profile):
    """Copy every missing scheme appearance into `design`, trying sources in
    order: local .adsklib library → other open documents. Returns a report;
    report['missing'] non-empty means the build must stop."""
    app = adsk.core.Application.get()
    needed = config_tables_store.needed_appearances(profile)
    report = {'copied': [], 'missing': [], 'notes': []}

    def missing_now():
        return [e for e in needed
                if find_appearance(design.appearances, e['code']) is None]

    def copy_from(container, label):
        for e in missing_now():
            src = find_appearance(container, e['code'])
            if src is None:
                continue
            try:
                design.appearances.addByCopy(src, e['name'])
                report['copied'].append(f"{e['name']}  ({label})")
            except Exception as ex:
                report['notes'].append(f"copy failed for {e['name']}: {ex}")

    if not missing_now():
        return report

    lib_path = str(profile.get('appearance_library') or '')
    if lib_path:
        if not os.path.isfile(lib_path):
            report['notes'].append(f'appearance library not found: {lib_path}')
        else:
            lib = None
            try:
                lib = app.materialLibraries.load(lib_path)
                copy_from(lib.appearances, 'library')
            except Exception as ex:
                report['notes'].append(f'could not use appearance library: {ex}')
            finally:
                try:
                    if lib:
                        lib.unload()
                except Exception:
                    pass

    if missing_now():
        for i in range(app.documents.count):
            try:
                doc = app.documents.item(i)
                if doc == app.activeDocument:
                    continue
                src = adsk.fusion.Design.cast(
                    doc.products.itemByProductType('DesignProductType'))
            except Exception:
                continue
            if src:
                copy_from(src.appearances, f'open doc: {doc.name}')
            if not missing_now():
                break

    report['missing'] = [e['name'] for e in missing_now()]
    return report


def open_source_document(profile):
    """Open the profile's cloud source document (e.g. WC_S2) by SEARCH — walk
    project → folders by name; lineage ids go stale so none are stored. Every
    cloud access can block for ~30 s when offline, so the whole crawl runs
    against a hard wall-clock budget and reports rather than hangs forever."""
    app = adsk.core.Application.get()
    src = profile.get('source_document') or {}
    name = str(src.get('name') or '').strip()
    if not name:
        return {'ok': False, 'error': 'profile has no source_document name'}
    t0 = time.time()
    BUDGET = 45.0

    def over_budget():
        return time.time() - t0 > BUDGET

    try:
        project = None
        want_proj = str(src.get('project') or '').strip().lower()
        projects = app.data.dataProjects
        for i in range(projects.count):
            if over_budget():
                return {'ok': False, 'error': 'cloud too slow (project list) — '
                                              'check your connection and retry'}
            p = projects.item(i)
            if not want_proj or p.name.strip().lower() == want_proj:
                project = p
                break
        if project is None:
            return {'ok': False,
                    'error': f"project '{src.get('project')}' not found"}

        want_folder = str(src.get('folder') or '').strip().lower()
        target, fallback = None, None
        queue, visits = [project.rootFolder], 0
        while queue and visits < 60 and not over_budget():
            folder = queue.pop(0)
            visits += 1
            try:
                files = folder.dataFiles
                for j in range(files.count):
                    f = files.item(j)
                    if f.name.strip() == name:
                        if not want_folder or folder.name.strip().lower() == want_folder:
                            target = f
                            break
                        fallback = fallback or f
                if target:
                    break
                subs = folder.dataFolders
                for j in range(subs.count):
                    queue.append(subs.item(j))
            except Exception:
                continue
        target = target or fallback
        if target is None:
            reason = 'crawl budget hit' if (over_budget() or visits >= 60) \
                else 'file not found'
            return {'ok': False, 'error': f"'{name}' not opened ({reason})"}
        doc = app.documents.open(target, True)
        return {'ok': True, 'opened': doc.name if doc else name}
    except Exception as ex:
        return {'ok': False, 'error': str(ex)}


# ---------------------------------------------------------------------------
# Scan / classify
# ---------------------------------------------------------------------------
def _kw(name, words):
    low = str(name).lower()
    return next((w for w in words if w in low), None)


_UNSET = object()


def _classify_leaf(occ, profile, comp=_UNSET, role=_UNSET, category=_UNSET,
                   name=None):
    """(role, source, reason) for one candidate part occurrence. Priority:
    persisted role attribute → wc_attrs category → name keywords (hardware
    first, then front, door, carcass) → category panel fallback → skip.

    _collect passes what it has already read (component, role, category,
    name) so the same attributes aren't fetched from Fusion twice per part."""
    if comp is _UNSET:
        comp = _comp(occ)
    if role is _UNSET:
        role = wc_attrs.get_role(comp) if comp else None
    if role:
        return role, 'attribute', f'role attribute: {role}'
    if category is _UNSET:
        category = wc_attrs.get_category(comp) if comp else None
    if category == config.WC_CAT_HARDWARE:
        return ROLE_SKIP, 'attribute', 'category: hardware'
    kws = profile['keywords']
    if name is None:
        name = occ.name
    w = _kw(name, kws['hardware'])
    if w:
        return ROLE_SKIP, 'keyword', f"hardware keyword '{w}'"
    w = _kw(name, kws['front'])
    if w:
        return ROLE_FRONT, 'keyword', f"front keyword '{w}'"
    w = _kw(name, kws['door'])
    if w:
        return ROLE_DOOR, 'keyword', f"door keyword '{w}'"
    w = _kw(name, kws['carcass'])
    if w:
        return ROLE_CARCASS, 'keyword', f"carcass keyword '{w}'"
    if comp and category in config.WC_SHEET_LIKE:
        return ROLE_CARCASS, 'attribute', 'category: panel'
    return ROLE_SKIP, 'unclassified', 'no attribute or keyword matched'


def _collect(occ, path, items, occs, profile, depth=0, display_name=None,
             wrappers=None):
    """Gather candidate columns under one child of a cabinet. A zero-body
    occurrence that has children is a WRAPPER (Door:1 → Right/Left Door:1,
    Shelf Assembly:N → Shelf + mounts): a column on it would repaint its
    hardware too, so descend instead of listing it.

    Hardware-ness is decided BEFORE the wrapper descent: an Innotech drawer
    assembly is zero-body with children too, but its Drawer Bottom/Back are
    purchased box parts, not fronts — descending would classify them as doors
    via the 'drawer' keyword (found the hard way on the VILLA-C kitchen). A
    persisted role attribute on the occurrence also wins over descent.

    Every listed item's LIVE occurrence proxy is stashed in `occs` by key —
    the builder uses these directly instead of re-resolving by name, which
    proved transiently flaky on real documents. Path elements are DISPLAY
    names (sibling duplicates carry a ' #N' suffix, see _unique_names)."""
    p = path + [display_name or occ.name]

    def _add(role, source, reason):
        key = key_of(p)
        items.append({'path': p, 'key': key, 'group': p[0],
                      'role': role, 'source': source, 'reason': reason})
        occs[key] = occ

    comp = _comp(occ)
    role = wc_attrs.get_role(comp) if comp else None
    category = _UNSET
    occ_name = occ.name
    if role is None:
        category = wc_attrs.get_category(comp) if comp else None
        if category == config.WC_CAT_HARDWARE:
            _add(ROLE_SKIP, 'attribute', 'category: hardware')
            return
        hw = _kw(occ_name, profile['keywords']['hardware'])
        if hw:
            _add(ROLE_SKIP, 'keyword', f"hardware keyword '{hw}'")
            return
        try:
            is_wrapper = (occ.bRepBodies.count == 0
                          and occ.childOccurrences.count > 0
                          and depth < _WRAPPER_DEPTH)
        except Exception:
            is_wrapper = False
        if is_wrapper:
            wkey = key_of(p)
            occs[wkey] = occ
            if wrappers is not None:
                wrappers.append({'path': p, 'key': wkey})
            for ch, child_name in _unique_names(occ.childOccurrences):
                _collect(ch, p, items, occs, profile, depth + 1, child_name,
                         wrappers)
            return
    role, source, reason = _classify_leaf(occ, profile, comp, role, category,
                                          occ_name)
    _add(role, source, reason)


def scan(design, profile, role_overrides=None, group_overrides=None):
    """Classify the whole tree. Read-only. Returns
    {'groups': [{name, kind, reason, doors, parts, bodies}], 'items': [...],
     'parts': [items with a final door/carcass role, in tree order]}.
    role_overrides: {item key: role} — final say, applied before the front
    resolution so an overridden door still counts toward the single-door rule.
    group_overrides: {top-level name: 'include'|'exclude'}."""
    role_overrides = role_overrides or {}
    group_overrides = group_overrides or {}
    root = design.rootComponent
    groups, items = [], []
    occs = {}          # item key -> live Occurrence (stripped before JSON)
    wrappers = []      # zero-body wrappers descended (Door:1 …) — stray-paint sweep targets

    for top, name in _unique_names(root.occurrences):
        try:
            child_count = top.childOccurrences.count
            body_count = top.bRepBodies.count
        except Exception:
            child_count, body_count = 0, 0
        override = group_overrides.get(name)
        top_comp = _comp(top)
        persisted_skip = top_comp is not None and \
            wc_attrs.get_role(top_comp) == ROLE_SKIP
        excl_kw = _kw(name, profile['keywords']['exclude'])
        if excl_kw and child_count >= EXCLUDE_MAX_CHILDREN:
            excl_kw = None                       # cabinet-sized: name lies, classify it
        if override == 'exclude' or (override != 'include'
                                     and (persisted_skip or excl_kw)):
            reason = ('user override' if override == 'exclude'
                      else 'role attribute: skip' if persisted_skip
                      else f"exclude keyword '{excl_kw}'")
            groups.append({'name': name, 'kind': 'excluded', 'reason': reason,
                           'doors': 0, 'parts': 0})
            # Still walk the children (as forced-skip, no wrapper tracking):
            # if a column was built for this cabinet BEFORE it was excluded,
            # map_columns must recognise and RETIRE it, or the excluded parts
            # keep following the theme forever. The REAL classification is
            # kept in 'origRole' (fronts resolved by the single-door rule)
            # so apply_custom_finish can still tell a door from a panel.
            shadow = []
            try:
                for ch, child_name in _unique_names(top.childOccurrences):
                    _collect(ch, [name], shadow, occs, profile, 0, child_name)
            except Exception:
                pass
            for it in shadow:
                if it['key'] in role_overrides:
                    it['role'] = role_overrides[it['key']]
            shadow_doors = len([i for i in shadow if i['role'] == ROLE_DOOR])
            for it in shadow:
                if it['role'] == ROLE_FRONT:
                    it['role'] = ROLE_DOOR if shadow_doors == 1 else ROLE_CARCASS
                it['origRole'] = it['role']
                it['role'] = ROLE_SKIP
                it['source'] = 'excluded'
                it['reason'] = f'inside excluded {name}'
            items.extend(shadow)
            continue

        raw = []
        if child_count == 0:
            if body_count >= FLATTENED_BODY_COUNT:
                groups.append({'name': name, 'kind': 'flattened',
                               'reason': f'{body_count} bodies, no child occurrences '
                                         '— cannot be split into door vs carcass',
                               'doors': 0, 'parts': 0, 'bodies': body_count})
                continue
            _collect(top, [], raw, occs, profile,    # loose part / filler / trim
                     display_name=name, wrappers=wrappers)
            kind = 'part'
        else:
            for ch, child_name in _unique_names(top.childOccurrences):
                _collect(ch, [name], raw, occs, profile, 0, child_name, wrappers)
            kind = 'cabinet'

        for it in raw:
            if it['key'] in role_overrides:
                it['role'] = role_overrides[it['key']]
                it['source'] = 'override'
                it['reason'] = 'user override'

        # Single-door rule, per cabinet: exactly 1 door → the fixed/front panel
        # takes the DOOR finish (L-corner units); otherwise fronts stay carcass.
        doors = len([it for it in raw if it['role'] == ROLE_DOOR])
        for it in raw:
            if it['role'] == ROLE_FRONT:
                final = ROLE_DOOR if doors == 1 else ROLE_CARCASS
                it['role'] = final
                it['reason'] += f' → {final} ({doors} door(s) in group)'

        groups.append({'name': name, 'kind': kind, 'reason': '', 'doors': doors,
                       'parts': len([i for i in raw
                                     if i['role'] in (ROLE_DOOR, ROLE_CARCASS)])})
        occs[name] = top          # cabinet occurrence — stray-paint sweep target
        items.extend(raw)

    return {'groups': groups, 'items': items, 'occs': occs, 'wrappers': wrappers,
            'parts': [i for i in items if i['role'] in (ROLE_DOOR, ROLE_CARCASS)]}


# ---------------------------------------------------------------------------
# Column ↔ occurrence mapping
# ---------------------------------------------------------------------------
def _load_colmap(root_comp):
    """Stored column→path list; entries may be None (a column we could not
    match to any occurrence — renamed, deleted, or unclassified part)."""
    raw = wc_attrs.get_value(root_comp, config.WC_APPEARANCE_COLS)
    try:
        paths = json.loads(raw) if raw else None
    except Exception:
        paths = None
    if isinstance(paths, list) and all(
            p is None or isinstance(p, list) for p in paths):
        return [([str(n) for n in p] if p else None) for p in paths]
    return None


def _save_colmap(root_comp, paths):
    wc_attrs.set_value(root_comp, config.WC_APPEARANCE_COLS, json.dumps(paths))


def map_columns(design, at, plan):
    """Resolve which occurrence each appearance column (past the root column)
    belongs to. Returns (col_info, new_parts, problem):
      col_info  [{path, key, role|None}] aligned to columns 1..N (role None =
                orphan: the occurrence is gone, reclassified, or unmatched —
                reported, its cells left alone)
      new_parts plan parts with no column yet (to append)
      problem   reserved (always None — mapping degrades, never hard-fails).

    Columns are PER-INSTANCE (a cabinet placed 8 times needs 8 'Left Panel:1'
    columns — verified live), but `ConfigurationAppearanceColumn.entity`
    reports only the NATIVE occurrence (context-free, no cabinet prefix), so
    which specific instance a column paints is NOT recoverable from the API.
    What we can know exactly: the native identity (same `name` + same
    `sourceComponent`; Component compares with ==, Occurrence does NOT), and
    therefore the COUNT of columns vs instances per native. A group with
    fewer columns than instances has unpainted cabinets, and since
    columns.add is IDEMPOTENT per instance (adding a covered instance is a
    silent no-op — verified live), the fix is to re-add every instance of any
    under-covered group. Name-twin cabinets are different components →
    distinct natives → never conflated. Title alignment survives only as a
    fallback for the hypothetical case that .entity is unavailable."""
    parts = plan['parts']
    occs = plan.get('occs') or {}
    count = at.columns.count

    def native_of(occ):
        try:
            nat = occ.nativeObject
            return nat if nat is not None else occ
        except Exception:
            return occ

    def same_native(a, b):
        try:
            if a.name != b.name:
                return False
        except Exception:
            return False
        try:
            sa, sb = a.sourceComponent, b.sourceComponent
            if sa is not None and sb is not None:
                return sa == sb
        except Exception:
            pass
        return True         # names agree, components unreadable — accept

    # Group plan parts (instances) by native identity.
    buckets, groups = {}, []      # name → [group]; group = {nat, parts, cols}
    for p in parts:
        occ = occs.get(p['key'])
        if occ is None:
            continue
        nat = native_of(occ)
        try:
            nm = nat.name
        except Exception:
            continue
        bucket = buckets.setdefault(nm, [])
        group = next((g for g in bucket if same_native(g['nat'], nat)), None)
        if group is None:
            group = {'nat': nat, 'parts': [], 'cols': 0}
            bucket.append(group)
            groups.append(group)
        group['parts'].append(p)

    # Natives of DELIBERATELY skipped/excluded occurrences: a column matching
    # one of these (and not claimed by a living part) gets RETIRED so the
    # excluded part stops following the theme.
    skip_buckets = {}
    for item in plan.get('items') or []:
        if item['role'] != ROLE_SKIP:
            continue
        occ = occs.get(item['key'])
        if occ is None:
            continue
        nat = native_of(occ)
        try:
            skip_buckets.setdefault(nat.name, []).append((item['key'], nat))
        except Exception:
            continue

    def retire_entry(ent):
        try:
            candidates = skip_buckets.get(ent.name, [])
        except Exception:
            candidates = []
        for item_key, nat in candidates:
            if same_native(nat, ent):
                return {'path': None, 'key': f'{item_key} (excluded — detaching)',
                        'role': ROLE_RETIRED}
        return None

    col_info, entity_ok = [], 0
    for i in range(1, count):
        column = at.columns.item(i)
        try:
            ent = column.entity
        except Exception:
            ent = None
        entry = None
        if ent is not None:
            entity_ok += 1
            ent = native_of(ent)
            try:
                bucket = buckets.get(ent.name, [])
            except Exception:
                bucket = []
            group = next((g for g in bucket if same_native(g['nat'], ent)), None)
            if group is not None:
                idx = group['cols']
                group['cols'] += 1
                if idx < len(group['parts']):
                    p = group['parts'][idx]
                    entry = {'path': p['path'], 'key': p['key'],
                             'role': p['role']}
                else:
                    # more columns than living instances: either an instance
                    # was deleted, or one placement of this part was excluded
                    # (which placement each column paints is unknowable, so
                    # count-wise retirement is the best the API allows)
                    entry = retire_entry(ent) or {
                        'path': None,
                        'key': f"{group['parts'][0]['key']} (surplus column)",
                        'role': None}
            else:
                entry = retire_entry(ent)
        if entry is None:
            label = None
            if ent is not None:
                try:
                    label = ent.name
                except Exception:
                    label = None
            if label is None:
                try:
                    label = column.title
                except Exception:
                    label = f'column {i}'
            entry = {'path': None, 'key': f'{label} (unmatched column)',
                     'role': None}
        col_info.append(entry)
    if entity_ok == 0 and count > 1:
        return _map_columns_by_name(design, at, plan)

    # Under-covered groups: fewer columns than instances means some cabinet
    # placements are unpainted, but WHICH ones is unknowable (entity is the
    # native) — so queue EVERY instance; covered adds are no-ops.
    new_parts = []
    for group in groups:
        if group['cols'] < len(group['parts']):
            new_parts.extend(group['parts'])
    _save_colmap(design.rootComponent, [c['path'] for c in col_info])
    return col_info, new_parts, None


def _map_columns_by_name(design, at, plan):
    """Legacy fallback: ordered title alignment (sidecar-assisted). Only used
    when no column exposes .entity."""
    parts = plan['parts']
    by_key = {p['key']: p for p in parts}
    titles = [at.columns.item(i).title for i in range(1, at.columns.count)]

    stored = _load_colmap(design.rootComponent)
    if stored is not None:
        # Use the sidecar only when it is complete and every mapped entry
        # still agrees with its column title (titles are RAW occurrence names,
        # stored leaves are display names — compare through _base); any gap or
        # rename → re-derive, which also lets a just-classified or renamed
        # part reclaim its column.
        if (len(stored) != len(titles) or any(p is None for p in stored)
                or any(_base(p[-1]) != t for p, t in zip(stored, titles))):
            stored = None

    if stored is None and titles:
        # Pass 1 — ordered greedy: an original build adds columns in tree
        # order, so walk the plan with a forward-only pointer.
        stored, ptr, used = [], 0, set()
        for t in titles:
            j = next((k for k in range(ptr, len(parts))
                      if _base(parts[k]['path'][-1]) == t), None)
            if j is None:
                stored.append(None)
            else:
                stored.append(parts[j]['path'])
                used.add(j)
                ptr = j + 1
        # Pass 2 — columns APPENDED by a later fix run sit at the table's end,
        # out of tree order, so the forward pointer misses them: match the
        # leftovers against still-unused plan parts, both sides in order.
        remaining = [k for k in range(len(parts)) if k not in used]
        for i, t in enumerate(titles):
            if stored[i] is not None:
                continue
            j = next((k for k in remaining
                      if _base(parts[k]['path'][-1]) == t), None)
            if j is not None:
                stored[i] = parts[j]['path']
                remaining.remove(j)
        # Whatever is STILL unmatched is a true orphan (renamed part,
        # unclassified name, deleted occurrence) — reported, its cells left
        # untouched; never a fatal error: one rename must not brick the table.
        _save_colmap(design.rootComponent, stored)

    stored = stored or []
    covered = {key_of(p) for p in stored if p}
    new_parts = [p for p in parts if p['key'] not in covered]
    col_info = []
    for i, p in enumerate(stored):
        if p is None:
            col_info.append({'path': None,
                             'key': f'{titles[i]} (unmatched column)',
                             'role': None})
            continue
        item = by_key.get(key_of(p))
        col_info.append({'path': p, 'key': key_of(p),
                         'role': item['role'] if item else None})
    return col_info, new_parts, None


def _find_in(collection, name):
    """One occurrence from a collection by DISPLAY name: 'Custom 1:1 #2' means
    the 2nd sibling whose raw name is 'Custom 1:1' (different components can
    share a name, so raw names are NOT unique). Iterates and counts — never
    itemByName, which silently returns the first name-twin."""
    m = _DUP_RE.match(str(name))
    want, ordinal = (m.group(1), int(m.group(2))) if m else (str(name), 1)
    seen = 0
    try:
        for occ in collection:
            if occ.name == want or occ.name.strip() == want.strip():
                seen += 1
                if seen == ordinal:
                    return occ
    except Exception:
        pass
    return None


def _resolve(root, path):
    """Occurrence at `path` (names from the root down), or None. Fallback for
    when a live proxy isn't at hand (plan['occs'] is preferred)."""
    try:
        collection = root.occurrences
        occ = None
        for n in path:
            occ = _find_in(collection, n)
            if occ is None:
                return None
            collection = occ.childOccurrences
        return occ
    except Exception:
        return None


def table_status(design, plan):
    """Read-only table summary for the palette."""
    st = {'configured': bool(design.isConfiguredDesign), 'exists': False,
          'rows': 0, 'cols': 0, 'newParts': [], 'orphans': [], 'problem': None}
    if not st['configured']:
        st['newParts'] = [p['key'] for p in plan['parts']]
        return st
    try:
        at = design.configurationTopTable.appearanceTable
    except Exception as ex:
        st['problem'] = f'appearance table unavailable: {ex}'
        return st
    st['cols'] = at.columns.count
    st['rows'] = at.rows.count
    st['exists'] = at.columns.count > 0
    col_info, new_parts, problem = map_columns(design, at, plan)
    st['problem'] = problem
    st['newParts'] = [p['key'] for p in new_parts]
    if col_info:
        st['orphans'] = [c['key'] for c in col_info if c['role'] is None]
        st['retiring'] = [c['key'] for c in col_info if c['role'] == ROLE_RETIRED]
    return st


# ---------------------------------------------------------------------------
# Build / fix (one code path — the sweep IS the builder)
# ---------------------------------------------------------------------------
def apply_plan(design, plan, profile, ui, create=False):
    """Bring the appearance table up to the plan: convert the design (create
    mode), source appearances, append missing columns and rows, fill every
    empty/wrong cell, verify. Idempotent — running it twice changes nothing
    the second time. Returns a report dict; report['ok'] False = stopped
    before completion (nothing destructive has happened: this only ever adds
    and fills)."""
    report = {'ok': True, 'notes': [], 'warnings': []}

    if not design.isConfiguredDesign:
        if not create:
            return {'ok': False, 'error': 'This design has no configuration '
                    'tables yet — run Build first.'}
        design.createConfiguredDesign()
        report['notes'].append('Converted to a configured design')

    appearances = ensure_appearances(design, profile)
    report['appearances'] = appearances
    if appearances['missing']:
        report['ok'] = False
        report['error'] = ('Missing appearances (open the source document or '
                           'set the appearance library in the profile): '
                           + ', '.join(appearances['missing']))
        return report

    tt = design.configurationTopTable
    at = tt.appearanceTable
    root = design.rootComponent

    col_info, new_parts, problem = map_columns(design, at, plan)
    if problem:
        return {'ok': False, 'error': problem, 'appearances': appearances}

    # --- retire columns of deliberately excluded parts ----------------------
    # Empty the cells first (so the part is no longer painted by any row),
    # then delete the column; the part keeps its current look but stops
    # following theme switches. Collect the column objects BEFORE deleting —
    # deletion shifts indices.
    retired_idx = [i for i, c in enumerate(col_info) if c['role'] == ROLE_RETIRED]
    if retired_idx:
        doomed = [(at.columns.item(i + 1), col_info[i]['key']) for i in retired_idx]
        report['columnsRetired'] = []
        for column, key in doomed:
            try:
                for j in range(at.rows.count):
                    try:
                        at.rows.item(j).getCellByColumnId(column.id).appearance = None
                    except Exception:
                        pass
                column.deleteMe()
                report['columnsRetired'].append(key)
            except Exception as ex:
                report['warnings'].append(f'could not retire column {key}: {ex}')
        col_info, new_parts, problem = map_columns(design, at, plan)
        if problem:
            return {'ok': False, 'error': problem, 'appearances': appearances}

    # --- columns -----------------------------------------------------------
    if at.columns.count == 0 and new_parts:
        at.columns.add(root)                    # root column first, always empty
    stored = [c['path'] for c in col_info]
    occs = plan.get('occs') or {}
    added_cols = already_covered = 0
    for p in new_parts:
        occ = occs.get(p['key']) or _resolve(root, p['path'])
        if occ is None:
            report['warnings'].append(
                f"could not resolve occurrence: {p['key']} — rescan and retry")
            continue
        before = at.columns.count
        try:
            at.columns.add(occ)
        except Exception as ex:
            report['warnings'].append(f"column add failed for {p['key']}: {ex}")
            continue
        # columns.add is idempotent per instance: no growth = this placement
        # already had a column (we re-add every instance of an under-covered
        # part because the API can't say which placements are the bare ones).
        if at.columns.count > before:
            stored.append(p['path'])
            col_info.append({'path': p['path'], 'key': p['key'],
                             'role': p['role']})
            added_cols += 1
        else:
            already_covered += 1
    if added_cols:
        _save_colmap(root, stored)
    report['columnsAdded'] = added_cols
    report['columnsAlreadyCovered'] = already_covered
    report['columns'] = at.columns.count

    # --- rows --------------------------------------------------------------
    with_doors = any(p['role'] == ROLE_DOOR for p in plan['parts'])
    names = config_tables_store.expected_row_names(profile, with_doors)
    existing = {at.rows.item(i).name for i in range(at.rows.count)}
    # Adding the first column auto-creates a phantom row ('Theme 1') — a lone
    # unexpected row gets renamed, never treated as corruption.
    if at.rows.count == 1 and at.rows.item(0).name not in names:
        at.rows.item(0).name = names[0]
        existing = {names[0]}
    added_rows = 0
    for nm in names:
        if nm not in existing:
            try:
                at.rows.add(nm)
                added_rows += 1
            except Exception as ex:
                report['warnings'].append(f"row add failed for {nm}: {ex}")
    report['rowsAdded'] = added_rows
    report['rows'] = at.rows.count

    # --- cells -------------------------------------------------------------
    fill = _fill_cells(design, at, col_info, profile, ui)
    report.update(fill)
    if fill.get('cancelled'):
        report['ok'] = False
        report['error'] = ('Cancelled mid-fill — run Verify & Fix to finish '
                           'the remaining cells (nothing is lost).')
        return report

    # --- stray paints that would defeat the theme ---------------------------
    report['strays'] = sweep_strays(design, plan, col_info, clear=True)

    report['verify'] = verify(design, at, col_info, profile)
    return report


def _fill_cells(design, at, col_info, profile, ui):
    """Write every cell that is empty or wrong; root column untouched (row 0 of
    an assembly deliberately shows 'From Physical Material'). Reads first, so a
    re-run over a healthy table writes nothing."""
    app_by_code = {}
    for e in config_tables_store.needed_appearances(profile):
        app_by_code[e['code'].strip().lower()] = find_appearance(
            design.appearances, e['code'])

    colids = [at.columns.item(i).id for i in range(1, at.columns.count)]
    out = {'cellsFilled': 0, 'rowsSkipped': [], 'cancelled': False}
    pd = None
    try:
        pd = ui.createProgressDialog()
        pd.isCancelButtonShown = True
        pd.show('WoodCraft — Appearance Config',
                'Filling appearance cells: row %v of %m', 0, max(1, at.rows.count), 0)
    except Exception:
        pd = None

    for j in range(at.rows.count):
        row = at.rows.item(j)
        carcass, finish = config_tables_store.parse_row_name(row.name, profile)
        if carcass is None:
            out['rowsSkipped'].append(row.name)   # hand-made row: leave it alone
            continue
        carc_app = app_by_code.get(carcass['code'].strip().lower())
        door_app = app_by_code.get(finish['code'].strip().lower()) if finish else carc_app
        if carc_app is None:
            out['rowsSkipped'].append(row.name)
            continue
        for i, cid in enumerate(colids):
            role = col_info[i]['role'] if i < len(col_info) else None
            if role not in (ROLE_DOOR, ROLE_CARCASS):
                continue                          # orphan/retired — not touched here
            want = door_app if role == ROLE_DOOR else carc_app
            if want is None:
                continue
            cell = row.getCellByColumnId(cid)
            current = cell.appearance
            if current is not None and current.name == want.name:
                continue
            try:
                cell.appearance = want
                out['cellsFilled'] += 1
            except Exception:
                pass
        if pd:
            try:
                pd.progressValue = j + 1
                adsk.doEvents()
                if pd.wasCancelled:
                    out['cancelled'] = True
                    break
            except Exception:
                pass
    if pd:
        try:
            pd.hide()
        except Exception:
            pass
    return out


def sweep_strays(design, plan, col_info, clear=False):
    """Detect (and with clear=True remove) appearance assignments that DEFEAT
    the configuration on managed parts:
      * body-level overrides (a manually painted body beats the theme's
        occurrence-level appearance forever — the walnut drawer / blue door
        bug seen live),
      * face-level overrides on those bodies,
      * occurrence-level paints on the zero-body WRAPPERS between a cabinet
        and its parts (the wrapper has no column, so the theme never
        overwrites a stale paint there).
    Occurrence-level appearances on the managed parts themselves are the
    configuration's own mechanism and are never touched. Components are
    visited once even when placed many times (clearing the native body heals
    every instance)."""
    occs = plan.get('occs') or {}
    report = {'cabinetPaints': [], 'bodyOverrides': [], 'faceOverrides': [],
              'wrapperPaints': [], 'hardwarePaints': [], 'cleared': 0}
    body_src = adsk.core.AppearanceSourceTypes.BodyAppearanceSource
    face_src = adsk.core.AppearanceSourceTypes.FaceAppearanceSource

    # Cabinet-level paints FIRST: Occurrence.appearance READS the effective
    # appearance (ancestors included), so a paint on the whole cabinet makes
    # every descendant read as painted. Top occurrences have no ancestors —
    # a non-None read there is a real paint, and clearing it un-shadows the
    # reads below. It would freeze the cabinet's look across theme switches.
    for g in plan.get('groups') or []:
        if g.get('kind') != 'cabinet':
            continue
        occ = occs.get(g['name'])
        if occ is None:
            continue
        try:
            a = occ.appearance
        except Exception:
            a = None
        if a is not None:
            if clear:
                stuck = False
                try:
                    occ.appearance = None
                    stuck = occ.appearance is None
                except Exception:
                    pass
                if stuck:
                    report['cleared'] += 1
                    report['cabinetPaints'].append(f"{g['name']} = {a.name} (cleared)")
                else:
                    # Paint captured in the configuration's base state — the
                    # assignment silently reverts (verified live). Harmless
                    # for themed parts (their deeper occurrence paints win);
                    # it only tints unmanaged hardware.
                    report['cabinetPaints'].append(
                        f"{g['name']} = {a.name} (baked into configuration — "
                        'themed parts unaffected, tints hardware only)')
            else:
                report['cabinetPaints'].append(f"{g['name']} = {a.name}")

    seen = []
    for c in col_info or []:
        if c.get('role') not in (ROLE_DOOR, ROLE_CARCASS):
            continue
        occ = occs.get(c['key'])
        comp = _comp(occ) if occ is not None else None
        if comp is None:
            continue
        try:
            if any(comp == s for s in seen):
                continue
            seen.append(comp)
        except Exception:
            pass
        try:
            bodies = comp.bRepBodies
        except Exception:
            continue
        for bi in range(bodies.count):
            body = bodies.item(bi)
            try:
                if body.appearanceSourceType == body_src:
                    a = body.appearance
                    report['bodyOverrides'].append(
                        f"{c['key']} :: {body.name} = {a.name if a else '?'}")
                    if clear:
                        body.appearance = None
                        report['cleared'] += 1
            except Exception:
                pass
            try:
                for fi in range(body.faces.count):
                    face = body.faces.item(fi)
                    if face.appearanceSourceType == face_src:
                        report['faceOverrides'].append(
                            f"{c['key']} :: {body.name} face {fi}")
                        if clear:
                            face.appearance = None
                            report['cleared'] += 1
            except Exception:
                pass
    for w in plan.get('wrappers') or []:
        occ = occs.get(w['key'])
        if occ is None:
            continue
        try:
            a = occ.appearance
        except Exception:
            a = None
        if a is not None:
            report['wrapperPaints'].append(f"{w['key']} = {a.name}")
            if clear:
                try:
                    occ.appearance = None
                    report['cleared'] += 1
                except Exception:
                    pass

    # Hardware / skip parts: report (never clear) — a paint here may be
    # deliberate styling, but a leftover from a cabinet-wide paint job will
    # otherwise stick out when the theme switches. Parts inside EXCLUDED
    # cabinets keep their paint by definition — reporting them is just noise.
    for item in plan.get('items') or []:
        if item['role'] != ROLE_SKIP or item.get('source') == 'excluded':
            continue
        occ = occs.get(item['key'])
        if occ is None:
            continue
        try:
            a = occ.appearance
        except Exception:
            a = None
        if a is not None:
            report['hardwarePaints'].append(f"{item['key']} = {a.name}")
    return report


def verify(design, at=None, col_info=None, profile=None, plan=None):
    """Read-only sweep: {'cells', 'empty', 'wrong': [...]} — root column and
    orphan columns excluded. Fast (a 4864-cell sweep measures ~2.5 s)."""
    if at is None:
        at = design.configurationTopTable.appearanceTable
    if col_info is None:
        col_info, _, problem = map_columns(design, at, plan)
        if problem:
            return {'problem': problem}
    app_by_code = {}
    for e in config_tables_store.needed_appearances(profile):
        app_by_code[e['code'].strip().lower()] = find_appearance(
            design.appearances, e['code'])
    colids = [at.columns.item(i).id for i in range(1, at.columns.count)]
    cells = empty = 0
    wrong = []
    for j in range(at.rows.count):
        row = at.rows.item(j)
        carcass, finish = config_tables_store.parse_row_name(row.name, profile)
        if carcass is None:
            continue
        carc_app = app_by_code.get(carcass['code'].strip().lower())
        door_app = app_by_code.get(finish['code'].strip().lower()) if finish else carc_app
        for i, cid in enumerate(colids):
            role = col_info[i]['role'] if i < len(col_info) else None
            if role not in (ROLE_DOOR, ROLE_CARCASS):
                continue
            cells += 1
            got = row.getCellByColumnId(cid).appearance
            want = door_app if role == ROLE_DOOR else carc_app
            if got is None:
                empty += 1
            elif want is not None and got.name != want.name:
                wrong.append(f"{row.name} | {col_info[i]['key']} | "
                             f"got {got.name}, want {want.name}")
    result = {'cells': cells, 'empty': empty, 'wrongCount': len(wrong),
              'wrong': wrong[:25]}
    # Read-only callers (the Check button) pass `plan`; the fix path clears
    # strays itself and calls verify without a plan afterwards.
    if plan is not None:
        result['strays'] = sweep_strays(design, plan, col_info, clear=False)
    return result


# ---------------------------------------------------------------------------
# Preview (visual check) — point the active configuration at a chosen row
# ---------------------------------------------------------------------------
_preview_state = {}     # doc name -> previous appearance-theme row name (or '')


def _appearance_theme_column(tt):
    """The top-table theme column that references the APPEARANCE table (its
    referencedTable is none of the custom theme tables), or None."""
    custom = {t.name for t in tt.customThemeTables}
    for c in tt.columns:
        if not c.objectType.split('::')[-1].endswith('ThemeColumn'):
            continue
        rt = getattr(c, 'referencedTable', None)
        if rt is not None and rt.name not in custom:
            return c
    return None


def _active_top_row(tt):
    try:
        row = tt.activeRow
        if row:
            return row
    except Exception:
        pass
    return tt.rows.item(0) if tt.rows.count else None


def preview(design, row_name):
    """Wire the active configuration's appearance cell to `row_name` (e.g. a
    Grey carcass + dark wood door) so doors vs carcass can be eyeballed."""
    tt = design.configurationTopTable if design.isConfiguredDesign else None
    if tt is None:
        return {'ok': False, 'error': 'Not a configured design yet — run Build first.'}
    at = tt.appearanceTable
    target = at.rows.itemByName(row_name)
    if target is None:
        return {'ok': False, 'error': f'no appearance row named {row_name}'}
    col = _appearance_theme_column(tt)
    if col is None:
        return {'ok': False, 'error': 'no appearance theme column in the top '
                'table yet — save the document and check the Configure UI'}
    top_row = _active_top_row(tt)
    if top_row is None:
        return {'ok': False, 'error': 'top table has no configuration rows'}
    cell = top_row.getCellByColumnId(col.id)
    doc = design.parentDocument.name
    if doc not in _preview_state:
        prev = cell.referencedTableRow
        _preview_state[doc] = prev.name if prev else ''
    cell.referencedTableRow = target
    # Setting the cell alone does NOT repaint the model — appearances apply
    # when the configuration row activates (verified live: the viewport kept
    # the old theme until re-activation).
    try:
        top_row.activate()
    except Exception:
        pass
    return {'ok': True, 'row': row_name, 'configuration': top_row.name}


def restore(design):
    """Put the appearance selection back to what it was before preview()."""
    doc = design.parentDocument.name
    if doc not in _preview_state:
        return {'ok': False, 'error': 'nothing to restore'}
    prev_name = _preview_state.pop(doc)
    tt = design.configurationTopTable if design.isConfiguredDesign else None
    if tt is None:
        return {'ok': False, 'error': 'Not a configured design.'}
    col = _appearance_theme_column(tt)
    top_row = _active_top_row(tt)
    if col is None or top_row is None:
        return {'ok': False, 'error': 'appearance column not found'}
    if not prev_name:
        return {'ok': True, 'note': 'previous selection was empty — pick the '
                'row you want in the Configure UI'}
    target = tt.appearanceTable.rows.itemByName(prev_name)
    if target is None:
        return {'ok': False, 'error': f'previous row {prev_name} no longer exists'}
    top_row.getCellByColumnId(col.id).referencedTableRow = target
    try:
        top_row.activate()
    except Exception:
        pass
    return {'ok': True, 'row': prev_name}


# ---------------------------------------------------------------------------
# Custom finish — one cabinet, its own look, outside the theme
# ---------------------------------------------------------------------------
def apply_custom_finish(design, plan, profile, ui, group_name,
                        carcass_code, door_code):
    """Give ONE cabinet a finish of its own instead of the theme's.

    Mechanism: the cabinet must already be EXCLUDED in `plan` (scan called with
    a group override), so the fix pass retires its columns — themes stop
    touching it — and then its parts are painted directly at occurrence level,
    doors with `door_code`, everything else with `carcass_code`, using the
    classification recorded in origRole before the exclusion forced the parts
    to skip. The paint sticks across theme switches precisely BECAUSE the
    columns are gone. Include + Build later returns the cabinet to the theme
    (which then repaints over the custom finish)."""
    report = {'ok': True, 'warnings': [], 'painted': 0}

    appearances = ensure_appearances(design, profile)
    if appearances['missing']:
        return {'ok': False, 'error': 'Missing appearances: '
                + ', '.join(appearances['missing'])}
    carcass = find_appearance(design.appearances, carcass_code)
    door = find_appearance(design.appearances, door_code) if door_code else carcass
    if carcass is None or door is None:
        return {'ok': False, 'error': f'appearance not found for carcass '
                f'{carcass_code!r} / door {door_code!r}'}

    # Retire this cabinet's columns (and repair the rest of the table) via the
    # normal fix pass. A design with no table yet has nothing to retire.
    if design.isConfiguredDesign:
        sub = apply_plan(design, plan, profile, ui, create=False)
        report['columnsRetired'] = sub.get('columnsRetired', [])
        report['warnings'].extend(sub.get('warnings', []))
        if not sub.get('ok'):
            report['warnings'].append(sub.get('error') or 'fix pass incomplete')

    occs = plan.get('occs') or {}
    for item in plan.get('items') or []:
        if item.get('group') != group_name or item.get('source') != 'excluded':
            continue
        role = item.get('origRole')
        if role not in (ROLE_DOOR, ROLE_CARCASS):
            continue
        occ = occs.get(item['key'])
        if occ is None:
            continue
        want = door if role == ROLE_DOOR else carcass
        try:
            occ.appearance = want
            report['painted'] += 1
        except Exception:
            report['warnings'].append(f"could not paint {item['key']}")
    if report['painted'] == 0:
        report['ok'] = False
        report['error'] = (f'no door/carcass parts found to paint in '
                           f'{group_name} — is it excluded and classified?')
    return report


# ---------------------------------------------------------------------------
# Override persistence
# ---------------------------------------------------------------------------
def persist_overrides(design, role_overrides, group_overrides):
    """Write user overrides back as component attributes so the next scan gets
    them for free. Role overrides land on the part's component (shared by all
    its occurrences — correcting one cabinet corrects every insert of it);
    a group 'exclude' lands as role=skip on the cabinet's component, and a
    group 'include' clears a persisted skip."""
    root = design.rootComponent
    written, failed = 0, []
    for key, role in (role_overrides or {}).items():
        occ = _resolve(root, key.split(PATH_SEP))
        comp = _comp(occ) if occ else None
        if comp is None or not wc_attrs.set_role(comp, role):
            failed.append(key)
        else:
            written += 1
    for name, action in (group_overrides or {}).items():
        occ = _resolve(root, [name])
        comp = _comp(occ) if occ else None
        if comp is None:
            failed.append(name)
            continue
        if action == 'exclude':
            if wc_attrs.set_role(comp, ROLE_SKIP):
                written += 1
            else:
                failed.append(name)
        elif action == 'include' and wc_attrs.get_role(comp) == ROLE_SKIP:
            wc_attrs.remove_role(comp)
            written += 1
    return {'written': written, 'failed': failed}
