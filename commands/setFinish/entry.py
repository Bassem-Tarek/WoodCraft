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

"""Set Finish — give a cabinet its finish spec in one shot.

Pick one or more cabinets (their top-level components) and say how each of the two
visible surface groups is finished:

  - **Carcass Type / Door Type** — Painted or Veneer. Written as attributes of
    exactly those names (config.WC_CARCASS_TYPE / WC_DOOR_TYPE) onto the components
    you selected, so a report can ask a cabinet how it is finished without having
    to look at any panel inside it.
  - **Carcass Material / Door Material** — a physical material from that
    dropdown's configured list. The command walks INSIDE each selected cabinet and
    assigns the carcass material to every component whose name is in
    config.WC_CARCASS_PART_NAMES (Left Panel, Shelf, Back Rail…) and the door
    material to every one in config.WC_DOOR_PART_NAMES (Door, Drawer Face, Filler…).
  - **Carcass Appearance / Door Appearance** — an appearance from that dropdown's
    own configured list, applied to the same two groups as a body-level override.

Every dropdown starts on "leave unchanged", and each is independent: set a material
without touching the look, recolour without re-specifying what the panel is made of,
do both at once, or neither and just tag the types.

Why names: the cabinet author already controls them — Carcass Maker and Shelf
Creator name what they build — so no extra tagging step is needed to tell a box
part from a front. Matching is case-insensitive and ignores Fusion's copy and
occurrence suffixes, so "Left Panel", "Left Panel (2)", "Left Panel:1" and
"left panel 2" are one and the same part. Alongside the exact lists there are
KEYWORD phrases (config.WC_*_PART_KEYWORDS): a component whose name merely CONTAINS
one joins that group, which is how every 'Drawer Face …' variant is caught without
enumerating them. Anything matching neither is left untouched: the command never
guesses, so hardware, worktops and your own custom parts keep whatever material you
gave them.

Where the dropdown contents come from: four curated lists in
commands/finish_store.py, one per dropdown, resolved against Fusion's loaded material
libraries (Emaar, Fusion Material, Assets, Favorites …) by commands/material_pool.py.
Separate lists per dropdown is the point — a carcass is MDF or melamine, a door front
is a decor, and the full libraries run to hundreds of entries. Edit the lists with
the **Finish Lists** command.

Material assignment writes BOTH the component's material and every solid body's
material, because that is the pair `panels.panel_material()` reads (a body-level
material wins over the component default) — anything less and the Cut List would
still report the old decor.

**Material and appearance are decoupled.** A physical material carries an
appearance, so assigning one normally drags that appearance along with it — setting
a panel's material to a walnut would repaint it walnut on screen. Set Finish
suppresses that: when no appearance is chosen it notes each body's current
appearance, assigns the material, then pins the old appearance back as an explicit
body override, so the material changes underneath and the viewport does not. When an
appearance IS chosen it becomes the override instead. What the panel is (what the
Cut List costs) and what it looks like stay two separate decisions you make
independently. A body that already carries its own appearance override needs no
protecting — an override outranks the material's appearance either way.
"""

import os

import adsk.core
import adsk.fusion

from .. import ui_helpers
from .. import wc_attrs
from .. import part_names
from .. import finish_store
from .. import material_pool
from ...lib import fusionAddInUtils as futil
from ... import config

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_setFinish'
CMD_NAME = 'Set Finish'
CMD_Description = (
    'Give the selected cabinets their finish spec: a Painted / Veneer type for the '
    'carcass and for the doors (written as "Carcass Type" and "Door Type" '
    'attributes), plus a physical material and an appearance for each, taken from '
    'the configured Finish Lists and applied to the matching panels inside every '
    'selected cabinet. Material and appearance are set independently — leave either one '
    'unchanged and it stays exactly as it was.'
)
IS_PROMOTED = True

# Kitchen, not Cabinet Builder: both act on an assembled run of cabinets — the
# finish spec for a whole kitchen and the lists that feed it — rather than on one
# cabinet being modelled.
PANEL_ID = config.KITCHEN_PANEL_ID
PANEL_NAME = config.KITCHEN_PANEL_NAME

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

