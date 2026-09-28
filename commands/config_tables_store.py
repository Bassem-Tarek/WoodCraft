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

"""Configuration-tables profile for the Appearance Config command.

One portable JSON file describes everything design-independent about the
appearance configuration scheme: the carcass colours, the door-finish palette,
the name keywords that classify occurrences, where copyable appearances live,
and which cloud document is the last-resort appearance source. The engine
(appearance_tables.py) GENERATES rows, row names and cell values from these
lists — nothing anywhere assumes "2 carcasses" or "16 finishes", so growing the
palette is: add the appearance to the library file, add one entry here. The fix
pass then grows existing tables to match (missing rows are added, not rebuilt).

Shipped defaults are the Emaar scheme. No Fusion API is used here, so this
module is unit-testable with plain Python like `sheets_store.py`.

Profile shape (appearances matched by the LEADING CODE, never the full name —
copied appearance names are UPPERCASE and the Crème one is mojibaked upstream):
    {"carcasses": [{"name": "White", "code": "8685",
                    "appearance": "8685 PE - Snow White"}, ...],
     "finishes":  [{"code": "0101", "name": "0101 PE - Front White"}, ...],
     "keywords":  {"hardware": [...], "front": [...], "door": [...],
                   "carcass": [...], "exclude": [...]},
     "appearance_library": "C:/path/to/WoodCraftAppearances.adsklib" | "",
     "source_document": {"name": "WC_S2", "project": "Emaar Library",
                         "folder": "Corrected"}}
"""

import json
import os

from . import file_cache
from . import sheets_store  # reuse the same per-platform WoodCraft data folder


# The Crème finish name is built with an explicit escape so no editor/encoding
# round-trip can corrupt it (the source appearance upstream is already mojibaked;
# this is the clean Title Case name WoodCraft uses everywhere).
_CREME = '7031 BS - Cr\u00e8me'

DEFAULT_PROFILE = {
    "carcasses": [
        {"name": "White", "code": "8685", "appearance": "8685 PE - Snow White"},
        {"name": "Grey", "code": "K096", "appearance": "K096 SU - Clay Grey"},
    ],
    "finishes": [
        {"code": "0101", "name": "0101 PE - Front White"},
        {"code": "5437", "name": "5437 EE - Lino Canovas"},
        {"code": "5527", "name": "5527 SN - Stone Oak"},
        {"code": "5981", "name": "5981 PD - Cashmere"},
        {"code": "7031", "name": _CREME},
        {"code": "7045", "name": "7045 SU - Satin"},
        {"code": "8685", "name": "8685 PE - Snow White"},
        {"code": "K008", "name": "K008 PW - Light Select Walnut"},
        {"code": "K022", "name": "K022 SN - Satin Blackwood"},
        {"code": "K085", "name": "K085 PW - Light Rockford Hickory"},
        {"code": "K088", "name": "K088 PW - White Nordic Wood"},
        {"code": "K096", "name": "K096 SU - Clay Grey"},
        {"code": "K359", "name": "K359 PW - Brandy Castello Oak"},
        {"code": "K543", "name": "K543 SN - Sand Barbera Oak"},
        {"code": "K680", "name": "K680 PD - Stone Beige"},
        {"code": "K681", "name": "K681 PD - Macadamia"},
    ],
    "keywords": {
        # Order matters to the classifier: hardware wins, then front, then door,
        # then carcass. 'hinge assembly' — NEVER bare 'hinge', which also matches
        # 'Hinged door:1' and silently drops the doors of fridge tall units.
        "hardware": ["minifix", "dwell", "hinge assembly", "adjustable leg",
                     "shelf mount", "innotech", "9249", "19557", "165 degree",
                     "9088", "9099", "9047", "handle", "knob", "hettich", "blum",
                     "runner", "slide", "screw", "dowel"],
        # 'front' is checked before 'door'/'carcass' ('front panel' would
        # otherwise hit the 'panel' carcass keyword). A front resolves to the
        # door finish only when its cabinet has exactly ONE door.
        "front": ["fixed panel", "front panel"],
        "door": ["door", "drawer panel", "flap", "drawer"],
        "carcass": ["panel", "rail", "shelf", "partition"],
        # Top-level occurrences excluded from the table entirely (appliances,
        # room shell, trim). Everything excluded is still LISTED in the palette
        # so it can be pulled back in with one click — nothing is silent.
        "exclude": ["sink", "stove", "hob", "oven", "fridge", "refrigerator",
                    "dishwasher", "appliance", "window", "plinth", "filler",
                    "fill", "worktop", "countertop"],
    },
    # Local .adsklib appearance library (preferred source — works offline, no
    # document needed). Empty = fall through to open documents / cloud source.
    "appearance_library": "",
    # Last-resort appearance source: opened by cloud search when nothing local
    # has the appearances. Resolved by name+project+folder — never a lineage id,
    # those go stale when files are recreated.
    "source_document": {"name": "WC_S2", "project": "Emaar Library",
                        "folder": "Corrected"},
}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def profile_path():
    """Absolute path of the profile JSON, next to sheets.json."""
    return os.path.join(sheets_store.library_dir(), "config_tables.json")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _clean_entry(row, need_name=True):
    """Coerce a carcass/finish entry → {'code','name'[,'appearance']} or None."""
    if not isinstance(row, dict):
        return None
    code = str(row.get("code", "")).strip()
    name = str(row.get("name", "")).strip()
    if not code or (need_name and not name):
        return None
    out = {"code": code, "name": name or code}
    appearance = str(row.get("appearance", "")).strip()
    if appearance:
        out["appearance"] = appearance
    return out


