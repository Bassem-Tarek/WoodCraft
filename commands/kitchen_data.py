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

"""Cloud-data plumbing shared by the Kitchen tab.

Everything here talks to Fusion's **Data** API (projects, folders, files) rather
than to geometry, and it is deliberately kept out of the three command modules so
the workflow reads in one place:

    Create Kitchen      Projects / <B2B|B2C> / Kitchen / "Kitchen Template <n>" / Library
    Template            <- recursive copy of Library / Kitchen
                        + a new hybrid design named after the folder

    New Kitchen         rename the NEWEST waiting template folder AND its design
                        to "<Customer>_Kitchen_<date>". No copying, so instant.

    Finish Kitchen      delete every file in that Library that the kitchen design
                        does not reference, then prune the folders left empty.

Splitting create from name is the whole point: copying a library is slow and
network-bound, so templates get stocked in a quiet moment and a designer starting
a real job pays only for two rename calls.

Two rules shape the code:

* **The network is slow and can fail mid-way.** Every walk is bounded and every
  per-item cloud call is wrapped, so one bad file reports itself instead of
  aborting the run. Callers pass a `progress` callback and get back a plain
  report dict — no UI in here.

* **The Library folder is found by ID, not by name.** Create Kitchen Template
  stamps the folder's id onto the design document (``config.WC_KITCHEN_LIB_FOLDER``).
  That matters more than ever now that New Kitchen works by RENAMING: the folder
  and the design both change name between creation and Finish Kitchen, and an id
  doesn't care. Name matching is only a fallback.

No adsk.fusion imports: this module is about files, not features.
"""

import datetime
import re

import adsk.core

from .. import config
from . import folder_store

app = adsk.core.Application.get()


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------
# Characters Fusion (and the OS, once a design is exported) will not accept in a
# folder or file name. Collapsed to a space rather than dropped so "A/B" reads as
# "A B" instead of "AB".
_ILLEGAL = re.compile(r'[\\/:*?"<>|]+')


def clean_name(text):
    """A name safe to hand to the Data API: illegal characters and runs of
    whitespace collapsed to single spaces, trimmed. Returns '' for junk input."""
    if not text:
        return ''
    return re.sub(r'\s+', ' ', _ILLEGAL.sub(' ', str(text))).strip()


def today_stamp():
    """Today in the short format configured by config.KITCHEN_DATE_FORMAT, or ''
    when that is empty (kitchens then get named after the customer alone)."""
    if not config.KITCHEN_DATE_FORMAT:
        return ''
    return datetime.date.today().strftime(config.KITCHEN_DATE_FORMAT)


def kitchen_folder_name(customer, stamp=None):
    """The name New Kitchen renames a template to — folder and design alike.

    Built from config.KITCHEN_NAME_PATTERN, which is '{customer}_Kitchen_{date}'
    by default. A trailing separator is trimmed, so switching KITCHEN_DATE_FORMAT
    off gives 'Al Rashid_Kitchen' rather than 'Al Rashid_Kitchen_'."""
    customer = clean_name(customer)
    stamp = today_stamp() if stamp is None else stamp
    pattern = getattr(config, 'KITCHEN_NAME_PATTERN', '{customer}_Kitchen_{date}')
    try:
        name = pattern.format(customer=customer, date=stamp)
    except Exception:
        # A pattern with a typo'd placeholder shouldn't stop someone starting a
        # kitchen — fall back to the documented default.
        name = f'{customer}_Kitchen_{stamp}'
    return clean_name(re.sub(r'[_\-\s]+$', '', name))


# ---------------------------------------------------------------------------
# Finding projects and folders
# ---------------------------------------------------------------------------
def _hubs_to_search():
    """The hubs a kitchen may live in: only the one active in Fusion right now
    when config.KITCHEN_ACTIVE_HUB_ONLY is on (the default), else the active hub
    first and then every other hub the user can see."""
    hubs = []
    try:
        active = app.data.activeHub
    except Exception:
        active = None
    if active:
        hubs.append(active)
    if getattr(config, 'KITCHEN_ACTIVE_HUB_ONLY', True) and active:
        return hubs
    try:
        all_hubs = app.data.dataHubs
        for h in range(all_hubs.count):
            hub = all_hubs.item(h)
            if not active or hub.id != active.id:
                hubs.append(hub)
    except Exception:
        pass
    return hubs


