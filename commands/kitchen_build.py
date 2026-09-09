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

"""Building one kitchen folder from scratch — folder, Library copy, design.

    <name>/
        <name>          a new hybrid design, saved
        Library/        a copy of the master cabinet library

TWO CALLERS, deliberately one implementation:

* **Create Kitchen Template** builds spares named ``Kitchen Template <n>`` and
  closes each one — nobody works in a template.
* **New Kitchen** falls back to this when no template is waiting, building the
  customer's job directly under its real name. Without that fallback the very
  first person to press New Kitchen on a fresh install hits "no templates" and is
  stuck, which is exactly the moment you can least afford to confuse someone.

This lives apart from kitchen_data because it creates DOCUMENTS: kitchen_data is
strictly about files and folders and imports no adsk.fusion. It stays apart from
the command modules because duplicating it would let the two paths drift, and a
kitchen built by the fallback must be indistinguishable from one built from a
template — same layout, same stamps, so Finish Kitchen can't tell them apart.

No UI in here either: callers pass a `progress` callback and get a plain report
dict back, so the progress dialog and the wording stay in the commands.
"""

import adsk.core
import adsk.fusion

from . import kitchen_data
from .. import config

app = adsk.core.Application.get()


def build_kitchen(parent, source, name, customer='', progress=None, keep_open=False):
    """Build one kitchen folder called `name` inside `parent`.

    `source` is the master library folder to copy; `progress(done, filename)` is
    called per file and may return False to cancel. `customer` is stamped on the
    design when the caller already knows it (the New Kitchen fallback does; Create
    Kitchen Template doesn't yet). `keep_open` leaves the design open and active.

    Returns a report dict — `error` is a fragment meant to be dropped into a
    sentence ("could not create the folder"), never a whole message, because the
    two callers word things differently:

        {'folder', 'library', 'document',
         'copied', 'failed', 'folders', 'cancelled', 'error'}

    A cancelled or failed build leaves whatever it managed in place rather than
    trying to unwind it: half-built folders are visible in the Data Panel and easy
    to delete, whereas an automatic clean-up that itself failed part-way would be
    worse than the mess it was tidying.
    """
    report = {'folder': None, 'library': None, 'document': None,
              'copied': 0, 'failed': [], 'folders': 0,
              'cancelled': False, 'error': None}

    try:
        folder = parent.dataFolders.add(name)
    except Exception:
        folder = None
    if not folder:
        report['error'] = 'the folder could not be created'
        return report
    report['folder'] = folder

    try:
        library = folder.dataFolders.add(config.KITCHEN_LIBRARY_FOLDER_NAME)
    except Exception:
        library = None
    if not library:
        report['error'] = (f"the '{config.KITCHEN_LIBRARY_FOLDER_NAME}' folder "
                           f"could not be created inside it")
        return report
    report['library'] = library

    copied = kitchen_data.copy_tree(source, library, progress=progress)
    for key in ('copied', 'failed', 'folders', 'cancelled'):
        report[key] = copied[key]
    if copied['cancelled']:
        return report

    document, error = _new_design(name, folder, library, customer, keep_open)
    report['document'] = document
    report['error'] = error
    return report


def _new_design(name, folder, library, customer, keep_open):
    """Create the hybrid design, stamp it and save it into `folder`.
    Returns (document, error-fragment-or-None)."""
    try:
        document = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    except Exception:
        return None, 'the design could not be created'

    # Documents.add already creates a hybrid design; this is belt-and-braces in
    # case a future default changes, and is harmless when it's already hybrid.
    try:
        design = document.products.itemByProductType('DesignProductType')
        design.designIntent = adsk.fusion.DesignIntentTypes.HybridDesignIntentType
    except Exception:
        pass

    # The library folder's ID, stamped on the document, is what lets Finish
    # Kitchen find this Library later — after any amount of renaming.
    kitchen_data.stamp_kitchen(document, customer, library)

    description = (f'Kitchen for {customer} — created by WoodCraft.' if customer
                   else 'Kitchen template — created by WoodCraft.')
    try:
        document.saveAs(name, folder, description, '')
    except Exception:
        return document, 'the design could not be saved into the folder'

    if not keep_open:
        try:
            document.close(False)
        except Exception:
            pass                                # harmless: it is already saved
    return document, None
