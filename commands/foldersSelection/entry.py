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

"""Folders Selection — choose which cloud folders the WoodCraft tools read from.

Insert Hardware, Fit Handles and the Kitchen tab commands all work against
folders on the Fusion cloud. By default they find them by the names set in
config.py, in the active hub. This dialog lets you point each one at an exact
folder instead — in any hub (team / tenant) you can see, any project, any depth.

    Reference   which tool's folder you are setting
    Hub         the team / tenant
    Project     a project in that hub
    Folder      browse: pick a sub-folder to open it, "‹ Up" to go back
    Use this folder   assigns the folder you are in to the Reference
    Use default       drops the choice; the tool goes back to config.py names

Nothing is written until OK, so Cancel always leaves things as they were.
Choices are saved per computer (folders.json beside the other WoodCraft data).
"""

import os

import adsk.core

from .. import folder_store
from .. import ui_helpers
from ...lib import fusionAddInUtils as futil
from ... import config

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_foldersSelection'
CMD_NAME = 'Folders Selection'
CMD_Description = (
    'Choose the cloud folders WoodCraft reads from — the hardware library, the '
    'handles, the master cabinet library and the kitchen folders — in any hub '
    'and project.'
)
IS_PROMOTED = True

# Offered in both tabs: the hardware references belong to the WoodCraft tab, the
# kitchen ones to the Kitchen tab, and people look for it where they work.
PANELS = (
    (config.OUTPUT_PANEL_ID, config.OUTPUT_PANEL_NAME, None, None),
    (config.KITCHEN_PROJECT_PANEL_ID, config.KITCHEN_PROJECT_PANEL_NAME,
     config.KITCHEN_TAB_ID, config.KITCHEN_TAB_NAME),
)

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

TARGET_ID = 'fs_target'
CURRENT_ID = 'fs_current'
HUB_ID = 'fs_hub'
PROJECT_ID = 'fs_project'
FOLDER_ID = 'fs_folder'
PATH_ID = 'fs_path'
ASSIGN_ID = 'fs_assign'
RESET_ID = 'fs_reset'

UP_ITEM = '‹ Up one level'
HINT_ITEM = '— open a sub-folder —'
NO_SUB_ITEM = '(no sub-folders)'
PICK_PROJECT = '— pick a project —'

local_handlers = []

# Dialog state (reset every time the dialog opens).
_pending = {}        # key -> selection dict or None, as it will be saved on OK
_hubs = []           # [DataHub]
_projects = []       # [DataProject] of the selected hub
_chain = []          # [rootFolder, sub, …] — where the browser is
_hub = None
_project = None
_sub_cache = {}      # folder id -> [DataFolder]
_busy = False        # guards against our own list edits re-firing inputChanged


def start():
    cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_Description, ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)
    for panel_id, panel_name, tab_id, tab_name in PANELS:
        panel = ui_helpers.get_panel(panel_id, panel_name, tab_id=tab_id, tab_name=tab_name)
        control = panel.controls.addCommand(cmd_def)
        control.isPromoted = IS_PROMOTED


