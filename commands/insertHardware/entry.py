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

"""Insert Hardware — insert a hardware part from the WoodCraft hardware project.

The hardware lives in a dedicated Fusion cloud project (set by
``config.HARDWARE_PROJECT_NAME``) whose top-level folders are the categories
(Hinges, Connectors, Dowels, ...) and whose files are the individual parts.

The dialog has two dropdowns: pick a category, then pick a part. On OK the chosen
part is inserted as a linked (referenced) component at the origin; position/joint
it afterwards and use Sculpt to machine its holes.

SPEED: enumerating a cloud project hits the network, which is slow. So the
catalogue is cached at module level — the FIRST open pays the cost, every
re-open after that is instant, and each category's parts are fetched lazily the
first time you view them. A **Refresh** button drops the cache and re-reads the
project, so newly-added folders/parts show up on demand without an add-in reload.

PLANNED: a thumbnail preview of the selected part (so parts named with bare
numbers are identifiable). Pending confirmation of the Data thumbnail API.
"""

import os
import re
import tempfile
import time

import adsk.core
import adsk.fusion

from .. import folder_store
from .. import ui_helpers
from ...lib import fusionAddInUtils as futil
from ... import config

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_insertHardware'
CMD_NAME = 'Insert Hardware'
CMD_Description = (
    'Insert a hardware part (hinge, connector, dowel, ...) from the WoodCraft '
    'hardware project, picked by category then part.'
)
IS_PROMOTED = True

# Lives in the Hardware panel alongside Sculpt.
PANEL_ID = config.HARDWARE_PANEL_ID
PANEL_NAME = config.HARDWARE_PANEL_NAME

RES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources')
ICON_FOLDER = os.path.join(RES_DIR, '')
PLACEHOLDER = os.path.join(RES_DIR, 'thumb_placeholder.png')
# Reuse Carcass Maker's "defaults" (reset) icon for the Refresh button.
DEFAULTS_ICON_FOLDER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'carcassMaker', 'resources', 'defaults')

TYPE_DD_ID = 'ih_type'
ITEM_DD_ID = 'ih_item'
THUMB_ID = 'ih_thumb'
REFRESH_ID = 'ih_refresh'
INFO_ID = 'ih_info'

# On-disk thumbnail cache (downloaded 256x256 PNGs keyed by file id + version).
_THUMB_DIR = os.path.join(tempfile.gettempdir(), 'woodcraft_thumbs')
_fetching = False        # guard against re-entrant thumbnail fetches

local_handlers = []

# Module-level cache (persists across dialog opens for speed; cleared by Refresh).
_project = None          # cached library ROOT DataFolder (None until found)
_source_sel = None       # the Folders Selection choice the cache was built from
_error = ''              # why the library couldn't be found (shown in the dialog)
_categories = None       # dict: category name -> DataFolder (None until loaded)
_items_cache = {}        # category name -> dict(part name -> DataFile)

# Interactive placement: after inserting, hand the part to Fusion's Move/Copy
# gizmo so it can be positioned + oriented like a native insert. Launching Move
# is deferred to a custom event, because a command can't be started from inside
# another command's execute handler.
MOVE_EVENT_ID = f'{config.COMPANY_NAME}_insertHardware_position'
_move_event = None         # the registered CustomEvent
_pending_occ = None        # occurrence awaiting interactive placement
_persistent_handlers = []  # keeps the custom-event handler alive across opens


def start():
    cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_Description, ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)

    # Custom event used to launch Move on the freshly inserted part (see execute).
    global _move_event
    try:
        _move_event = app.registerCustomEvent(MOVE_EVENT_ID)
    except Exception:
        app.unregisterCustomEvent(MOVE_EVENT_ID)
        _move_event = app.registerCustomEvent(MOVE_EVENT_ID)
    futil.add_handler(_move_event, _on_position_event, local_handlers=_persistent_handlers)

    panel = ui_helpers.get_panel(PANEL_ID, PANEL_NAME)
    control = panel.controls.addCommand(cmd_def)
    control.isPromoted = IS_PROMOTED


def stop():
    ui_helpers.remove_command(PANEL_ID, CMD_ID)
    try:
        app.unregisterCustomEvent(MOVE_EVENT_ID)
    except Exception:
        pass
    _persistent_handlers.clear()


