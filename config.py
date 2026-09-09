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

# Application Global Variables
# This module serves as a way to share variables across different
# modules (global variables).

import os

# Flag that indicates to run in Debug mode or not. When running in Debug mode
# more information is written to the Text Command window. Generally, it's useful
# to set this to True while developing an add-in and set it to False when you
# are ready to distribute it.
DEBUG = True

# Gets the name of the add-in from the name of the folder the py file is in.
# This is used when defining unique internal names for various UI elements 
# that need a unique name. It's also recommended to use a company name as 
# part of the ID to better ensure the ID is unique.
ADDIN_NAME = os.path.basename(os.path.dirname(__file__))
COMPANY_NAME = 'WoodCraft'

# ---------------------------------------------------------------------------
# UI layout
# ---------------------------------------------------------------------------
# All WoodCraft commands live in a dedicated tab inside the Design workspace.
# The tab is created on demand by the first command that starts (see
# commands/ui_helpers.py) and removed when the last command stops.
DESIGN_WORKSPACE_ID = 'FusionSolidEnvironment'

TAB_ID = f'{COMPANY_NAME}_tab'
TAB_NAME = 'WoodCraft'

# Toolbar panels (groups of related commands inside the tab). Commands are split
# across panels so each step reads as a distinct group (the gap between panels
# acts as the separator):
#   - Cabinet Builder : modelling commands (Carcass Maker, Trim, Edit Thickness,
#                       Shelf Creator, Set Type).
#   - Hardware        : Insert Hardware + Sculpt (place parts, cut their holes).
#   - Output / Dev    : reports and debug tools (see below).
CABINET_PANEL_ID = f'{COMPANY_NAME}_cabinet_panel'
CABINET_PANEL_NAME = 'Cabinet Builder'

HARDWARE_PANEL_ID = f'{COMPANY_NAME}_hardware_panel'
HARDWARE_PANEL_NAME = 'Hardware'

# Output / production reports (Sheets stock library + Cut List & Nest today;
# BOM, labels later).
OUTPUT_PANEL_ID = f'{COMPANY_NAME}_output_panel'
OUTPUT_PANEL_NAME = 'Output'

# Kitchen — whole-kitchen commands that act on an ASSEMBLED run of cabinets
# rather than on one cabinet: the worktop that spans them, the per-cabinet finish
# spec, and the kitchen schedule that falls out of both. Kept in their own panel
# because they are the last step of the workflow, after the boxes exist.
KITCHEN_PANEL_ID = f'{COMPANY_NAME}_kitchen_panel'
KITCHEN_PANEL_NAME = 'Kitchen'

# Developer / debug tools — kept in their own segment so they're easy to find
# and easy to strip out before release.
DEV_PANEL_ID = f'{COMPANY_NAME}_dev_panel'
DEV_PANEL_NAME = 'Dev'

# ---------------------------------------------------------------------------
# The Kitchen tab (job / project management)
# ---------------------------------------------------------------------------
# A SECOND tab, separate from the WoodCraft one, because its commands do not
# touch geometry at all: they create and clear cloud folders for a customer's
# job. Keeping them out of the modelling tab means a designer starting a kitchen
# never scrolls past twenty modelling buttons to find the one they want, and the
# tab can be handed to people who never model a cabinet themselves.
#
# NOTE the KITCHEN_PANEL_* constants above are a different thing: a panel of
# whole-kitchen MODELLING commands (Countertop, Skirting, Set Finish) that lives
# inside the WoodCraft tab. This is the tab; that is a panel.
KITCHEN_TAB_ID = f'{COMPANY_NAME}_kitchen_tab'
KITCHEN_TAB_NAME = 'Kitchen'

KITCHEN_PROJECT_PANEL_ID = f'{COMPANY_NAME}_kitchen_project_panel'
KITCHEN_PROJECT_PANEL_NAME = 'Kitchen Project'