SEL_ID = 'sf_selection'
CARCASS_TYPE_ID = 'sf_carcass_type'
DOOR_TYPE_ID = 'sf_door_type'
CARCASS_MAT_ID = 'sf_carcass_material'
DOOR_MAT_ID = 'sf_door_material'
CARCASS_APP_ID = 'sf_carcass_appearance'
DOOR_APP_ID = 'sf_door_appearance'
INFO_ID = 'sf_info'

# First entry in every material/appearance dropdown: leave that property of the
# geometry alone. Lets the command be used purely as a "tag these cabinets" tool,
# to set a material without touching the look, or to recolour without touching the
# material — and keeps it usable at all when a configured list is empty.
KEEP_LABEL = '— leave unchanged —'

# Outcome of applying a finish to one component. NOTHING is deliberately distinct
# from FAILED: an assembly component whose name matches (a 'Door' that is a panel
# plus a handle) owns no bodies of its own, so there is simply nothing to paint on
# it — its children are handled separately. Reporting that as a failure told users
# their model was read-only when it was fine.
APPLIED = 'applied'
FAILED = 'failed'
NOTHING = 'nothing'

local_handlers = []


def start():
    cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_Description, ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)

    panel = ui_helpers.get_panel(PANEL_ID, PANEL_NAME)
    control = panel.controls.addCommand(cmd_def)
    control.isPromoted = IS_PROMOTED


def stop():
    ui_helpers.remove_command(PANEL_ID, CMD_ID)


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------
# Lives in commands/part_names.py, shared with the Skirting command, which needs the
# same "is this component a front?" answer to work out which way a cabinet faces.
CARCASS_NAMES = part_names.CARCASS_NAMES
DOOR_NAMES = part_names.DOOR_NAMES
CARCASS_KEYWORDS = part_names.CARCASS_KEYWORDS
DOOR_KEYWORDS = part_names.DOOR_KEYWORDS

_fold = part_names.fold
_normalize = part_names.normalize
_group_for = part_names.group_for


# ---------------------------------------------------------------------------
# Configured choices
# ---------------------------------------------------------------------------
# The four dropdowns are filled from commands/finish_store.py — one curated list per
# dropdown, resolved against the loaded material libraries by
# commands/material_pool.py. They used to be filled from Fusion's Favorites library,
# which meant one shared shortlist answering four different questions: a carcass is
# MDF or melamine, a door front is a decor, and offering both lists everywhere was
# just noise. Edit the lists with the Finish Lists command.
def _choices(category):
    """([(name, object)], [missing name]) for one dropdown, in configured order."""
    try:
        return material_pool.resolve(category)
    except Exception:
        futil.handle_error(f'Set Finish: resolving {category}')
        return [], []


def _fill_choice_dropdown(dropdown, choices):
    """KEEP_LABEL first, then one item per configured choice. The dropdown index maps
    straight onto `choices[index - 1]`, so nothing is looked up by name a second
    time — two library entries can legitimately share a name and only the position
    tells them apart."""
    dropdown.listItems.add(KEEP_LABEL, True)
    for name, _obj in choices:
        dropdown.listItems.add(name, False)


def _selected_choice(inputs, dropdown_id, choices):
    """The chosen Material/Appearance object, or None for "leave unchanged"."""
    dropdown = inputs.itemById(dropdown_id)
    item = dropdown.selectedItem if dropdown else None
    index = item.index if item else 0
    if index <= 0 or index > len(choices):
        return None
    return choices[index - 1][1]


def _set_body_appearance(design, body, appearance) -> bool:
    """Give `body` an explicit appearance override.

    Direct assignment is the primary path and handles every source: an appearance
    from the Favorites library assigns straight onto a body and Fusion pulls it into
    the document's own appearance collection on the way, keeping its id — so there
    is no addByCopy step to do, and none to get wrong (addByCopy in fact THROWS once
    the document already holds that name, which is exactly the second run of this
    command). The by-name fallback only matters if a future Fusion build refuses a
    library appearance; by then the name is already in the document."""
    if appearance is None:
        return False
    try:
        body.appearance = appearance
        return True
    except Exception:
        pass
    try:
        existing = design.appearances.itemByName(appearance.name)
        if existing:
            body.appearance = existing
            return True
    except Exception:
        pass
    # Defensive: reading .name off a stale entity can itself raise, and a logging
    # line must never be what aborts a run half way through a kitchen.
    try:
        label = appearance.name
    except Exception:
        label = '?'
    futil.log(f'Set Finish: could not apply appearance "{label}"')
    return False


