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

"""Sculpt — cut selected panels using hardware accessory tool bodies.

Scans the assembly for hardware "tool" bodies (the hidden cut/hole bodies modelled
inside connectors, dowels, hinges, etc.), maps them into the root assembly context,
and Combine-cuts the selected panels with every tool that actually intersects them.
The tool bodies are kept intact so the hardware stays in the model.

A tool body qualifies if its name matches one of the filter terms (default
"cut, hole") OR — when "Scan hidden bodies" is on — its visibility lightbulb is
off. Panels and skeleton bodies are never used as tools.

Matching is two-stage: a cheap bounding-box overlap prunes obviously-disjoint
pairs, then a precise minimum-distance test confirms the solids really touch, so
hardware whose bounding box overlaps a panel but whose body does not is skipped.
"""

import os
import adsk.core
import adsk.fusion

from .. import ui_helpers
from ...lib import fusionAddInUtils as futil
from ... import config

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_sculpt'
CMD_NAME = 'Sculpt'
CMD_Description = (
    'Cut selected panels automatically with hardware accessory tool bodies '
    '(e.g., dowels, connectors) present in the assembly.'
)
IS_PROMOTED = True

PANEL_ID = config.HARDWARE_PANEL_ID
PANEL_NAME = config.HARDWARE_PANEL_NAME

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

TARGETS_INPUT_ID = 'sculpt_targets'
FILTER_INPUT_ID = 'sculpt_filter'
HIDDEN_INPUT_ID = 'sculpt_hidden'

# Component-name hints used to recognise a hardware component. If such a
# component holds no explicitly-named/hidden tool body, all of its bodies are
# used as tools (fallback).
HARDWARE_NAME_HINTS = ['dowel', 'minifix', 'screw', 'connector', 'fastener', 'hardware', 'hinge']

# Default body-name terms used to spot tool bodies (overridable in the dialog).
DEFAULT_FILTER_TERMS = ['cut', 'hole']

# Solids closer than this (cm, Fusion internal units) are treated as touching /
# overlapping. Penetrating tool bodies measure 0.0.
INTERSECT_TOL_CM = 1e-4

local_handlers = []


def start():
    cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_Description, ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)

    panel = ui_helpers.get_panel(PANEL_ID, PANEL_NAME)
    control = panel.controls.addCommand(cmd_def)
    control.isPromoted = IS_PROMOTED


def stop():
    ui_helpers.remove_command(PANEL_ID, CMD_ID)


def command_created(args: adsk.core.CommandCreatedEventArgs):
    futil.log(f'{CMD_NAME} Command Created Event')
    inputs = args.command.commandInputs

    # Target selection: Panels to be cut
    targets_input = inputs.addSelectionInput(TARGETS_INPUT_ID, 'Panels to cut', 'Select the wood panels to be cut')
    targets_input.addSelectionFilter('SolidBodies')
    targets_input.setSelectionLimits(1, 0)

    # Name filter terms input
    filter_input = inputs.addStringValueInput(FILTER_INPUT_ID, 'Name Filter Terms', ', '.join(DEFAULT_FILTER_TERMS))
    filter_input.tooltip = (
        'Comma-separated terms (case-insensitive). A body whose name contains any '
        'of these is treated as a hardware tool body. Leave blank to use the '
        'defaults (cut, hole).'
    )

    # Scan hidden bodies checkbox
    hidden_input = inputs.addBoolValueInput(HIDDEN_INPUT_ID, 'Scan hidden bodies', True, '', True)
    hidden_input.tooltip = 'Include bodies whose visibility lightbulb is off (common for drill templates modeled inside hardware components).'

    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.validateInputs, command_validate_input, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)


