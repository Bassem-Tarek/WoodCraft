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

"""Create Configurations — every combination of the theme tables, named alike.

Run it on a configured design (the cabinet itself, not a kitchen that places
one). It reads that design's own theme tables and does two things:

    1. renames the rows already in the table to the naming scheme, so a row
       typed by hand years ago reads the same as one made this morning;
    2. adds a row for every combination that is not there yet.

The dialog IS the dry run: opening it shows every rename and every new row and
changes nothing, so OK is only ever pressed on a plan you have read. Renaming can
be switched off with the checkbox if you only want the new rows.

It creates rows and stops. It does not build them and it does not save. Fusion
builds a configuration when the row is activated, and a row cannot be built until
the document has been saved anyway — build one created moments ago in an unsaved
document and Fusion answers "Select failed because Configuration was temporarily
unavailable".

Safe to run again — it skips combinations that already exist and rows already
named right, so adding a value to a theme table and re-running only creates the
rows that value made possible.
"""

import os

import adsk.core
import adsk.fusion

from .. import config_table
from .. import ui_helpers
from ...lib import fusionAddInUtils as futil
from ... import config

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_configRows'
CMD_NAME = 'Create Configurations'
CMD_Description = (
    'Rename this configured design\'s existing configurations to the naming '
    'scheme and add a row for every combination of its theme tables that is '
    'missing. Rows only — nothing is built and nothing is saved.'
)
# Sits on the face of the panel next to the other Cabinet Builder commands
# rather than inside the drop-down.
IS_PROMOTED = True

# Cabinet Builder: this shapes a cabinet's own library file, not a kitchen.
PANEL_ID = config.CABINET_PANEL_ID
PANEL_NAME = config.CABINET_PANEL_NAME

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

INFO_ID = 'cr_info'
RENAME_ID = 'cr_rename'

# Row creation is quick, but it is still table surgery, and the same rule that
# bit Fit Handles applies: Fusion does not settle a document mid-command. The
# work runs from a custom event, after the dialog has closed.
RUN_EVENT_ID = 'WoodCraftCreateConfigurationsRun'

local_handlers = []
_event_handlers = []
_run_event = None
_pending = None          # the rename flag while a run is queued, else None


class _RunHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        global _pending
        job, _pending = _pending, None
        if job is None:
            return
        try:
            _create(job)
        except Exception:
            futil.handle_error(CMD_NAME)


def _arm_event():
    """Register the run event with a live handler, replacing any stale one."""
    global _run_event
    try:
        app.unregisterCustomEvent(RUN_EVENT_ID)
    except Exception:
        pass
    _run_event = app.registerCustomEvent(RUN_EVENT_ID)
    handler = _RunHandler()
    _run_event.add(handler)
    _event_handlers.append(handler)
    del _event_handlers[:-2]


def start():
    cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_Description, ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)
    _arm_event()

    panel = ui_helpers.get_panel(PANEL_ID, PANEL_NAME)
    control = panel.controls.addCommand(cmd_def)
    # isPromotedByDefault as well as isPromoted: Fusion remembers what the user
    # last had promoted, and without the default it would keep hiding the button
    # in the drop-down for anyone who ran an older build.
    control.isPromotedByDefault = IS_PROMOTED
    control.isPromoted = IS_PROMOTED


def stop():
    global _run_event
    _run_event = None
    _event_handlers.clear()
    try:
        app.unregisterCustomEvent(RUN_EVENT_ID)
    except Exception:
        pass
    ui_helpers.remove_command(PANEL_ID, CMD_ID)


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------
def _current_plan(rename=True):
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        return None, None
    return design, config_table.plan(design, app.activeDocument.name, rename)


def _rename_lines(result):
    """The rename half of the summary."""
    lines = []
    if result.to_rename:
        lines.append('<b>%d row(s) to rename</b> (%d already named right).'
                     % (len(result.to_rename), result.named_right))
        for item in result.to_rename[:4]:
            lines.append('&nbsp;&nbsp;%s &nbsp;&rarr;&nbsp; <b>%s</b>'
                         % (item['old'], item['new']))
        if len(result.to_rename) > 4:
            lines.append('&nbsp;&nbsp;… and %d more.'
                         % (len(result.to_rename) - 4))
    elif result.named_right == result.rows and result.rows:
        lines.append('All %d existing row(s) already match the naming scheme.'
                     % result.rows)
    else:
        lines.append('No existing row will be renamed.')
    if result.rename_dupes:
        lines.append('<b>%d row(s) keep their name</b> — another row wants the '
                     'same one: %s' % (len(result.rename_dupes),
                                       ', '.join(result.rename_dupes[:4])))
    if result.unreadable:
        lines.append('<b>%d row(s) could not be read</b> and are left alone: %s'
                     % (len(result.unreadable),
                        ', '.join(result.unreadable[:4])))
    return lines