# ---------------------------------------------------------------------------
# Data project enumeration (network — cached)
# ---------------------------------------------------------------------------
def _project_in_hub(hub, target_name):
    projects = hub.dataProjects
    for i in range(projects.count):
        project = projects.item(i)
        if project.name == target_name:
            return project
    return None


def _find_hardware_project():
    """Locate the configured hardware project, checking the active hub first."""
    target = config.HARDWARE_PROJECT_NAME
    try:
        active = app.data.activeHub
        if active:
            found = _project_in_hub(active, target)
            if found:
                return found
        hubs = app.data.dataHubs
        for h in range(hubs.count):
            hub = hubs.item(h)
            if active and hub.id == active.id:
                continue  # already checked
            found = _project_in_hub(hub, target)
            if found:
                return found
    except Exception:
        futil.log('Insert Hardware: failed while scanning data hubs/projects')
    return None


def _library_root():
    """(DataFolder, error) for the hardware library: the folder picked in Folders
    Selection when there is one (any hub, found straight by id), else the root
    of config.HARDWARE_PROJECT_NAME found by name."""
    configured, folder = folder_store.resolve('hardware')
    if configured:
        if folder is None:
            return None, folder_store.missing_message('hardware')
        return folder, ''
    project = _find_hardware_project()
    if project is None:
        return None, ''
    try:
        return project.rootFolder, ''
    except Exception:
        return None, ''


def _ensure_loaded():
    """Populate the library + category cache if it isn't already (slow once)."""
    global _project, _categories, _error, _items_cache, _source_sel
    # A new choice in Folders Selection invalidates what was cached for the old one.
    sel = folder_store.get('hardware')
    if sel != _source_sel:
        _project, _categories, _items_cache, _source_sel = None, None, {}, sel
    if _project is None:
        _project, _error = _library_root()
    if _project is not None and _categories is None:
        _categories = {}
        folders = _project.dataFolders
        for i in range(folders.count):
            folder = folders.item(i)
            _categories[folder.name] = folder


def _items_for(category_name):
    """Parts in a category, fetched + cached on first view (lazy)."""
    if category_name in _items_cache:
        return _items_cache[category_name]
    items = {}
    folder = (_categories or {}).get(category_name)
    if folder:
        files = folder.dataFiles
        for i in range(files.count):
            data_file = files.item(i)
            name = data_file.name
            if name in items:                  # de-dup identical names
                name = f'{name} ({i + 1})'
            items[name] = data_file
    _items_cache[category_name] = items
    return items


# ---------------------------------------------------------------------------
# Command UI
# ---------------------------------------------------------------------------
def command_created(args: adsk.core.CommandCreatedEventArgs):
    futil.log(f'{CMD_NAME} Command Created Event')

    # Widen the dialog beyond its auto-fit width. CAVEAT: Fusion remembers a
    # dialog's size once it has been shown, so this reliably applies only on the
    # first-ever open; afterwards the remembered (or user-dragged) size wins.
    try:
        args.command.setDialogInitialSize(340, 500)
    except Exception:
        pass

    inputs = args.command.commandInputs

    type_dd = inputs.addDropDownCommandInput(
        TYPE_DD_ID, 'Hardware type', adsk.core.DropDownStyles.TextListDropDownStyle)
    item_dd = inputs.addDropDownCommandInput(
        ITEM_DD_ID, 'Hardware', adsk.core.DropDownStyles.TextListDropDownStyle)

    # Thumbnail preview of the selected part (so number-named parts are identifiable).
    try:
        thumb = inputs.addImageCommandInput(THUMB_ID, '', PLACEHOLDER)
        # ~0.78 of the native 256x256 thumbnail ≈ 200px preview.
        thumb.scaleFactor = 0.78
    except Exception:
        futil.log('Insert Hardware: could not create thumbnail image input')

    # Selected-part caption, directly under the image.
    info = inputs.addTextBoxCommandInput(INFO_ID, '', '', 2, True)

    # Refresh reuses Carcass Maker's reset/defaults icon.
    refresh_btn = inputs.addBoolValueInput(REFRESH_ID, 'Refresh list', False, DEFAULTS_ICON_FOLDER, False)
    refresh_btn.tooltip = (
        'Re-read categories and parts from the project. The list is cached for '
        'speed, so click this after adding new hardware to the project.')

    _ensure_loaded()

    if _project is None:
        type_dd.listItems.add('Hardware library not found', True)
        type_dd.isEnabled = False
        item_dd.isEnabled = False
        if _error:
            info.formattedText = _error
        else:
            info.formattedText = (
                f"Project '{config.HARDWARE_PROJECT_NAME}' was not found. Pick the "
                f"hardware library with <b>Folders Selection</b> (any hub or "
                f"project), then click Refresh.")
        _add_handlers(args)
        return

    if not _categories:
        type_dd.listItems.add('(no category folders)', True)
        type_dd.isEnabled = False
        item_dd.isEnabled = False
        info.formattedText = (
            f"'{_project.name}' has no sub-folders. Add a folder per hardware "
            f"category (or pick another folder with Folders Selection), then "
            f"click Refresh.")
        _add_handlers(args)
        return

    for idx, name in enumerate(_categories):
        type_dd.listItems.add(name, idx == 0)
    _populate_items(inputs, next(iter(_categories)))
    _add_handlers(args)