def active_hub_name():
    """Display name of the hub (team) open in Fusion, or '' if unknown."""
    try:
        return app.data.activeHub.name
    except Exception:
        return ''


_project_cache = {}     # (active hub id, project name) -> DataProject


def find_project(project_name):
    """The named cloud project in the active hub (and, only when
    KITCHEN_ACTIVE_HUB_ONLY is off, any other hub). Returns None if not found.

    Cached per active hub for the session: listing a hub's projects is a network
    round trip, and every Kitchen dialog asks for the same one or two projects
    over and over (once per dropdown change)."""
    try:
        hub_id = app.data.activeHub.id
    except Exception:
        hub_id = ''
    key = (hub_id, project_name)
    cached = _project_cache.get(key)
    if cached is not None:
        try:
            if cached.isValid:
                return cached
        except Exception:
            return cached
    for hub in _hubs_to_search():
        try:
            found = _project_in_hub(hub, project_name)
        except Exception:
            found = None
        if found:
            _project_cache[key] = found
            return found
    return None


def _project_in_hub(hub, project_name):
    projects = hub.dataProjects
    for i in range(projects.count):
        project = projects.item(i)
        if project.name == project_name:
            return project
    return None


def all_project_names():
    """Every project name in the searched hub(s) — shown in the dialog when a
    configured project can't be found, so the exact spelling can be copied."""
    names = []
    for hub in _hubs_to_search():
        try:
            projects = hub.dataProjects
            for i in range(projects.count):
                names.append(projects.item(i).name)
        except Exception:
            continue
    return names


def _not_found(what, setting):
    hub = active_hub_name()
    where = f" in the '{hub}' hub" if hub else ''
    return (f"{what} not found{where}. Set {setting} in config.py to one of: "
            f"{', '.join(all_project_names()) or '(no projects visible)'}")


def subfolder_by_name(folder, name):
    """A direct child folder by name (case-insensitive), or None."""
    if not folder or not name:
        return None
    try:
        exact = folder.dataFolders.itemByName(name)
        if exact:
            return exact
    except Exception:
        pass
    try:
        folders = folder.dataFolders
        target = name.strip().lower()
        for i in range(folders.count):
            sub = folders.item(i)
            if sub.name.strip().lower() == target:
                return sub
    except Exception:
        pass
    return None


def library_source_folder():
    """(folder, error) for the cabinet library New Kitchen copies FROM.

    The folder picked in Folders Selection wins. Otherwise
    config.LIBRARY_SOURCE_FOLDER names a folder inside config.LIBRARY_PROJECT_NAME;
    leave it empty to copy the whole project root instead. `error` is a
    ready-to-show sentence when the folder can't be resolved."""
    configured, folder = folder_store.resolve('library')
    if configured:
        if folder is None:
            return None, folder_store.missing_message('library')
        return folder, None
    project = find_project(config.LIBRARY_PROJECT_NAME)
    if not project:
        return None, _not_found(f"Library project '{config.LIBRARY_PROJECT_NAME}'",
                                'LIBRARY_PROJECT_NAME')
    root = project.rootFolder
    wanted = (config.LIBRARY_SOURCE_FOLDER or '').strip()
    if not wanted:
        return root, None
    folder = subfolder_by_name(root, wanted)
    if not folder:
        return None, (f"Folder '{wanted}' not found in project "
                      f"'{config.LIBRARY_PROJECT_NAME}'. Set LIBRARY_SOURCE_FOLDER "
                      f"in config.py (leave it empty to copy the whole project).")
    return folder, None


def library_where():
    """Display text for the master library location."""
    sel = folder_store.get('library')
    if sel:
        return folder_store.describe(sel)
    label = config.LIBRARY_SOURCE_FOLDER or '(project root)'
    return f'{config.LIBRARY_PROJECT_NAME} / {label}'


def child_folder_names(parent):
    """Lower-cased names of every direct sub-folder of `parent` — ONE listing,
    so a dialog can test many candidate names without a round trip each."""
    names = set()
    try:
        folders = parent.dataFolders
        for i in range(folders.count):
            try:
                names.add(folders.item(i).name.strip().lower())
            except Exception:
                continue
    except Exception:
        pass
    return names