def command_execute(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Execute Event')
    inputs = args.command.commandInputs

    targets_input: adsk.core.SelectionCommandInput = inputs.itemById(TARGETS_INPUT_ID)
    filter_terms: str = inputs.itemById(FILTER_INPUT_ID).value
    scan_hidden: bool = inputs.itemById(HIDDEN_INPUT_ID).value

    target_bodies = [targets_input.selection(i).entity for i in range(targets_input.selectionCount)]

    design = adsk.fusion.Design.cast(app.activeProduct)
    root = design.rootComponent

    # Search terms drive name-based tool detection; fall back to the defaults if
    # the field is blank.
    search_terms = [term.strip().lower() for term in filter_terms.split(',') if term.strip()]
    if not search_terms:
        search_terms = list(DEFAULT_FILTER_TERMS)

    accessory_tool_bodies = _collect_tool_bodies(root, search_terms, scan_hidden)

    combines = root.features.combineFeatures
    combine_count = 0

    # Every tool's box is read ONCE (six API calls) rather than once per target
    # panel; the prune below is then plain float comparisons.
    tools = []
    for tool_body in accessory_tool_bodies:
        try:
            tools.append((tool_body, _box(tool_body.boundingBox)))
        except Exception:
            continue

    for target_body in target_bodies:
        try:
            target_bbox = _box(target_body.boundingBox)
        except Exception:
            continue
        matching_tools = adsk.core.ObjectCollection.create()

        for tool_body, tool_box in tools:
            # Cheap bounding-box prune first, then a precise interference test.
            if not _boxes_overlap(target_bbox, tool_box):
                continue
            try:
                if tool_body == target_body:
                    continue
            except Exception:
                continue
            if _bodies_intersect(target_body, tool_body):
                matching_tools.add(tool_body)

        if matching_tools.count == 0:
            continue

        try:
            combine_input = combines.createInput(target_body, matching_tools)
            combine_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
            combine_input.isKeepToolBodies = True  # keep the hardware itself
            combines.add(combine_input)
            combine_count += 1
        except Exception as e:
            futil.log(f'Sculpt: combine failed for "{target_body.name}": {e}')

    if combine_count == 0:
        ui.messageBox(
            'No cuts were made.\n\n'
            f'Found {len(accessory_tool_bodies)} candidate tool body(ies), but none '
            'intersected the selected panel(s).\n\n'
            'Check that the hardware actually penetrates the panels, that "Scan '
            'hidden bodies" is on, and that the filter terms match the tool body names.'
        )
    else:
        ui.messageBox(f'Sculpt: cut {combine_count} of {len(target_bodies)} selected panel(s).')


def _collect_tool_bodies(root: adsk.fusion.Component, search_terms, scan_hidden: bool):
    """Gather hardware tool bodies from every occurrence, as root-context proxies.

    A body qualifies if its name matches a filter term, or (when scan_hidden) its
    lightbulb is off. Components named like a panel/skeleton are skipped entirely
    so they can never act as cutting tools. Hardware components that expose no
    explicit tool body fall back to using all of their bodies.
    """
    tool_bodies = []
    for occ in root.allOccurrences:
        comp = occ.component
        comp_name = comp.name.lower()
        occ_name = occ.name.lower()

        # Never use panels or the skeleton as tools.
        if 'panel' in comp_name or 'skeleton' in comp_name:
            continue

        is_hardware = any(hint in comp_name or hint in occ_name for hint in HARDWARE_NAME_HINTS)

        bodies_in_occ = []
        comp_bodies = comp.bRepBodies
        for i in range(comp_bodies.count):
            body = comp_bodies.item(i)
            name = body.name.lower()
            is_named_tool = any(term in name for term in search_terms)
            is_hidden = not body.isLightBulbOn
            if is_named_tool or (scan_hidden and is_hidden):
                bodies_in_occ.append(body)

        # Fallback: a recognised hardware component with no flagged tool body —
        # use all of its bodies.
        if not bodies_in_occ and is_hardware:
            bodies_in_occ = [comp_bodies.item(i) for i in range(comp_bodies.count)]

        for body in bodies_in_occ:
            try:
                proxy = body.createForAssemblyContext(occ)
                if proxy:
                    tool_bodies.append(proxy)
            except Exception:
                pass

    return tool_bodies


def _bodies_intersect(body_a: adsk.fusion.BRepBody, body_b: adsk.fusion.BRepBody) -> bool:
    """Precise interference test: two solids touch/overlap when their minimum
    distance is ~0. Operates in assembly/world space, so it is correct for
    proxies from different components. Falls back to True if the measurement
    can't be taken (the bbox pre-filter has already passed, so including it is
    the safe choice)."""
    try:
        result = app.measureManager.measureMinimumDistance(body_a, body_b)
        return result.value < INTERSECT_TOL_CM
    except Exception:
        return True


def _box(bb):
    """BoundingBox3D → plain (x0, y0, z0, x1, y1, z1) floats."""
    lo, hi = bb.minPoint, bb.maxPoint
    return (lo.x, lo.y, lo.z, hi.x, hi.y, hi.z)


def _boxes_overlap(a, b, eps=0.01):
    """bbox_overlaps on plain tuples (same 0.01 cm tolerance)."""
    return not (a[3] + eps < b[0] or a[0] - eps > b[3] or
                a[4] + eps < b[1] or a[1] - eps > b[4] or
                a[5] + eps < b[2] or a[2] - eps > b[5])


def bbox_overlaps(box1: adsk.core.BoundingBox3D, box2: adsk.core.BoundingBox3D) -> bool:
    """Cheap pre-filter: do two axis-aligned bounding boxes overlap in 3D?"""
    eps = 0.01  # Tolerance buffer
    return not (
        box1.maxPoint.x + eps < box2.minPoint.x or
        box1.minPoint.x - eps > box2.maxPoint.x or
        box1.maxPoint.y + eps < box2.minPoint.y or
        box1.minPoint.y - eps > box2.maxPoint.y or
        box1.maxPoint.z + eps < box2.minPoint.z or
        box1.minPoint.z - eps > box2.maxPoint.z
    )


def command_validate_input(args: adsk.core.ValidateInputsEventArgs):
    inputs = args.inputs
    targets_input: adsk.core.SelectionCommandInput = inputs.itemById(TARGETS_INPUT_ID)
    args.areInputsValid = targets_input.selectionCount > 0


def command_destroy(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Destroy Event')
    global local_handlers
    local_handlers = []
