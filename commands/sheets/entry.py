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

"""Sheets — global stock-sheet library editor, as an HTML palette tree.

Mirrors Fusion's Nesting "Process Material Library": a **Material** (its Fusion
material name + thickness + category + a display colour) holds one or more **sheets**
(stock board sizes with nesting params). Cut List & Nest matches each panel to the
material of its Fusion material name + thickness, then nests it on a chosen sheet.

The rich tree UI lives in resources/html/ (vanilla HTML/CSS/JS). Python here is just:
  - a launcher button (Output panel) that shows the palette, and
  - a thin bridge (incomingFromHTML) that loads/saves the library
    (commands/sheets_store.py) and feeds the design's real material names
    (panels.design_panel_materials) so the names always match nesting.

Bridge actions (JS -> Python via adsk.fusionSendData; reply via args.returnData):
  ready  -> {library:{materials:[...]}, designMaterials:[...], path:"..."}
  save   -> persists payload.materials; {ok, count, path}
  export -> file Save dialog, writes the library elsewhere; {ok, path|cancelled}
  import -> file Open dialog, reads+validates; {ok, materials, path} (JS re-renders;
            user then Saves to commit)
"""

import json
import os
import pathlib

import adsk.core
import adsk.fusion

from .. import ui_helpers
from .. import panels
from .. import sheets_store
from ...lib import fusionAddInUtils as futil
from ... import config

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_sheets'
CMD_NAME = 'Sheets'
CMD_Description = (
    'Edit the global stock-sheet library (materials → sheets, colour, cost, '
    'nesting) used by Cut List & Nest. Opens a docked panel.'
)
IS_PROMOTED = True

PANEL_ID = config.OUTPUT_PANEL_ID
PANEL_NAME = config.OUTPUT_PANEL_NAME
ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

PALETTE_ID = f'{config.COMPANY_NAME}_sheets_palette'
PALETTE_NAME = 'Sheets — Stock Library'
# A proper file:// URI (forward slashes, percent-encoded). Passing a raw Windows
# path with backslashes makes Fusion build a broken file:///C:/%5C... URL.
PALETTE_URL = pathlib.Path(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'resources', 'html', 'index.html')).as_uri()

local_handlers = []
palette_handlers = []   # handlers tied to the palette's lifetime (incomingFromHTML)


def start():
    cmd_def = ui.commandDefinitions.itemById(CMD_ID)
    if not cmd_def:
        cmd_def = ui.commandDefinitions.addButtonDefinition(
            CMD_ID, CMD_NAME, CMD_Description, ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)

    panel = ui_helpers.get_panel(PANEL_ID, PANEL_NAME)
    control = panel.controls.addCommand(cmd_def)
    control.isPromoted = IS_PROMOTED


def stop():
    global palette_handlers
    palette = ui.palettes.itemById(PALETTE_ID)
    if palette:
        try:
            palette.deleteMe()
        except Exception:
            pass
    palette_handlers = []
    ui_helpers.remove_command(PANEL_ID, CMD_ID)


def command_created(args: adsk.core.CommandCreatedEventArgs):
    futil.log(f'{CMD_NAME} Command Created Event')
    # Launcher: just open the palette. We add NO command inputs, so the command
    # auto-executes (Command.isAutoExecute defaults to True) and NO command dialog
    # is shown — clicking the button only opens the side panel.
    _show_palette()
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)


def _show_palette():
    palette = ui.palettes.itemById(PALETTE_ID)
    if not palette:
        palette = ui.palettes.add(
            PALETTE_ID, PALETTE_NAME, PALETTE_URL,
            True,    # isVisible
            True,    # showCloseButton
            True,    # isResizable
            620, 640)
        try:
            palette.setMinimumSize(460, 420)
        except Exception:
            pass
        try:
            palette.dockingOption = adsk.core.PaletteDockOptions.PaletteDockOptionsToVerticalAndHorizontal
            palette.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateRight
        except Exception:
            pass
        futil.add_handler(palette.incomingFromHTML, palette_incoming,
                          local_handlers=palette_handlers)
    else:
        # Guard against a stale palette that cached a bad URL from an earlier run.
        try:
            if palette.htmlFileURL != PALETTE_URL:
                palette.htmlFileURL = PALETTE_URL
        except Exception:
            pass
    palette.isVisible = True


# ---------------------------------------------------------------------------
# JS <-> Python bridge
# ---------------------------------------------------------------------------
def _active_design():
    """The active Design, robust to activeProduct not being the Design (e.g. when
    a CAM product is active) — falls back to the active document's Design product."""
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design:
        return design
    try:
        doc = app.activeDocument
        if doc:
            return adsk.fusion.Design.cast(doc.products.itemByProductType('DesignProductType'))
    except Exception:
        pass
    return None


def _payload():
    design = _active_design()
    # Walk the design ONCE for both lists (it used to be walked twice).
    try:
        found = panels.collect_panel_instances(design) if design else []
    except Exception:
        found = []
    return {
        'library': sheets_store.load(),
        'designMaterials': panels.design_panel_materials(design, found),
        'designGroups': panels.design_panel_groups(design, found),
        'path': sheets_store.library_path(),
        'rotations': list(sheets_store.ROTATIONS),
    }


def _export(materials, edgebands):
    dlg = ui.createFileDialog()
    dlg.title = 'Export WoodCraft stock library'
    dlg.filter = 'JSON files (*.json)'
    dlg.initialFilename = 'woodcraft_sheets.json'
    if dlg.showSave() != adsk.core.DialogResults.DialogOK:
        return {'ok': False, 'cancelled': True}
    sheets_store.write_path(dlg.filename, materials, edgebands)
    return {'ok': True, 'path': dlg.filename}


def _import():
    dlg = ui.createFileDialog()
    dlg.title = 'Import WoodCraft stock library'
    dlg.filter = 'JSON files (*.json)'
    if dlg.showOpen() != adsk.core.DialogResults.DialogOK:
        return {'ok': False, 'cancelled': True}
    try:
        library = sheets_store.read_path(dlg.filename)
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    # edgebands is None when the imported file predates the band section — the
    # palette then keeps its current bands instead of clearing them.
    return {'ok': True, 'materials': library['materials'],
            'edgebands': library['edgebands'], 'path': dlg.filename}


def palette_incoming(args: adsk.core.HTMLEventArgs):
    """Handle one message from the palette's JavaScript and reply via returnData."""
    action = args.action
    try:
        data = json.loads(args.data) if args.data else {}
    except Exception:
        data = {}

    try:
        if action == 'ready':
            result = _payload()
        elif action == 'save':
            # edgebands absent from the payload (None) → sheets_store preserves
            # the on-disk catalogue rather than wiping it.
            saved = sheets_store.save(data.get('materials', []),
                                      data.get('edgebands'))
            result = {'ok': True, 'count': len(saved['materials']),
                      'bands': len(saved['edgebands']),
                      'path': sheets_store.library_path()}
        elif action == 'export':
            result = _export(data.get('materials', []), data.get('edgebands'))
        elif action == 'import':
            result = _import()
        else:
            result = {'ok': False, 'error': f'unknown action: {action}'}
    except Exception as e:
        futil.handle_error('Sheets palette bridge')
        result = {'ok': False, 'error': str(e)}

    args.returnData = json.dumps(result)


def command_destroy(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Destroy Event')
    global local_handlers
    local_handlers = []