def unique_folder_name(parent, name, taken=None):
    """`name` if free inside `parent`, else 'name (2)', 'name (3)', … Two kitchens
    for the same customer on the same day must not collide.

    `taken` (from child_folder_names) lets a dialog re-check on every keystroke
    without listing the folder again; without it the folder is listed once."""
    if taken is None:
        taken = child_folder_names(parent)
    if name.strip().lower() not in taken:
        return name
    for n in range(2, 100):
        candidate = f'{name} ({n})'
        if candidate.strip().lower() not in taken:
            return candidate
    return f'{name} ({datetime.datetime.now().strftime("%H%M%S")})'


# ---------------------------------------------------------------------------
# The kitchens root, and the templates waiting in it
# ---------------------------------------------------------------------------
def project_types():
    """The project types offered in the dialogs (config.KITCHEN_PROJECT_TYPES),
    e.g. ['B2B', 'B2C']."""
    types = getattr(config, 'KITCHEN_PROJECT_TYPES', ()) or ()
    if isinstance(types, str):
        types = [types]
    return [t for t in (str(x).strip() for x in types) if t]


def kitchens_where(project_type):
    """'Projects / B2B / Kitchen' — the display path of a type's kitchens folder
    (or the folder picked for it in Folders Selection)."""
    sel = folder_store.get(folder_store.kitchens_key(project_type))
    if sel:
        return folder_store.describe(sel)
    parts = [config.KITCHENS_PROJECT_NAME, project_type, config.KITCHENS_FOLDER_NAME]
    return ' / '.join(p for p in parts if p)


def kitchens_root(project_type):
    """(folder, error) for the folder that holds every template and job folder of
    one project type — config.KITCHENS_PROJECT_NAME / <project_type> /
    config.KITCHENS_FOLDER_NAME, e.g. Projects / B2B / Kitchen.

    The innermost Kitchen folder is CREATED if it's missing, but the project and
    the type folder are never created: a wrong name should be reported, not
    silently worked around.

    A folder picked for the type in Folders Selection is used as-is instead."""
    key = folder_store.kitchens_key(project_type)
    configured, picked = folder_store.resolve(key)
    if configured:
        if picked is None:
            return None, folder_store.missing_message(key)
        return picked, None
    project = find_project(config.KITCHENS_PROJECT_NAME)
    if not project:
        return None, _not_found(f"Project '{config.KITCHENS_PROJECT_NAME}'",
                                'KITCHENS_PROJECT_NAME')
    folder = project.rootFolder

    project_type = (project_type or '').strip()
    if project_type:
        type_folder = subfolder_by_name(folder, project_type)
        if not type_folder:
            return None, (f"Folder '{project_type}' not found in project "
                          f"'{config.KITCHENS_PROJECT_NAME}'. Check "
                          f"KITCHEN_PROJECT_TYPES in config.py.")
        folder = type_folder

    wanted = (config.KITCHENS_FOLDER_NAME or '').strip()
    if not wanted:
        return folder, None
    kitchens = subfolder_by_name(folder, wanted)
    if kitchens:
        return kitchens, None
    try:
        kitchens = folder.dataFolders.add(wanted)
    except Exception:
        kitchens = None
    if not kitchens:
        return None, (f"Could not find or create the '{wanted}' folder in "
                      f"'{kitchens_where(project_type).rsplit(' / ', 1)[0]}'.")
    return kitchens, None


def default_project_type():
    """The type to pre-select in a dialog: the one picked last time if it is
    still configured, else the first configured type ('' if none)."""
    types = project_types()
    try:
        from . import settings_store
        last = settings_store.get_kitchen_project_type()
    except Exception:
        last = ''
    for t in types:
        if t.lower() == (last or '').lower():
            return t
    return types[0] if types else ''


def remember_project_type(project_type):
    """Remember the chosen type for next time. Never fatal."""
    try:
        from . import settings_store
        settings_store.set_kitchen_project_type(project_type)
    except Exception:
        pass


