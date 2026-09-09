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

"""Shared helpers for building WoodCraft's toolbar UI.

WoodCraft's commands live in custom tabs inside the Design workspace: the
"WoodCraft" tab for everything that models or reports on cabinets, and the
"Kitchen" tab for the per-job project commands (New Kitchen / Finish Kitchen),
which manage cloud folders rather than geometry.

Commands ask for the panel they belong to via get_panel() on start and tear their
button down via remove_command() on stop. Both default to the WoodCraft tab, so a
command only names a tab when it wants a different one. Tabs and panels are
created lazily by the first command that needs them and pruned once the last
command has removed its button, so no single command "owns" either.
"""

import adsk.core

from .. import config
from . import wc_attrs

app = adsk.core.Application.get()
ui = app.userInterface


def get_panel(panel_id: str, panel_name: str,
              tab_id: str = None, tab_name: str = None) -> adsk.core.ToolbarPanel:
    """Return the named panel inside a WoodCraft tab, creating both if needed.

    tab_id/tab_name default to the main WoodCraft tab; the Kitchen commands pass
    config.KITCHEN_TAB_ID / KITCHEN_TAB_NAME to get their own tab instead."""
    workspace = ui.workspaces.itemById(config.DESIGN_WORKSPACE_ID)

    tab_id = tab_id or config.TAB_ID
    tab_name = tab_name or config.TAB_NAME

    tab = workspace.toolbarTabs.itemById(tab_id)
    if not tab:
        tab = workspace.toolbarTabs.add(tab_id, tab_name)

    panel = tab.toolbarPanels.itemById(panel_id)
    if not panel:
        panel = tab.toolbarPanels.add(panel_id, panel_name)

    return panel


def remove_command(panel_id: str, cmd_id: str, tab_id: str = None):
    """Remove a command button and its definition, then prune empty UI containers.

    tab_id must match the one the command passed to get_panel(); it defaults to
    the main WoodCraft tab."""
    workspace = ui.workspaces.itemById(config.DESIGN_WORKSPACE_ID)

    tab = workspace.toolbarTabs.itemById(tab_id or config.TAB_ID)
    if tab:
        panel = tab.toolbarPanels.itemById(panel_id)
        if panel:
            control = panel.controls.itemById(cmd_id)
            if control:
                control.deleteMe()
            # Drop the panel once it no longer holds any controls.
            if panel.controls.count == 0:
                panel.deleteMe()
        # Drop the whole tab once it holds no panels.
        if tab.toolbarPanels.count == 0:
            tab.deleteMe()

    cmd_def = ui.commandDefinitions.itemById(cmd_id)
    if cmd_def:
        cmd_def.deleteMe()


def tag_as_panel(component) -> bool:
    """Classify a component as a WoodCraft panel (idempotent) so the cut list and
    other output commands can find it regardless of how it was modelled. Returns
    False if it couldn't be written (e.g. a referenced/read-only component).

    Thin wrapper over wc_attrs.set_category so Carcass Maker / Shelf Creator keep a
    one-call auto-classify; richer classification lives in the Set Type command."""
    return wc_attrs.set_category(component, config.WC_CAT_PANEL)
