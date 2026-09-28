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

"""New Kitchen — start a customer's kitchen.

Pick the project type (B2B or B2C), type the customer's name and OK. The kitchen
ends up at ``Projects / <type> / Kitchen / <Customer>_Kitchen_<date>`` in the hub
open in Fusion, holding a design of the same
name and its own ``Library`` copy of the cabinet catalogue, and the design opens
ready to work in.

TWO ROUTES TO THE SAME RESULT, and the designer shouldn't have to care which:

* **A template is waiting** in that type's Kitchen folder (the normal case) —
  rename it. B2B and B2C keep separate pools of spares. The folder AND the
  design inside it are renamed, and that's the whole operation: two metadata
  writes, instant.

      Kitchen Template 3/                 Al Rashid_Kitchen_2026-09-09/
          Kitchen Template 3        ->        Al Rashid_Kitchen_2026-09-09
          Library/                            Library/

* **Nothing is waiting** — build it from scratch, exactly as Create Kitchen
  Template would, but under the customer's name directly. This takes minutes
  rather than moments because the whole library has to be copied, so the dialog
  says so before OK is pressed.

  The fallback exists because "no templates available" is a dead end at the worst
  possible moment: a fresh install, or a busy week that drained the spares, with a
  customer waiting. Both routes produce an identical folder with identical stamps,
  so nothing downstream can tell them apart.

WHY TEMPLATES AT ALL, GIVEN THE FALLBACK: copying a library is slow and
network-bound. Stocking spares in a quiet moment is what makes the common case
instant — the fallback is the safety net, not the plan.

There is deliberately NO template picker: templates are interchangeable, so a
choice would be a question with no wrong answer. The dialog just says which one it
will use.

TIMING: Fusion forbids creating or opening a document inside a command's event
handlers (a command runs in a transaction), so `execute` parks the inputs and the
work runs from a custom event fired as the dialog closes.
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

CMD_ID = f'{config.COMPANY_NAME}_newKitchen'
CMD_NAME = 'New Kitchen'
# Ribbon tooltip: what the designer gets, in one line. The mechanics (templates,
# renaming, folder layout) belong in the dialog, not on a hover.
CMD_Description = "Start a new kitchen for a customer, ready to design."
IS_PROMOTED = True

PANEL_ID = config.KITCHEN_PROJECT_PANEL_ID
PANEL_NAME = config.KITCHEN_PROJECT_PANEL_NAME
TAB_ID = config.KITCHEN_TAB_ID
TAB_NAME = config.KITCHEN_TAB_NAME

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

TYPE_ID = 'nk_type'
CUSTOMER_ID = 'nk_customer'
OPEN_ID = 'nk_open'
PREVIEW_ID = 'nk_preview'
INFO_ID = 'nk_info'

local_handlers = []

START_EVENT_ID = f'{config.COMPANY_NAME}_newKitchen_start'
_start_event = None
_pending = None            # {'row', 'customer', 'open', 'root', 'source', 'where'}
_template = None           # the template this dialog will claim, if any
_waiting = 0
_project_type = ''
_root = None
_root_error = None
_source = None             # master library — only resolved for the build fallback
_source_error = None
_taken = set()             # lower-cased folder names in _root (for name previews)
_source_count = None       # files in the master library, counted on demand
_persistent_handlers = []


def start():
    cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_Description, ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)

    global _start_event
    try:
        _start_event = app.registerCustomEvent(START_EVENT_ID)
    except Exception:
        app.unregisterCustomEvent(START_EVENT_ID)
        _start_event = app.registerCustomEvent(START_EVENT_ID)
    futil.add_handler(_start_event, _on_start_event, local_handlers=_persistent_handlers)

    panel = ui_helpers.get_panel(PANEL_ID, PANEL_NAME, tab_id=TAB_ID, tab_name=TAB_NAME)
    control = panel.controls.addCommand(cmd_def)
    control.isPromoted = IS_PROMOTED


def stop():
    ui_helpers.remove_command(PANEL_ID, CMD_ID, tab_id=TAB_ID)
    try:
        app.unregisterCustomEvent(START_EVENT_ID)
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
        args.command.setDialogInitialSize(430, 300)
    except Exception:
        pass

    kind = inputs.addDropDownCommandInput(TYPE_ID, 'Project type',
                                          adsk.core.DropDownStyles.TextListDropDownStyle)
    chosen = kitchen_data.default_project_type()
    for t in kitchen_data.project_types():
        kind.listItems.add(t, t == chosen, '')
    kind.tooltip = 'Which kind of project this kitchen is — it is filed under that folder.'
    _resolve(chosen)

    name_box = inputs.addStringValueInput(CUSTOMER_ID, 'Customer name', '')
    name_box.tooltip = 'Names the kitchen folder and the design inside it.'

    open_after = inputs.addBoolValueInput(OPEN_ID, 'Open the kitchen afterwards',
                                          True, '', True)
    open_after.tooltip = 'Opens the design straight away so you can start placing cabinets.'
    open_after.isVisible = _can_run()

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
    futil.add_handler(args.command.validateInputs, command_validate_input, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)


def _resolve(project_type):
    """Look up the kitchens folder, the template to claim and (only if needed)
    the master library for one project type."""
    global _project_type, _template, _waiting, _root, _root_error, _source, _source_error
    global _taken, _source_count
    _project_type = project_type or ''
    _root, _root_error = kitchen_data.kitchens_root(_project_type)
    # One listing of the kitchens folder serves the template count, the choice of
    # template and the "is this customer name free" check on every keystroke.
    rows = kitchen_data.template_folders(_root, details=False) if _root else []
    _template = kitchen_data.latest_template(_root, rows) if rows else None
    _waiting = len(rows)
    _taken = kitchen_data.child_folder_names(_root) if _root else set()
    _source_count = None

    # Only look up the master library when it's actually needed — the normal
    # (template) path never touches it, and it's a network round trip.
    if _root and not _template:
        _source, _source_error = kitchen_data.library_source_folder()
    else:
        _source, _source_error = None, None


def _library_count():
    """Files in the master library, counted once per dialog."""
    global _source_count
    if _source_count is None:
        _source_count = kitchen_data.count_files(_source) if _source else 0
    return _source_count


def _can_run():
    """A kitchen can be started either way: from a template, or built fresh."""
    return bool(_root and (_template or _source))


def _where():
    return kitchen_data.kitchens_where(_project_type)


def _setup_summary():
    hub = kitchen_data.active_hub_name()
    body = _setup_body()
    return f'<b>Hub:</b> {hub}<br>{body}' if hub else body


def _setup_body():
    if not _root:
        return f"<font color='#d1242f'><b>{_root_error}</b></font>"

    if _template:
        left = _waiting - 1
        tail = (f"{left} more are ready after this one." if left > 0 else
                "<b>This is the last one ready</b> &mdash; prepare more when you "
                "get a chance.")
        return (f"<font color='#1a7f37'>&#10003;</font> Ready to go instantly, "
                f"using <b>{_template['name']}</b> ({_template['cabinets']} "
                f"cabinets). {tail}")

    if not _source:
        return f"<font color='#d1242f'><b>{_source_error}</b></font>"

    # The fallback. Say plainly that this one is slow and why, so nobody thinks
    # Fusion has hung — and point at the fix for next time.
    return (f"<b>No kitchens are prepared and waiting</b>, so this one will be set "
            f"up from scratch — copying {_library_count()} "
            f"cabinets into it takes a few minutes.<br>"
            f"<i>Run <b>Create Kitchen Template</b> when you have a spare moment "
            f"and the next one is instant.</i>")


def command_input_changed(args: adsk.core.InputChangedEventArgs):
    inputs = args.input.parentCommand.commandInputs
    if args.input.id == TYPE_ID:
        item = args.input.selectedItem
        _resolve(item.name if item else '')
        inputs.itemById(OPEN_ID).isVisible = _can_run()
        inputs.itemById(INFO_ID).formattedText = _setup_summary()
        _update_preview(inputs)
    elif args.input.id == CUSTOMER_ID:
        _update_preview(inputs)


def _update_preview(inputs):
    preview = inputs.itemById(PREVIEW_ID)
    customer = kitchen_data.clean_name(inputs.itemById(CUSTOMER_ID).value)
    if not _can_run():
        preview.formattedText = ''
        return
    if not customer:
        preview.formattedText = '<i>Type a customer name.</i>'
        return
    new_name = kitchen_data.unique_folder_name(
        _root, kitchen_data.kitchen_folder_name(customer), _taken)
    preview.formattedText = (
        f"<b>Your kitchen will be</b><br>"
        f"{_where()} / <b>{new_name}</b><br>"
        f"<i>with its own cabinet library inside.</i>")


def command_validate_input(args: adsk.core.ValidateInputsEventArgs):
    customer = kitchen_data.clean_name(args.inputs.itemById(CUSTOMER_ID).value)
    args.areInputsValid = bool(customer and _can_run())


def command_execute(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Execute Event')
    inputs = args.command.commandInputs
    customer = kitchen_data.clean_name(inputs.itemById(CUSTOMER_ID).value)
    if not customer or not _can_run():
        return
    global _pending
    _pending = {'row': _template, 'customer': customer,
                'open': inputs.itemById(OPEN_ID).value,
                'root': _root, 'source': _source, 'where': _where()}
    kitchen_data.remember_project_type(_project_type)
    app.fireCustomEvent(START_EVENT_ID)


def command_destroy(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Destroy Event')
    global local_handlers
    local_handlers = []


# ---------------------------------------------------------------------------
# The work (runs after the dialog has closed)
# ---------------------------------------------------------------------------
def _on_start_event(args: adsk.core.CustomEventArgs):
    global _pending
    job = _pending
    _pending = None
    if not job:
        return
    try:
        # The name is worked out here, not when the dialog opened, so a kitchen
        # someone else created in between can't be collided with.
        name = kitchen_data.unique_folder_name(
            job['root'], kitchen_data.kitchen_folder_name(job['customer']))
        if job['row']:
            _from_template(job, name)
        else:
            _from_scratch(job, name)
    except Exception:
        futil.log(f'{CMD_NAME}: {traceback.format_exc()}')
        ui.messageBox(f'New Kitchen could not finish:\n{traceback.format_exc()}', CMD_NAME)


def _from_template(job, name):
    """The fast path: rename a waiting template, folder and design alike."""
    row = job['row']
    ok, warning = kitchen_data.rename_kitchen(row['folder'], row['design'], name)
    if not ok:
        ui.messageBox(warning, CMD_NAME)
        return

    lines = [f"{name} is ready in {job['where']}."]
    if warning:
        lines.append(warning)
    if job['open'] and row['design']:
        lines.append(_open_line(_open_and_stamp(row['design'], job['customer'])))
    ui.messageBox('\n'.join(lines), CMD_NAME)


def _from_scratch(job, name):
    """The fallback: build the kitchen the way Create Kitchen Template would,
    straight under the customer's name."""
    customer = job['customer']
    total = max(1, kitchen_data.count_files(job['source']))

    progress_dialog = ui.createProgressDialog()
    progress_dialog.isCancelButtonShown = True
    progress_dialog.show(CMD_NAME, f'Setting up {name} — %v of %m', 0, total, 0)

    def on_progress(done, file_name):
        progress_dialog.progressValue = min(done, total)
        # %v / %m are ProgressDialog placeholders, so a '%' in a filename has to
        # be escaped or the dialog eats the rest of the message.
        progress_dialog.message = (
            f"Adding {str(file_name).replace('%', '%%')} — %v of %m")
        adsk.doEvents()
        return not progress_dialog.wasCancelled

    try:
        report = kitchen_build.build_kitchen(job['root'], job['source'], name,
                                             customer=customer,
                                             progress=on_progress,
                                             keep_open=job['open'])
    finally:
        progress_dialog.hide()

    if report['cancelled']:
        ui.messageBox(
            f"Stopped after adding {report['copied']} cabinet(s).\n\n"
            f"'{name}' was left in place with what it got — delete it in the Data "
            f"Panel if you'd rather start again.", CMD_NAME)
        return
    if report['error']:
        ui.messageBox(f"'{name}' is not complete: {report['error']}.\n\n"
                      f"Check it in the Data Panel.", CMD_NAME)
        return

    lines = [f"{name} is ready in {job['where']}.",
             f"{report['copied']} cabinet(s) copied into its library."]
    if report['failed']:
        lines.append(f"{len(report['failed'])} could not be copied.")
    if job['open']:
        lines.append(_open_line(report['document'] is not None))
    lines.append('\nTip: run Create Kitchen Template when you have a moment — the '
                 'next kitchen then starts instantly.')
    ui.messageBox('\n'.join(lines), CMD_NAME)


def _open_line(opened):
    return ('The design is open — drag cabinets in from its Library folder.'
            if opened else 'Open it from the Data Panel to start designing.')


def _open_and_stamp(design_file, customer):
    """Open a renamed design and record the customer on it.

    The stamp is only for display — Finish Kitchen falls back to the folder name —
    so a failure here is worth nothing more than skipping the save."""
    try:
        document = app.documents.open(design_file.latestVersion or design_file, True)
    except Exception:
        try:
            document = app.documents.open(design_file, True)
        except Exception:
            return False
    if not document:
        return False
    try:
        if kitchen_data.stamp_customer(document, customer):
            document.save(f'Assigned to {customer}.')
    except Exception:
        pass
    return True
