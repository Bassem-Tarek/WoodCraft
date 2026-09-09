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

"""Switching a whole kitchen's cabinets between the Gola and Handles themes.

A Gola profile is not hardware you place — it is machined into the cabinet, so it
belongs to the cabinet's own configuration. Every other handle IS hardware, and
the cabinet has to be configured NOT to have a Gola profile before one is fitted.
So picking a handle is really two decisions, and this module makes the first one:
every base and tall cabinet in the kitchen is switched to the Handles theme that
matches what was picked, and only then does Fit Handles place anything.

None of the names involved are matched exactly. The theme column and its two
values are found through the alias lists in config_table, ignoring case, spacing
and punctuation, and each library is then written using its OWN spelling of
them — so libraries that disagree about what to call the column, or call the
profile "Gola" where another says "Gola C Profile", are all handled in one run.

Wall cabinets are left alone — WALL_PREFIXES — because they carry no Gola profile.

What costs time, measured
-------------------------
Almost everything about an occurrence is free to read. Two things are not, and
neither is cached by Fusion, so every access pays again:

    occurrence.configurationRow      ~3.0 s   EVERY time
    occurrence.configuredDataFile    ~0.8 s   EVERY time

Reading them once per cabinet is what made this take a minute on two cabinets.
So neither is read per cabinet any more:

  * The row NAME comes from `occurrence.component.name`, which is free and equal
    to it. It is checked against the library table, and only if it is not there
    does the slow property get read for that one cabinet.
  * The library DataFile is fetched once per LIBRARY, not once per cabinet. The
    first cabinet pays 0.8 s; every later cabinet whose row is in that library's
    table is known to belong to it and pays nothing.
  * Nothing is read at all for a cabinet already on the right theme, which is the
    common case when you are trying handles one after another.

Everything hangs off objects held from the one read: once you hold a row, its
name, its parent table and every row in that table are free.

Three more constraints shape the rest of it:

  * A cabinet's theme VALUES cannot be read from the kitchen at all. Every route
    answers "API Function not yet implemented", and having the library file open
    is not enough — only the configured design as the ACTIVE document will say.
    Activating an already-open document costs 0.23 s and reading its whole table
    a millisecond, so it happens ONCE per library file.
  * A row added to a library file is invisible to the kitchen until that file is
    SAVED, and a save costs about 25 seconds. So every row a run needs is created
    in one visit and committed in one save.
  * A placement is pinned to the library version it was inserted from. After a
    save it still sees the old table, and neither reopening the kitchen nor
    "Get All Latest Versions" moves it. The row has to come from
    `configuredDataFile.latestVersion.configurationTable`, and switching to one
    of those advances the reference as a side effect.
"""

import adsk.core
import adsk.fusion

from .. import config_table
from ... import config
from ...lib import fusionAddInUtils as futil


# The theme column that decides whether a cabinet is machined for a Gola profile,
# and its two values. Named rather than positional: a library grows columns.
#
# Each is an ALIAS LIST rather than one string, because these names are typed by
# hand into every library and drift between them — "Handles" here, "Handle Type"
# there; "Gola" in one, "Gola C Profile" in the next. Nothing is compared as an
# exact string: case, spacing and punctuation are ignored, the aliases are tried
# in preference order, and whatever a library turns out to call the column and
# its values is then used AS THAT LIBRARY SPELLS IT. A library naming things its
# own way gets switched instead of being reported as having no Handles theme.
HANDLES_THEME_NAMES = config_table.HANDLES_TITLES
GOLA_VALUE_NAMES = config_table.GOLA_VALUES
OTHER_VALUE_NAMES = config_table.HANDLE_VALUES

# What to call them to the user, and what a new library should call them. Only
# ever used for wording — never to match anything.
HANDLES_THEME = HANDLES_THEME_NAMES[0]
GOLA_VALUE = GOLA_VALUE_NAMES[0]
OTHER_VALUE = OTHER_VALUE_NAMES[0]

# Which cabinets take part. Base and tall units carry a Gola profile; wall units
# do not, so switching them would be a rebuild for no change.
CABINET_PREFIXES = ('BC', 'TC')
WALL_PREFIXES = ('WC',)

# Themes whose value is decided by configuration rules rather than by us. They
# are copied from the source row and never written, and never named. Shared with
# Create Configurations so both halves treat the table the same way.
EXCLUDE_THEMES = config_table.EXCLUDE


