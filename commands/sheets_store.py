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

"""Global stock library for WoodCraft — Fusion-style Material → Sheets, plus edgebands.

Mirrors how Fusion's Nesting "Process Material Library" is organised: a **material**
(its Fusion material name + thickness + category + a display colour) holds one or more
**sheets** (Fusion calls them "packagings") — each a stock board size with its own
nesting params. Cut List & Nest matches every panel to the material of its Fusion
material name + thickness, then nests it on a chosen sheet.

The same file also carries the **edgeband** catalogue: the banding rolls (name, band
thickness, roll width, cost per metre) the Edgeband command offers and the BOM prices
tagged edges against. One JSON file = the whole purchasable stock.

The library is a single portable JSON file shared across designs (so you define your
stock once, and can hand the file to someone else). No Fusion API is used here, so
this module is unit-testable with plain Python like `nesting.py`.

Library shape (sizes in millimetres; edgeband cost is per METRE of banding):
    {"materials": [
        {"name": "MDF Medium Density Fiberboard", "thickness": 18.0,
         "category": "Boards", "color": "#C9A86A", "comment": "",
         "sheets": [
            {"name": "Standard", "length": 2440, "width": 1220, "form": "Rectangular",
             "cost": 0.0, "rotation": "all", "separation": 3.0, "trim": 10.0,
             "comment": ""}]}],
     "edgebands": [
        {"name": "PVC White 0.8", "thickness": 0.8, "width": 22.0,
         "cost": 0.0, "color": "#F2F2F2", "comment": ""}]}

`rotation` is one of: 'all' | 'none' | '90_270' | '180' (only 'all'/'90_270' let our
guillotine packer rotate a part 90°; '180' doesn't change a rectangle's footprint).
"""

import json
import os

from . import file_cache

ROTATIONS = ('all', 'none', '90_270', '180')
DEFAULT_COLOR = '#C9A86A'

# Seeded into a fresh / unreadable library so there's something to edit and match.
DEFAULT_LIBRARY = {
    "materials": [
        {"name": "MDF", "thickness": 18.0, "category": "Boards", "color": "#C9A86A",
         "comment": "",
         "sheets": [{"name": "Standard", "length": 2440.0, "width": 1220.0,
                     "form": "Rectangular", "cost": 0.0, "rotation": "all",
                     "separation": 3.0, "trim": 10.0, "comment": ""}]},
        {"name": "Plywood", "thickness": 18.0, "category": "Boards", "color": "#D6B894",
         "comment": "",
         "sheets": [{"name": "Standard", "length": 2440.0, "width": 1220.0,
                     "form": "Rectangular", "cost": 0.0, "rotation": "all",
                     "separation": 3.0, "trim": 10.0, "comment": ""}]},
    ]
}

# Seeded when the library has no edgeband section yet (fresh install or a file
# saved by a pre-edgeband version) — three common banding types to edit/price.
DEFAULT_EDGEBANDS = [
    {"name": "PVC White 0.8 mm", "thickness": 0.8, "width": 22.0, "cost": 0.0,
     "color": "#F2F2F2", "comment": ""},
    {"name": "PVC Oak 2 mm", "thickness": 2.0, "width": 22.0, "cost": 0.0,
     "color": "#C9A86A", "comment": ""},
    {"name": "ABS Anthracite 1 mm", "thickness": 1.0, "width": 22.0, "cost": 0.0,
     "color": "#4A4A4A", "comment": ""},
]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def library_dir():
    """Folder holding the library file, per platform."""
    appdata = os.environ.get("APPDATA")
    if appdata:  # Windows
        return os.path.join(appdata, "WoodCraft")
    home = os.path.expanduser("~")
    mac = os.path.join(home, "Library", "Application Support")
    if os.path.isdir(mac):  # macOS
        return os.path.join(mac, "WoodCraft")
    return os.path.join(home, ".woodcraft")  # fallback


def library_path():
    """Absolute path to the global library JSON file."""
    return os.path.join(library_dir(), "sheets.json")


# ---------------------------------------------------------------------------
# Coercion / validation
# ---------------------------------------------------------------------------
def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_sheet(row):
    """Coerce a raw sheet into a clean dict, or None if it has no usable size."""
    if not isinstance(row, dict):
        return None
    length = _num(row.get("length"))
    width = _num(row.get("width"))
    if length <= 0 or width <= 0:
        return None
    rotation = str(row.get("rotation", "all")).strip().lower()
    if rotation not in ROTATIONS:
        rotation = "all"
    return {
        "name": (str(row.get("name", "")).strip() or "Sheet"),
        "length": round(length, 3),
        "width": round(width, 3),
        "form": (str(row.get("form", "")).strip() or "Rectangular"),
        "cost": round(_num(row.get("cost")), 4),
        "rotation": rotation,
        "separation": round(max(0.0, _num(row.get("separation"))), 3),
        "trim": round(max(0.0, _num(row.get("trim"))), 3),
        "comment": str(row.get("comment", "") or ""),
    }