def stop():
    # Remove every button first, then the definition (remove_command deletes the
    # definition as well, so the last call does that).
    for panel_id, _name, tab_id, _tab_name in reversed(PANELS):
        ui_helpers.remove_command(panel_id, CMD_ID, tab_id=tab_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _name(obj):
    try:
        return obj.name
    except Exception:
        return '?'


def _subfolders(folder):
    try:
        key = folder.id
    except Exception:
        key = None
    if key and key in _sub_cache:
        return _sub_cache[key]
    subs = []
    try:
        folders = folder.dataFolders
        for i in range(folders.count):
            subs.append(folders.item(i))
    except Exception:
        pass
    subs.sort(key=lambda f: _name(f).lower())
    if key:
        _sub_cache[key] = subs
    return subs


def _fill(dropdown, labels, selected_index=0):
    items = dropdown.listItems
    items.clear()
    for i, label in enumerate(labels):
        items.add(label, i == selected_index, '')


def _selected_key(inputs):
    item = inputs.itemById(TARGET_ID).selectedItem
    if not item:
        return None
    for key, label, _use, _default in folder_store.targets():
        if label == item.name:
            return key
    return None


def _target_info(key):
    for k, label, use, default in folder_store.targets():
        if k == key:
            return label, use, default
    return key, '', ''


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------
def command_created(args: adsk.core.CommandCreatedEventArgs):
    futil.log(f'{CMD_NAME} Command Created Event')
    global _pending, _hubs, _projects, _chain, _hub, _project, _sub_cache, _busy
    _pending = dict(folder_store.load())
    _hubs, _projects, _chain = [], [], []
    _hub = _project = None
    _sub_cache = {}
    _busy = True

    inputs = args.command.commandInputs
    try:
        args.command.setDialogInitialSize(460, 440)
    except Exception:
        pass

    target = inputs.addDropDownCommandInput(TARGET_ID, 'Reference',
                                            adsk.core.DropDownStyles.TextListDropDownStyle)
    for i, (_key, label, _use, _default) in enumerate(folder_store.targets()):
        target.listItems.add(label, i == 0, '')
    target.tooltip = 'Which tool\'s folder you are choosing.'

    current = inputs.addTextBoxCommandInput(CURRENT_ID, '', '', 4, True)
    current.isFullWidth = True

    hub_dd = inputs.addDropDownCommandInput(HUB_ID, 'Hub',
                                            adsk.core.DropDownStyles.TextListDropDownStyle)
    hub_dd.tooltip = 'The team (tenant) the folder lives in.'
    inputs.addDropDownCommandInput(PROJECT_ID, 'Project',
                                   adsk.core.DropDownStyles.TextListDropDownStyle)
    folder_dd = inputs.addDropDownCommandInput(FOLDER_ID, 'Folder',
                                               adsk.core.DropDownStyles.TextListDropDownStyle)
    folder_dd.tooltip = 'Pick a sub-folder to open it; "‹ Up one level" goes back.'
    inputs.addTextBoxCommandInput(PATH_ID, 'Location', '', 2, True)

    assign = inputs.addBoolValueInput(ASSIGN_ID, 'Use this folder', False, '', False)
    assign.text = 'Use this folder'
    assign.tooltip = 'Use the folder shown under Location for the chosen Reference.'
    reset = inputs.addBoolValueInput(RESET_ID, 'Use default', False, '', False)
    reset.text = 'Use default'
    reset.tooltip = 'Forget the choice — the tool goes back to the names in config.py.'

    try:
        _hubs = [app.data.dataHubs.item(i) for i in range(app.data.dataHubs.count)]
    except Exception:
        _hubs = []
    _busy = False

    _show_target(inputs)

    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.inputChanged, command_input_changed, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)


def _show_current(inputs):
    key = _selected_key(inputs)
    label, use, default = _target_info(key)
    sel = _pending.get(key)
    lines = [f'<i>{use}</i>']
    if sel:
        lines.append(f'<b>Using:</b> {folder_store.describe(sel)}')
    else:
        lines.append(f'<b>Using the default:</b> {default} '
                     f'<font color="#777777">(from config.py)</font>')
    changed = sel != folder_store.get(key)
    if changed:
        lines.append('<font color="#b35900"><i>Changed — saved when you press OK.</i></font>')
    inputs.itemById(CURRENT_ID).formattedText = '<br>'.join(lines)


def _show_target(inputs):
    """Point the browser at the reference's current folder (or the active hub)."""
    key = _selected_key(inputs)
    _show_current(inputs)
    sel = _pending.get(key)

    hub = project = None
    chain = []
    if sel:
        hub = folder_store._find_hub(sel)
        if hub:
            project = folder_store._find_project(hub, sel)
        if project:
            try:
                chain = [project.rootFolder]
                for name in sel.get('path') or []:
                    nxt = next((f for f in _subfolders(chain[-1]) if _name(f) == name), None)
                    if not nxt:
                        break
                    chain.append(nxt)
            except Exception:
                chain = []
    if hub is None:
        try:
            hub = app.data.activeHub
        except Exception:
            hub = None
    _set_hub(inputs, hub, project, chain)


def _set_hub(inputs, hub, project=None, chain=None):
    global _busy, _hub, _projects
    _busy = True
    try:
        hub_dd = inputs.itemById(HUB_ID)
        index = 0
        for i, h in enumerate(_hubs):
            try:
                if hub and h.id == hub.id:
                    index = i
            except Exception:
                pass
        _fill(hub_dd, [_name(h) for h in _hubs] or ['(no hubs)'], index)
        _hub = _hubs[index] if _hubs else None
        try:
            projects = _hub.dataProjects
            _projects = [projects.item(i) for i in range(projects.count)]
        except Exception:
            _projects = []
        _projects.sort(key=lambda p: _name(p).lower())
    finally:
        _busy = False
    _set_project(inputs, project, chain)