# ---------------------------------------------------------------------------
# Kitchen jobs — where customer kitchens and the cabinet library live
# ---------------------------------------------------------------------------
# The shape on the cloud:
#
#   <KITCHENS_PROJECT_NAME>            project, e.g. "Clients"
#   └── <KITCHENS_FOLDER_NAME>/        e.g. "Kitchens"
#       ├── Kitchen Template 1/        made by Create Kitchen Template
#       │   ├── Kitchen Template 1     hybrid design
#       │   └── <KITCHEN_LIBRARY_FOLDER_NAME>/   copy of the master library
#       └── Al Rashid_Kitchen_2026-09-09/   a template New Kitchen has renamed
#           ├── Al Rashid_Kitchen_2026-09-09
#           └── <KITCHEN_LIBRARY_FOLDER_NAME>/
#
# Create Kitchen Template does the slow work (folders + copying the library) up
# front, so templates can be stocked in a quiet moment. New Kitchen then just
# RENAMES a waiting template folder and its design — instant, no copying — and
# Finish Kitchen later deletes whatever of the library copy went unused.
#
# Every name below is the EXACT display name as it appears in the Data Panel. If
# something is renamed on the cloud, change it here: both dialogs show a red
# cross beside anything they can't find, and list the projects they can see.
KITCHENS_PROJECT_NAME = 'Clients'

# The folder inside that project which holds the kitchen/template folders. Set
# to '' to put them straight in the project root. Created if it doesn't exist.
KITCHENS_FOLDER_NAME = 'Kitchens'

# The master library Create Kitchen Template copies FROM: a folder inside a
# project. Set LIBRARY_SOURCE_FOLDER to '' to copy the whole project root.
LIBRARY_PROJECT_NAME = 'Emaar Library'
LIBRARY_SOURCE_FOLDER = 'Kitchen Library'

# What the per-kitchen copy of the library is called inside the job folder.
KITCHEN_LIBRARY_FOLDER_NAME = 'Library'

# Templates are named "<prefix> <n>" and New Kitchen offers every folder matching
# that shape. Changing the prefix orphans templates already on the cloud (they
# simply stop being offered), so change it before stocking any.
KITCHEN_TEMPLATE_PREFIX = 'Kitchen Template'

# What New Kitchen renames a template to. {customer} is what the designer typed,
# {date} is today in KITCHEN_DATE_FORMAT; the literal "Kitchen" in the middle says
# what kind of job this is, so a Clients folder can hold other project types later
# without the names becoming ambiguous.
#
# A dangling separator is trimmed automatically, so an empty KITCHEN_DATE_FORMAT
# gives "Al Rashid_Kitchen" rather than "Al Rashid_Kitchen_".
KITCHEN_NAME_PATTERN = '{customer}_Kitchen_{date}'

# The date in that pattern, in strftime form. The default sorts chronologically in
# the Data Panel because the year leads; '%d-%m-%y' or '%d %b %y' read more
# naturally if sorting doesn't matter. Set to '' to drop the date entirely.
KITCHEN_DATE_FORMAT = '%Y-%m-%d'

# Safety rails for the folder walks. A library is a handful of levels deep in
# practice; the cap stops a cyclic or pathological tree from hanging Fusion.
# MAX_LISTED only limits how many filenames the Finish Kitchen dialog PRINTS —
# it never limits how many are deleted.
KITCHEN_MAX_DEPTH = 12
KITCHEN_MAX_LISTED = 200

# ---------------------------------------------------------------------------
# Hardware library
# ---------------------------------------------------------------------------
# The Insert Hardware command reads its catalogue from a dedicated Fusion cloud
# project whose top-level folders are the hardware categories (e.g. "Hinges",
# "Connectors", "Dowels") and whose files are the individual hardware parts.
# Set this to the EXACT display name of that project. If the command can't find
# it, the dialog lists the projects it CAN see so you can copy the right name.
HARDWARE_PROJECT_NAME = 'WoodCraft Hardware'

# ---------------------------------------------------------------------------
# Component classification (attribute schema)
# ---------------------------------------------------------------------------
# Every WoodCraft-relevant component is stamped with invisible custom attributes,
# all under ONE group (WC_GROUP) as plain name/value pairs. Output commands
# (Cut List, BOM, labels) collect and split components by these. The scheme is a
# deliberately flat, extensible key/value store: a new command adds a new key
# constant here and reads/writes it through commands/wc_attrs.py — no schema churn,
# no migration. Attributes live on the component, are saved inside the .f3d, and
# survive across referenced cabinets and configuration changes.
WC_GROUP = 'WoodCraft'