def normalize_material(row):
    """Coerce a raw material into a clean dict, or None if unusable. A material
    needs a name + positive thickness; it may carry zero sheets (still editable)."""
    if not isinstance(row, dict):
        return None
    name = str(row.get("name", "")).strip()
    thickness = _num(row.get("thickness"))
    if not name or thickness <= 0:
        return None
    color = (str(row.get("color", "")).strip() or DEFAULT_COLOR)
    sheets = [s for s in (normalize_sheet(x) for x in (row.get("sheets") or [])) if s]
    return {
        "name": name,
        "thickness": round(thickness, 3),
        "category": str(row.get("category", "") or "").strip(),
        "color": color,
        "comment": str(row.get("comment", "") or ""),
        "sheets": sheets,
    }


def clean_materials(rows):
    """Normalize a list of materials, dropping any that aren't usable."""
    out = []
    for row in rows or []:
        norm = normalize_material(row)
        if norm:
            out.append(norm)
    return out


def normalize_edgeband(row):
    """Coerce a raw edgeband into a clean dict, or None if unusable. A band only
    NEEDS a name — the Edgeband command tags faces by name; thickness/width/cost
    are catalogue data the BOM uses when present. `cost` is per METRE."""
    if not isinstance(row, dict):
        return None
    name = str(row.get("name", "")).strip()
    if not name:
        return None
    return {
        "name": name,
        "thickness": round(max(0.0, _num(row.get("thickness"))), 3),
        "width": round(max(0.0, _num(row.get("width"))), 3),
        "cost": round(max(0.0, _num(row.get("cost"))), 4),
        "color": (str(row.get("color", "")).strip() or DEFAULT_COLOR),
        "comment": str(row.get("comment", "") or ""),
    }