def _summary(result):
    if result is None:
        return 'Open a configured design first.', False
    if result.error:
        return result.error, False

    lines = ['<b>%d combinations</b> of %s.' % (
        result.total,
        ', '.join('%s (%d)' % (t, len(a))
                  for t, a in zip(result.vary, result.axes)))]
    lines.append('%d already in the table, <b>%d to create</b>.'
                 % (result.present, len(result.to_create)))
    if result.untouched:
        lines.append('')
        lines.append('Not varied — inherited from "%s": %s' % (
            result.base.name,
            ', '.join('%s = %s' % (t, v)
                      for t, v in sorted(result.untouched.items()))))

    lines.append('')
    lines.extend(_rename_lines(result))

    if result.clashes:
        lines.append('')
        lines.append('<b>%d new name(s) would collide</b> and Fusion would append '
                     '"(1)": %s' % (len(result.clashes),
                                    ', '.join(result.clashes[:4])))
    lines.append('')
    if result.to_create:
        lines.append('First few to create: ' +
                     ', '.join(n for n, _c in result.to_create[:3]))
        if len(result.to_create) > 3:
            lines.append('… and %d more.' % (len(result.to_create) - 3))
        lines.append('')
    lines.append('Rows and names only — nothing is built and nothing is saved.')
    return '<br>'.join(lines), bool(result.to_create or result.to_rename)


def command_created(args: adsk.core.CommandCreatedEventArgs):
    futil.log(f'{CMD_NAME} Command Created Event')
    inputs = args.command.commandInputs

    try:
        args.command.setDialogInitialSize(480, 420)
    except Exception:
        pass

    _design, result = _current_plan(True)

    rename = inputs.addBoolValueInput(
        RENAME_ID, 'Rename existing configurations to match', True, '', True)
    rename.tooltip = ('Bring the rows already in the table into line with the '
                      'naming scheme before the missing ones are added.')

    text, can_run = _summary(result)
    info = inputs.addTextBoxCommandInput(INFO_ID, '', text, 14, True)
    info.isFullWidth = True

    args.command.okButtonText = 'Create'
    args.command.isOKButtonVisible = can_run

    futil.add_handler(args.command.inputChanged, command_input_changed, local_handlers=local_handlers)
    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)


def command_input_changed(args: adsk.core.InputChangedEventArgs):
    """Ticking the rename box re-plans: it changes which names are free."""
    if args.input.id != RENAME_ID:
        return
    command = args.firingEvent.sender
    inputs = command.commandInputs
    box = inputs.itemById(RENAME_ID)
    _design, result = _current_plan(bool(box.value) if box else True)
    text, can_run = _summary(result)
    info = inputs.itemById(INFO_ID)
    if info:
        info.formattedText = text
    try:
        command.isOKButtonVisible = can_run
    except Exception:
        pass


def command_execute(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Execute Event')
    global _pending
    box = args.command.commandInputs.itemById(RENAME_ID)
    _pending = bool(box.value) if box else True
    _arm_event()
    app.fireCustomEvent(RUN_EVENT_ID)


def _create(do_rename=True):
    """Runs after the dialog has closed, where the document settles normally."""
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        return
    result = config_table.plan(design, app.activeDocument.name, do_rename)
    if result.error:
        ui.messageBox(result.error, CMD_NAME)
        return

    lines = []
    problems = []

    renamed = 0
    if do_rename and result.to_rename:
        renamed, rename_problems = config_table.rename(design, result)
        problems.extend(rename_problems)
        lines.append(f'Renamed {renamed} configuration(s).')
        # The table has changed under the plan, so work the rest out again
        # rather than trusting names that were only ever a proposal.
        result = config_table.plan(design, app.activeDocument.name, False)
        if result.error:
            ui.messageBox('\n'.join(lines + ['', result.error]), CMD_NAME)
            return
    elif do_rename:
        lines.append('No configuration needed renaming.')

    made = 0
    if result.to_create:
        made, create_problems = config_table.create(design, result)
        problems.extend(create_problems)
    lines.append(f'Created {made} configuration row(s).')

    table = config_table.top_table(design)
    if table is not None:
        lines.append(f'The table now has {table.rows.count} rows.')
    if result.rename_dupes:
        lines.append('')
        lines.append('Kept their own name — another row wants the same one:')
        lines.extend(f'  {n}' for n in result.rename_dupes[:10])
    if result.unreadable:
        lines.append('')
        lines.append('Could not be read, so left alone:')
        lines.extend(f'  {n}' for n in result.unreadable[:10])
    if problems:
        lines.append('')
        lines.append('Problems:')
        lines.extend(f'  {p}' for p in problems[:10])
        if len(problems) > 10:
            lines.append(f'  … and {len(problems) - 10} more')
        lines.append('')
        lines.append('If the design is still being saved, wait for the save to '
                     'finish and run the command again.')
    lines.append('')
    lines.append('Nothing has been built and nothing has been saved. Save the '
                 'document before opening a new row — a row created since the '
                 'last save cannot be activated.')
    ui.messageBox('\n'.join(lines), CMD_NAME)


def command_destroy(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Destroy Event')
    global local_handlers
    local_handlers = []
