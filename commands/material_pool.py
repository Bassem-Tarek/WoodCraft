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

"""Turning the names in finish_store into live Fusion materials and appearances.

The split of responsibilities: finish_store.py owns WHICH names are offered (pure
Python, testable, hand-editable JSON); this module owns FINDING them in Fusion's
loaded material libraries and copying them into a design. Keeping the store free of
Fusion imports is what lets the list survive a library being renamed, re-imported or
temporarily unavailable — the name stays, the lookup simply comes up empty until the
library is back.

Names are matched case- and whitespace-insensitively. A decor is spelled
'K022 SN - Satin Blackwood' as a material and 'K022 SN - SATIN BLACKWOOD' as an
appearance in the very libraries this add-in reads, and no cabinetmaker should have
to care which one they typed.
"""

import adsk.core
import adsk.fusion

from . import finish_store
from ..lib import fusionAddInUtils as futil

app = adsk.core.Application.get()


def fold(name) -> str:
    """Match key for a material/appearance name: case- and whitespace-insensitive.

    A decor is spelled 'K022 SN - Satin Blackwood' as a material and
    'K022 SN - SATIN BLACKWOOD' as an appearance in these very libraries, so every
    comparison in this add-in goes through here."""
    return ' '.join(str(name or '').split()).casefold()


_fold = fold        # internal alias, kept so call sites read as private helpers


# ---------------------------------------------------------------------------
# Libraries
# ---------------------------------------------------------------------------
def library_names():
    """Every loaded material library, in Fusion's own order."""
    out = []
    try:
        libs = app.materialLibraries
        for i in range(libs.count):
            try:
                out.append(libs.item(i).name)
            except Exception:
                continue
    except Exception:
        futil.log('WoodCraft: could not read the material libraries')
    return out


def _selected_libraries(wanted=None):
    """The library objects to search: those named in `wanted` (or the store's
    configured list), else EVERY loaded library.

    Empty means all, deliberately — it is what makes a shortlist spanning the Emaar
    Library, the Assets Library and Favorites work with no configuration at all.
    Unknown names are skipped rather than raising, so a library that hasn't been
    imported on this machine costs nothing."""
    if wanted is None:
        wanted = finish_store.get_libraries()
    keep = {_fold(n) for n in wanted} if wanted else None

    out = []
    try:
        libs = app.materialLibraries
        for i in range(libs.count):
            try:
                lib = libs.item(i)
            except Exception:
                continue
            if keep is None or _fold(lib.name) in keep:
                out.append(lib)
    except Exception:
        futil.log('WoodCraft: could not read the material libraries')
    return out


def _collection(library, kind):
    """A library's materials or appearances, or None if it has neither."""
    try:
        return library.materials if kind == finish_store.KIND_MATERIAL \
            else library.appearances
    except Exception:
        return None


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------
def available(kind, libraries=None):
    """[(name, library name, object)] of everything of `kind` in the searched
    libraries, de-duplicated by name.

    First occurrence wins, so library order decides who owns a shared name — the
    Favorites library legitimately holds two different appearances both called
    '0101 PE - FRONT WHITE', and a name-keyed list can only mean one of them.
    Sorted by name so a 324-entry library is navigable in a dropdown.

    Cached: a full library scan reads every entry's name through the API (the
    appearance libraries hold thousands), and Set Finish asks for four lists
    when its dialog opens and again on OK. The cache key is each searched
    library's name and entry count, so importing, removing or editing a library
    is picked up on the next call."""
    selected = _selected_libraries(libraries)
    key = [kind]
    for library in selected:
        collection = _collection(library, kind)
        try:
            key.append((library.name, collection.count if collection else 0))
        except Exception:
            key.append(('?', 0))
    key = tuple(key)
    hit = _available_cache.get(key)
    if hit is not None and all(_alive(obj) for _n, _l, obj in hit[:1]):
        return list(hit)
    found = _scan(selected, kind)
    _available_cache[key] = found
    return list(found)


