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

    Create Kitchen      Clients / Kitchens / "Kitchen Template <n>" / Library
    Template            <- recursive copy of Emaar Library / Kitchen Library
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
def find_project(project_name):
    """The named cloud project, checking the active hub first then every other
    hub the user can see. Returns None if no hub has it."""
    try:
        active = app.data.activeHub
        if active:
            found = _project_in_hub(active, project_name)
            if found:
                return found
        hubs = app.data.dataHubs
        for h in range(hubs.count):
            hub = hubs.item(h)
            if active and hub.id == active.id:
                continue  # already checked
            found = _project_in_hub(hub, project_name)
            if found:
                return found
    except Exception:
        pass
    return None


def _project_in_hub(hub, project_name):
    projects = hub.dataProjects
    for i in range(projects.count):
        project = projects.item(i)
        if project.name == project_name:
            return project
    return None


def all_project_names():
    """Every project name visible to the user — shown in the dialog when a
    configured project can't be found, so the exact spelling can be copied."""
    names = []
    try:
        hubs = app.data.dataHubs
        for h in range(hubs.count):
            projects = hubs.item(h).dataProjects
            for i in range(projects.count):
                names.append(projects.item(i).name)
    except Exception:
        pass
    return names


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

    config.LIBRARY_SOURCE_FOLDER names a folder inside config.LIBRARY_PROJECT_NAME;
    leave it empty to copy the whole project root instead. `error` is a
    ready-to-show sentence when the folder can't be resolved."""
    project = find_project(config.LIBRARY_PROJECT_NAME)
    if not project:
        return None, (f"Library project '{config.LIBRARY_PROJECT_NAME}' not found. "
                      f"Set LIBRARY_PROJECT_NAME in config.py to one of: "
                      f"{', '.join(all_project_names()) or '(no projects visible)'}")
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


def unique_folder_name(parent, name):
    """`name` if free inside `parent`, else 'name (2)', 'name (3)', … Two kitchens
    for the same customer on the same day must not collide."""
    if not subfolder_by_name(parent, name):
        return name
    for n in range(2, 100):
        candidate = f'{name} ({n})'
        if not subfolder_by_name(parent, candidate):
            return candidate
    return f'{name} ({datetime.datetime.now().strftime("%H%M%S")})'


# ---------------------------------------------------------------------------
# The kitchens root, and the templates waiting in it
# ---------------------------------------------------------------------------
def kitchens_root():
    """(folder, error) for the folder that holds every template and job folder —
    config.KITCHENS_PROJECT_NAME / config.KITCHENS_FOLDER_NAME.

    The sub-folder is CREATED if it's missing (a fresh Clients project has no
    Kitchens folder yet), but the project is never created: a wrong project name
    should be reported, not silently worked around."""
    project = find_project(config.KITCHENS_PROJECT_NAME)
    if not project:
        return None, (f"Project '{config.KITCHENS_PROJECT_NAME}' not found. "
                      f"Set KITCHENS_PROJECT_NAME in config.py to one of: "
                      f"{', '.join(all_project_names()) or '(no projects visible)'}")
    root = project.rootFolder
    wanted = (config.KITCHENS_FOLDER_NAME or '').strip()
    if not wanted:
        return root, None

    folder = subfolder_by_name(root, wanted)
    if folder:
        return folder, None
    try:
        folder = root.dataFolders.add(wanted)
    except Exception:
        folder = None
    if not folder:
        return None, (f"Could not find or create the '{wanted}' folder in "
                      f"'{config.KITCHENS_PROJECT_NAME}'.")
    return folder, None


# "Kitchen Template 7" -> 7. Anchored, so "Kitchen Template 7 (old)" is NOT a
# template and won't be offered for renaming.
def _template_number(folder_name):
    prefix = (config.KITCHEN_TEMPLATE_PREFIX or '').strip()
    if not prefix:
        return None
    match = re.fullmatch(rf'{re.escape(prefix)}\s+(\d+)', (folder_name or '').strip(),
                         re.IGNORECASE)
    return int(match.group(1)) if match else None


def template_folders(root):
    """Every waiting template in `root`, lowest number first, as dicts:
    {'folder', 'number', 'name', 'design', 'cabinets'}.

    `design` is the design DataFile sitting directly in the template folder (None
    if someone deleted it); `cabinets` counts the files in its Library copy so the
    New Kitchen dialog can show a template is actually stocked."""
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
            number = _template_number(folder.name)
        except Exception:
            continue
        if number is None:
            continue
        library = subfolder_by_name(folder, config.KITCHEN_LIBRARY_FOLDER_NAME)
        out.append({
            'folder': folder,
            'number': number,
            'name': folder.name,
            'design': design_file_in(folder),
            'cabinets': count_files(library) if library else 0,
        })
    out.sort(key=lambda row: row['number'])
    return out


def _created_at(data_file):
    """DataFile.dateCreated as UNIX epoch seconds, or 0 when unavailable."""
    try:
        return int(data_file.dateCreated) if data_file else 0
    except Exception:
        return 0


def latest_template(root):
    """The template New Kitchen will claim: the most recently built one.

    Newest by the design's creation date, because template NUMBERS are reused as
    gaps open up — "Kitchen Template 2" can easily be newer than 3 — so the
    highest number is not the newest folder. The number only breaks ties.

    Templates whose Library is empty (a copy that was cancelled part-way) are
    skipped while any stocked one exists, so a half-built spare doesn't get handed
    to a customer. Returns None when nothing is waiting."""
    rows = template_folders(root)
    if not rows:
        return None
    usable = [row for row in rows if row['cabinets']] or rows
    usable.sort(key=lambda row: (_created_at(row['design']), row['number']))
    return usable[-1]


def next_template_name(root):
    """The next template name: the LOWEST unused number, not max+1.

    Templates are consumed by being renamed, so gaps open up constantly. Filling
    them keeps the numbers small and readable instead of drifting to
    'Kitchen Template 47' after a busy month."""
    prefix = (config.KITCHEN_TEMPLATE_PREFIX or 'Kitchen Template').strip()
    taken = {row['number'] for row in template_folders(root)}
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