def _same_asset(current, wanted) -> bool:
    """Same material/appearance (by id, else by name)? False when either is None."""
    if current is None or wanted is None:
        return False
    try:
        if current.id and current.id == wanted.id:
            return True
    except Exception:
        pass
    try:
        return current.name == wanted.name
    except Exception:
        return False


def _has_own_appearance(body, appearance) -> bool:
    """True when the body ALREADY carries `appearance` as its own override — the
    write would change nothing. An appearance merely inherited from the material
    doesn't count: the command's promise is an explicit override."""
    try:
        if body.appearanceSourceType != adsk.core.AppearanceSourceTypes.BodyAppearanceSource:
            return False
        return _same_asset(body.appearance, appearance)
    except Exception:
        return False


def _inherits_appearance(body) -> bool:
    """True when the body is showing its MATERIAL's appearance rather than one of
    its own. Only these bodies would visibly change when the material changes; a
    body/occurrence/face override already outranks the material and survives on
    its own, so it must be left strictly alone."""
    try:
        return body.appearanceSourceType == adsk.core.AppearanceSourceTypes.MaterialAppearanceSource
    except Exception:
        return False


def _apply_finish(design, component, material, appearance) -> str:
    """Give one component its material and/or its appearance. Either may be None,
    meaning "leave that property exactly as it is".

    Returns APPLIED (something was written), FAILED (there was work to do and Fusion
    refused it — typically a referenced component from another document, which is
    read-only here) or NOTHING (this component had no surface the request applies to,
    which is normal for an assembly node and must not be reported as an error).

    Material is written to the component AND to each of its solid bodies, because
    `panels.panel_material()` (and Fusion's own display) prefer a body-level
    material over the component default — setting only the component would leave a
    previously body-painted panel reporting its old decor.

    The two properties are kept strictly independent, which takes a little care in
    the one direction Fusion couples them: a material carries its own appearance,
    and a body with no appearance of its own simply shows the material's, so
    assigning a material would silently repaint the panel. Hence:

      - appearance chosen  → set it as an explicit body override once the material
                             is in place; it wins over whatever the material brought.
      - appearance not set → note what each body was showing BEFORE the material
                             lands, then pin that back afterwards, so the material
                             changes underneath and the viewport does not.

    Bodies that already carry their own override need no protecting in the second
    case — an override outranks a material's appearance anyway — so they are left
    strictly alone rather than re-pinned."""
    try:
        bodies = [component.bRepBodies.item(i) for i in range(component.bRepBodies.count)]
    except Exception:
        bodies = []

    # Snapshot before touching anything: once the material is assigned the old
    # appearance is gone and there is nothing left to read back. Only needed when
    # the appearance is meant to survive rather than be replaced.
    keep = [body.appearance if (material is not None and appearance is None
                                and _inherits_appearance(body)) else None
            for body in bodies]

    attempted = False
    failed = False

    if material is not None:
        attempted = True
        wrote = False
        # Writes that would change nothing are skipped: every material/appearance
        # write is a document edit Fusion recomputes and records, so re-running Set
        # Finish on a kitchen that is mostly right used to cost as much as the
        # first run.
        try:
            if _same_asset(component.material, material):
                wrote = True
            else:
                component.material = material
                wrote = True
        except Exception:
            pass
        for body in bodies:
            try:
                if not _same_asset(body.material, material):
                    body.material = material
                wrote = True
            except Exception:
                pass
        failed = failed or not wrote

    if appearance is not None and bodies:
        # No bodies is NOT a failure — see the NOTHING case below.
        attempted = True
        wrote = False
        for body in bodies:
            if _has_own_appearance(body, appearance) or \
                    _set_body_appearance(design, body, appearance):
                wrote = True
        failed = failed or not wrote

    # Appearance preservation (material set, no appearance chosen). Best-effort:
    # _set_body_appearance already logs, and losing the old look is not a reason to
    # report the whole component as failed when its material did land.
    if appearance is None:
        for body, previous in zip(bodies, keep):
            if previous is not None:
                _set_body_appearance(design, body, previous)

    if not attempted:
        # NOTHING to do rather than a failure. The usual case is an ASSEMBLY
        # component whose name matches — a "Door" made of a panel plus a handle owns
        # no bodies itself, so an appearance-only run has no surface to paint here.
        # Its children are matched and handled on their own. Counting this as a
        # failure is what produced the bogus 'could not update "Door"' warning.
        return NOTHING
    return FAILED if failed else APPLIED