def wants_gola(handle_label):
    """Is the chosen handle a Gola profile rather than a piece of hardware?

    The word decides however it is written, so "GOLA-C 3m" and "Gola_C" in the
    hardware library both read as Gola."""
    return config_table.is_gola(handle_label)


def theme_title(columns):
    """This table's own spelling of the Handles theme column, or None."""
    return config_table.pick_name(list(columns), HANDLES_THEME_NAMES)


def theme_value(values, want_gola):
    """This theme's own spelling of the value wanted, or None.

    Gola is decided by the word itself: any value carrying "gola" is one, and the
    alias list only settles which to prefer when a theme offers several. The
    hardware value is then whatever is left — matched against its alias list
    first, and failing that, if the theme has exactly one non-Gola value, that
    one, whatever it happens to be called. A two-value theme therefore always
    resolves, even when neither name is one we would have guessed."""
    values = [value for value in values if str(value or '').strip()]
    gola = [value for value in values if config_table.is_gola(value)]
    if want_gola:
        return config_table.pick_name(gola, GOLA_VALUE_NAMES) or (gola[0] if gola
                                                                  else None)
    rest = [value for value in values if not config_table.is_gola(value)]
    return (config_table.pick_name(rest, OTHER_VALUE_NAMES)
            or (rest[0] if len(rest) == 1 else None))


# Naming comes from config_table so a row added here and a row added by Create
# Configurations are named by the same rules — two places generating names for
# the same table is how they drift apart.
row_name_for = config_table.row_name


def is_cabinet(occurrence):
    """A base or tall cabinet placed as a configuration. Wall units excluded."""
    try:
        if not occurrence.isConfiguration:
            return False
        name = (occurrence.name or '').strip().upper()
    except Exception:
        return False
    if name.startswith(WALL_PREFIXES):
        return False
    return name.startswith(CABINET_PREFIXES)


class CabinetRef:
    """One cabinet, with everything cheap read once and the costly bits deferred.

    `row_name` is taken from the component name, which is free and matches the
    configuration row. It is treated as a guess until the library table confirms
    it; `confirm` swaps in the authoritative name if it does not."""

    __slots__ = ('occurrence', 'name', 'row_name', 'guessed', 'data_file', 'target')

    def __init__(self, occurrence):
        self.occurrence = occurrence
        self.name = occurrence.name
        try:
            self.row_name = occurrence.component.name
        except Exception:
            self.row_name = self.name.rsplit(':', 1)[0]
        self.guessed = True         # not yet checked against a real table
        self.data_file = None
        self.target = None          # target row name, once decided

    def confirm(self, rows):
        """True once row_name is known to be this cabinet's row.

        Costs nothing when the free guess is right, which it is whenever the
        component has not been renamed. Otherwise it falls back to the slow
        property for this one cabinet."""
        if not self.guessed or self.row_name in rows:
            self.guessed = False
            return self.row_name in rows
        try:
            self.row_name = self.occurrence.configurationRow.name
        except Exception:
            return False
        self.guessed = False
        return self.row_name in rows

    def file(self):
        """The configured design this cabinet came from, read at most once."""
        if self.data_file is None:
            self.data_file = self.occurrence.configuredDataFile
        return self.data_file


def scan(root):
    """[CabinetRef] for every base/tall cabinet, reading nothing expensive."""
    return [CabinetRef(root.occurrences.item(i))
            for i in range(root.occurrences.count)
            if is_cabinet(root.occurrences.item(i))]


def _open_design(app, data_file):
    """The configured design as an open document, opening it only if it isn't."""
    for i in range(app.documents.count):
        document = app.documents.item(i)
        try:
            if document.dataFile and document.dataFile.id == data_file.id:
                return document
        except Exception:
            continue
    try:
        return app.documents.open(data_file, True)
    except Exception:
        return None


def read_table(design):
    """(rows, theme titles, handles title) off an ACTIVE configured design.

    (None, None, None) when it has no configuration table, or no column that
    reads as the Handles theme. rows is {row name: {theme title: value name}},
    and the third item is this table's own spelling of the Handles column —
    carried around from here on so nothing has to guess at it twice."""
    table = config_table.top_table(design)
    if table is None:
        return None, None, None
    columns = config_table.theme_columns(table)
    title = theme_title(columns)
    if title is None:
        return None, None, None
    rows = {}
    for i in range(table.rows.count):
        row = table.rows.item(i)
        rows[row.name] = config_table.combination_of(columns, row)
    return rows, list(columns), title