def clean_edgebands(rows):
    """Normalize a list of edgebands, dropping unusable ones and duplicate names
    (first wins — face attributes reference bands BY NAME, so it must be unique)."""
    out = []
    seen = set()
    for row in rows or []:
        norm = normalize_edgeband(row)
        if not norm:
            continue
        key = norm["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
    return out


# ---------------------------------------------------------------------------
# Migration from the old flat format
# ---------------------------------------------------------------------------
def migrate_flat(sheets):
    """Convert the old flat list [{material,thickness,length,width,cost}] into the
    nested model: group by (material, thickness) → one material, each old row a
    'Standard' sheet under it. Preserves data saved by the first Sheets version."""
    groups = {}
    order = []
    for s in sheets or []:
        if not isinstance(s, dict):
            continue
        mat = str(s.get("material", "")).strip()
        th = _num(s.get("thickness"))
        if not mat or th <= 0:
            continue
        key = (mat.lower(), round(th, 1))
        if key not in groups:
            groups[key] = {"name": mat, "thickness": round(th, 3), "category": "",
                           "color": DEFAULT_COLOR, "comment": "", "sheets": []}
            order.append(key)
        groups[key]["sheets"].append({
            "name": "Standard", "length": _num(s.get("length")),
            "width": _num(s.get("width")), "form": "Rectangular",
            "cost": _num(s.get("cost")), "rotation": "all",
            "separation": 3.0, "trim": 0.0, "comment": "",
        })
    return clean_materials([groups[k] for k in order])


def _materials_from_data(data):
    """Extract a materials list from any supported on-disk shape (new, old-flat,
    or a bare list), already cleaned. Empty list if nothing usable."""
    if isinstance(data, dict) and "materials" in data:
        return clean_materials(data["materials"])
    if isinstance(data, dict) and "sheets" in data:
        return migrate_flat(data["sheets"])
    if isinstance(data, list):
        return migrate_flat(data)
    return []


def _edgebands_from_data(data):
    """Cleaned edgebands from an on-disk shape, or None when the file predates the
    edgeband section entirely. The distinction matters: an absent key means 'seed
    the defaults', while a present-but-empty list means the user deleted them all
    on purpose and must stay empty."""
    if isinstance(data, dict) and "edgebands" in data:
        return clean_edgebands(data["edgebands"])
    return None


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------
def load():
    """Return {'materials': [...], 'edgebands': [...]} from the global library,
    migrating the old flat format if needed. Missing/corrupt/empty → defaults
    (DEFAULT_LIBRARY materials + DEFAULT_EDGEBANDS). Never raises, never writes.
    Cached until the file changes (see file_cache) — reports call this often."""
    return file_cache.cached(library_path(), _load_from_disk)


def _load_from_disk():
    try:
        with open(library_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        materials = _materials_from_data(data)
        if materials:
            edgebands = _edgebands_from_data(data)
            if edgebands is None:
                edgebands = clean_edgebands(DEFAULT_EDGEBANDS)
            return {"materials": materials, "edgebands": edgebands}
    except Exception:
        pass
    return {"materials": clean_materials(DEFAULT_LIBRARY["materials"]),
            "edgebands": clean_edgebands(DEFAULT_EDGEBANDS)}


def save(materials, edgebands=None):
    """Write the (cleaned) library to the global file, creating the folder if
    needed. `edgebands=None` preserves whatever the file already holds (so a
    caller that only edits materials can't wipe the band catalogue). Returns the
    {'materials', 'edgebands'} dict actually written."""
    if edgebands is None:
        edgebands = load()["edgebands"]
    cleaned = {"materials": clean_materials(materials),
               "edgebands": clean_edgebands(edgebands)}
    os.makedirs(library_dir(), exist_ok=True)
    with open(library_path(), "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2)
    file_cache.forget(library_path())
    return cleaned


def write_path(path, materials, edgebands=None):
    """Export: write a (cleaned) library to an arbitrary path. `edgebands=None`
    exports the on-disk catalogue so a shared file is complete. Returns the dict."""
    if edgebands is None:
        edgebands = load()["edgebands"]
    cleaned = {"materials": clean_materials(materials),
               "edgebands": clean_edgebands(edgebands)}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2)
    return cleaned


def read_path(path):
    """Import: read + validate a library from an arbitrary path (any supported
    shape). Returns {'materials': [...], 'edgebands': [...] | None} — edgebands
    None when the file has no band section (caller keeps its current ones).
    Raises on unreadable file / invalid JSON so the caller can report it."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {"materials": _materials_from_data(data),
            "edgebands": _edgebands_from_data(data)}


# ---------------------------------------------------------------------------
# Matching helpers (used by Cut List)
# ---------------------------------------------------------------------------
def find_material(materials, name, thickness, tol=0.5):
    """The material whose name (trimmed, case-insensitive) and thickness (within
    `tol` mm) match a panel, or None."""
    want = str(name or "").strip().lower()
    if not want:
        return None
    for m in materials:
        if str(m.get("name", "")).strip().lower() != want:
            continue
        if abs(_num(m.get("thickness")) - _num(thickness)) <= tol:
            return m
    return None


def cost_rate_per_m2(material):
    """Average cost per square metre across this material's PRICED sheets, or None
    if the material is missing / no sheet has a cost. Each sheet contributes its
    own rate (cost / raw L×W area) and the rates are averaged — averaging the
    costs themselves would be meaningless across different sheet sizes. Raw area
    (not trim-adjusted) on purpose: this feeds a rough BOM estimate; the real
    spend is the Cut List's sheets-used × sheet-cost."""
    if not material:
        return None
    rates = []
    for s in material.get('sheets') or []:
        cost = _num(s.get('cost'))
        length = _num(s.get('length'))
        width = _num(s.get('width'))
        if cost > 0 and length > 0 and width > 0:
            rates.append(cost / (length * width / 1e6))
    if not rates:
        return None
    return sum(rates) / len(rates)


def find_edgeband(edgebands, name):
    """The edgeband whose name matches (trimmed, case-insensitive), or None.
    Face attributes store the band NAME, so this is the report-time join."""
    want = str(name or "").strip().lower()
    if not want:
        return None
    for band in edgebands or []:
        if str(band.get("name", "")).strip().lower() == want:
            return band
    return None


def edgeband_cost_per_m(band):
    """This band's cost per metre, or None when the band is missing/unpriced —
    None (not 0) so reports can flag 'tagged but unpriced' edges explicitly."""
    if not band:
        return None
    cost = _num(band.get("cost"))
    return cost if cost > 0 else None


def rotation_allows_rotation(code):
    """Does this rotation setting let our guillotine packer rotate a part 90°?
    'all'/'90_270' yes; 'none'/'180' no (180° doesn't change a rectangle)."""
    return str(code or "all").strip().lower() in ("all", "90_270")