def project_type_of(document):
    """The project type (e.g. 'B2B') a saved kitchen design belongs to, read off
    its folder chain: <job folder> / Kitchen / B2B / (Projects root). Returns ''
    when the design isn't inside any configured type folder."""
    types = {t.lower(): t for t in project_types()}
    if not types:
        return ''
    # Folders picked in Folders Selection are recognised by id.
    by_id = {}
    for t in types.values():
        sel = folder_store.get(folder_store.kitchens_key(t))
        if sel and sel.get('folder_id'):
            by_id[sel['folder_id']] = t
    try:
        folder = document.dataFile.parentFolder
    except Exception:
        return ''
    for _ in range(config.KITCHEN_MAX_DEPTH):
        if not folder:
            break
        try:
            if by_id:
                match = by_id.get(folder.id)
                if match:
                    return match
            match = types.get(folder.name.strip().lower())
            if match:
                return match
            if folder.isRoot:
                break
            folder = folder.parentFolder
        except Exception:
            break
    return ''


# "Kitchen Template 7" -> 7. Anchored, so "Kitchen Template 7 (old)" is NOT a
# template and won't be offered for renaming.
def _template_number(folder_name):
    prefix = (config.KITCHEN_TEMPLATE_PREFIX or '').strip()
    if not prefix:
        return None
    match = re.fullmatch(rf'{re.escape(prefix)}\s+(\d+)', (folder_name or '').strip(),
                         re.IGNORECASE)
    return int(match.group(1)) if match else None


def template_folders(root, details=True):
    """Every waiting template in `root`, lowest number first, as dicts:
    {'folder', 'number', 'name'} plus, with details=True, 'design' and
    'cabinets'.

    `design` is the design DataFile sitting directly in the template folder (None
    if someone deleted it); `cabinets` counts the files in its Library copy so the
    New Kitchen dialog can show a template is actually stocked.

    details=False lists only the folder names — ONE network call instead of
    several per template (and a whole library walk each). Use it wherever only
    the numbers or the count are needed."""
    out = []
    if not root:
        return out
    try:
        folders = root.dataFolders
        count = folders.count
    except Exception:
        return out

    for i in range(count):
        try:
            folder = folders.item(i)
            name = folder.name
            number = _template_number(name)
        except Exception:
            continue
        if number is None:
            continue
        row = {'folder': folder, 'number': number, 'name': name}
        if details:
            _add_details(row)
        out.append(row)
    out.sort(key=lambda row: row['number'])
    return out


def _add_details(row):
    library = subfolder_by_name(row['folder'], config.KITCHEN_LIBRARY_FOLDER_NAME)
    row['design'] = design_file_in(row['folder'])
    row['cabinets'] = count_files(library) if library else 0
    return row


def has_files(folder, _depth=0):
    """True as soon as ANY file is found in the tree — stops at the first one
    instead of counting the whole library."""
    if not folder or _depth > config.KITCHEN_MAX_DEPTH:
        return False
    try:
        if folder.dataFiles.count:
            return True
        subs = folder.dataFolders
        for i in range(subs.count):
            if has_files(subs.item(i), _depth + 1):
                return True
    except Exception:
        pass
    return False


def _created_at(data_file):
    """DataFile.dateCreated as UNIX epoch seconds, or 0 when unavailable."""
    try:
        return int(data_file.dateCreated) if data_file else 0
    except Exception:
        return 0


def latest_template(root, rows=None):
    """The template New Kitchen will claim: the most recently built one.

    Newest by the design's creation date, because template NUMBERS are reused as
    gaps open up — "Kitchen Template 2" can easily be newer than 3 — so the
    highest number is not the newest folder. The number only breaks ties.

    Templates whose Library is empty (a copy that was cancelled part-way) are
    skipped while any stocked one exists, so a half-built spare doesn't get handed
    to a customer. Returns None when nothing is waiting.

    Only the chosen template's library is counted in full; the others are just
    checked for being non-empty, newest first, stopping at the first stocked one.
    `rows` may be a details=False listing the caller already has."""
    if rows is None:
        rows = template_folders(root, details=False)
    if not rows:
        return None
    for row in rows:
        if 'design' not in row:
            row['design'] = design_file_in(row['folder'])
    ordered = sorted(rows, key=lambda row: (_created_at(row['design']), row['number']),
                     reverse=True)
    for row in ordered:
        library = subfolder_by_name(row['folder'], config.KITCHEN_LIBRARY_FOLDER_NAME)
        if library and has_files(library):
            row['cabinets'] = count_files(library)
            return row
    chosen = ordered[0]
    chosen.setdefault('cabinets', 0)
    return chosen


