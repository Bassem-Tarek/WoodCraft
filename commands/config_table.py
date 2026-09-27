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

"""Reading, naming and filling in a configured design's configuration table.

Shared by Create Configurations, and by Fit Handles, which needs the same naming
rules when it adds a row of its own.

Create Configurations does three things, all driven from here, in this order:

    plan                   read the table and work out everything below without
                           changing anything — the dialog shows it as a dry run.
    delete_duplicates      remove rows carrying the same combination of theme
                           values as another row, keeping one of each.
    rename                 bring the rows left in the table into line with the
                           naming scheme below, so hand-made rows and generated
                           ones read the same.
    create                 add a row for every combination still missing.

None of them builds geometry. Fusion builds a configuration when it is activated, and
a row cannot be built until the document has been saved anyway, so the geometry
is left to Fusion and to whoever opens the row.
"""

import itertools
import re

import adsk.core
import adsk.fusion


# Themes to vary. Empty means every theme the design has, bar the exclusions —
# which is what lets these commands work on a cabinet they have never seen.
VARY = ()

# Themes never varied and never named. Partition is decided by configuration
# rules, and writing it through the API does not make those rules fire, so a row
# inherits whatever its source row had. Matched loosely, like everything below.
EXCLUDE = ('Partition',)


# ---------------------------------------------------------------------------
# Names, matched loosely
# ---------------------------------------------------------------------------
# Every name in a configured design is typed by hand, and hand-typed names
# drift: "Handles", "handles", "Handle Type"; "Gola C Profile" in one library
# and "Gola" in the next. So nothing here is compared as an exact string. A name
# is reduced to its letters and digits, lower-cased, and the alias lists below
# are tried in order until one answers. A library that spells it its own way
# still resolves instead of the command reporting the theme as missing.
#
# The lists are in PREFERENCE order: the first entry is what a new library
# should call the thing, and what the user is shown before any library has been
# read; the rest are what is accepted when the first one is not there.

# The theme column that says whether a cabinet is machined for a Gola profile.
HANDLES_TITLES = ('Handles', 'Handle', 'Handle Type', 'Handle Style',
                  'Handles Theme', 'Handle Theme', 'Handles Type', 'Handling')

# Its Gola value. Any value carrying the word "gola" counts as one however it is
# spelled, so this list only decides which to prefer when a theme offers several.
GOLA_VALUES = ('Gola', 'Gola C Profile', 'Gola C', 'C Gola', 'Gola Profile',
               'Gola C-Profile', 'Gola Channel', 'Gola Rail', 'Gola Handle')

# Its hardware value — the state a cabinet must be in before a handle is fitted.
HANDLE_VALUES = ('Handles', 'Handle', 'Other Handles', 'Other Handle',
                 'Normal Handles', 'Standard Handles', 'Hardware Handles',
                 'Hardware', 'Handle Hardware', 'No Gola', 'None')


def normalized(text):
    """A name reduced to its letters and digits, lower-cased.

    Case, spaces, hyphens and underscores all stop mattering, which is most of
    how these names differ from one library to the next."""
    return re.sub(r'[^a-z0-9]+', '', str('' if text is None else text).lower())


def same_name(one, other):
    """Names differing only in case, spacing or punctuation. '' matches nothing."""
    key = normalized(one)
    return bool(key) and key == normalized(other)


def pick_name(names, aliases, loose=True):
    """The entry of `names` that best answers to one of `aliases`, or None.

    Returned AS THE TABLE SPELLS IT — what gets written back has to be the
    library's own name for the thing, never ours.

    Matched in passes rather than alias by alias, so preference order actually
    decides: every alias gets its exact chance before any gets a
    case-insensitive one, and only when no alias matched either way is a partial
    name accepted. That is what lets "Gola" find "Gola C Profile" without
    letting it beat a column literally called "Gola". Pass loose=False where a
    wrong guess would be worse than no match at all."""
    names = [name for name in names if name is not None]
    for alias in aliases:
        for name in names:
            if name == alias:
                return name
    for alias in aliases:
        for name in names:
            if same_name(name, alias):
                return name
    if not loose:
        return None
    for alias in aliases:
        key = normalized(alias)
        if len(key) < 4:        # too short to contain-match without false hits
            continue
        for name in names:
            if key in normalized(name):
                return name
    return None