def _set_project(inputs, project, chain=None):
    global _busy, _project, _chain
    _busy = True
    try:
        project_dd = inputs.itemById(PROJECT_ID)
        index = None
        for i, p in enumerate(_projects):
            try:
                if project and p.id == project.id:
                    index = i
            except Exception:
                pass
        labels = [PICK_PROJECT] + [_name(p) for p in _projects]
        _fill(project_dd, labels, 0 if index is None else index + 1)
        _project = _projects[index] if index is not None else None
        if _project is None:
            _chain = []
        elif chain:
            _chain = list(chain)
        else:
            try:
                _chain = [_project.rootFolder]
            except Exception:
                _chain = []
    finally:
        _busy = False
    _show_folder(inputs)


def _show_folder(inputs):
    global _busy
    _busy = True
    try:
        folder_dd = inputs.itemById(FOLDER_ID)
        path_box = inputs.itemById(PATH_ID)
        assign = inputs.itemById(ASSIGN_ID)
        if not _chain:
            _fill(folder_dd, ['(pick a project first)'])
            folder_dd.isEnabled = False
            path_box.formattedText = ''
            assign.isEnabled = False
            return
        subs = _subfolders(_chain[-1])
        labels = [HINT_ITEM if subs else NO_SUB_ITEM]
        if len(_chain) > 1:
            labels.append(UP_ITEM)
        labels.extend(_name(f) for f in subs)
        _fill(folder_dd, labels, 0)
        folder_dd.isEnabled = len(labels) > 1
        parts = [_name(_project) + ' (root)' if len(_chain) == 1 else _name(_project)]
        parts += [_name(f) for f in _chain[1:]]
        path_box.formattedText = f'<b>{_name(_hub)} ›</b> ' + ' / '.join(parts)
        assign.isEnabled = True
    finally:
        _busy = False


def command_input_changed(args: adsk.core.InputChangedEventArgs):
    if _busy:
        return
    changed = args.input
    inputs = changed.parentCommand.commandInputs
    try:
        if changed.id == TARGET_ID:
            _show_target(inputs)
        elif changed.id == HUB_ID:
            item = changed.selectedItem
            hub = next((h for h in _hubs if item and _name(h) == item.name), None)
            _set_hub(inputs, hub)
        elif changed.id == PROJECT_ID:
            item = changed.selectedItem
            index = item.index - 1 if item else -1     # 0 is the hint row
            project = _projects[index] if item and 0 <= index < len(_projects) else None
            _set_project(inputs, project)
        elif changed.id == FOLDER_ID:
            _on_folder_pick(inputs, changed.selectedItem)
        elif changed.id == ASSIGN_ID:
            if changed.value:
                changed.value = False
                key = _selected_key(inputs)
                if key and _chain and _project:
                    _pending[key] = folder_store.selection_for(_hub, _project, _chain)
                    _show_current(inputs)
        elif changed.id == RESET_ID:
            if changed.value:
                changed.value = False
                key = _selected_key(inputs)
                if key:
                    _pending[key] = None
                    _show_current(inputs)
    except Exception:
        futil.handle_error(f'{CMD_NAME}: input changed')


def _on_folder_pick(inputs, item):
    global _chain
    if not item or not _chain:
        return
    label = item.name
    if label in (HINT_ITEM, NO_SUB_ITEM):
        return
    if label == UP_ITEM:
        if len(_chain) > 1:
            _chain = _chain[:-1]
    else:
        nxt = next((f for f in _subfolders(_chain[-1]) if _name(f) == label), None)
        if nxt:
            _chain = _chain + [nxt]
    _show_folder(inputs)


def command_execute(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Execute Event')
    saved = folder_store.save_all(_pending)
    lines = []
    for key, label, _use, default in folder_store.targets():
        sel = saved.get(key)
        lines.append(f'{label}: ' + (folder_store.describe(sel) if sel
                                     else f'default — {default}'))
    ui.messageBox('Folders saved.\n\n' + '\n'.join(lines), CMD_NAME)


def command_destroy(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Destroy Event')
    global local_handlers, _sub_cache
    local_handlers = []
    _sub_cache = {}