def _add_handlers(args):
    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.inputChanged, command_input_changed, local_handlers=local_handlers)
    futil.add_handler(args.command.validateInputs, command_validate_input, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)


def _populate_items(inputs, category_name):
    """Refill the part dropdown from a category (uses the lazy per-category cache)."""
    item_dd = inputs.itemById(ITEM_DD_ID)
    item_dd.listItems.clear()

    items = _items_for(category_name)
    if not items:
        item_dd.listItems.add('(empty)', True)
        item_dd.isEnabled = False
    else:
        for idx, name in enumerate(items):
            item_dd.listItems.add(name, idx == 0)
        item_dd.isEnabled = True

    _set_thumbnail(inputs, PLACEHOLDER)
    _update_info(inputs)


def command_input_changed(args: adsk.core.InputChangedEventArgs):
    changed = args.input
    inputs = changed.parentCommand.commandInputs
    if changed.id == TYPE_DD_ID:
        sel = changed.selectedItem
        if sel:
            _populate_items(inputs, sel.name)
            _update_thumbnail(inputs)
    elif changed.id == ITEM_DD_ID:
        _update_info(inputs)
        _update_thumbnail(inputs)
    elif changed.id == REFRESH_ID:
        if changed.value:
            _refresh(inputs)
            changed.value = False  # re-arm the button


def _refresh(inputs):
    """Drop the cache, re-read the project, and rebuild both dropdowns."""
    global _project, _categories, _items_cache
    _project = None
    _categories = None
    _items_cache = {}
    folder_store.invalidate('hardware')
    _ensure_loaded()

    type_dd = inputs.itemById(TYPE_DD_ID)
    item_dd = inputs.itemById(ITEM_DD_ID)
    type_dd.listItems.clear()
    item_dd.listItems.clear()

    if _project is None or not _categories:
        type_dd.listItems.add('(project not found / empty)', True)
        type_dd.isEnabled = False
        item_dd.isEnabled = False
        _update_info(inputs)
        return

    type_dd.isEnabled = True
    for idx, name in enumerate(_categories):
        type_dd.listItems.add(name, idx == 0)
    _populate_items(inputs, next(iter(_categories)))


def _update_info(inputs):
    info = inputs.itemById(INFO_ID)
    type_dd = inputs.itemById(TYPE_DD_ID)
    item_dd = inputs.itemById(ITEM_DD_ID)
    cat = type_dd.selectedItem.name if type_dd.selectedItem else '-'
    part = item_dd.selectedItem.name if (item_dd.isEnabled and item_dd.selectedItem) else '-'
    info.formattedText = f"<div align='center'><b>Selected:</b> {cat} / {part}</div>"


def _set_thumbnail(inputs, path):
    thumb = inputs.itemById(THUMB_ID)
    if not thumb:
        return
    try:
        if path and os.path.exists(path):
            thumb.imageFile = path
        elif os.path.exists(PLACEHOLDER):
            thumb.imageFile = PLACEHOLDER
    except Exception:
        pass