def is_gola(value):
    """Is this theme value a Gola profile rather than a piece of hardware?

    The word decides, not the spelling: "Gola", "Gola C Profile", "GOLA-C" and
    "C Gola" are all one."""
    return 'gola' in normalized(value)


def is_excluded(title):
    """Is this theme one whose value is inherited rather than chosen?"""
    return any(same_name(title, name) for name in EXCLUDE)


def is_handles_theme(title):
    """Is this theme column the one carrying the Gola / handles choice?"""
    return any(same_name(title, name) for name in HANDLES_TITLES)

# Row naming. None takes the document's own name as the prefix.
PREFIX = None
PART_NUMBER_FROM_NAME = False



# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------
def digits(value):
    found = re.findall(r'\d+', str(value))
    return found[0] if found else re.sub(r'[^A-Za-z0-9]+', '', str(value))


def name_part(title, value):
    """One piece of a row name. '' leaves the theme out of the name entirely.

    Titles are recognised loosely, so a library whose column reads "handle type"
    is named by the same rules as one that reads "Handles" rather than falling
    through to the generic branch and spelling the value out in row names."""
    if same_name(title, 'Width'):
        return digits(value)
    if same_name(title, 'Legs Height'):
        return 'L' + digits(value)
    if same_name(title, 'Countertop Height'):
        return 'C' + digits(value)
    if is_handles_theme(title):
        return 'Gola' if is_gola(value) else ''
    return re.sub(r'[^A-Za-z0-9]+', '', str(value))


def row_name(prefix, combination, order):
    """The name for a generated row.

    Excluded themes are left out: their value is inherited rather than chosen, so
    naming by it would claim a decision that was never made."""
    parts = [prefix] if prefix else []
    for title in order:
        if is_excluded(title):
            continue
        piece = name_part(title, combination.get(title, ''))
        if piece:
            parts.append(piece)
    return '_'.join(parts)


# ---------------------------------------------------------------------------
# Reading a table
# ---------------------------------------------------------------------------
def top_table(design):
    """The configuration table, or None if this is not a configured design.

    Only the design as the ACTIVE document answers: reached through a DataFile or
    a placement, every one of these calls returns "not yet implemented"."""
    try:
        return design.configurationTopTable
    except Exception:
        return None


def theme_columns(table):
    """{title: column} for the theme columns, in table order."""
    out = {}
    for i in range(table.columns.count):
        column = table.columns.item(i)
        if 'ThemeColumn' in column.objectType:
            out[column.title] = column
    return out


def property_columns(table):
    out = {}
    for i in range(table.columns.count):
        column = table.columns.item(i)
        if 'PropertyColumn' in column.objectType:
            out[column.title] = column
    return out


def theme_values(column):
    """The value names a theme column can take, in table order."""
    referenced = column.referencedTable
    return [referenced.rows.item(i).name for i in range(referenced.rows.count)]


def combination_of(columns, row):
    """{theme title: value name} for one row; '' where a cell cannot be read."""
    out = {}
    for title, column in columns.items():
        try:
            referenced = column.getCellByRowId(row.id).referencedTableRow
            out[title] = referenced.name if referenced else ''
        except Exception:
            out[title] = ''
    return out


def row_by_name(table, name):
    """itemByName is unreliable on these tables; walk them instead."""
    for i in range(table.rows.count):
        if table.rows.item(i).name == name:
            return table.rows.item(i)
    return None


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------
class Plan:
    """What Create Configurations would do, worked out without doing any of it."""

    __slots__ = ('vary', 'axes', 'total', 'present', 'to_create', 'clashes',
                 'untouched', 'base', 'prefix', 'error',
                 'rows', 'to_rename', 'named_right', 'rename_dupes',
                 'unreadable', 'to_delete', 'undeletable')

    def __init__(self, error=None):
        self.vary = ()
        self.axes = []
        self.total = 0
        self.present = 0
        self.to_create = []      # [(name, {theme: value})]
        self.clashes = []
        self.untouched = {}
        self.base = None
        self.prefix = ''
        self.error = error
        # Rows already in the table.
        self.rows = 0
        self.to_rename = []      # [{'index': i, 'old': name, 'new': name}]
        self.named_right = 0     # rows the scheme already agrees with
        self.rename_dupes = []   # rows whose scheme name a DIFFERENT combination wants
        self.unreadable = []     # rows with a cell that could not be read
        # Duplicates: rows carrying the same combination as a row being kept.
        self.to_delete = []      # [{'id': row id, 'name': name, 'keeps': name}]
        self.undeletable = []    # duplicates Fusion will not delete (the active row)


