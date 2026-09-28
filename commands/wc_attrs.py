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

"""Central read/write for WoodCraft's component attributes.

Every WoodCraft attribute lives under ONE group (config.WC_GROUP) as a plain
name/value string pair on a Component. This module is the only place that touches
that store, so the scheme can grow — new key constants in config.py — without
scattering attribute code across commands.

Design notes:
- Attributes are stamped on the COMPONENT (shared by all its occurrences), so
  classification and per-type data are per-component; physical quantities (counts,
  sizes) are derived per-occurrence by the collector in panels.py.
- Values are always strings on disk; typed helpers (e.g. cost) convert at the edge.
- `Attributes.add` is add-or-update, so set_value is idempotent.
- Functions take a component but work on any entity exposing `.attributes`, which
  keeps the door open for face/edge attributes later without changing this API.
"""

import adsk.core
import adsk.fusion

from .. import config


# ---------------------------------------------------------------------------
# Generic key/value access (the extensible core)
# ---------------------------------------------------------------------------
def set_value(component, name, value) -> bool:
    """Add or replace a WoodCraft attribute on `component`. Returns False if it
    couldn't be written (e.g. a referenced/read-only component).

    An attribute already holding `value` is left alone: a write is a document
    edit (dirty flag, undo record), a read is not, so re-stamping a whole
    kitchen with what it already says costs next to nothing."""
    text = '' if value is None else str(value)
    try:
        attributes = component.attributes
        existing = attributes.itemByName(config.WC_GROUP, name)
        if existing is not None and existing.value == text:
            return True
        attributes.add(config.WC_GROUP, name, text)
        return True
    except Exception:
        return False


def get_value(component, name, default=None):
    """Value of a WoodCraft attribute, or `default` if absent/unreadable."""
    try:
        attr = component.attributes.itemByName(config.WC_GROUP, name)
        return attr.value if attr else default
    except Exception:
        return default


def remove_value(component, name) -> bool:
    """Delete a WoodCraft attribute if present. Returns True if one was removed."""
    try:
        attr = component.attributes.itemByName(config.WC_GROUP, name)
        if attr:
            attr.deleteMe()
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Category
# ---------------------------------------------------------------------------
def get_category(component):
    """'panel' | 'hardware' | 'countertop' | None (unclassified)."""
    return get_value(component, config.WC_CATEGORY)


def set_category(component, category) -> bool:
    return set_value(component, config.WC_CATEGORY, category)


def is_panel(component) -> bool:
    return get_category(component) == config.WC_CAT_PANEL


def is_hardware(component) -> bool:
    return get_category(component) == config.WC_CAT_HARDWARE


def is_countertop(component) -> bool:
    return get_category(component) == config.WC_CAT_COUNTERTOP


def is_sheet_like(component) -> bool:
    """Panel OR countertop — anything measured, priced by area and edgebanded like
    a sheet good. Use this (not is_panel) where the question is "does this get
    costed / banded"; use is_panel where the question is "does this get NESTED",
    which is the one thing a worktop doesn't do."""
    return get_category(component) in config.WC_SHEET_LIKE


# ---------------------------------------------------------------------------
# Appearance role (used by the Appearance Config command)
# ---------------------------------------------------------------------------
def get_role(component):
    """'door' | 'front' | 'carcass' | 'skip' | None (classify by name keywords).
    Anything unexpected on disk reads as None so a bad value can't wedge a scan."""
    value = get_value(component, config.WC_ROLE)
    return value if value in config.WC_ROLES else None


def set_role(component, role) -> bool:
    """Persist an appearance role. Unknown role → refuse rather than store junk."""
    if role not in config.WC_ROLES:
        return False
    return set_value(component, config.WC_ROLE, role)


def remove_role(component) -> bool:
    return remove_value(component, config.WC_ROLE)


# ---------------------------------------------------------------------------
# Hardware unit cost
# ---------------------------------------------------------------------------
def get_cost(component, default=0.0) -> float:
    raw = get_value(component, config.WC_COST)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def set_cost(component, cost) -> bool:
    try:
        return set_value(component, config.WC_COST, f'{float(cost):.4f}')
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Edgeband (a FACE attribute — the entity passed in is a BRepFace)
# ---------------------------------------------------------------------------
def get_edgeband(face, default=None):
    """Name of the edgeband assigned to this face, or `default` if untagged."""
    return get_value(face, config.WC_EDGEBAND, default)


def set_edgeband(face, band_name) -> bool:
    """Tag a face with an edgeband by its Sheets-library name."""
    return set_value(face, config.WC_EDGEBAND, band_name)


def remove_edgeband(face) -> bool:
    """Strip the edgeband tag from a face. True if one was removed."""
    return remove_value(face, config.WC_EDGEBAND)


# ---------------------------------------------------------------------------
# Hardware purchase mode (pack vs separate parts)
# ---------------------------------------------------------------------------
def get_purchase_mode(component):
    """'pack' (default — also for components stamped before this key existed) or
    'separate'. Only meaningful on hardware components."""
    value = get_value(component, config.WC_PURCHASE)
    return config.WC_PURCHASE_SEPARATE if value == config.WC_PURCHASE_SEPARATE \
        else config.WC_PURCHASE_PACK


def set_purchase_mode(component, mode) -> bool:
    return set_value(component, config.WC_PURCHASE, mode)


# ---------------------------------------------------------------------------
# Finish spec (Carcass Type / Door Type)
# ---------------------------------------------------------------------------
# Written by the Set Finish command onto the CABINET component. The keys are
# config.WC_CARCASS_TYPE / WC_DOOR_TYPE — human-readable names ('Carcass Type',
# 'Door Type') so the pair reads the same in Fusion's attribute inspector as it
# does in the dialog. Values are always one of config.WC_FINISH_TYPES.
def get_carcass_type(component, default=None):
    """'Painted' | 'Veneer' | `default` if the cabinet has no finish spec yet."""
    return get_value(component, config.WC_CARCASS_TYPE, default)


def set_carcass_type(component, finish) -> bool:
    return set_value(component, config.WC_CARCASS_TYPE, finish)


def get_door_type(component, default=None):
    """'Painted' | 'Veneer' | `default` if the cabinet has no finish spec yet."""
    return get_value(component, config.WC_DOOR_TYPE, default)


def set_door_type(component, finish) -> bool:
    return set_value(component, config.WC_DOOR_TYPE, finish)
