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

"""Memoised JSON-store loads.

The WoodCraft stores (sheets, settings, finish lists, appearance-table profile)
each re-read and re-validated their JSON file on EVERY load(), and several
commands call load() repeatedly while a dialog is open or a report runs. The
files only change when a WoodCraft dialog saves them (or someone edits them by
hand), so a load is cached against the file's modification time and size and
re-read only when either changes.

A deep copy is handed out every time so a caller that edits what it got can
never corrupt the cached value for the next caller. No Fusion API here.
"""

import copy
import os

_cache = {}     # path -> (stamp, value)


def _stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None          # missing file: the loader's defaults are cached too


def cached(path, loader):
    """loader() the first time / whenever `path` changed, else the cached value
    (always as a fresh deep copy)."""
    stamp = _stamp(path)
    hit = _cache.get(path)
    if hit is not None and hit[0] == stamp:
        return copy.deepcopy(hit[1])
    value = loader()
    _cache[path] = (stamp, value)
    return copy.deepcopy(value)


def forget(path=None):
    """Drop one cached file (or all of them)."""
    if path is None:
        _cache.clear()
    else:
        _cache.pop(path, None)
