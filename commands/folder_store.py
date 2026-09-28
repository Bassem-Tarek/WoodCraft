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

"""Which cloud folders the WoodCraft tools read from — chosen in Folders Selection.

Several commands work against folders on the Fusion cloud rather than against
geometry:

    hardware        Insert Hardware — each sub-folder is a hardware category
    handles         Fit Handles     — the handle designs
    library         Create Kitchen Template / New Kitchen — the master cabinet
                    library copied into every kitchen
    kitchens:<T>    Create Kitchen Template / New Kitchen / Finish Kitchen — the
                    folder holding the kitchens of project type T (B2B, B2C, …)

Out of the box each one is found BY NAME from config.py (project names and folder
names, searched in the active hub). The Folders Selection command lets the user
point any of them at an exact folder instead — in any hub (team / tenant) and any
project — and that choice is stored here and wins over config.py.

A selection is stored by ID (hub, project, folder) with the display names beside
them for the dialog and for a name-based fallback, in folders.json next to the
other WoodCraft data files. Resolving by ID is also much faster than the old
name search, which listed every project of every hub on each run.

Resolved folders are cached for the session (DataFolder objects stay valid), so
only the first command to need a folder pays for the network round trip.
"""

import json
import os
import time

import adsk.core

from . import file_cache
from . import sheets_store
from .. import config

app = adsk.core.Application.get()

# Resolved DataFolders: key -> (selection dict it was resolved from, DataFolder)
_resolved = {}


# ---------------------------------------------------------------------------
# The references the tools use
# ---------------------------------------------------------------------------
def _kitchen_types():
    types = getattr(config, 'KITCHEN_PROJECT_TYPES', ()) or ()
    if isinstance(types, str):
        types = [types]
    return [t for t in (str(x).strip() for x in types) if t]


def kitchens_key(project_type):
    return f'kitchens:{(project_type or "").strip()}'


def targets():
    """[(key, label, what it is used for, default location text)] in dialog order."""
    hw = config.HARDWARE_PROJECT_NAME
    out = [
        ('hardware', 'Hardware library',
         'Insert Hardware reads its categories from the sub-folders of this folder.',
         f'project "{hw}" (root)'),
        ('handles', 'Handles',
         'Fit Handles offers the designs in this folder.',
         f'project "{hw}" / Handles'),
        ('library', 'Master cabinet library',
         'Create Kitchen Template and New Kitchen copy this folder into every kitchen.',
         f'project "{config.LIBRARY_PROJECT_NAME}" / '
         f'{config.LIBRARY_SOURCE_FOLDER or "(root)"}'),
    ]
    for t in _kitchen_types():
        out.append((kitchens_key(t), f'{t} kitchens',
                    f'Where {t} kitchen templates and customer kitchens are kept.',
                    ' / '.join(p for p in (f'project "{config.KITCHENS_PROJECT_NAME}"',
                                           t, config.KITCHENS_FOLDER_NAME) if p)))
    return out


def label_of(key):
    for k, label, _use, _default in targets():
        if k == key:
            return label
    return key


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def store_path():
    return os.path.join(sheets_store.library_dir(), 'folders.json')


def _clean(sel):
    if not isinstance(sel, dict):
        return None
    out = {}
    for k in ('hub_id', 'hub_name', 'project_id', 'project_name', 'folder_id'):
        v = sel.get(k)
        out[k] = v.strip() if isinstance(v, str) else ''
    path = sel.get('path')
    out['path'] = [str(p) for p in path] if isinstance(path, list) else []
    if not (out['project_id'] or out['project_name']):
        return None
    return out