def next_template_name(root, taken=None):
    """The next template name: the LOWEST unused number, not max+1.

    Templates are consumed by being renamed, so gaps open up constantly. Filling
    them keeps the numbers small and readable instead of drifting to
    'Kitchen Template 47' after a busy month."""
    prefix = (config.KITCHEN_TEMPLATE_PREFIX or 'Kitchen Template').strip()
    if taken is None:
        taken = {row['number'] for row in template_folders(root, details=False)}
    number = 1
    while number in taken:
        number += 1
    return f'{prefix} {number}'


def design_file_in(folder):
    """The kitchen design sitting directly in a job/template folder.

    A job folder holds exactly one design (the Library copy is a SUB-folder, so
    its cabinets are never candidates). Where a folder somehow holds several, the
    one named after the folder wins, then the first Fusion design."""
    if not folder:
        return None
    try:
        files = folder.dataFiles
        count = files.count
    except Exception:
        return None

    designs = []
    for i in range(count):
        try:
            data_file = files.item(i)
        except Exception:
            continue
        if data_file.name.strip().lower() == folder.name.strip().lower():
            return data_file
        try:
            if (data_file.fileExtension or '').lower() in ('f3d', 'f3z', ''):
                designs.append(data_file)
        except Exception:
            designs.append(data_file)
    return designs[0] if designs else None


def rename_kitchen(folder, design, new_name):
    """Rename a template folder and the design inside it. Returns (ok, error).

    Renaming is a cheap metadata change — the docs are explicit that it modifies
    a DataFile WITHOUT creating a new version — and it doesn't disturb references,
    which are held by id. The folder is renamed first because it is the thing the
    designer looks for; if the design rename then fails they get a correctly named
    folder and a clear message, rather than the confusing reverse."""
    if not folder:
        return False, 'That template folder is no longer available.'
    new_name = clean_name(new_name)
    if not new_name:
        return False, 'No name given.'

    try:
        folder.name = new_name
    except Exception:
        return False, (f"Could not rename the folder to '{new_name}'. Someone may "
                       f"have it open, or a folder of that name already exists.")
    if not design:
        return True, (f"Renamed the folder, but no design was found inside it to "
                      f"rename. Check '{new_name}' in the Data Panel.")
    try:
        design.name = new_name
    except Exception:
        return True, (f"Renamed the folder to '{new_name}', but the design inside "
                      f"kept its old name — it may be open or in use.")
    return True, None


# ---------------------------------------------------------------------------
# Walking / copying
# ---------------------------------------------------------------------------
def count_files(folder, _depth=0):
    """Total files in a folder tree — used to size the progress dialog before a
    copy starts. Depth-capped like every walk here so a pathological tree can't
    hang Fusion."""
    total = 0
    if not folder or _depth > config.KITCHEN_MAX_DEPTH:
        return total
    try:
        total += folder.dataFiles.count
    except Exception:
        return total
    try:
        subs = folder.dataFolders
        for i in range(subs.count):
            total += count_files(subs.item(i), _depth + 1)
    except Exception:
        pass
    return total


def walk_files(folder, _prefix='', _depth=0):
    """Yield (DataFile, 'sub/folder/path') for every file in the tree, deepest
    paths included. The path is display-only (progress text, delete report)."""
    if not folder or _depth > config.KITCHEN_MAX_DEPTH:
        return
    try:
        files = folder.dataFiles
        for i in range(files.count):
            yield files.item(i), _prefix
    except Exception:
        pass
    try:
        subs = folder.dataFolders
        for i in range(subs.count):
            sub = subs.item(i)
            yield from walk_files(sub, f'{_prefix}{sub.name}/', _depth + 1)
    except Exception:
        pass