_available_cache = {}


def _alive(obj):
    try:
        return obj.isValid if hasattr(obj, 'isValid') else True
    except Exception:
        return False


def clear_cache():
    _available_cache.clear()


def _scan(selected, kind):
    found, seen = [], set()
    for library in selected:
        collection = _collection(library, kind)
        if not collection:
            continue
        try:
            lib_name = library.name
        except Exception:
            lib_name = ''
        try:
            count = collection.count
        except Exception:
            continue
        for i in range(count):
            try:
                item = collection.item(i)
                name = item.name
            except Exception:
                continue
            key = _fold(name)
            if not key or key in seen:
                continue
            seen.add(key)
            found.append((name, lib_name, item))
    found.sort(key=lambda row: _fold(row[0]))
    return found


def lookup(kind, libraries=None):
    """{folded name: object} for fast resolution of a configured list."""
    return {_fold(name): obj for name, _lib, obj in available(kind, libraries)}



def resolve(category, libraries=None):
    """(found, missing) for one finish_store category.

    `found` is [(name, object)] in the order the JSON lists them — that order is the
    user's choice and is what the dropdown shows. `missing` is the configured names
    that no searched library currently offers; the caller surfaces those rather than
    dropping them silently, because a name vanishing from a dropdown with no
    explanation is indistinguishable from the command being broken."""
    kind = finish_store.CATEGORY_KINDS.get(category, finish_store.KIND_MATERIAL)
    index = lookup(kind, libraries)

    found, missing = [], []
    for name in finish_store.get_names(category):
        obj = index.get(_fold(name))
        if obj is None:
            missing.append(name)
        else:
            found.append((name, obj))
    return found, missing


# ---------------------------------------------------------------------------
# First-run seeding
# ---------------------------------------------------------------------------
def seed_appearance_defaults():
    """Write the shipped defaults on first run, filling the two appearance lists
    from the user's Favorites library.

    Why not hard-code them like the materials: decor names carry accents and
    inconsistent case ('7031 BS - CRÈME'), and a name transcribed by hand is a name
    that silently stops matching. Reading them from Fusion copies the exact strings.

    Does nothing once the file exists, so a list the user has deliberately emptied
    stays empty."""
    if finish_store.exists():
        return None

    names = []
    seen = set()
    try:
        favorites = app.favoriteAppearances
        for i in range(favorites.count):
            try:
                name = favorites.item(i).name
            except Exception:
                continue
            key = _fold(name)
            if key and key not in seen:
                seen.add(key)
                names.append(name)
    except Exception:
        futil.log('WoodCraft: could not read the favourite appearances while seeding')

    data = dict(finish_store.DEFAULTS)
    data[finish_store.CARCASS_APPEARANCE] = list(names)
    data[finish_store.DOOR_APPEARANCE] = list(names)
    written = finish_store.save(data)
    futil.log(f'WoodCraft: seeded {finish_store.lists_path()} '
              f'with {len(names)} appearance(s)')
    return written


# ---------------------------------------------------------------------------
# Assignment helpers
# ---------------------------------------------------------------------------
def material_in_design(design, material):
    """A design-local copy of `material`, ready to assign.

    A library material can't be handed straight to `Component.material` in every
    Fusion build. Copying it into the document's own collection once (and reusing
    that copy on later runs, matched by name) is the path that works everywhere and
    keeps the document self-contained. Falls back to the library material if the
    copy fails, so a quirk in one Fusion version degrades to "probably still works"
    rather than "nothing happens"."""
    if material is None:
        return None
    try:
        existing = design.materials.itemByName(material.name)
        if existing:
            return existing
    except Exception:
        pass
    try:
        copied = design.materials.addByCopy(material, material.name)
        if copied:
            return copied
    except Exception:
        futil.log(f'WoodCraft: could not copy material "{material.name}" into the design')
    return material