def _active_row_id(table):
    try:
        row = table.activeRow
        return row.id if row else None
    except Exception:
        return None


def plan(design, document_name, rename=True, dedupe=True):
    """Work out what to delete, rename and create. Reads only; changes nothing.

    `dedupe` finds rows that carry exactly the same combination of the varied
    themes as another row. One of each group is kept — the table's first row if
    it is in the group (Fusion will not delete that one), then the active row,
    then a row already named to the scheme, then the earliest — and the rest are
    listed for deletion. Their names are then free for the rows that stay.

    `rename` says whether the rows that stay will be brought into line with the
    naming scheme. It only affects the plan's view of which names are taken —
    with renaming on, an old hand-typed name is about to be freed and so cannot
    collide with a row about to be created."""
    table = top_table(design)
    if table is None:
        return Plan('This document has no configuration table. Open the '
                    'configured design itself, not an assembly that places one.')

    columns = theme_columns(table)
    if VARY:
        # A configured theme is matched loosely and then replaced by the table's
        # own spelling of it, so everything downstream indexes `columns` with a
        # title that is actually in there.
        wanted, missing = [], []
        for title in VARY:
            found = pick_name(list(columns), (title,))
            if found is None:
                missing.append(title)
            else:
                wanted.append(found)
        if missing:
            return Plan('These themes are not columns in this table:\n  ' +
                        '\n  '.join(missing))
        vary = tuple(wanted)
    else:
        vary = tuple(t for t in columns if not is_excluded(t))
    if not vary:
        return Plan('Nothing to vary — every theme in this table is excluded.')
    if table.rows.count == 0:
        return Plan('This table has no rows to copy from.')

    result = Plan()
    result.vary = vary
    result.axes = [theme_values(columns[t]) for t in vary]
    combinations = [dict(zip(vary, values))
                    for values in itertools.product(*result.axes)]
    result.total = len(combinations)
    result.base = table.rows.item(0)
    result.prefix = PREFIX if PREFIX is not None else document_name

    inherited = combination_of(columns, result.base)
    result.untouched = {t: v for t, v in inherited.items() if t not in vary}

    result.rows = table.rows.count
    active_id = _active_row_id(table)

    # Group the rows already in the table by the combination they carry.
    taken = set()
    groups = {}              # combination key -> [(index, row)]
    for i in range(table.rows.count):
        row = table.rows.item(i)
        whole = combination_of(columns, row)
        # A cell that would not read leaves the row alone rather than naming or
        # deleting it from a hole: '' is not a value, it is a failure to find one.
        if any(not whole.get(t) for t in vary):
            result.unreadable.append(row.name)
            taken.add(row.name)
            continue
        key = tuple(whole[t] for t in vary)
        groups.setdefault(key, []).append((i, row, whole))

    wanted = {}
    for key, members in groups.items():
        scheme = row_name(result.prefix, members[0][2], vary)

        def rank(member):
            i, row, _whole = member
            return (i != 0, row.id != active_id, row.name != scheme, i)

        members.sort(key=rank)
        keeper = members[0]
        for i, row, _whole in members[1:]:
            if not dedupe:
                result.rename_dupes.append(row.name)
                taken.add(row.name)
            elif i == 0 or row.id == active_id:
                # Fusion refuses to delete the first row, and deleting the row
                # that is open would pull the design out from under the user.
                result.undeletable.append(row.name)
                taken.add(row.name)
            else:
                result.to_delete.append({'id': row.id, 'name': row.name,
                                         'keeps': keeper[1].name})

        i, row, _whole = keeper
        if scheme in wanted:
            # A DIFFERENT combination the scheme spells the same way. Only the
            # first takes the name; the rest keep theirs and are reported.
            result.rename_dupes.append(row.name)
            taken.add(row.name)
            continue
        wanted[scheme] = i
        if row.name == scheme:
            result.named_right += 1
            taken.add(row.name)
            continue
        if rename:
            result.to_rename.append({'index': i, 'old': row.name, 'new': scheme})
            taken.add(scheme)
        else:
            taken.add(row.name)

    planned = set()
    for combination in combinations:
        key = tuple(combination[t] for t in vary)
        if key in groups:
            continue
        name = row_name(result.prefix, combination, vary)
        if name in taken or name in planned:
            result.clashes.append(name)
        planned.add(name)
        result.to_create.append((name, combination))
    result.present = result.total - len(result.to_create)
    return result


