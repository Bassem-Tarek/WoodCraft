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

"""Global add-on settings for WoodCraft — a tiny key/value JSON store.

Lives next to the stock-sheet library (same folder, see sheets_store.library_dir)
so all WoodCraft user data travels together. Settings are add-on-wide, not
per-design: they tune how reports are computed, not what the model contains.

Current keys:
    waste_percent — % added on top of a panel's raw area when estimating its
        cost from sheet prices in the BOM (nesting never uses 100% of a sheet,
        so a pure area × rate estimate would systematically undershoot).
    kitchen_project_type — the project type (B2B / B2C) last picked in Create
        Kitchen Template or New Kitchen, pre-selected next time.

New settings plug in by adding a key to DEFAULTS; load() overlays the file on
the defaults so older files simply pick up new keys. No Fusion API here — this
module is unit-testable with plain Python.
"""

import json
import os

from . import file_cache
from . import sheets_store

DEFAULTS = {
    'waste_percent': 10.0,
    'kitchen_project_type': '',
}


def settings_path():
    """Absolute path to the global settings JSON file."""
    return os.path.join(sheets_store.library_dir(), 'settings.json')


def _num(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize(data):
    """A full settings dict: DEFAULTS overlaid with whatever is usable in `data`."""
    out = dict(DEFAULTS)
    if isinstance(data, dict):
        out['waste_percent'] = max(0.0, _num(data.get('waste_percent'),
                                              DEFAULTS['waste_percent']))
        kind = data.get('kitchen_project_type')
        out['kitchen_project_type'] = kind.strip() if isinstance(kind, str) else ''
    return out


def load():
    """Settings dict from disk, defaults for anything missing/corrupt. Never
    raises and never writes. Cached until the file changes (see file_cache)."""
    return file_cache.cached(settings_path(), _load_from_disk)


def _load_from_disk():
    try:
        with open(settings_path(), 'r', encoding='utf-8') as f:
            return normalize(json.load(f))
    except Exception:
        return dict(DEFAULTS)


def save(settings):
    """Write (normalized) settings to disk, creating the folder if needed.
    Returns the dict actually written."""
    cleaned = normalize(settings)
    os.makedirs(sheets_store.library_dir(), exist_ok=True)
    with open(settings_path(), 'w', encoding='utf-8') as f:
        json.dump(cleaned, f, indent=2)
    file_cache.forget(settings_path())
    return cleaned


def get_waste_percent():
    """Panel-cost waste factor in percent (e.g. 10.0 → estimates cost 10% more
    area than the panel's raw footprint)."""
    return load()['waste_percent']


def set_waste_percent(value):
    settings = load()
    settings['waste_percent'] = value
    return save(settings)['waste_percent']


def get_kitchen_project_type():
    """The kitchen project type picked last time ('' if never)."""
    return load()['kitchen_project_type']


def set_kitchen_project_type(value):
    settings = load()
    settings['kitchen_project_type'] = value or ''
    return save(settings)['kitchen_project_type']