# Category — what KIND of item the component is. Decides which BOM section it lands
# in and whether the cut list / nesting includes it. Carcass Maker and Shelf
# Creator stamp 'panel' automatically; Set Type classifies anything by hand.
WC_CATEGORY = 'category'
WC_CAT_PANEL = 'panel'          # sheet-good panel → cut list, nesting, BOM panels
WC_CAT_HARDWARE = 'hardware'    # purchased item   → BOM purchased-items only
WC_CAT_COUNTERTOP = 'countertop'  # worktop slab   → BOM + banding, NOT nested
WC_CATEGORIES = (WC_CAT_PANEL, WC_CAT_HARDWARE, WC_CAT_COUNTERTOP)

# Categories that are measured, priced and edgebanded like a sheet good. NESTING
# is what separates them: a panel is cut FROM a stock sheet, so it belongs in the
# cut list and on a nesting diagram; a worktop is bought as a slab or a cut length
# and only needs to appear on the bill. Anything that should be costed by area but
# never nested joins this tuple rather than becoming a second kind of 'panel'.
WC_SHEET_LIKE = (WC_CAT_PANEL, WC_CAT_COUNTERTOP)

# Hardware unit cost (string-encoded float; currency is the user's own). Read back
# by the BOM and Cut List. Only meaningful on hardware components. Panels never
# store a cost — theirs is derived at report time from the Sheets library (avg
# sheet cost/m² × raw area, plus the Settings waste factor).
WC_COST = 'cost'

# How a hardware ASSEMBLY is purchased (hardware like a Minifix or a hinge is
# correctly modelled as an assembly, but can be bought either way):
#   'pack'     — one purchased unit: ITS cost applies and covers everything inside,
#                so reports never also bill its children. Default (a lone screw is
#                trivially its own pack).
#   'separate' — translucent: its parts are bought individually, so reports look
#                THROUGH it and sum the children's costs; a stored pack cost is
#                kept on the component but ignored (switching modes loses nothing).
WC_PURCHASE = 'purchase'
WC_PURCHASE_PACK = 'pack'
WC_PURCHASE_SEPARATE = 'separate'

# Edgeband — stamped on a panel's edge FACES (BRepFace), not the component: the
# value is the NAME of an edgeband from the Sheets library's band catalogue. The
# Edgeband command writes it; the BOM joins name → catalogue row at report time
# to price the summed edge lengths (cost per metre lives in the library, never
# on the face — same philosophy as panel costs being derived, never stored).
WC_EDGEBAND = 'edgeband'

# Finish spec (Set Finish command) — how a cabinet's two visible surface groups are
# finished. Stamped on the CABINET component the user selects, not on every panel,
# because the spec is a property of the cabinet as a whole: "this box is painted,
# its doors are veneer". The attribute NAMES are the human-readable labels shown in
# the dialog, so they read the same in Fusion's own attribute inspector as they do
# in the WoodCraft UI and in any exported report.
WC_CARCASS_TYPE = 'Carcass Type'
WC_DOOR_TYPE = 'Door Type'

# The two finishes a carcass or a door can have. Kept as a tuple so the dialog's
# radio buttons and any future validation both read the same list — adding a third
# finish means adding it here and nowhere else.
WC_FINISH_PAINTED = 'Painted'
WC_FINISH_VENEER = 'Veneer'
WC_FINISH_TYPES = (WC_FINISH_PAINTED, WC_FINISH_VENEER)

# ---------------------------------------------------------------------------
# Finish part-name maps (Set Finish command)
# ---------------------------------------------------------------------------
# Set Finish assigns ONE physical material to the box and ANOTHER to the fronts.
# Which group a component belongs to is decided by its NAME, because that is what
# the cabinet author already controls: Carcass Maker and Shelf Creator name what
# they build, and a hand-modelled cabinet only has to use the same words. Matching
# is case-insensitive and ignores Fusion's copy/occurrence suffixes ("Left Panel",
# "Left Panel (2)", "Left Panel:1" and "left panel 2" all match) — see
# commands/setFinish/entry.py.
#
# A name in NEITHER list is left alone: Set Finish never guesses, so hardware,
# worktops and anything you named yourself keep the material you gave them.
WC_CARCASS_PART_NAMES = (
    'Left Panel',
    'Right Panel',
    'Bottom Panel',
    'Back Panel',
    'Rail',
    'Front Rail',
    'Back Rail',
    'Bottom Back Rail',
    'Top Back Rail',
    'Top Panel',
    'Shelf',
    'Fixed Shelf',
    'Oven Shelf',
    'Oven Support',
)