def copy_tree(src, dst, progress=None, _depth=0):
    """Copy every file and sub-folder of `src` into `dst`, recursively.

    DataFolder has no copy method, so folders are recreated with dataFolders.add
    and files copied one at a time. `progress(done, name)` is called after each
    file and may return False to cancel — a cancelled copy stops where it is and
    reports what it managed (the caller decides whether to keep or bin it).

    Returns a report dict: copied, failed [(name, path)], folders, cancelled.
    """
    report = {'copied': 0, 'failed': [], 'folders': 0, 'cancelled': False}
    _copy_into(src, dst, progress, report, '', _depth)
    return report


def _copy_into(src, dst, progress, report, path, depth):
    if depth > config.KITCHEN_MAX_DEPTH:
        report['failed'].append(('(tree too deep — not copied)', path))
        return
    try:
        files = src.dataFiles
        count = files.count
    except Exception:
        report['failed'].append(('(could not read folder)', path))
        return

    for i in range(count):
        if report['cancelled']:
            return
        try:
            data_file = files.item(i)
            name = data_file.name
        except Exception:
            report['failed'].append(('(unreadable file)', path))
            continue
        try:
            data_file.copy(dst)
            report['copied'] += 1
        except Exception:
            report['failed'].append((name, path))
        if progress and not progress(report['copied'] + len(report['failed']), name):
            report['cancelled'] = True
            return

    try:
        subs = src.dataFolders
        sub_count = subs.count
    except Exception:
        return

    for i in range(sub_count):
        if report['cancelled']:
            return
        try:
            sub = subs.item(i)
            new_sub = dst.dataFolders.add(sub.name)
        except Exception:
            report['failed'].append(('(folder) ' + _safe_name(subs, i), path))
            continue
        if not new_sub:
            report['failed'].append(('(folder) ' + _safe_name(subs, i), path))
            continue
        report['folders'] += 1
        _copy_into(sub, new_sub, progress, report, f'{path}{new_sub.name}/', depth + 1)


def _safe_name(collection, index):
    try:
        return collection.item(index).name
    except Exception:
        return '?'


# ---------------------------------------------------------------------------
# The kitchen stamp (document attributes)
# ---------------------------------------------------------------------------
# New Kitchen writes these onto the design document so Finish Kitchen knows,
# without asking, which folder is this kitchen's private library copy. Document
# attributes travel inside the .f3d and survive rename, move and re-open.
def stamp_kitchen(document, customer, library_folder, stamp=None):
    """Record customer / library-folder-id / creation date on the design. Returns
    False if the attributes couldn't be written (never fatal — Finish Kitchen
    falls back to finding the folder by name)."""
    try:
        attrs = document.attributes
        attrs.add(config.WC_GROUP, config.WC_KITCHEN_CUSTOMER, clean_name(customer))
        attrs.add(config.WC_GROUP, config.WC_KITCHEN_CREATED, stamp or today_stamp())
        attrs.add(config.WC_GROUP, config.WC_KITCHEN_LIB_FOLDER, library_folder.id)
        return True
    except Exception:
        return False


def stamp_customer(document, customer):
    """Record the customer on an already-created design — what New Kitchen writes
    when it renames a template into a real job. The creation date is re-stamped
    too, because the day the job STARTED is the day it got a customer, not the day
    the spare template happened to be built.

    Display only: Finish Kitchen falls back to the folder name, so this failing
    costs nothing. Returns True if the attributes were written."""
    try:
        attrs = document.attributes
        attrs.add(config.WC_GROUP, config.WC_KITCHEN_CUSTOMER, clean_name(customer))
        attrs.add(config.WC_GROUP, config.WC_KITCHEN_CREATED, today_stamp())
        return True
    except Exception:
        return False


def _attr(document, name):
    try:
        attr = document.attributes.itemByName(config.WC_GROUP, name)
        return attr.value if attr else None
    except Exception:
        return None


def kitchen_customer(document):
    """The customer this design belongs to, for display.

    Prefers the stamp New Kitchen writes; falls back to the name of the folder the
    design lives in, which IS the customer name once a template has been renamed.
    Returns '' for a design that still looks like an un-renamed template."""
    stamped = _attr(document, config.WC_KITCHEN_CUSTOMER)
    if stamped:
        return stamped
    try:
        folder_name = document.dataFile.parentFolder.name
    except Exception:
        return ''
    if _template_number(folder_name) is not None:
        return ''
    return folder_name


