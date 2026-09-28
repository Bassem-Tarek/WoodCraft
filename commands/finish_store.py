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

"""Which materials and appearances Set Finish offers — a global JSON list store.

Set Finish used to fill its dropdowns from Fusion's Favorites library, which meant
one shared shortlist for four different questions. A carcass is made of MDF or
melamine; a door front is a decor. Offering the same list for both is noise, so each
of the four dropdowns now has its OWN curated list of names, kept here:

    carcass_material    door_material       — physical materials
    carcass_appearance  door_appearance     — appearances

The file stores NAMES, not material objects, because that is what survives a library
being reloaded, re-imported or updated: the name is the stable handle a cabinetmaker
actually thinks in. Resolving a name to a live Fusion object is the job of
commands/material_pool.py — deliberately not this module, which stays pure Python so
it can be reasoned about and tested without Fusion.

`libraries` narrows where those names are looked up. Empty (the default) means every
loaded material library, which is what makes 'MDF' (Assets Library) and
'MFC - Melamine Face Chipboard' (Favorites Library) resolve alongside the Emaar
Library decors without anyone configuring anything. Narrow it later if two libraries
start disagreeing about a name.

Lives next to settings.json and the stock-sheet library (sheets_store.library_dir())
so all WoodCraft user data travels together. Edited in Fusion by the Finish Lists
command; hand-editable too, and malformed content degrades to the defaults rather
than raising.
"""

import json
import os

from . import file_cache
from . import sheets_store

# Category keys. These are the JSON keys AND the identifiers passed around in code,
# so a new category (say a plinth or a worktop list) is added here and picked up by
# both the editor dialog and Set Finish without either learning about it separately.
CARCASS_MATERIAL = 'carcass_material'
CARCASS_APPEARANCE = 'carcass_appearance'
DOOR_MATERIAL = 'door_material'
DOOR_APPEARANCE = 'door_appearance'

# (key, label shown in the UI, kind) where kind selects which side of a material
# library the names are looked up in: its materials or its appearances.
KIND_MATERIAL = 'material'
KIND_APPEARANCE = 'appearance'

CATEGORIES = (
    (CARCASS_MATERIAL, 'Carcass Material', KIND_MATERIAL),
    (CARCASS_APPEARANCE, 'Carcass Appearance', KIND_APPEARANCE),
    (DOOR_MATERIAL, 'Door Material', KIND_MATERIAL),
    (DOOR_APPEARANCE, 'Door Appearance', KIND_APPEARANCE),
)

CATEGORY_KEYS = tuple(key for key, _label, _kind in CATEGORIES)
CATEGORY_LABELS = {key: label for key, label, _kind in CATEGORIES}
CATEGORY_KINDS = {key: kind for key, _label, kind in CATEGORIES}

# Shipped defaults. The two carcass/door materials are the ones asked for and are
# plain ASCII, so they are safe to hard-code. The APPEARANCE lists are deliberately
# empty here and seeded on first run from the user's own Favorites library (see
# material_pool.seed_appearance_defaults) — those decor names carry accents and
# mixed case, and copying them by hand is exactly how a name stops matching.
DEFAULTS = {
    'libraries': [],
    CARCASS_MATERIAL: ['MDF', 'MFC - Melamine Face Chipboard'],
    DOOR_MATERIAL: ['MDF', 'MFC - Melamine Face Chipboard'],
    CARCASS_APPEARANCE: [],
    DOOR_APPEARANCE: [],
}


def lists_path():
    """Absolute path to the global finish-lists JSON file."""
    return os.path.join(sheets_store.library_dir(), 'finish_lists.json')


def exists() -> bool:
    """True if the file has been written at least once. Callers use this to decide
    whether to seed first-run defaults rather than silently re-seeding a list the
    user has deliberately emptied."""
    try:
        return os.path.isfile(lists_path())
    except Exception:
        return False


def _clean_names(value):
    """A list of unique, non-empty, stripped names, order preserved.

    Order is the order they appear in the dropdown, so it is the user's and must
    survive a round trip. De-duplication is case-insensitive because that is how the
    names are matched against a library."""
    out, seen = [], set()
    if isinstance(value, (list, tuple)):
        for item in value:
            # Strings only. str(item) would turn a JSON null into the literal name
            # "None" and a nested object into "{'a': 1}" — a malformed entry should
            # disappear, not become a material nothing will ever match.
            if not isinstance(item, str):
                continue
            name = item.strip()
            if not name:
                continue
            fold = name.casefold()
            if fold in seen:
                continue
            seen.add(fold)
            out.append(name)
    return out


def normalize(data):
    """A full, well-formed store dict: DEFAULTS overlaid with whatever is usable in
    `data`. Never raises — a hand-edited file with one bad key still loads."""
    out = {key: list(value) for key, value in DEFAULTS.items()}
    if isinstance(data, dict):
        for key in CATEGORY_KEYS:
            if key in data:
                out[key] = _clean_names(data.get(key))
        if 'libraries' in data:
            out['libraries'] = _clean_names(data.get('libraries'))
    return out


def load():
    """Store dict from disk, defaults for anything missing or corrupt. Never raises
    and never writes. Cached until the file changes (see file_cache)."""
    return file_cache.cached(lists_path(), _load_from_disk)


def _load_from_disk():
    try:
        with open(lists_path(), 'r', encoding='utf-8') as f:
            return normalize(json.load(f))
    except Exception:
        return normalize(None)


def save(data):
    """Write the (normalized) store, creating the folder if needed. Returns the dict
    actually written. UTF-8 with ensure_ascii=False so a decor called 'CRÈME' reads
    as itself when the file is opened in an editor."""
    cleaned = normalize(data)
    os.makedirs(sheets_store.library_dir(), exist_ok=True)
    with open(lists_path(), 'w', encoding='utf-8') as f:
        json.dump(cleaned, f, indent=2, ensure_ascii=False)
    file_cache.forget(lists_path())
    return cleaned


def get_names(category):
    """The configured names for one category, in dropdown order."""
    return load().get(category, [])


def set_names(category, names):
    """Replace one category's list, leaving the others untouched."""
    data = load()
    data[category] = _clean_names(names)
    return save(data)


def get_libraries():
    """Names of the material libraries to search, or [] meaning 'all loaded ones'."""
    return load().get('libraries', [])
