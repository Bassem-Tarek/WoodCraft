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

"""FCC Export — write the nesting XML a Nanxing panel line imports.

The same panels the Cut List collects, laid out on the same stock sheets by the
same guillotine nester, written as the machine's job packet instead of a report:
the layout for the router, the drilling and grooving for the CNC borer, the
edgebanding spec for the bander, and the order data the labels carry.

What the line's own software still does: choose the tool, generate the NC
program, place the labels and round the corners. Those are the fields that tell
a post-nest file from a pre-nest one, and this command deliberately writes the
pre-nest form. See docs/FCC_EXPORT.md.

The heavy lifting lives in two modules that can be tested without Fusion:
`commands/fcc_writer.py` (the format) and `commands/fcc_features.py` (B-Rep to
flat-panel holes and grooves).
"""

import os
import datetime

import adsk.core
import adsk.fusion

from .. import ui_helpers
from .. import panels
from .. import nesting
from .. import sheets_store
from .. import fcc_writer
from .. import fcc_features
from ...lib import fusionAddInUtils as futil
from ... import config

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_fccExport'
CMD_NAME = 'FCC Export'
CMD_Description = (
    'Nest the design\'s panels and write the FCC XML a Nanxing production line '
    'imports — cutting layout, drilling, grooving and edgebanding in one file. '
    'Panels match the Sheets library by material + thickness, exactly as Cut List does.'
)
IS_PROMOTED = False

PANEL_ID = config.OUTPUT_PANEL_ID
PANEL_NAME = config.OUTPUT_PANEL_NAME
ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

JOB_ID = 'fcc_job'
SCOPE_ID = 'fcc_scope'
PREFIX_ID = 'fcc_prefix'
PREMILL_ID = 'fcc_premill'
TOOL_ID = 'fcc_tool'
BAND_ID = 'fcc_band'
ORDER_ID = 'fcc_order'
CUSTOMER_ID = 'fcc_customer'
BATCH_ID = 'fcc_batch'
INFO_ID = 'fcc_info'
PICK_GROUP_ID = 'fcc_pickers'
META_GROUP_ID = 'fcc_meta'

# Pre-milling removed from a banded edge before the tape goes on. The production
# sample runs 0.3 mm under 0.8 mm tape; WoodCraft's edgeband library has no such
# field, so it is a job setting rather than a per-band one.
DEFAULT_PREMILL_MM = 0.3
DEFAULT_TOOL_DIAMETER_MM = 6.0
NO_BAND = '(leave bare)'

local_handlers = []
_pickers = []
# Occurrences picked in the scope selector, captured WHILE THE DIALOG IS OPEN.
# Fusion releases a SelectionCommandInput's selections before `execute` runs, so
# reading them there can hand back a count with no entities behind it — which
# would silently export nothing. `inputChanged` fires while they are still live.
_scope_components = []


def start():
    cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_Description,
                                                        ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)
    panel = ui_helpers.get_panel(PANEL_ID, PANEL_NAME)
    control = panel.controls.addCommand(cmd_def)
    control.isPromoted = IS_PROMOTED


def stop():
    ui_helpers.remove_command(PANEL_ID, CMD_ID)


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------
def _sheet_label(sheet):
    return (f"{sheet.get('name') or 'Sheet'} — "
            f"{fcc_writer.num(sheet.get('length'))} × {fcc_writer.num(sheet.get('width'))}")


