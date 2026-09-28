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

"""Per-dialog memo of values computed from a selected entity, plus a batched
wireframe helper for command previews.

Countertop and Skirting redraw their preview on EVERY input change, and each
redraw used to walk every selected cabinet's occurrence tree again to measure
its bodies — dozens of API calls per cabinet, repeated while the user merely
nudged a number. The selections rarely change between redraws, so what was
measured for an entity is remembered for the life of the dialog.

Entities are matched with == (Fusion's own identity test). Their id (or name)
is only a bucket, because entityToken isn't guaranteed stable and ids can
repeat across referenced components.
"""

import adsk.core
import adsk.fusion


class EntityMemo:
    def __init__(self):
        self._buckets = {}

    def clear(self):
        self._buckets.clear()

    def get(self, entity, compute):
        """compute(entity), memoised. Always returns a fresh list/tuple copy when
        the value is a list, so a caller extending it can't corrupt the memo."""
        key = None
        try:
            # Components have an id (a tight bucket); occurrences and bodies
            # fall back to their name.
            key = (entity.objectType, entity.id)
        except Exception:
            try:
                key = (entity.objectType, getattr(entity, 'name', ''))
            except Exception:
                key = None
        bucket = self._buckets.setdefault(key, [])
        for other, value in bucket:
            try:
                if other == entity:
                    return list(value) if isinstance(value, list) else value
            except Exception:
                continue
        value = compute(entity)
        bucket.append((entity, value))
        return list(value) if isinstance(value, list) else value


class LineBatch:
    """Collects wireframe segments per colour and draws each colour with ONE
    CustomGraphicsLines call, instead of one API call per polyline (a kitchen's
    preview used to be hundreds of addLines calls on every redraw)."""

    def __init__(self):
        self._by_colour = []      # [(colour effect, [coords], [indices])]

    def _slot(self, colour):
        for entry in self._by_colour:
            if entry[0] is colour:
                return entry
        entry = (colour, [], [])
        self._by_colour.append(entry)
        return entry

    def strip(self, colour, points):
        """An open polyline through `points` [(x, y, z)] (world cm)."""
        if len(points) < 2:
            return
        _colour, coords, indices = self._slot(colour)
        base = len(coords) // 3
        for x, y, z in points:
            coords.extend((x, y, z))
        for i in range(len(points) - 1):
            indices.extend((base + i, base + i + 1))

    def draw(self, group, weight=None):
        """Add everything to `group`. Returns the created line entities."""
        made = []
        for colour, coords, indices in self._by_colour:
            if not indices:
                continue
            lines = group.addLines(
                adsk.fusion.CustomGraphicsCoordinates.create(coords), indices, False)
            if weight is not None:
                lines.weight = weight
            lines.color = colour
            made.append(lines)
        return made