class Library:
    """One configured design, its table, and the cabinets that came from it."""

    __slots__ = ('data_file', 'document', 'rows', 'titles', 'theme', 'refs',
                 'created')

    def __init__(self, data_file, document, rows, titles, theme):
        self.data_file = data_file
        self.document = document
        self.rows = rows
        self.titles = titles
        self.theme = theme          # this library's own name for the column
        self.refs = []
        self.created = 0


def gather(app, refs, problems):
    """Group cabinets by the library they came from, one DataFile read per library.

    The grouping trick: a cabinet's row name is unique within its own library, so
    once a library's table is in hand, every remaining cabinet whose row is in
    that table belongs to it — and needs no DataFile read of its own."""
    libraries, waiting = [], list(refs)
    while waiting:
        seed = waiting[0]
        try:
            data_file = seed.file()
        except Exception as exc:
            problems.append(f'{seed.name}: cannot tell which design it came from '
                            f'({exc})')
            waiting.pop(0)
            continue

        document = _open_design(app, data_file)
        if document is None:
            problems.append(f'{data_file.name}: could not be opened')
            waiting.pop(0)
            continue
        try:
            document.activate()
            design = adsk.fusion.Design.cast(app.activeProduct)
            rows, titles, theme = read_table(design)
        except Exception as exc:
            problems.append(f'{data_file.name}: could not be read ({exc})')
            waiting.pop(0)
            continue
        if rows is None:
            problems.append(f'{data_file.name}: no theme reading as '
                            f'"{HANDLES_THEME}" — its cabinets were left alone')
            waiting = [r for r in waiting if r is not seed]
            continue

        library = Library(data_file, document, rows, titles, theme)
        libraries.append(library)
        left = []
        for ref in waiting:
            if ref is seed:
                # The seed belongs here whatever its name says — its DataFile was
                # read directly. confirm() still runs so a renamed component gets
                # its real row name rather than a wrong one.
                ref.confirm(rows)
                library.refs.append(ref)
            elif ref.confirm(rows):
                ref.data_file = data_file
                library.refs.append(ref)
            else:
                left.append(ref)
        waiting = left
    return libraries


def _match(rows, wanted):
    """The name of an existing row whose themes are exactly `wanted`, or None.

    Every theme is compared, Partition included. `wanted` is the cabinet's own
    current combination with only the Handles value changed, so an exact match is
    precisely "the same cabinet with the other handle theme" — the row to reuse.
    Matching loosely would let a run reuse a row that differs in Partition,
    quietly changing the cabinet.

    The whole file is searched, so a row added by hand, by Create Configurations,
    or by an earlier run is found and reused rather than duplicated."""
    for name, values in rows.items():
        if all(values.get(title) == value for title, value in wanted.items()):
            return name
    return None


def decide(library, want_gola, log, problems):
    """Give every cabinet in this library its target row, creating what is missing.

    The library's document must already be active. The value to switch to is
    resolved HERE rather than passed in, because each library names its own
    values and two libraries in one kitchen need not agree: one may offer "Gola
    C Profile" and the next plain "Gola".

    Rows are created here but the save happens once, afterwards, because a save
    costs about 25 seconds."""
    design = adsk.fusion.Design.cast(adsk.core.Application.get().activeProduct)
    table = config_table.top_table(design)
    columns = config_table.theme_columns(table)
    prefix = library.document.name

    # The column is found by name once and then used as an object. Its own
    # referencedTable is the theme table it points at, which beats looking that
    # table up by name a second time — a column and its theme table need not be
    # spelled the same way.
    title = library.theme if library.theme in columns else theme_title(columns)
    column = columns.get(title)
    if column is None:
        problems.append(f'{library.data_file.name}: its "{HANDLES_THEME}" theme '
                        f'went missing between reading and writing')
        return
    try:
        values = config_table.theme_values(column)
    except Exception as exc:
        problems.append(f'{library.data_file.name}: could not read the '
                        f'"{title}" values ({exc})')
        return
    target_value = theme_value(values, want_gola)
    if target_value is None:
        problems.append(f'{library.data_file.name}: its "{title}" theme has no '
                        f'{"Gola" if want_gola else "hardware-handle"} value '
                        f'({", ".join(values) if values else "it has no values"})')
        return
    if not config_table.same_name(target_value, GOLA_VALUE if want_gola
                                  else OTHER_VALUE):
        log(f'  {library.data_file.name}: "{title}" → "{target_value}"')

    for ref in library.refs:
        current = library.rows.get(ref.row_name)
        if current is None:
            problems.append(f'{ref.name}: row "{ref.row_name}" is not in '
                            f'{library.data_file.name}')
            continue
        if config_table.same_name(current.get(title), target_value):
            continue                       # already right — costs nothing

        wanted = dict(current)
        wanted[title] = target_value
        match = _match(library.rows, wanted)
        if match is None:
            name = config_table.row_name(prefix, wanted, library.titles)
            try:
                source = config_table.row_by_name(table, ref.row_name)
                new_row = source.copy(name)
                cell = column.getCellByRowId(new_row.id)
                cell.referencedTableRow = config_table.row_by_name(
                    column.referencedTable, target_value)
            except Exception as exc:
                problems.append(f'{library.data_file.name}: could not add '
                                f'"{name}" ({exc})')
                continue
            match = new_row.name
            library.rows[match] = wanted
            library.created += 1
            log(f'  {library.data_file.name}: added {match}')
        ref.target = match


