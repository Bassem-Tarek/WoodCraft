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

"""Finish Kitchen — clear the cabinets the customer's kitchen never used.

Create Kitchen Template gives every job a private copy of the whole cabinet
library. Once the design is finished most of that copy is dead weight, so this
command deletes every file in the kitchen's ``Library`` folder that the design
does not reference, and prunes the sub-folders that leaves empty. What survives is
exactly the set of cabinets in this kitchen.

A note on WORDING: everything the user sees says "clear" and "tidy", never
"delete". That is not spin — deleted files go to the project's deleted items and
are restorable — but a designer who is scared of a button won't press it, and this
one is the whole point of giving each job its own library copy.

USED means referenced, at any depth: ``allDocumentReferences`` covers a placed
cabinet AND the hardware that cabinet itself references, so Finish Kitchen can't
delete a part out from under a cabinet still in the design.

It REFUSES to run on an unassigned ``Kitchen Template <n>`` — a fresh template
references nothing, so "delete everything unused" would wipe the library copy that
makes the template worth having. Run New Kitchen on it first.

TWO SAFETY NETS, because this deletes cloud files:
* The dialog lists every file it is about to delete before you press OK.
* Fusion's own ``DataFile.deleteMe()`` refuses a file another file references or
  that someone has open. Anything that refuses is reported as kept, not as an
  error — so even a wrong answer from the reference scan can't orphan a cabinet.

The design must be SAVED first: the cloud decides what is referenced from the
saved version, so a cabinet placed but not yet saved would look unused.
"""

import os
import traceback

import adsk.core

from .. import ui_helpers
from .. import kitchen_data
from ...lib import fusionAddInUtils as futil
from ... import config

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_finishKitchen'
CMD_NAME = 'Finish Kitchen'
# Ribbon tooltip: what it is FOR, in one line. See the wording note above.
CMD_Description = "Tidy this kitchen's cabinet library down to the cabinets you used."
IS_PROMOTED = True

PANEL_ID = config.KITCHEN_PROJECT_PANEL_ID
PANEL_NAME = config.KITCHEN_PROJECT_PANEL_NAME
TAB_ID = config.KITCHEN_TAB_ID
TAB_NAME = config.KITCHEN_TAB_NAME

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

LIST_ID = 'fk_list'
PRUNE_ID = 'fk_prune'

local_handlers = []

# Scanned in command_created, consumed by the deferred delete. Deleting cloud
# files from inside a command's execute means doing network work inside Fusion's
# command transaction; the custom event runs after the dialog has closed, which
# also lets the progress dialog behave.
DELETE_EVENT_ID = f'{config.COMPANY_NAME}_finishKitchen_delete'
_delete_event = None
_pending = None            # {'rows': [...], 'library': DataFolder, 'prune': bool}
_scan = None               # last scan result, rebuilt on every dialog open
_persistent_handlers = []


def start():
    cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_Description, ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)

    global _delete_event
    try:
        _delete_event = app.registerCustomEvent(DELETE_EVENT_ID)
    except Exception:
        app.unregisterCustomEvent(DELETE_EVENT_ID)
        _delete_event = app.registerCustomEvent(DELETE_EVENT_ID)
    futil.add_handler(_delete_event, _on_delete_event, local_handlers=_persistent_handlers)

    panel = ui_helpers.get_panel(PANEL_ID, PANEL_NAME, tab_id=TAB_ID, tab_name=TAB_NAME)
    control = panel.controls.addCommand(cmd_def)
    control.isPromoted = IS_PROMOTED


def stop():
    ui_helpers.remove_command(PANEL_ID, CMD_ID, tab_id=TAB_ID)
    try:
        app.unregisterCustomEvent(DELETE_EVENT_ID)
    except Exception:
        pass
    _persistent_handlers.clear()


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
def _scan_kitchen():
    """{'error': str} when the command can't run, otherwise
    {'library', 'rows', 'used', 'customer', 'how'}."""
    document = app.activeDocument
    if not document:
        return {'error': 'Open the kitchen design first.'}

    if not document.isSaved:
        return {'error': 'This design has never been saved. Save it into the '
                         'customer folder first.'}
    if kitchen_data.is_unassigned_template(document):
        return {'error': 'This is still an unassigned <b>kitchen template</b>.<br>'
                         'A fresh template references nothing, so clearing "unused" '
                         'cabinets would empty its whole Library.<br>'
                         'Run <b>New Kitchen</b> on it first to assign it to a '
                         'customer.'}
    if document.isModified:
        return {'error': 'Save the design first.<br>The cloud works out which '
                         'cabinets are in use from the saved version, so unsaved '
                         'cabinets would look unused and be cleared.'}

    library, how = kitchen_data.find_library_folder(document)
    if not library:
        return {'error': f"No '{config.KITCHEN_LIBRARY_FOLDER_NAME}' folder found "
                         f"for this design.<br>Finish Kitchen works on a design "
                         f"built by <b>Create Kitchen Template</b>, or on any design "
                         f"saved beside a '{config.KITCHEN_LIBRARY_FOLDER_NAME}' "
                         f"folder."}

    used = kitchen_data.referenced_file_ids(document)
    rows = kitchen_data.unused_files(library, used)
    return {'library': library, 'rows': rows, 'used': used, 'how': how,
            'customer': kitchen_data.kitchen_customer(document) or ''}


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------
def command_created(args: adsk.core.CommandCreatedEventArgs):
    futil.log(f'{CMD_NAME} Command Created Event')
    inputs = args.command.commandInputs

    try:
        args.command.setDialogInitialSize(460, 520)
    except Exception:
        pass

    global _scan
    _scan = _scan_kitchen()

    box = inputs.addTextBoxCommandInput(LIST_ID, '', _summary_html(_scan), 22, True)
    try:
        box.isFullWidth = True
    except Exception:
        pass

    prune = inputs.addBoolValueInput(PRUNE_ID, 'Tidy up empty folders too',
                                     True, '', True)
    prune.tooltip = 'Leaves the library neat, with no empty folders behind.'
    prune.isVisible = bool(_scan.get('rows'))

    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.validateInputs, command_validate_input, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)


