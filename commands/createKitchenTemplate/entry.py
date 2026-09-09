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

"""Create Kitchen Template — stock a ready-to-use kitchen ahead of time.

Builds, under ``Clients / Kitchens`` (``config.KITCHENS_PROJECT_NAME`` /
``config.KITCHENS_FOLDER_NAME``):

    Kitchen Template <n>/
        Kitchen Template <n>     a new hybrid design, saved and closed
        Library/                 a copy of the master cabinet library

No customer name is asked for, because at this point there is no customer — that
is the entire point. Copying a library is slow and network-bound, so templates
are built in a quiet moment and **New Kitchen** later turns one into a real job by
renaming it, which is instant.

The number is the LOWEST unused one, not the highest plus one: templates are
consumed by being renamed, so gaps open constantly and filling them keeps the
numbers small.

TIMING: Fusion forbids creating a document inside a command's event handlers — a
command runs inside a transaction and document creation can't be transacted. So
`execute` only parks the count; the work runs from a custom event fired as the
dialog closes (the same trick Insert Hardware uses to launch Move).
"""

import os
import traceback

import adsk.core

from .. import ui_helpers
from .. import kitchen_data
from .. import kitchen_build
from ...lib import fusionAddInUtils as futil
from ... import config

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_createKitchenTemplate'
CMD_NAME = 'Create Kitchen Template'
# Ribbon tooltip: one line on what it gives you. The folder layout and the copy
# mechanics belong in the dialog and the docstring, not on a hover.
CMD_Description = "Prepare spare kitchens in advance, ready for the next customer."
IS_PROMOTED = True

PANEL_ID = config.KITCHEN_PROJECT_PANEL_ID
PANEL_NAME = config.KITCHEN_PROJECT_PANEL_NAME
TAB_ID = config.KITCHEN_TAB_ID
TAB_NAME = config.KITCHEN_TAB_NAME

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

COUNT_ID = 'ckt_count'
PREVIEW_ID = 'ckt_preview'
INFO_ID = 'ckt_info'

MAX_BATCH = 20

local_handlers = []

CREATE_EVENT_ID = f'{config.COMPANY_NAME}_createKitchenTemplate_create'
_create_event = None
_pending_count = 0
_persistent_handlers = []


def start():
    cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_Description, ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)

    global _create_event
    try:
        _create_event = app.registerCustomEvent(CREATE_EVENT_ID)
    except Exception:
        app.unregisterCustomEvent(CREATE_EVENT_ID)
        _create_event = app.registerCustomEvent(CREATE_EVENT_ID)
    futil.add_handler(_create_event, _on_create_event, local_handlers=_persistent_handlers)

    panel = ui_helpers.get_panel(PANEL_ID, PANEL_NAME, tab_id=TAB_ID, tab_name=TAB_NAME)
    control = panel.controls.addCommand(cmd_def)
    control.isPromoted = IS_PROMOTED


def stop():
    ui_helpers.remove_command(PANEL_ID, CMD_ID, tab_id=TAB_ID)
    try:
        app.unregisterCustomEvent(CREATE_EVENT_ID)
    except Exception:
        pass
    _persistent_handlers.clear()


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------
def command_created(args: adsk.core.CommandCreatedEventArgs):
    futil.log(f'{CMD_NAME} Command Created Event')
    inputs = args.command.commandInputs

    try:
        args.command.setDialogInitialSize(420, 280)
    except Exception:
        pass

    count = inputs.addIntegerSpinnerCommandInput(COUNT_ID, 'How many', 1, MAX_BATCH, 1, 1)
    count.tooltip = ('How many spares to prepare. Each one takes a few minutes to '
                     'set up, so it is worth doing a batch while you are free.')

    preview = inputs.addTextBoxCommandInput(PREVIEW_ID, '', '', 3, True)
    info = inputs.addTextBoxCommandInput(INFO_ID, '', '', 4, True)
    for box in (preview, info):
        try:
            box.isFullWidth = True
        except Exception:
            pass
    info.formattedText = _setup_summary()
    _update_preview(inputs)

    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.inputChanged, command_input_changed, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)


def _setup_summary():
    """Tick / cross for the two configured locations, so a name that has drifted
    on the cloud is obvious before anyone commits to a long copy."""
    lines = []
    root, root_error = kitchen_data.kitchens_root()
    where = config.KITCHENS_PROJECT_NAME + (
        f' / {config.KITCHENS_FOLDER_NAME}' if config.KITCHENS_FOLDER_NAME else '')
    lines.append(_status_line('Kitchens', where, root is not None))

    source, source_error = kitchen_data.library_source_folder()
    label = config.LIBRARY_SOURCE_FOLDER or '(project root)'
    lines.append(_status_line('Library', f'{config.LIBRARY_PROJECT_NAME} / {label}',
                              source is not None))
    if source is not None:
        lines.append(f'<i>{kitchen_data.count_files(source)} cabinet file(s) will be '
                     f'copied into each template.</i>')
    for error in (root_error, source_error):
        if error:
            lines.append(f'<i>{error}</i>')
    return '<br>'.join(lines)


def _status_line(label, value, ok):
    mark = "<font color='#1a7f37'>&#10003;</font>" if ok else "<font color='#d1242f'>&#10007;</font>"
    return f"{mark} <b>{label}:</b> {value}" + ('' if ok else ' &mdash; <i>not found</i>')