# ---------------------------------------------------------------------------
# Traversal
# ---------------------------------------------------------------------------
def _component_of(entity):
    """Resolve a selection (an Occurrence, a body, or a root Component) to its
    owning Component. Mirrors Set Type so both commands accept the same picks."""
    if entity.objectType == adsk.fusion.Occurrence.classType():
        return adsk.fusion.Occurrence.cast(entity).component
    if entity.objectType == adsk.fusion.BRepBody.classType():
        return adsk.fusion.BRepBody.cast(entity).parentComponent
    if entity.objectType == adsk.fusion.Component.classType():
        return adsk.fusion.Component.cast(entity)
    return None


class _ComponentSet:
    """"Have I already visited this component?" — the one question this command has
    to answer correctly, and the one that has bitten it repeatedly.

    Fusion's `==` on two Component objects compares the underlying entity, and it is
    the ONLY identity here that holds up. Everything cheaper is wrong on a real
    kitchen:

      - `id(comp)` (a Python address) — Fusion hands back a fresh wrapper every time
        you touch a component and the old one is garbage collected, so CPython
        recycles addresses and an untouched component inherits the identity of a
        freed one. Skips at random.
      - `entityToken` — not guaranteed to return the same string twice for one
        entity, and slow enough on a Component to stall Fusion outright.
      - `(owning design root name, component name)` — collapses configured library
        cabinets. A kitchen built from a configured library is full of DIFFERENT
        components all called 'Custom 1' or 'Left Panel'; measured on a real kitchen,
        this merged 176 genuinely distinct components down to far fewer and skipped
        whole cabinets.
      - `Component.id` — stable per component but NOT unique: two configured copies
        of one library part share it while being separate components that can hold
        different materials. Measured on the same kitchen, 51 of its id buckets
        mixed distinct components together.

    `==` is O(n) per lookup, so entries are bucketed by component name first and only
    compared within a bucket. A component's name never changes underneath us, so two
    wrappers for one component always land in the same bucket — the bucketing is
    pure speed, never a correctness assumption. Measured at 1469 component visits
    across a 17-cabinet kitchen: 0.01s.

    Holding the component objects also keeps them alive, which removes the wrapper
    churn that made the `id()` version unsafe in the first place."""

    def __init__(self):
        self._buckets = {}

    def add(self, component) -> bool:
        """True if this component had not been seen before (and is now recorded)."""
        try:
            name = component.name
        except Exception:
            name = None                 # unnamed/unreadable all share one bucket
        bucket = self._buckets.setdefault(name, [])
        for other in bucket:
            try:
                if other == component:
                    return False
            except Exception:
                continue                # uncomparable → assume different, never skip
        bucket.append(component)
        return True


def _subtree_components(component, seen: _ComponentSet):
    """Every distinct component at or below `component`, the selected one included.

    Walks `occurrences → childOccurrences` and de-duplicates, so a part used ten
    times in the cabinet is handled once — material and appearance both live on the
    component, so touching it once is complete as well as ten times faster. `seen` is
    shared across selections so a part two selected cabinets genuinely have in common
    isn't counted twice; because it compares by entity rather than by name, two
    same-named parts from two different cabinets stay separate."""
    found = []

    def walk(comp):
        if not seen.add(comp):
            return
        found.append(comp)
        # Fusion forbids circular assemblies, so recursion terminates on its own.
        try:
            occurrences = comp.occurrences
        except Exception:
            return
        for i in range(occurrences.count):
            try:
                walk(occurrences.item(i).component)
            except Exception:
                continue

    walk(component)
    return found


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------
def _parts_blurb(names, keywords) -> str:
    """Human-readable "which components does this hit" line for a tooltip: the exact
    names, plus any keyword phrases spelled out as the substring rules they are."""
    text = ', '.join(names)
    if keywords:
        text += ' — plus anything whose name contains ' + \
                ' or '.join(f'"{k}"' for k in keywords)
    return text