def command_created(args: adsk.core.CommandCreatedEventArgs):
    futil.log(f'{CMD_NAME} Command Created Event')
    inputs = args.command.commandInputs
    design = adsk.fusion.Design.cast(app.activeProduct)

    default_job = ''
    if design:
        try:
            default_job = design.parentDocument.name
        except Exception:
            default_job = ''
    inputs.addStringValueInput(JOB_ID, 'Job name', default_job)

    scope = inputs.addSelectionInput(
        SCOPE_ID, 'Assemblies (optional)',
        'Limit the export to the selected assemblies (pick one or more, e.g. a few '
        'cabinets from a kitchen); leave empty for the whole design. In a single-cabinet '
        'document the panels are already the top level, so leave this empty.')
    scope.addSelectionFilter('Occurrences')
    scope.setSelectionLimits(0, 0)

    inputs.addStringValueInput(PREFIX_ID, 'Barcode prefix', _default_prefix(default_job))
    inputs.addValueInput(TOOL_ID, 'Router tool Ø (mm)', 'mm',
                         adsk.core.ValueInput.createByReal(DEFAULT_TOOL_DIAMETER_MM / 10.0))
    inputs.addValueInput(PREMILL_ID, 'Edge pre-milling (mm)', 'mm',
                         adsk.core.ValueInput.createByReal(DEFAULT_PREMILL_MM / 10.0))

    bands = sheets_store.load().get('edgebands') or []
    band_dd = inputs.addDropDownCommandInput(BAND_ID, 'Band untagged edges with',
                                             adsk.core.DropDownStyles.TextListDropDownStyle)
    band_dd.listItems.add(NO_BAND, True)
    for band in bands:
        band_dd.listItems.add(band['name'], False)
    band_dd.tooltip = (
        'Panels whose edges the Edgeband command has not tagged are exported bare. '
        'Pick a band here to apply it to every edge of every untagged panel instead — '
        'useful for a whole-carcass material where every edge takes the same tape.')

    meta = inputs.addGroupCommandInput(META_GROUP_ID, 'Order data (printed on labels)')
    meta.isExpanded = False
    meta.children.addStringValueInput(ORDER_ID, 'Order no.', '')
    meta.children.addStringValueInput(CUSTOMER_ID, 'Customer', '')
    meta.children.addStringValueInput(BATCH_ID, 'Batch no.', '')

    info = inputs.addTextBoxCommandInput(INFO_ID, '', '', 4, True)
    try:
        info.isFullWidth = True
    except Exception:
        pass

    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.inputChanged, command_input_changed,
                      local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)

    global _scope_components
    _scope_components = []
    _build_pickers(inputs, design)


def command_input_changed(args: adsk.core.InputChangedEventArgs):
    """Keep a live copy of the scope selection. See `_scope_components`."""
    try:
        if args.input.id == SCOPE_ID:
            _capture_scope(args.input)
    except Exception:
        futil.log(f'{CMD_NAME}: could not read the scope selection')


def _capture_scope(scope_input):
    """Resolve the selector's occurrences to components, keeping the previous
    capture if the selection can't be read at all."""
    global _scope_components
    found = []
    try:
        count = scope_input.selectionCount
    except Exception:
        return
    for i in range(count):
        try:
            occurrence = adsk.fusion.Occurrence.cast(scope_input.selection(i).entity)
        except Exception:
            continue
        if occurrence:
            found.append(occurrence.component)
    _scope_components = found


def _default_prefix(job_name):
    letters = ''.join(ch for ch in str(job_name).upper() if ch.isalnum())[:3]
    return letters or 'WC'


def _build_pickers(inputs, design):
    """A sheet-picker per matched material that stocks more than one sheet, plus a
    plain-text summary of what will and won't export. Mirrors Cut List so the two
    commands agree about which sheet a material nests on."""
    global _pickers
    _pickers = []

    materials = sheets_store.load()['materials']
    groups = panels.group_by_material_thickness(
        panels.collect_panel_instances(design)) if design else []
    for group in groups:
        group['mat'] = sheets_store.find_material(materials, group['material'],
                                                  group['thickness'])
    matched = [g for g in groups if g['mat']]
    unmatched = [g for g in groups if not g['mat']]

    if not groups:
        lines = ['No panels found. Build or convert panels, then reopen.']
    else:
        pieces = sum(len(g['items']) for g in matched)
        lines = [f'{pieces} panel(s) across {len(matched)} stock material(s) will export.']
        if unmatched:
            names = ', '.join(f"{g['material']} ({g['thickness']:.0f} mm)" for g in unmatched)
            lines.append(f'Skipped — no stock material: {names}. '
                         f'Add them in the Sheets palette (+ From design).')
    inputs.itemById(INFO_ID).text = '\n'.join(lines)

    multi = [g for g in matched if len(g['mat'].get('sheets') or []) >= 2]
    if not multi:
        return
    group_input = inputs.addGroupCommandInput(PICK_GROUP_ID, 'Sheet to nest on')
    group_input.isExpanded = True
    for idx, group in enumerate(multi):
        sheets = group['mat']['sheets']
        dd = group_input.children.addDropDownCommandInput(
            f'fcc_pick_{idx}', f"{group['material']} {group['thickness']:.0f}mm",
            adsk.core.DropDownStyles.TextListDropDownStyle)
        for i, sheet in enumerate(sheets):
            dd.listItems.add(_sheet_label(sheet), i == 0)
        _pickers.append({'key': group['key'], 'picker_id': dd.id,
                         'sheet_names': [s.get('name') for s in sheets]})