WC_DOOR_PART_NAMES = (
    'Front Panel',
    'Door Panel',
    'Door',
    'Left Door',
    'Right Door',
    'Top Door',
    'Bottom Door',
    'Fixed Panel',
    'Drawer',
    'Drawer Face',
    'Drawer Face Top',
    'Drawer Face Middle',
    'Drawer Face Bottom',
    'Top Drawer',
    'Bottom Drawer',
    'Filler',
)

# ---------------------------------------------------------------------------
# Finish part-name KEYWORDS (Set Finish command)
# ---------------------------------------------------------------------------
# The tuples above match a component's whole name. These match a FRAGMENT of it: a
# component whose name CONTAINS one of these phrases joins that group no matter what
# else is in the name, so 'Drawer Face', 'Drawer Face Top', 'Middle Drawer Face' and
# 'Drawer Face - Oak' are all fronts without anyone maintaining a list of every
# variant a cabinet author might type.
#
# Order of resolution (see commands/setFinish/entry.py): an exact name in either list
# above wins first, then carcass keywords, then door keywords. A keyword can
# therefore never steal a component that a listed exact name already claims.
#
# Keep these phrases SPECIFIC. A keyword is a substring test, so a short or generic
# fragment ('panel', 'door') would swallow half the cabinet — that is what the exact
# lists are for. Empty strings are ignored rather than matching everything.
WC_CARCASS_PART_KEYWORDS = ()

WC_DOOR_PART_KEYWORDS = (
    'Drawer Face',
)
# Appearance role — HOW a part is painted when a configuration row is applied by
# the Appearance Config command. Orthogonal to WC_CATEGORY on purpose: a door IS
# a panel (nested, costed, edgebanded like any panel); the role only decides
# whether it takes the row's door finish or the carcass appearance. Absent role =
# classify by name keywords (the Appearance Config profile). Set by the Appearance
# Config palette when the user persists an override.
WC_ROLE = 'role'
WC_ROLE_DOOR = 'door'        # takes the configuration row's door finish
WC_ROLE_FRONT = 'front'      # fixed/front panel: door finish ONLY when its cabinet
                             # has exactly one door (L-corner rule), else carcass
WC_ROLE_CARCASS = 'carcass'  # always the carcass appearance
WC_ROLE_SKIP = 'skip'        # never gets an appearance column (hardware, trim…)
WC_ROLES = (WC_ROLE_DOOR, WC_ROLE_FRONT, WC_ROLE_CARCASS, WC_ROLE_SKIP)

# Appearance column map — stamped on the ROOT component by Appearance Config:
# a JSON list of occurrence paths, one per appearance-table column (in column
# order, root column excluded). Column titles are just leaf names ('Panel:1')
# and repeat across cabinets, so this sidecar is the only exact record of which
# occurrence each column belongs to; the fix pass needs it to grow/verify a
# table without rebuilding it.
WC_APPEARANCE_COLS = 'appearanceColumns'

# Kitchen job stamp — written on the DOCUMENT (not a component) by New Kitchen so
# Finish Kitchen knows which cloud folder holds this kitchen's private library
# copy, without having to guess from names. Document attributes live inside the
# .f3d and survive renaming or moving both the design and the folder; the folder
# NAME is only a fallback for kitchens someone assembled by hand.
WC_KITCHEN_CUSTOMER = 'kitchenCustomer'
WC_KITCHEN_CREATED = 'kitchenCreated'
WC_KITCHEN_LIB_FOLDER = 'kitchenLibraryFolderId'

# Future keys plug in here with no core change, e.g.:
#   WC_PART_NO = 'partNumber'