def delete_duplicates(design, result):
    """Delete every duplicate row the plan lists. Returns (deleted, problems).

    Rows are found again by id, never by index: every deletion shifts the
    indexes of the rows after it. Run before rename(), and plan again after it —
    the indexes the plan recorded are stale once anything has gone."""
    table = top_table(design)
    if table is None or not result.to_delete:
        return 0, []
    deleted, problems = 0, []
    for item in result.to_delete:
        try:
            row = table.rows.itemById(item['id'])
            if row is None:
                continue
            if row.deleteMe():
                deleted += 1
            else:
                problems.append(f"{item['name']}: Fusion would not delete it")
        except Exception as exc:
            problems.append(f"{item['name']}: {exc}")
    return deleted, problems


def rename(design, result):
    """Rename every row the plan calls for. Returns (renamed, problems).

    Run before create(), so the names the new rows want are free by the time
    they are asked for.

    Where a name is passing from one row to another, the row that currently
    holds it is parked on a temporary name first. Fusion does not swap names for
    you: ask for one that is taken and it appends "(1)", leaving two rows that
    read almost alike."""
    table = top_table(design)
    if table is None or not result.to_rename:
        return 0, []

    moving = {item['index']: item['new'] for item in result.to_rename}
    held = {}
    for i in range(table.rows.count):
        held[table.rows.item(i).name] = i

    problems = []
    blocked = set()
    for item in result.to_rename:
        owner = held.get(item['new'])
        if owner is None or owner == item['index']:
            continue
        if owner not in moving:
            # Someone who is staying put has the name — the plan should not have
            # produced this, so say so rather than letting Fusion invent a "(1)".
            problems.append(f"{item['old']}: \"{item['new']}\" is already taken "
                            f"by a row that is staying as it is — left alone")
            blocked.add(item['index'])
            continue
        try:
            table.rows.item(owner).name = '_wc_rename_%d' % owner
        except Exception as exc:
            problems.append(f'{table.rows.item(owner).name}: {exc}')

    renamed = 0
    for item in result.to_rename:
        if item['index'] in blocked:
            continue
        try:
            table.rows.item(item['index']).name = item['new']
            renamed += 1
        except Exception as exc:
            # e.g. "Rename is unavailable because the Configured Design is still
            # being saved" while a cloud save is in flight.
            problems.append(f"{item['old']}: {exc}")
    return renamed, problems


def create(design, result):
    """Add every row the plan calls for. Returns (made, problems).

    Nothing is built here and nothing is saved — creating a row is instant, and
    Fusion builds a configuration when it is activated."""
    table = top_table(design)
    columns = theme_columns(table)
    parts = property_columns(table)
    made, problems = 0, []

    for name, combination in result.to_create:
        try:
            row = result.base.copy(name)
            if row is None:
                problems.append(f'{name}: copy failed')
                continue
            for title in result.vary:
                column = columns[title]
                target = row_by_name(column.referencedTable, combination[title])
                column.getCellByRowId(row.id).referencedTableRow = target
            if PART_NUMBER_FROM_NAME and 'Part Number' in parts:
                try:
                    parts['Part Number'].getCellByRowId(row.id).value = row.name
                except Exception:
                    pass
            made += 1
        except Exception as exc:
            problems.append(f'{name}: {exc}')
    return made, problems
