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

"""Repair Line Boring — rebuild old / hand-drawn shelf-pin boring the current way.

One click on the open cabinet: finds every shelf-pin boring, rebuilds the sketch
its holes sit on the way Line Boring builds it today (fully defined, follows
size and configuration changes) and keeps the Hole / Pattern features, so shelf
mounts and other joints to the holes stay attached. It lists what it found and
asks before changing anything; hand-placed sets that don't match the tool's
spacing are only moved after a second confirmation. The logic lives in
commands/repair_boring.py.
"""

import os

import adsk.core

from .. import ui_helpers
from .. import repair_boring
from ...lib import fusionAddInUtils as futil
from ... import config

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_repairLineBoring'
CMD_NAME = 'Repair Line Boring'
CMD_Description = (
    'Update the shelf-pin holes in this cabinet to the current Line Boring method '
    'so they follow size and configuration changes. The holes stay, so anything '
    'attached to them stays attached.'
)
IS_PROMOTED = False

PANEL_ID = config.CABINET_PANEL_ID
PANEL_NAME = config.CABINET_PANEL_NAME

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

local_handlers = []


def start():
    cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_Description, ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)

    panel = ui_helpers.get_panel(PANEL_ID, PANEL_NAME)
    # Sit right after Line Boring in the panel.
    control = panel.controls.addCommand(cmd_def, f'{config.COMPANY_NAME}_lineBoring', False)
    control.isPromoted = IS_PROMOTED


def stop():
    ui_helpers.remove_command(PANEL_ID, CMD_ID)


def command_created(args: adsk.core.CommandCreatedEventArgs):
    futil.log(f'{CMD_NAME} Command Created Event')
    # No dialog: the command runs straight away. Running inside a command makes
    # the whole repair a single undo step.
    args.command.isAutoExecute = True
    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)


def command_execute(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Execute Event')
    try:
        repair_boring.repair_active_design(confirm=True, log=futil.log)
    except Exception:
        futil.handle_error(CMD_NAME)


def command_destroy(args: adsk.core.CommandEventArgs):
    global local_handlers
    local_handlers = []