def _load_from_disk():
    try:
        with open(store_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, sel in data.items():
        sel = _clean(sel)
        if sel:
            out[str(key)] = sel
    return out


def load():
    """{key: selection} for every reference the user has pointed somewhere."""
    return file_cache.cached(store_path(), _load_from_disk)


def save_all(selections):
    """Replace the whole store. `selections` maps key -> selection or None
    (None = back to the config.py default)."""
    cleaned = {}
    for key, sel in (selections or {}).items():
        sel = _clean(sel)
        if sel:
            cleaned[key] = sel
    os.makedirs(sheets_store.library_dir(), exist_ok=True)
    with open(store_path(), 'w', encoding='utf-8') as f:
        json.dump(cleaned, f, indent=2, ensure_ascii=False)
    file_cache.forget(store_path())
    invalidate()
    forget_listings()
    return cleaned


def get(key):
    """The saved selection for `key`, or None when it uses the config.py default."""
    return load().get(key)


def is_configured(key):
    return get(key) is not None


def describe(sel):
    """'Hub › Project / A / B' for a selection dict."""
    if not sel:
        return ''
    parts = [sel.get('project_name') or '?'] + list(sel.get('path') or [])
    where = ' / '.join(parts)
    hub = sel.get('hub_name')
    return f'{hub} › {where}' if hub else where


def selection_for(hub, project, folder_chain):
    """A storable selection for a folder reached as hub → project → chain, where
    `folder_chain` is [rootFolder, sub, sub, …] (the last one is the pick)."""
    target = folder_chain[-1]
    return {
        'hub_id': _safe(lambda: hub.id),
        'hub_name': _safe(lambda: hub.name),
        'project_id': _safe(lambda: project.id),
        'project_name': _safe(lambda: project.name),
        'folder_id': _safe(lambda: target.id),
        'path': [_safe(lambda f=f: f.name) for f in folder_chain[1:]],
    }


def _safe(fn, default=''):
    try:
        value = fn()
        return value if value is not None else default
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Resolving a selection to a live DataFolder
# ---------------------------------------------------------------------------
def invalidate(key=None):
    """Forget resolved folders (all, or one) — the next resolve() goes back to
    the cloud. Called after a save and by the tools' Refresh buttons."""
    if key is None:
        _resolved.clear()
    else:
        _resolved.pop(key, None)


def _valid(folder):
    try:
        return bool(folder) and folder.isValid
    except Exception:
        return bool(folder)


def _find_hub(sel):
    try:
        hubs = app.data.dataHubs
        count = hubs.count
    except Exception:
        return None
    by_name = None
    for i in range(count):
        try:
            hub = hubs.item(i)
            if sel['hub_id'] and hub.id == sel['hub_id']:
                return hub
            if by_name is None and sel['hub_name'] and hub.name == sel['hub_name']:
                by_name = hub
        except Exception:
            continue
    return by_name


def _find_project(hub, sel):
    try:
        projects = hub.dataProjects
    except Exception:
        return None
    if sel['project_id']:
        try:
            project = projects.itemById(sel['project_id'])
            if project:
                return project
        except Exception:
            pass
    try:
        for i in range(projects.count):
            project = projects.item(i)
            if (sel['project_id'] and project.id == sel['project_id']) or \
                    project.name == sel['project_name']:
                return project
    except Exception:
        pass
    return None


def _walk(folder, names):
    for name in names:
        if not folder:
            return None
        try:
            nxt = folder.dataFolders.itemByName(name)
        except Exception:
            nxt = None
        if not nxt:
            try:
                subs = folder.dataFolders
                target = name.strip().lower()
                for i in range(subs.count):
                    sub = subs.item(i)
                    if sub.name.strip().lower() == target:
                        nxt = sub
                        break
            except Exception:
                nxt = None
        folder = nxt
    return folder


def resolve_selection(sel):
    """The live DataFolder a selection points at, or None.

    Fastest first: straight to the folder by id; then hub → project by id and
    down the saved path by name (covers a folder that was deleted and re-made
    with the same name)."""
    if not sel:
        return None
    if sel.get('folder_id'):
        try:
            folder = app.data.findFolderById(sel['folder_id'])
            if _valid(folder):
                return folder
        except Exception:
            pass
    hub = _find_hub(sel)
    if not hub:
        return None
    project = _find_project(hub, sel)
    if not project:
        return None
    try:
        root = project.rootFolder
    except Exception:
        return None
    return _walk(root, sel.get('path') or [])


def resolve(key):
    """(configured, folder) for a reference.

    configured False → the user never picked one; the tool falls back to its
    config.py name search. configured True with folder None → the picked folder
    can't be reached (deleted, no access, other hub offline) — the tool should
    say so and point at Folders Selection rather than silently guessing."""
    sel = get(key)
    if not sel:
        return False, None
    hit = _resolved.get(key)
    if hit and hit[0] == sel and _valid(hit[1]):
        return True, hit[1]
    folder = resolve_selection(sel)
    if folder:
        _resolved[key] = (sel, folder)
    return True, folder


def missing_message(key):
    """A ready-to-show sentence for a configured folder that can't be found."""
    return (f"The folder chosen for '{label_of(key)}' ({describe(get(key))}) "
            f"can't be found. Pick it again with Folders Selection.")


# ---------------------------------------------------------------------------
# A small time-limited cache the tools share for folder listings
# ---------------------------------------------------------------------------
_listing_cache = {}      # (tag, folder id) -> (time, value)
LISTING_TTL = 600        # seconds; Refresh buttons and saves clear it sooner


def cached_listing(tag, folder, build):
    """build(folder) memoised per folder for LISTING_TTL seconds."""
    try:
        key = (tag, folder.id)
    except Exception:
        return build(folder)
    hit = _listing_cache.get(key)
    now = time.time()
    if hit and now - hit[0] < LISTING_TTL:
        return hit[1]
    value = build(folder)
    _listing_cache[key] = (now, value)
    return value


def forget_listings(tag=None):
    if tag is None:
        _listing_cache.clear()
    else:
        for key in [k for k in _listing_cache if k[0] == tag]:
            _listing_cache.pop(key, None)