def command_created(args: adsk.core.CommandCreatedEventArgs):
    futil.log(f'{CMD_NAME} Command Created Event')
    inputs = args.command.commandInputs

    # First run writes the shipped lists (and copies the decor names out of your
    # Favorites library verbatim) so the dialog is never empty out of the box.
    try:
        material_pool.seed_appearance_defaults()
    except Exception:
        futil.handle_error('Set Finish: seeding the finish lists')

    # Resolve all four lists once per dialog: Fusion can't repopulate a dropdown
    # mid-command, and each dropdown's index must map onto the list it was built
    # from. Each is now its OWN list — a carcass material is not a door decor.
    carcass_materials, miss_cm = _choices(finish_store.CARCASS_MATERIAL)
    carcass_appearances, miss_ca = _choices(finish_store.CARCASS_APPEARANCE)
    door_materials, miss_dm = _choices(finish_store.DOOR_MATERIAL)
    door_appearances, miss_da = _choices(finish_store.DOOR_APPEARANCE)

    sel = inputs.addSelectionInput(
        SEL_ID, 'Cabinets', 'Select the cabinet component(s) to finish')
    sel.addSelectionFilter('Occurrences')
    sel.addSelectionFilter('SolidBodies')
    # Lets a PART document's root component be picked from the browser (a part file
    # has no occurrences to select). Guarded: an unknown filter name would throw,
    # and command_created swallows exceptions — killing the whole dialog.
    try:
        sel.addSelectionFilter('RootComponents')
    except Exception:
        futil.log('Set Finish: RootComponents selection filter unavailable')
    sel.setSelectionLimits(1, 0)
    sel.tooltip = ('Pick the cabinet, not its individual panels — the command looks '
                   'inside each selection for the parts it should re-material.')

    carcass_type = inputs.addRadioButtonGroupCommandInput(CARCASS_TYPE_ID, 'Carcass Type')
    for i, finish in enumerate(config.WC_FINISH_TYPES):
        carcass_type.listItems.add(finish, i == 0)

    carcass_parts = _parts_blurb(config.WC_CARCASS_PART_NAMES,
                                 config.WC_CARCASS_PART_KEYWORDS)
    door_parts = _parts_blurb(config.WC_DOOR_PART_NAMES,
                              config.WC_DOOR_PART_KEYWORDS)

    carcass_mat = inputs.addDropDownCommandInput(
        CARCASS_MAT_ID, 'Carcass Material', adsk.core.DropDownStyles.TextListDropDownStyle)
    _fill_choice_dropdown(carcass_mat, carcass_materials)
    carcass_mat.tooltip = ('Physical material for the box parts inside each selected '
                           'cabinet: ' + carcass_parts + '.')

    carcass_app = inputs.addDropDownCommandInput(
        CARCASS_APP_ID, 'Carcass Appearance', adsk.core.DropDownStyles.TextListDropDownStyle)
    _fill_choice_dropdown(carcass_app, carcass_appearances)
    carcass_app.tooltip = ('How those same box parts LOOK. Independent of the '
                           'material: leave this unchanged and the panels keep their '
                           'current appearance no matter which material you pick.')

    door_type = inputs.addRadioButtonGroupCommandInput(DOOR_TYPE_ID, 'Door Type')
    for i, finish in enumerate(config.WC_FINISH_TYPES):
        door_type.listItems.add(finish, i == 0)

    door_mat = inputs.addDropDownCommandInput(
        DOOR_MAT_ID, 'Door Material', adsk.core.DropDownStyles.TextListDropDownStyle)
    _fill_choice_dropdown(door_mat, door_materials)
    door_mat.tooltip = ('Physical material for the front parts inside each selected '
                        'cabinet: ' + door_parts + '.')

    door_app = inputs.addDropDownCommandInput(
        DOOR_APP_ID, 'Door Appearance', adsk.core.DropDownStyles.TextListDropDownStyle)
    _fill_choice_dropdown(door_app, door_appearances)
    door_app.tooltip = ('How those same front parts LOOK. Independent of the '
                        'material — pick one to recolour without re-specifying what '
                        'the panel is made of.')

    # Tell the user when a dropdown is empty or when a configured name no longer
    # resolves. A name silently vanishing from a dropdown is indistinguishable from
    # the command being broken, so say which one and where to fix it.
    notes = []
    empty = [finish_store.CATEGORY_LABELS[key]
             for key, items in ((finish_store.CARCASS_MATERIAL, carcass_materials),
                                (finish_store.CARCASS_APPEARANCE, carcass_appearances),
                                (finish_store.DOOR_MATERIAL, door_materials),
                                (finish_store.DOOR_APPEARANCE, door_appearances))
             if not items]
    if empty:
        notes.append('No entries configured for: ' + ', '.join(empty) + '.')
    absent = sorted({n for group in (miss_cm, miss_ca, miss_dm, miss_da) for n in group})
    if absent:
        notes.append('Not found in any loaded material library: '
                     + ', '.join(f'"{n}"' for n in absent) + '.')
    if notes:
        notes.append('Edit the lists with the Finish Lists command.')
        info = inputs.addTextBoxCommandInput(INFO_ID, '', ' '.join(notes), 3, True)
        info.isFullWidth = True

    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.validateInputs, command_validate_input, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)