def is_unassigned_template(document):
    """True if this design is still a spare template nobody has claimed.

    Matters because a fresh template references NOTHING, so Finish Kitchen would
    read its whole Library as unused and wipe the copy that makes the template
    worth having. Judged by the folder name, which is exactly what New Kitchen
    changes when it assigns a template to a customer."""
    try:
        return _template_number(document.dataFile.parentFolder.name) is not None
    except Exception:
        return False


def find_library_folder(document):
    """(folder, how) for the Library folder belonging to an open kitchen design.

    Preferred route is the stamped folder id; the fallback looks for a folder
    named config.KITCHEN_LIBRARY_FOLDER_NAME beside the design, which covers
    kitchens created by hand. `how` is 'stamp', 'name' or None."""
    folder_id = _attr(document, config.WC_KITCHEN_LIB_FOLDER)
    if folder_id:
        try:
            folder = app.data.findFolderById(folder_id)
            if folder and folder.isValid:
                return folder, 'stamp'
        except Exception:
            pass
    try:
        parent = document.dataFile.parentFolder
    except Exception:
        return None, None
    folder = subfolder_by_name(parent, config.KITCHEN_LIBRARY_FOLDER_NAME)
    return (folder, 'name') if folder else (None, None)


# ---------------------------------------------------------------------------
# What the design actually uses
# ---------------------------------------------------------------------------
def referenced_file_ids(document):
    """Lineage ids of every design this document references, sub-assemblies
    included.

    allDocumentReferences is the deep list (a cabinet's own hardware counts as
    used, so Finish Kitchen never deletes a part out from under a placed
    cabinet); documentReferences is the shallow fallback on older builds. Ids are
    lineage ids, so they match regardless of which version is placed."""
    ids = set()
    for prop in ('allDocumentReferences', 'documentReferences'):
        try:
            refs = getattr(document, prop)
        except Exception:
            continue
        if not refs:
            continue
        try:
            for i in range(refs.count):
                try:
                    data_file = refs.item(i).dataFile
                    if data_file:
                        ids.add(data_file.id)
                except Exception:
                    continue
        except Exception:
            continue
        if ids:
            break
    return ids


def unused_files(library_folder, used_ids):
    """[(DataFile, name, 'sub/path')] for every library file the design doesn't
    reference — exactly what Finish Kitchen offers to delete."""
    out = []
    for data_file, path in walk_files(library_folder):
        try:
            if data_file.id in used_ids:
                continue
            out.append((data_file, data_file.name, path))
        except Exception:
            continue
    out.sort(key=lambda row: (row[2].lower(), row[1].lower()))
    return out


def delete_files(rows, progress=None):
    """Delete the given [(DataFile, name, path)] rows.

    deleteMe() refuses a file that another file references or that is open, so a
    cabinet still placed in the kitchen survives even if the reference scan
    missed it — the failure list is a safety net, not just an error report.

    Returns {'deleted': n, 'failed': [(name, path)], 'cancelled': bool}."""
    report = {'deleted': 0, 'failed': [], 'cancelled': False}
    for index, (data_file, name, path) in enumerate(rows):
        try:
            if data_file.deleteMe():
                report['deleted'] += 1
            else:
                report['failed'].append((name, path))
        except Exception:
            report['failed'].append((name, path))
        if progress and not progress(index + 1, name):
            report['cancelled'] = True
            break
    return report


def prune_empty_folders(folder, _depth=0):
    """Delete sub-folders left with no files and no folders, deepest first.
    `folder` itself is never deleted. Returns how many went."""
    removed = 0
    if not folder or _depth > config.KITCHEN_MAX_DEPTH:
        return removed
    try:
        subs = [folder.dataFolders.item(i) for i in range(folder.dataFolders.count)]
    except Exception:
        return removed
    for sub in subs:
        removed += prune_empty_folders(sub, _depth + 1)
        try:
            if sub.dataFiles.count == 0 and sub.dataFolders.count == 0:
                if sub.deleteMe():
                    removed += 1
        except Exception:
            continue
    return removed