def command_input_changed(args: adsk.core.InputChangedEventArgs):
    if args.input.id == COUNT_ID:
        _update_preview(args.input.parentCommand.commandInputs)


def _update_preview(inputs):
    preview = inputs.itemById(PREVIEW_ID)
    root, error = kitchen_data.kitchens_root()
    if not root:
        preview.formattedText = f"<i>{error}</i>"
        return

    count = inputs.itemById(COUNT_ID).value
    # Names are predicted by pretending each one already exists, so a batch shows
    # the real numbers it will take rather than the same one repeated.
    taken = {row['number'] for row in kitchen_data.template_folders(root)}
    names, number = [], 1
    prefix = (config.KITCHEN_TEMPLATE_PREFIX or 'Kitchen Template').strip()
    while len(names) < min(count, 4):
        if number not in taken:
            names.append(f'{prefix} {number}')
            taken.add(number)
        number += 1
    shown = ', '.join(f'<b>{n}</b>' for n in names)
    if count > len(names):
        shown += f' … (+{count - len(names)} more)'

    where = config.KITCHENS_PROJECT_NAME + (
        f' / {config.KITCHENS_FOLDER_NAME}' if config.KITCHENS_FOLDER_NAME else '')
    waiting = len(kitchen_data.template_folders(root))
    preview.formattedText = (
        f"<b>Will create in</b> {where}<br>{shown}<br>"
        f"<i>{waiting} template(s) already waiting there.</i>")


def command_execute(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Execute Event')
    global _pending_count
    _pending_count = int(args.command.commandInputs.itemById(COUNT_ID).value or 0)
    if _pending_count > 0:
        app.fireCustomEvent(CREATE_EVENT_ID)


def command_destroy(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Destroy Event')
    global local_handlers
    local_handlers = []


# ---------------------------------------------------------------------------
# The actual work (runs after the dialog has closed)
# ---------------------------------------------------------------------------
def _on_create_event(args: adsk.core.CustomEventArgs):
    global _pending_count
    count = _pending_count
    _pending_count = 0
    if count < 1:
        return
    try:
        _build_templates(count)
    except Exception:
        futil.log(f'{CMD_NAME}: {traceback.format_exc()}')
        ui.messageBox(f'Create Kitchen Template failed:\n{traceback.format_exc()}', CMD_NAME)


def _build_templates(count):
    root, error = kitchen_data.kitchens_root()
    if not root:
        ui.messageBox(error, CMD_NAME)
        return

    source, error = kitchen_data.library_source_folder()
    if not source:
        ui.messageBox(error, CMD_NAME)
        return

    per_template = max(1, kitchen_data.count_files(source))
    progress_dialog = ui.createProgressDialog()
    progress_dialog.isCancelButtonShown = True
    progress_dialog.show(CMD_NAME, 'Preparing…', 0, per_template * count, 0)

    built, failures, copy_failures = [], [], 0
    done_before = 0
    try:
        for index in range(count):
            name = kitchen_data.next_template_name(root)
            progress_dialog.message = f'{name} — %v of %m'
            adsk.doEvents()
            if progress_dialog.wasCancelled:
                break

            result = _build_one(root, source, name, progress_dialog, done_before)
            done_before += per_template
            if result is None:                      # cancelled mid-copy
                break
            if result['error']:
                failures.append((name, result['error']))
                continue
            copy_failures += len(result['failed'])
            built.append((name, result['copied']))
    finally:
        progress_dialog.hide()

    _report(built, failures, copy_failures, cancelled=progress_dialog.wasCancelled)


def _build_one(root, source, name, progress_dialog, done_before):
    """One template, via the shared builder. Returns None if the user cancelled
    during the copy, else the builder's report."""
    def on_progress(done, file_name):
        progress_dialog.progressValue = done_before + done
        # %v / %m are ProgressDialog placeholders, so a '%' in a filename has to
        # be escaped or the dialog eats the rest of the message.
        progress_dialog.message = f"{name} — {str(file_name).replace('%', '%%')} — %v of %m"
        adsk.doEvents()
        return not progress_dialog.wasCancelled

    # customer='' and keep_open=False: a template has no customer yet, and nobody
    # works in a template — the designer's next stop is New Kitchen.
    report = kitchen_build.build_kitchen(root, source, name, customer='',
                                         progress=on_progress, keep_open=False)
    return None if report['cancelled'] else report


def _report(built, failures, copy_failures, cancelled):
    lines = []
    if built:
        names = ', '.join(name for name, _ in built)
        cabinets = built[0][1] if built else 0
        lines.append(f'{len(built)} template(s) ready: {names}')
        lines.append(f'{cabinets} cabinet file(s) copied into each.')
        lines.append('Run New Kitchen to turn one into a customer job.')
    else:
        lines.append('No templates were created.')
    if cancelled:
        lines.append('\nCancelled — any part-built folder was left in the Data '
                     'Panel for you to delete.')
    if copy_failures:
        lines.append(f'\n{copy_failures} library file(s) could not be copied.')
    for name, why in failures:
        lines.append(f'{name}: {why}')
    ui.messageBox('\n'.join(lines), CMD_NAME)