def _clean_keywords(raw):
    """Lower-cased, de-duplicated keyword lists; missing groups fall back to the
    defaults (an absent group means 'not customised', not 'match nothing')."""
    out = {}
    for group, fallback in DEFAULT_PROFILE["keywords"].items():
        words = raw.get(group) if isinstance(raw, dict) else None
        if not isinstance(words, list):
            words = fallback
        seen, cleaned = set(), []
        for w in words:
            w = str(w).strip().lower()
            if w and w not in seen:
                seen.add(w)
                cleaned.append(w)
        out[group] = cleaned
    return out


def normalize_profile(data):
    """Coerce arbitrary JSON into a full, valid profile (defaults fill gaps)."""
    if not isinstance(data, dict):
        data = {}
    carcasses = [e for e in (_clean_entry(r) for r in data.get("carcasses") or [])
                 if e]
    finishes = [e for e in (_clean_entry(r) for r in data.get("finishes") or [])
                if e]
    if not carcasses:
        carcasses = [dict(e) for e in DEFAULT_PROFILE["carcasses"]]
    if not finishes:
        finishes = [dict(e) for e in DEFAULT_PROFILE["finishes"]]
    source = data.get("source_document")
    if not isinstance(source, dict) or not str(source.get("name", "")).strip():
        source = dict(DEFAULT_PROFILE["source_document"])
    else:
        source = {"name": str(source.get("name", "")).strip(),
                  "project": str(source.get("project", "")).strip(),
                  "folder": str(source.get("folder", "")).strip()}
    return {
        "carcasses": carcasses,
        "finishes": finishes,
        "keywords": _clean_keywords(data.get("keywords")),
        "appearance_library": str(data.get("appearance_library", "") or "").strip(),
        "source_document": source,
    }


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------
def load():
    """The profile from disk, normalized. Missing/corrupt → defaults. Never
    raises, never writes (first Save creates the file). Cached until the
    file changes (see file_cache)."""
    return file_cache.cached(profile_path(), _load_from_disk)


def _load_from_disk():
    try:
        with open(profile_path(), "r", encoding="utf-8") as f:
            return normalize_profile(json.load(f))
    except Exception:
        return normalize_profile(DEFAULT_PROFILE)


def save(profile):
    """Write the normalized profile; returns what was written."""
    cleaned = normalize_profile(profile)
    os.makedirs(sheets_store.library_dir(), exist_ok=True)
    with open(profile_path(), "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2)
    file_cache.forget(profile_path())
    return cleaned


# ---------------------------------------------------------------------------
# Derived data (row naming — the one format both build and fix must agree on)
# ---------------------------------------------------------------------------
def expected_row_names(profile, with_doors=True):
    """All appearance row names, in table order: every finish under carcass 1,
    then every finish under carcass 2, … ('#1-White-0101 PE - Front White').
    Doorless designs configure only the carcass: '#1-White', '#2-Grey'."""
    names = []
    i = 1
    if with_doors:
        for carcass in profile["carcasses"]:
            for finish in profile["finishes"]:
                names.append(f'#{i}-{carcass["name"]}-{finish["name"]}')
                i += 1
    else:
        for carcass in profile["carcasses"]:
            names.append(f'#{i}-{carcass["name"]}')
            i += 1
    return names


def parse_row_name(name, profile):
    """(carcass_entry, finish_entry|None) for an appearance row name, matching
    '#N-<carcass>[-<finish>]'. Finish is matched by its LEADING CODE so legacy
    rows with slightly different display names still resolve. (None, None) when
    the name doesn't follow the scheme (a hand-made row: the sweep leaves it)."""
    parts = str(name or "").split("-", 2)
    if len(parts) < 2 or not parts[0].startswith("#"):
        return None, None
    carcass = next((c for c in profile["carcasses"]
                    if c["name"].strip().lower() == parts[1].strip().lower()), None)
    if carcass is None:
        return None, None
    if len(parts) < 3:
        return carcass, None
    code = parts[2].strip().split(" ")[0].lower()
    finish = next((f for f in profile["finishes"]
                   if f["code"].strip().lower() == code), None)
    return carcass, finish


def needed_appearances(profile):
    """[{'code', 'name'}] every appearance the scheme uses (carcasses + door
    finishes), de-duplicated by code — the ensure-appearances checklist."""
    out, seen = [], set()
    for entry in list(profile["carcasses"]) + list(profile["finishes"]):
        code = entry["code"].strip().lower()
        if code in seen:
            continue
        seen.add(code)
        out.append({"code": entry["code"],
                    "name": entry.get("appearance") or entry["name"]})
    return out