def command_validate_input(args: adsk.core.ValidateInputsEventArgs):
    sel = args.inputs.itemById(SEL_ID)
    args.areInputsValid = bool(sel and sel.selectionCount > 0)


def _selected_finish(inputs, input_id):
    """The Painted / Veneer value of a radio group (index 0 if somehow unset)."""
    group = inputs.itemById(input_id)
    item = group.selectedItem if group else None
    index = item.index if item else 0
    return config.WC_FINISH_TYPES[index if 0 <= index < len(config.WC_FINISH_TYPES) else 0]


def command_execute(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Execute Event')
    inputs = args.command.commandInputs
    sel: adsk.core.SelectionCommandInput = inputs.itemById(SEL_ID)

    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        ui.messageBox('Set Finish needs an open design.')
        return

    # Rebuild the same four lists the dialog was filled from. The store hasn't
    # changed while the dialog was open (the editor is a separate command), so index
    # N here is the same entry the user picked.
    carcass_materials, _ = _choices(finish_store.CARCASS_MATERIAL)
    carcass_appearances, _ = _choices(finish_store.CARCASS_APPEARANCE)
    door_materials, _ = _choices(finish_store.DOOR_MATERIAL)
    door_appearances, _ = _choices(finish_store.DOOR_APPEARANCE)

    carcass_finish = _selected_finish(inputs, CARCASS_TYPE_ID)
    door_finish = _selected_finish(inputs, DOOR_TYPE_ID)
    carcass_material = _selected_choice(inputs, CARCASS_MAT_ID, carcass_materials)
    door_material = _selected_choice(inputs, DOOR_MAT_ID, door_materials)
    carcass_appearance = _selected_choice(inputs, CARCASS_APP_ID, carcass_appearances)
    door_appearance = _selected_choice(inputs, DOOR_APP_ID, door_appearances)

    # Snapshot ALL selected components BEFORE writing anything. Writing a component
    # attribute mutates the document, which invalidates the SelectionCommandInput's
    # live list mid-loop — Fusion then throws "invalid argument index" on the next
    # sel.selection(i) and only the first selection gets processed. (Same trap Set
    # Type documents.)
    cabinets = []
    for i in range(sel.selectionCount):
        comp = _component_of(sel.selection(i).entity)
        if comp:
            cabinets.append(comp)

    # Copy the chosen materials into the document once, up front, so the per-part
    # loop is a plain assignment and the copy can't happen mid-traversal.
    carcass_material = material_pool.material_in_design(design, carcass_material) if carcass_material else None
    door_material = material_pool.material_in_design(design, door_material) if door_material else None

    tagged = 0
    tag_failed = 0
    for comp in cabinets:
        ok_carcass = wc_attrs.set_carcass_type(comp, carcass_finish)
        ok_door = wc_attrs.set_door_type(comp, door_finish)
        if ok_carcass and ok_door:
            tagged += 1
        else:
            tag_failed += 1
            futil.log(f'Set Finish: could not tag "{getattr(comp, "name", "?")}" '
                      f'(referenced/read-only?)')

    # What each group is getting. A group with neither a material nor an appearance
    # is skipped entirely, so choosing only a door colour never walks the carcass.
    wanted = {
        'carcass': (carcass_material, carcass_appearance),
        'door': (door_material, door_appearance),
    }

    done = {'carcass': 0, 'door': 0}
    apply_failed = 0
    if any(m or a for m, a in wanted.values()):
        # Walk EVERY selected cabinet first, collect the work, and only then write.
        # Traversal reads live Fusion collections; applying mutates the document.
        # Separating the two means a write can never disturb a walk still in
        # progress over a later cabinet — the same discipline as the selection
        # snapshot above, and cheap insurance on the one code path that has already
        # eaten a multi-select bug.
        seen = _ComponentSet()
        targets = []
        for comp in cabinets:
            for part in _subtree_components(comp, seen):
                group = _group_for(part)
                material, appearance = wanted.get(group, (None, None))
                if material is not None or appearance is not None:
                    targets.append((part, group, material, appearance))
        futil.log(f'Set Finish: {len(cabinets)} cabinet(s) selected, '
                  f'{len(targets)} matching component(s) to update')

        for part, group, material, appearance in targets:
            outcome = _apply_finish(design, part, material, appearance)
            if outcome == APPLIED:
                done[group] += 1
            elif outcome == FAILED:
                apply_failed += 1
                futil.log(f'Set Finish: could not update '
                          f'"{getattr(part, "name", "?")}" (referenced/read-only?)')
            # NOTHING: no bodies to act on (an assembly node) — silently fine.

    ui.messageBox(_summary(
        len(cabinets), tagged, tag_failed, carcass_finish, door_finish,
        wanted, done, apply_failed))


def _summary(picked, tagged, tag_failed, carcass_finish, door_finish,
             wanted, done, apply_failed):
    """The one message the user reads after Apply — what was tagged, what each group
    received, and (specifically) why nothing happened when nothing did."""
    lines = [f'{tagged} of {picked} cabinet(s) tagged: '
             f'Carcass Type = {carcass_finish}, Door Type = {door_finish}.']

    for group, label in (('carcass', 'Carcass'), ('door', 'Door')):
        material, appearance = wanted[group]
        if not material and not appearance:
            continue
        applied = ' + '.join(
            part for part in (f'material "{material.name}"' if material else '',
                              f'appearance "{appearance.name}"' if appearance else '')
            if part)
        lines.append(f'{label}: {applied} → {done[group]} component(s).')

    any_wanted = any(m or a for m, a in wanted.values())
    any_done = done['carcass'] + done['door'] > 0
    if not any_wanted:
        lines.append('No material or appearance chosen — types only, geometry left '
                     'as it was.')
    elif not any_done:
        lines.append('No component inside the selection matched a known part name, '
                     'so nothing was changed. Set Finish matches by component name '
                     '(Left Panel, Shelf, Door, Drawer Face…) — check the names in '
                     'the browser, or select the cabinet rather than a single panel.')
    elif all(a is None for _m, a in wanted.values()):
        # Material without appearance: nothing moves in the viewport, which on its
        # own reads as "the command did nothing".
        lines.append('Appearances were left as they were — this set the physical '
                     'material only, so the model looks unchanged on screen.')

    if tag_failed:
        lines.append(f'{tag_failed} cabinet(s) could not be tagged — likely '
                     f'referenced/read-only. Open the source design to tag those.')
    if apply_failed:
        lines.append(f'{apply_failed} component(s) could not be updated — likely '
                     f'referenced/read-only.')
    return '\n'.join(lines)


def command_destroy(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Destroy Event')
    global local_handlers
    local_handlers = []