def apply_theme(app, root, want_gola, log):
    """Switch every base and tall cabinet to the chosen Handles theme.

    Returns (switched, unchanged, notes, problems)."""
    kitchen_doc = app.activeDocument
    problems, notes = [], []

    refs = scan(root)
    if not refs:
        return 0, 0, notes, problems

    libraries = gather(app, refs, problems)
    for library in libraries:
        try:
            library.document.activate()
            decide(library, want_gola, log, problems)
        except Exception as exc:
            problems.append(f'{library.data_file.name}: {exc}')

    # One save per library, and only when something was added — the new rows are
    # invisible to the kitchen until it happens.
    for library in libraries:
        if not library.created:
            continue
        notes.append(f'{library.data_file.name}: {library.created} new '
                     f'configuration(s)')
        try:
            library.document.activate()
            library.document.save(f'WoodCraft: {library.created} handle '
                                  f'configuration(s)')
        except Exception as exc:
            problems.append(f'{library.data_file.name}: could not save ({exc})')
            for ref in library.refs:
                ref.target = None

    try:
        kitchen_doc.activate()
    except Exception:
        pass

    pending = [r for r in refs if r.target]
    unchanged = len(refs) - len(pending)
    if not pending:
        return 0, unchanged, notes, problems

    switched, saved_kitchen = 0, False
    tables = {}
    for ref in pending:
        try:
            key = ref.data_file.id
            table = tables.get(key)
            if table is None:
                table = _wait_for_row(ref, ref.target)
                if table is not None:
                    tables[key] = table
            target = config_table.row_by_name(table, ref.target) if table else None
            if target is None:
                problems.append(f'{ref.name}: "{ref.target}" never appeared in '
                                f'the latest {ref.data_file.name}')
                continue

            try:
                done = ref.occurrence.switchConfiguration(target)
            except Exception as exc:
                # Switching to a row published since this cabinet was placed has
                # to move its reference to the newer library version, and Fusion
                # will not re-point a reference in an unsaved document. Only rows
                # created by this run hit that, so the kitchen is saved lazily —
                # once, and only when a switch actually asks for it.
                if 'saved document' not in str(exc) or saved_kitchen:
                    raise
                kitchen_doc.save('WoodCraft: handle configuration switch')
                saved_kitchen = True
                notes.append('the kitchen was saved so its cabinets could be '
                             'pointed at the updated library')
                done = ref.occurrence.switchConfiguration(target)

            if done:
                switched += 1
            else:
                problems.append(f'{ref.name}: switch to "{ref.target}" was refused')
        except Exception as exc:
            problems.append(f'{ref.name}: {exc}')
    return switched, unchanged, notes, problems


def _wait_for_row(ref, row_name, attempts=40):
    """The latest version's table, once `row_name` is actually in it.

    A save does not publish instantly: for a second or two afterwards the latest
    version still serves the table as it was, and a switch against it fails with
    the row "not in the latest" — a race, not a missing row. The DataFile is
    re-read each time round because its idea of the latest version is cached."""
    for _attempt in range(attempts):
        try:
            table = ref.occurrence.configuredDataFile.latestVersion.configurationTable
            if config_table.row_by_name(table, row_name) is not None:
                return table
        except Exception:
            pass
        for _ in range(50):
            adsk.doEvents()
    return None