def _update_thumbnail(inputs):
    """Show the selected part's thumbnail (downloading + caching it on first use)."""
    global _fetching
    if _fetching:
        return
    type_dd = inputs.itemById(TYPE_DD_ID)
    item_dd = inputs.itemById(ITEM_DD_ID)
    if not (type_dd.selectedItem and item_dd.isEnabled and item_dd.selectedItem):
        _set_thumbnail(inputs, PLACEHOLDER)
        return
    data_file = _items_cache.get(type_dd.selectedItem.name, {}).get(item_dd.selectedItem.name)
    if not data_file:
        _set_thumbnail(inputs, PLACEHOLDER)
        return
    _fetching = True
    try:
        _set_thumbnail(inputs, _thumbnail_path(data_file) or PLACEHOLDER)
    finally:
        _fetching = False


def _thumbnail_path(data_file):
    """Path to the part's 256x256 PNG thumbnail, downloading + caching on first use.

    The thumbnail lives on the cloud and arrives via a DataObjectFuture, so we poll
    briefly (bounded) for it. Returns None if it isn't ready in time — the caller
    falls back to the placeholder, and a later re-selection picks up the cached file
    once it has finished downloading."""
    try:
        key = re.sub(r'[^A-Za-z0-9]+', '_', f'{data_file.id}_{data_file.versionNumber}')
    except Exception:
        return None
    try:
        os.makedirs(_THUMB_DIR, exist_ok=True)
    except Exception:
        return None
    path = os.path.join(_THUMB_DIR, key + '.png')
    if os.path.exists(path):
        return path
    try:
        future = data_file.thumbnail
        if not future:
            return None
        for _ in range(30):                 # bounded wait (~1.5 s) for the cloud PNG
            data_object = future.dataObject
            if data_object:
                data_object.saveToFile(path)
                return path if os.path.exists(path) else None
            adsk.doEvents()
            time.sleep(0.05)
    except Exception:
        futil.log('Insert Hardware: thumbnail fetch failed or timed out')
    return None


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------
def command_execute(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Execute Event')
    inputs = args.command.commandInputs

    type_dd = inputs.itemById(TYPE_DD_ID)
    item_dd = inputs.itemById(ITEM_DD_ID)
    if not item_dd.isEnabled or not item_dd.selectedItem or not type_dd.selectedItem:
        ui.messageBox('Select a hardware part to insert.')
        return

    items = _items_cache.get(type_dd.selectedItem.name, {})
    data_file = items.get(item_dd.selectedItem.name)
    if not data_file:
        ui.messageBox('Could not resolve the selected hardware part.')
        return

    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        ui.messageBox('Open a design before inserting hardware.')
        return

    # A linked (referenced) insert requires the host document to be saved first.
    if not app.activeDocument.isSaved:
        ui.messageBox(
            'Save the active document once before inserting linked hardware, '
            'then run Insert Hardware again.')
        return

    occ = None
    try:
        root = design.rootComponent
        transform = adsk.core.Matrix3D.create()  # identity → insert at the origin
        occ = root.occurrences.addByInsert(data_file, transform, True)  # True = linked
        occ.isGrounded = False
    except Exception:
        futil.handle_error('Insert Hardware: insert failed', show_message_box=True)
        return

    # Hand the new part to Fusion's Move/Copy gizmo so it can be positioned and
    # oriented interactively, like a native component insert. Deferred via a
    # custom event because a command can't be started from inside execute.
    global _pending_occ
    _pending_occ = occ
    app.fireCustomEvent(MOVE_EVENT_ID)


def _on_position_event(args: adsk.core.CustomEventArgs):
    """Fires just after the Insert Hardware dialog closes: select the new part and
    launch Fusion's Move/Copy command so it can be placed interactively. If the
    user cancels Move, the part stays at the origin (undo removes it)."""
    global _pending_occ
    occ = _pending_occ
    _pending_occ = None
    if not occ:
        return
    try:
        selections = ui.activeSelections
        selections.clear()
        selections.add(occ)
        move_cmd = ui.commandDefinitions.itemById('FusionMoveCommand')
        if move_cmd:
            move_cmd.execute()
        else:
            futil.log('Insert Hardware: FusionMoveCommand not found; part left at origin.')
    except Exception:
        futil.handle_error('Insert Hardware: launch Move')


def command_validate_input(args: adsk.core.ValidateInputsEventArgs):
    inputs = args.inputs
    item_dd = inputs.itemById(ITEM_DD_ID)
    args.areInputsValid = bool(item_dd and item_dd.isEnabled and item_dd.selectedItem)


def command_destroy(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Destroy Event')
    global local_handlers
    local_handlers = []