def _summary_html(scan):
    if scan.get('error'):
        return f"<font color='#d1242f'><b>Can't run yet.</b></font><br>{scan['error']}"

    rows = scan['rows']
    used = len(scan['used'])
    header = (f"Kitchen: <b>{scan['customer']}</b><br>" if scan['customer'] else '')
    header += (f"Library: <b>{scan['library'].name}</b> &nbsp;&mdash;&nbsp; "
               f"{used} file(s) referenced by this design.<br>")

    if not rows:
        return header + ("<br><b>Nothing to tidy.</b> Every cabinet in the library "
                         "is used by this kitchen.")

    lines = [header]
    if not used:
        lines.append("<font color='#d1242f'><b>This design references no cabinets "
                     "at all</b>, so everything below will go. Check you have the "
                     "right kitchen open.</font><br>")
    lines.append(f"<br><b>{len(rows)} cabinet(s) aren't used in this kitchen and "
                 f"will be cleared out:</b><br>")
    for data_file, name, path in rows[:config.KITCHEN_MAX_LISTED]:
        where = f"<font color='#777777'>{path}</font>" if path else ''
        lines.append(f'&nbsp;&nbsp;{where}{name}')
    if len(rows) > config.KITCHEN_MAX_LISTED:
        lines.append(f'&nbsp;&nbsp;<i>… and {len(rows) - config.KITCHEN_MAX_LISTED} more</i>')
    lines.append("<br><i>Anything cleared goes to the project's deleted items, so "
                 "it can be put back from the Data Panel at any time.</i>")
    return '<br>'.join(lines)


def command_validate_input(args: adsk.core.ValidateInputsEventArgs):
    args.areInputsValid = bool(_scan and not _scan.get('error') and _scan.get('rows'))


def command_execute(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Execute Event')
    if not _scan or _scan.get('error') or not _scan.get('rows'):
        return
    global _pending
    _pending = {
        'rows': _scan['rows'],
        'library': _scan['library'],
        'prune': args.command.commandInputs.itemById(PRUNE_ID).value,
    }
    app.fireCustomEvent(DELETE_EVENT_ID)


def command_destroy(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Destroy Event')
    global local_handlers
    local_handlers = []


# ---------------------------------------------------------------------------
# Delete (runs after the dialog has closed)
# ---------------------------------------------------------------------------
def _on_delete_event(args: adsk.core.CustomEventArgs):
    global _pending
    job = _pending
    _pending = None
    if not job:
        return
    try:
        _run_delete(job)
    except Exception:
        futil.log(f'{CMD_NAME}: {traceback.format_exc()}')
        ui.messageBox(f'Finish Kitchen failed:\n{traceback.format_exc()}', CMD_NAME)


def _run_delete(job):
    rows = job['rows']
    total = max(1, len(rows))

    progress_dialog = ui.createProgressDialog()
    progress_dialog.isCancelButtonShown = True
    progress_dialog.show(CMD_NAME, 'Clearing unused cabinets — %v of %m', 0, total, 0)

    def on_progress(done, name):
        progress_dialog.progressValue = min(done, total)
        progress_dialog.message = f"Clearing {str(name).replace('%', '%%')} — %v of %m"
        adsk.doEvents()
        return not progress_dialog.wasCancelled

    try:
        report = kitchen_data.delete_files(rows, progress=on_progress)
        pruned = 0
        if job['prune'] and not report['cancelled']:
            progress_dialog.message = 'Tidying empty folders…'
            adsk.doEvents()
            pruned = kitchen_data.prune_empty_folders(job['library'])
    finally:
        progress_dialog.hide()

    lines = [f"{report['deleted']} unused cabinet(s) cleared from "
             f"{job['library'].name}."]
    if pruned:
        lines.append(f'{pruned} empty folder(s) tidied away.')
    if report['cancelled']:
        lines.append('Cancelled — the rest were left in place. '
                     'Run Finish Kitchen again to continue.')
    if report['failed']:
        shown = ', '.join(name for name, _ in report['failed'][:10])
        more = f" (+{len(report['failed']) - 10} more)" if len(report['failed']) > 10 else ''
        lines.append(f"\n{len(report['failed'])} were kept because they are still "
                     f"in use or open somewhere: {shown}{more}")
    ui.messageBox('\n'.join(lines), CMD_NAME)