def _read_choices(inputs):
    choices = {}
    for picker in _pickers:
        dd = inputs.itemById(picker['picker_id'])
        if dd and dd.selectedItem:
            index = dd.selectedItem.index
            if 0 <= index < len(picker['sheet_names']):
                choices[picker['key']] = picker['sheet_names'][index]
    return choices


def _pick_sheet(material, key, choices):
    sheets = (material or {}).get('sheets') or []
    if not sheets:
        return None
    wanted = choices.get(key)
    if wanted:
        for sheet in sheets:
            if sheet.get('name') == wanted:
                return sheet
    return sheets[0]


# ---------------------------------------------------------------------------
# Feature extraction, cached per component
# ---------------------------------------------------------------------------
class FeatureCache:
    """Holes, grooves and banded edges per COMPONENT.

    A kitchen inserts the same cabinet many times and the same panel many times
    inside it; the B-Rep walk is the expensive part of this command, so each
    unique component is read once. `tagged` comes from one findAttributes sweep
    per design, as panels.design_band_faces does for the Cut List."""

    def __init__(self):
        self._by_component = {}
        self._band_faces = {}
        self.unreliable = []

    def _tagged_faces(self, component):
        try:
            design = component.parentDesign
            key = design.parentDocument.name
        except Exception:
            return None
        if key not in self._band_faces:
            self._band_faces[key] = panels.design_band_faces(design)
        sweep = self._band_faces[key]
        if sweep is None:
            return None         # the sweep isn't available — fall back to a face scan
        # A component missing from a SUCCESSFUL sweep simply has no tagged faces;
        # saying so costs nothing, where returning None would re-scan every face
        # of every untagged panel and undo the point of the sweep.
        return sweep.get(component.name, [])

    def get(self, component):
        try:
            key = component.entityToken
        except Exception:
            key = component.name
        if key in self._by_component:
            return self._by_component[key]

        frame = fcc_features.panel_frame(component)
        if not frame:
            result = None
        else:
            if not fcc_features.frame_is_reliable(component, frame):
                self.unreliable.append(component.name)
            result = {
                'frame': frame,
                'holes': fcc_features.holes(component, frame),
                'slots': fcc_features.slots(component, frame),
                'bands': fcc_features.banded_faces(component, frame,
                                                   tagged=self._tagged_faces(component)),
            }
        self._by_component[key] = result
        return result


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------
def command_execute(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Execute Event')
    inputs = args.command.commandInputs

    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        ui.messageBox('Open a design first.', CMD_NAME)
        return

    job_name = (inputs.itemById(JOB_ID).value or 'job').strip()
    prefix = (inputs.itemById(PREFIX_ID).value or 'WC').strip()
    tool_diameter = inputs.itemById(TOOL_ID).value * 10.0
    premill = max(0.0, inputs.itemById(PREMILL_ID).value * 10.0)
    band_item = inputs.itemById(BAND_ID).selectedItem
    default_band = band_item.name if band_item and band_item.name != NO_BAND else None

    meta = {}
    for key, input_id in (('OrderNo', ORDER_ID), ('Customer', CUSTOMER_ID),
                          ('BatchNo', BATCH_ID)):
        field = inputs.itemById(input_id)
        if field and field.value.strip():
            meta[key] = field.value.strip()
    meta['OrderDate'] = datetime.date.today().isoformat()

    scope_input = inputs.itemById(SCOPE_ID)
    instances = _collect(design, scope_input)
    if not instances:
        ui.messageBox(_nothing_found(design), CMD_NAME)
        return

    library = sheets_store.load()
    choices = _read_choices(inputs)
    cache = FeatureCache()
    job, skipped = _build_job(instances, library, choices, cache, meta,
                              tool_diameter, premill, default_band, prefix)

    if not job['materials']:
        ui.messageBox('None of these panels match a stock material in the Sheets '
                      'library, so there is nothing to nest.\n\n'
                      'Add the materials in the Sheets palette (+ From design).', CMD_NAME)
        return

    path = _ask_where_to_save(job_name)
    if not path:
        return
    xml_text = fcc_writer.build(job)
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(xml_text)
    except OSError as error:
        ui.messageBox(f'Could not write the file:\n{error}', CMD_NAME)
        return

    warnings = fcc_writer.check(job)
    warnings.extend(skipped)
    if cache.unreliable:
        names = ', '.join(sorted(set(cache.unreliable))[:6])
        warnings.append(
            f'Not square to their own component, so their hole positions may be wrong: '
            f'{names}. Model panels axis-aligned inside their component.')
    ui.messageBox(_summary(job, path, warnings), CMD_NAME)


def _collect(design, scope_input):
    """Panels in scope. With cabinets selected, each is collected under its own
    name so parts are attributed to the cabinet they belong to; otherwise the
    whole design.

    Scope comes from the capture made while the dialog was open. One last read is
    attempted here in case `inputChanged` never fired, but an empty capture means
    the whole design — never an empty export."""
    if not _scope_components:
        _capture_scope(scope_input)
    if not _scope_components:
        return panels.collect_panel_instances(design)
    instances = []
    for component in _scope_components:
        instances.extend(panels.collect_panel_instances(
            design, root=component, root_name=component.name))
    return instances


def _nothing_found(design):
    """Why the export is empty, in terms the user can act on: whether anything is
    classified at all, and whether the scope is what excluded it."""
    try:
        in_design = len(panels.collect_panel_instances(design))
    except Exception:
        in_design = -1
    scope = ', '.join(c.name for c in _scope_components) if _scope_components else ''

    if in_design == 0:
        return ('No panels to export.\n\n'
                'Nothing in this design is classified as a WoodCraft panel. Classify '
                'parts with Set Type, or build them with Carcass Maker / Shelf Creator, '
                'then try again.\n\n'
                'If the panels live in referenced cabinets, open the assembly that '
                'contains them rather than a drawing or an empty document.')
    if scope:
        return (f'No panels to export.\n\n'
                f'This design has {in_design} classified panel(s), but none of them are '
                f'inside the selected cabinet(s): {scope}.\n\n'
                f'Clear the "Assemblies" box to export the whole design, or select the '
                f'assemblies that actually contain the panels. In a single-cabinet '
                f'document the panels are the top level, so the box should be empty.')
    return ('No panels to export.\n\n'
            f'This design reports {in_design} classified panel(s) but none could be '
            f'measured — a panel needs at least one visible body of its own. Check that '
            f'the panels are not hidden and that their geometry lives on the component '
            f'itself rather than only in its children.')


def _grain(sheet):
    """'L' when the stock may not be turned (a grain direction to respect), else
    'N'. WoodCraft records grain as the sheet's rotation rule rather than a panel
    attribute, so that rule is what the machine is told."""
    return 'N' if sheets_store.rotation_allows_rotation(sheet.get('rotation')) else 'L'


def _edge_spec(band_name, library, premill):
    """One EdgeGroup entry from a band name, or None when the edge is bare."""
    if not band_name:
        return None
    band = sheets_store.find_edgeband(library.get('edgebands') or [], band_name)
    thickness = float((band or {}).get('thickness') or 0.0)
    width = float((band or {}).get('width') or 0.0)
    return {
        'name': band_name,
        'thickness': thickness,
        'width': width,
        'pre_milling': min(premill, thickness) if thickness else 0.0,
        'code': _band_code(band_name),
        'grade': 'A',
    }


def _band_code(name):
    """A short machine-side code for a band, from its library name: letters and
    digits only, upper-cased, at most 10 characters ('PVC White 0.8 mm' ->
    'PVCWHITE08'). The edgebander matches on this, so it has to be stable — it is
    derived from the name, which is also what the face tag stores."""
    cleaned = ''.join(ch for ch in str(name).upper() if ch.isalnum())
    return cleaned[:10] or 'BAND'


def _build_job(instances, library, choices, cache, meta, tool_diameter, premill,
               default_band, prefix):
    """Group, match, nest, and turn every placement into a writer workpiece."""
    materials = library['materials']
    job = {'meta': meta, 'materials': []}
    skipped = []
    serial = 0

    for group in panels.group_by_material_thickness(instances):
        material = sheets_store.find_material(materials, group['material'],
                                              group['thickness'])
        if not material:
            skipped.append(f"Skipped {len(group['items'])} panel(s) in "
                           f"{group['material']} {group['thickness']:.0f} mm — "
                           f"no matching stock material.")
            continue
        sheet = _pick_sheet(material, group['key'], choices)
        if not sheet:
            skipped.append(f"Skipped {group['material']} — the stock material has no sheet.")
            continue

        gap = max(0.0, float(sheet.get('separation') or 0.0))
        trim = max(0.0, float(sheet.get('trim') or 0.0))
        allow_rotation = sheets_store.rotation_allows_rotation(sheet.get('rotation'))

        rects = []
        for index, item in enumerate(group['items']):
            rects.append({'id': index, 'label': item['comp_name'],
                          'parent': (item.get('parent') or '').strip(),
                          'w': item['L'], 'h': item['W']})
        packed = nesting.pack(rects, sheet['length'], sheet['width'], gap, trim,
                              allow_rotation)
        if packed['unplaced']:
            names = ', '.join(sorted({r['label'] for r in packed['unplaced']}))
            skipped.append(f"Too big for a {sheet.get('name') or 'stock'} sheet, "
                           f"so not exported: {names}.")

        entry = {
            'name': material['name'],
            'length': sheet['length'], 'width': sheet['width'],
            'thickness': material['thickness'], 'grain': _grain(sheet),
            'tool_diameter': tool_diameter, 'separation': gap, 'trim': trim,
            'sheets': [],
        }
        for packed_sheet in packed['sheets']:
            workpieces = []
            for order, placement in enumerate(packed_sheet['placements'], start=1):
                serial += 1
                item = group['items'][placement['id']]
                workpieces.append(_workpiece(item, placement, trim, order, serial,
                                             prefix, material, sheet, cache, library,
                                             premill, default_band))
            entry['sheets'].append({'workpieces': workpieces, 'oddments': []})
        if entry['sheets']:
            job['materials'].append(entry)

    return job, skipped


def _workpiece(item, placement, trim, order, serial, prefix, material, sheet, cache,
               library, premill, default_band):
    """One writer-ready workpiece from one nester placement.

    The nester works inside the trimmed area, so its x/y are offset by the trim
    to reach sheet coordinates. Its `w`/`h` are the part as placed; `rotated`
    says the design-frame length now runs along sheet Y, which is what the
    writer needs to re-key the faces and the feature coordinates."""
    component = item['component']
    features = cache.get(component)
    frame = features['frame'] if features else None

    bands = dict(features['bands']) if features else {}
    if not bands and default_band:
        bands = {face: default_band for face in (1, 2, 3, 4)}
    edges = {face: _edge_spec(name, library, premill) for face, name in bands.items()}

    label = panels.instance_label(item)
    return {
        'workpiece_id': f'{prefix}{serial:05d}',
        'name': label,
        'material': material['name'],
        'length': frame.length if frame else item['L'],
        'width': frame.width if frame else item['W'],
        'thickness': frame.thickness if frame else item['T'],
        'x': trim + placement['x'],
        'y': trim + placement['y'],
        'rotated': bool(placement.get('rotated')),
        'grain': _grain(sheet),
        'cutting_order': order,
        'edges': edges,
        'holes': features['holes'] if features else [],
        'slots': features['slots'] if features else [],
        'memo': item.get('parent') or '',
        'meta': {'PanelNo': item['comp_name']},
    }


def _ask_where_to_save(job_name):
    dialog = ui.createFileDialog()
    dialog.title = 'Save FCC nesting file'
    dialog.filter = 'FCC nesting XML (*.xml)'
    dialog.filterIndex = 0
    dialog.isMultiSelectEnabled = False
    stamp = datetime.date.today().strftime('%Y%m%d')
    safe = ''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in job_name)[:40]
    dialog.initialFilename = f'{safe or "job"}-FccPattern-{stamp}.xml'
    if dialog.showSave() != adsk.core.DialogResults.DialogOK:
        return None
    return dialog.filename


def _summary(job, path, warnings):
    sheets = sum(len(m['sheets']) for m in job['materials'])
    pieces = sum(len(s['workpieces']) for m in job['materials'] for s in m['sheets'])
    holes = sum(len(w['holes']) for m in job['materials'] for s in m['sheets']
                for w in s['workpieces'])
    slots = sum(len(w['slots']) for m in job['materials'] for s in m['sheets']
                for w in s['workpieces'])
    lines = [f'Written to:\n{path}', '',
             f'{pieces} panel(s) on {sheets} sheet(s) across '
             f'{len(job["materials"])} material(s)',
             f'{holes} hole(s), {slots} groove(s)']
    if warnings:
        lines.append('')
        lines.append('Check before running:')
        lines.extend(f'  • {w}' for w in warnings[:12])
        if len(warnings) > 12:
            lines.append(f'  • …and {len(warnings) - 12} more')
    return '\n'.join(lines)


def command_destroy(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Destroy Event')
    global local_handlers, _pickers, _scope_components
    local_handlers = []
    _pickers = []
    _scope_components = []
