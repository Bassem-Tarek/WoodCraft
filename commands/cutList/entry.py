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

"""Cut List & Nest — collect WoodCraft panels and produce a cut-list / nest report.

Panels are collected via the shared collector (commands/panels.py), grouped by
**(Fusion material name, thickness)**, and each group is matched to a material in the
global Sheets library (commands/sheets_store.py). Each matched group is nested on one
of that material's stock sheets — when a material has more than one sheet, the dialog
shows a dropdown so you pick which sheet to nest on (per-sheet params: item
separation → part gap, edge trim, rotation). The report is colour-coded per material
(the colour set in the Sheets palette) so it reads at a glance, with per-sheet SVG
layouts, yield, cost, a printable label sheet, and warnings for unmatched/oversized
parts.
"""

import os
import pathlib
import tempfile
import webbrowser
import datetime

import adsk.core
import adsk.fusion

from .. import ui_helpers
from .. import panels
from .. import nesting
from .. import sheets_store
from .. import report_utils
from ...lib import fusionAddInUtils as futil
from ... import config

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_cutList'
CMD_NAME = 'Cut List & Nest'
CMD_Description = (
    'Collect all WoodCraft panels and open a colour-coded cut-list & nesting report. '
    'Panels match the Sheets library by material + thickness; pick the stock sheet to '
    'nest on when a material has several.'
)
IS_PROMOTED = True

PANEL_ID = config.OUTPUT_PANEL_ID
PANEL_NAME = config.OUTPUT_PANEL_NAME
ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

NAME_ID = 'cl_name'
QTY_ID = 'cl_qty'
SCOPE_ID = 'cl_scope'
INFO_ID = 'cl_info'
PICK_GROUP_ID = 'cl_pickers'

UNMATCHED_COLOR = '#c2c2c2'

local_handlers = []
# Per-invocation pickers: [{'key': (name_lower, thickness), 'picker_id', 'sheet_names'}].
_pickers = []
_dialog_found = None     # panels + hardware read when the dialog opened


def start():
    cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_Description, ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)

    panel = ui_helpers.get_panel(PANEL_ID, PANEL_NAME)
    control = panel.controls.addCommand(cmd_def)
    control.isPromoted = IS_PROMOTED


def stop():
    ui_helpers.remove_command(PANEL_ID, CMD_ID)


# ---------------------------------------------------------------------------
# Grouping + matching helpers (shared by the dialog and the report)
# ---------------------------------------------------------------------------
# Shared with BOM (defined once in panels.py).
_group_instances = panels.group_by_material_thickness


def _sheet_label(sheet):
    return f"{sheet.get('name', 'Sheet')} ({sheet['length']:.0f}×{sheet['width']:.0f})"


# Shared with BOM (defined once in panels.py).
_panel_label = panels.instance_label


def _pick_sheet(material, key, choices):
    """The chosen sheet for a material: the dropdown selection if any, else its
    first/primary sheet. None if the material has no sheets."""
    sheets = material.get('sheets') or []
    if not sheets:
        return None
    want = choices.get(key)
    if want:
        for sh in sheets:
            if sh.get('name') == want:
                return sh
    return sheets[0]


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------
def command_created(args: adsk.core.CommandCreatedEventArgs):
    futil.log(f'{CMD_NAME} Command Created Event')
    inputs = args.command.commandInputs

    design = adsk.fusion.Design.cast(app.activeProduct)
    default_name = ''
    if design:
        try:
            default_name = design.parentDocument.name
        except Exception:
            default_name = ''
    inputs.addStringValueInput(NAME_ID, 'Report name', default_name)
    inputs.addIntegerSpinnerCommandInput(QTY_ID, 'Quantity (assemblies)', 1, 1000, 1, 1)

    scope = inputs.addSelectionInput(
        SCOPE_ID, 'Assemblies (optional)',
        'Limit the cut list to the selected assemblies (pick one or more, e.g. a few '
        'cabinets from a kitchen); leave empty for the whole design')
    scope.addSelectionFilter('Occurrences')
    scope.setSelectionLimits(0, 0)   # 0 max = unlimited

    info = inputs.addTextBoxCommandInput(INFO_ID, '', '', 4, True)
    try:
        info.isFullWidth = True
    except Exception:
        pass

    # Register handlers BEFORE building dynamic pickers: command_created swallows
    # exceptions, so a throw while building pickers must not strip the handlers.
    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)

    _build_pickers(inputs, design)


def _build_pickers(inputs, design):
    """Group the whole design's panels, match to the library, and add a sheet-picker
    dropdown for every matched material that has MORE THAN ONE sheet (single-sheet
    and unmatched materials need no choice). Whole-design grouping is a superset of
    any scoped subset, so the pickers cover every group execute might see."""
    global _pickers, _dialog_found
    _pickers = []

    materials = sheets_store.load()['materials']
    # Panels AND purchased items in one walk, kept for OK: the model can't change
    # while this dialog is open, so a whole-design report reuses this walk.
    _dialog_found = (panels.collect_instances(
        design, categories={config.WC_CAT_PANEL, config.WC_CAT_HARDWARE})
        if design else [])
    groups = _group_instances([it for it in _dialog_found
                               if it['category'] == config.WC_CAT_PANEL]) if design else []
    for g in groups:
        g['mat'] = sheets_store.find_material(materials, g['material'], g['thickness'])

    matched = [g for g in groups if g['mat']]
    unmatched = [g for g in groups if not g['mat']]

    # This command-input textbox renders as PLAIN text (HTML tags show up raw),
    # so use plain text + newlines — no <b>/<br>.
    parts = [f"{len(groups)} material/thickness group(s); {len(matched)} matched to a stock material."]
    if unmatched:
        names = ', '.join(f"{g['material']} ({g['thickness']:.0f} mm)" for g in unmatched)
        parts.append(f"{len(unmatched)} with no stock material: {names}. "
                     f"Add them in the Sheets palette (+ From design).")
    inputs.itemById(INFO_ID).text = '\n'.join(parts) if groups else \
        'No panels found yet. Build/convert panels, then reopen.'

    multi = [g for g in matched if len(g['mat'].get('sheets') or []) >= 2]
    if not multi:
        return

    grp = inputs.addGroupCommandInput(PICK_GROUP_ID, 'Sheet to nest on')
    grp.isExpanded = True
    for idx, g in enumerate(multi):
        sheets = g['mat']['sheets']
        dd = grp.children.addDropDownCommandInput(
            f'cl_pick_{idx}', f"{g['material']} {g['thickness']:.0f}mm",
            adsk.core.DropDownStyles.TextListDropDownStyle)
        for i, sh in enumerate(sheets):
            dd.listItems.add(_sheet_label(sh), i == 0)
        _pickers.append({'key': g['key'], 'picker_id': dd.id,
                         'sheet_names': [sh.get('name') for sh in sheets]})


def _read_choices(inputs):
    """Map each picker's (material, thickness) key → chosen sheet name."""
    choices = {}
    for p in _pickers:
        dd = inputs.itemById(p['picker_id'])
        if dd and dd.selectedItem:
            idx = dd.selectedItem.index
            if 0 <= idx < len(p['sheet_names']):
                choices[p['key']] = p['sheet_names'][idx]
    return choices


def command_execute(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Execute Event')
    inputs = args.command.commandInputs

    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        ui.messageBox('Open a design first.')
        return

    scope = inputs.itemById(SCOPE_ID)
    roots = []
    for i in range(scope.selectionCount):
        try:
            roots.append(scope.selection(i).entity.component)
        except Exception:
            pass

    qty = max(1, int(inputs.itemById(QTY_ID).value))
    report_name = (inputs.itemById(NAME_ID).value or '').strip()
    if not report_name:
        try:
            report_name = design.parentDocument.name
        except Exception:
            report_name = 'WoodCraft'

    # ONE walk of the model collects panels and purchased items together (it used
    # to walk the design once for each), then they are split by category.
    wanted = {config.WC_CAT_PANEL, config.WC_CAT_HARDWARE}
    if roots:
        # Union of the selected assemblies. Each selected occurrence contributes its
        # own subtree once, so selecting two placements of the same cabinet counts
        # its panels twice (correct).
        found = []
        for comp in roots:
            found += panels.collect_instances(design, root=comp, categories=wanted,
                                              root_name=comp.name)
    elif _dialog_found is not None:
        found = list(_dialog_found)
    else:
        found = panels.collect_instances(design, categories=wanted)
    instances = [it for it in found if it['category'] == config.WC_CAT_PANEL]
    hw_instances = [it for it in found if it['category'] == config.WC_CAT_HARDWARE]
    if not instances:
        ui.messageBox('No panels found.\n\nClassify panels with Set Type, or build '
                      'them with Carcass Maker / Shelf Creator, then try again.')
        return
    instances = instances * qty   # global assembly quantity multiplier

    # Purchased items (hardware) for the same scope — listed on the report so the
    # shop has the full order alongside the cut sheets.
    hw_instances = hw_instances * qty
    hardware_rows = _hardware_rows(hw_instances)

    materials = sheets_store.load()['materials']
    choices = _read_choices(inputs)
    sections = _build_sections(instances, materials, choices)

    try:
        html = _build_html(sections, instances, report_name, materials, hardware_rows)
        out = os.path.join(tempfile.gettempdir(), _safe_filename(report_name) + '.html')
        with open(out, 'w', encoding='utf-8') as f:
            f.write(html)
        webbrowser.open(pathlib.Path(out).as_uri())
    except Exception:
        futil.handle_error('Cut List: failed to build/open the report', show_message_box=True)
        return

    total_sheets = sum(s['num_sheets'] for s in sections)
    total_unplaced = sum(len(s['unplaced']) for s in sections)
    unmatched = [s for s in sections if not s['matched']]
    total_unmatched = sum(s['pieces'] for s in unmatched)
    total_cost = sum(s['cost'] for s in sections)

    hw_count = sum(r['qty'] for r in hardware_rows)

    msg = (f'Cut list "{report_name}" (×{qty}): {len(instances)} panel(s) across '
           f'{len(sections)} material/thickness group(s) → {total_sheets} sheet(s).')
    if hw_count:
        msg += f'\n{hw_count} purchased item(s) listed.'
    if total_cost:
        msg += f'\nEstimated stock cost: {total_cost:.2f}.'
    if total_unmatched:
        groups = ', '.join(f"{s['material']} ({s['thickness']:.0f} mm)" for s in unmatched)
        msg += (f'\n⚠ {total_unmatched} panel(s) have no matching stock material: '
                f'{groups}. Add them in the Sheets palette (the "+ From design" button '
                f'creates them from the design), then give each a sheet.')
    if total_unplaced:
        msg += f'\n⚠ {total_unplaced} part(s) too large for their sheet (see report).'
    msg += '\n\nReport opened in your browser.'
    ui.messageBox(msg)


# ---------------------------------------------------------------------------
# Build sections (group → matched sheet → nest)
# ---------------------------------------------------------------------------
def _build_sections(instances, materials, choices):
    sections = []
    for g in _group_instances(instances):
        items = g['items']
        material, t, key = g['material'], g['thickness'], g['key']

        parts = {}
        area = 0.0
        rects = []
        for idx, it in enumerate(items):
            L, W = max(it['L'], it['W']), min(it['L'], it['W'])
            pk = (round(L, 1), round(W, 1))
            p = parts.setdefault(pk, {'L': pk[0], 'W': pk[1], 'qty': 0, 'names': {}})
            p['qty'] += 1
            label = _panel_label(it)
            p['names'][label] = p['names'].get(label, 0) + 1
            area += it['L'] * it['W']
            rects.append({'id': idx, 'label': it['comp_name'],
                          'parent': (it.get('parent') or '').strip(),
                          'w': it['L'], 'h': it['W']})
        rows = sorted(parts.values(), key=lambda p: (-p['L'], -p['W']))

        mat = sheets_store.find_material(materials, material, t)
        sheet = _pick_sheet(mat, key, choices) if mat else None
        section = {
            'material': material, 'thickness': t, 'rows': rows, 'pieces': len(items),
            'area_m2': area / 1.0e6, 'matched': sheet is not None, 'sheet': sheet,
            'color': (mat.get('color') if mat else None) or UNMATCHED_COLOR,
            'sheets': [], 'num_sheets': 0, 'yield': 0.0, 'unplaced': [],
            'usable_w': 0.0, 'usable_h': 0.0, 'cost': 0.0,
        }
        if sheet:
            gap = max(0.0, float(sheet.get('separation') or 0.0))
            trim = max(0.0, float(sheet.get('trim') or 0.0))
            allow_rot = sheets_store.rotation_allows_rotation(sheet.get('rotation'))
            result = nesting.pack(rects, sheet['length'], sheet['width'], gap, trim, allow_rot)
            num_sheets = len(result['sheets'])
            usable_area = result['usable_w'] * result['usable_h']
            placed_area = sum(nesting.sheet_used_area(sh) for sh in result['sheets'])
            yld = (placed_area / (num_sheets * usable_area) * 100.0) if (num_sheets and usable_area) else 0.0
            section.update({
                'sheets': result['sheets'], 'num_sheets': num_sheets, 'yield': yld,
                'unplaced': result['unplaced'], 'usable_w': result['usable_w'],
                'usable_h': result['usable_h'],
                'cost': num_sheets * float(sheet.get('cost', 0.0) or 0.0),
                'allow_rot': allow_rot, 'gap': gap, 'trim': trim,
            })
        sections.append(section)
    return sections


def _hardware_rows(hw_instances):
    """Purchased items grouped by component name (summed across cabinets — you buy
    them in total): each {name, qty, unit, line, used_in}. Empty if no hardware."""
    groups = {}
    order = []
    for it in hw_instances:
        name = it['comp_name']
        if name not in groups:
            groups[name] = {'name': name, 'qty': 0, 'unit': float(it.get('cost') or 0.0),
                            'parents': set()}
            order.append(name)
        groups[name]['qty'] += 1
        parent = (it.get('parent') or '').strip()
        if parent:
            groups[name]['parents'].add(parent)
    rows = []
    for name in order:
        g = groups[name]
        g['line'] = g['qty'] * g['unit']
        g['used_in'] = ', '.join(sorted(g['parents']))
        rows.append(g)
    rows.sort(key=lambda r: r['name'].lower())
    return rows


# Shared report helpers (defined once in report_utils.py).
_esc = report_utils.esc
_safe_filename = report_utils.safe_filename
_swatch = report_utils.swatch


def _hardware_section_html(hardware_rows, has_hw_cost, hw_cost):
    """The Purchased-items table for the cut-list report, or '' if no hardware."""
    if not hardware_rows:
        return ''
    show_cab = any(r['used_in'] for r in hardware_rows)
    out = ["<h2>Purchased items</h2><table><thead><tr><th class='l'>Item</th>"]
    if show_cab:
        out.append("<th class='l'>Used in</th>")
    out.append("<th>Qty</th>")
    if has_hw_cost:
        out.append("<th>Unit cost</th><th>Total</th>")
    out.append("</tr></thead><tbody>")
    for r in hardware_rows:
        out.append(f"<tr><td class='l'>{_esc(r['name'])}</td>")
        if show_cab:
            out.append(f"<td class='l'>{_esc(r['used_in'])}</td>")
        out.append(f"<td>{r['qty']}</td>")
        if has_hw_cost:
            out.append(f"<td>{r['unit']:.2f}</td><td>{r['line']:.2f}</td>")
        out.append("</tr>")
    if has_hw_cost:
        span = 3 if show_cab else 2
        out.append(f"<tr><td class='l' colspan='{span}'><b>Total</b></td>"
                   f"<td></td><td><b>{hw_cost:.2f}</b></td></tr>")
    out.append("</tbody></table>")
    return ''.join(out)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _build_html(sections, instances, title, materials, hardware_rows=None):
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    hardware_rows = hardware_rows or []
    total_pieces = len(instances)
    matched = [s for s in sections if s['matched']]
    total_sheets = sum(s['num_sheets'] for s in matched)
    total_unplaced = sum(len(s['unplaced']) for s in matched)
    total_unmatched = sum(s['pieces'] for s in sections if not s['matched'])

    board_m2 = sum(s['num_sheets'] * s['sheet']['length'] * s['sheet']['width']
                   for s in matched) / 1.0e6
    placed_m2 = sum(nesting.sheet_used_area(sh) for s in matched for sh in s['sheets']) / 1.0e6
    overall_yield = (placed_m2 / board_m2 * 100.0) if board_m2 else 0.0
    waste_m2 = max(0.0, board_m2 - placed_m2)
    total_cost = sum(s['cost'] for s in matched)
    has_cost = any((s['sheet'].get('cost') or 0) for s in matched)

    hw_count = sum(r['qty'] for r in hardware_rows)
    hw_cost = sum(r['line'] for r in hardware_rows)
    has_hw_cost = any(r['line'] for r in hardware_rows)

    css = """<style>
      body{font-family:'Segoe UI',Arial,sans-serif;margin:24px;color:#222;}
      h1{font-size:20px;margin:0 0 2px;} .sub{color:#777;font-size:12px;margin-bottom:14px;}
      .params{background:#f5f5f5;border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:14px;break-inside:avoid;}
      .legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin:8px 0 16px;font-size:12px;}
      .summary{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px;break-inside:avoid;}
      .summary div{background:#fff;border:1px solid #e3dcc4;border-radius:8px;padding:8px 14px;min-width:82px;text-align:center;}
      .summary span{display:block;font-size:11px;color:#888;} .summary b{font-size:17px;}
      h2{font-size:15px;margin:24px 0 6px;border-bottom:2px solid #E5C05B;padding-bottom:4px;break-after:avoid;}
      table{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:10px;}
      th,td{border:1px solid #ddd;padding:6px 8px;text-align:right;}
      th{background:#fafafa;} td.l,th.l{text-align:left;}
      thead{display:table-header-group;} tr{break-inside:avoid;}
      .section{break-inside:avoid;}
      .sheets{display:flex;flex-wrap:wrap;gap:18px;margin:8px 0 4px;align-items:flex-start;}
      .sheet{font-size:11px;color:#555;break-inside:avoid;} .sheet .cap{margin-bottom:3px;font-weight:600;color:#333;}
      .warn{color:#b00;font-size:12px;margin:6px 0;}
      .labels{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:8px;}
      .label{border:1px solid #ccc;border-radius:6px;padding:8px;font-size:12px;break-inside:avoid;border-left-width:6px;}
      .label .lc{color:#888;font-size:11px;text-transform:uppercase;letter-spacing:.3px;margin-bottom:1px;}
      .label .ln{font-weight:600;} .label .ld{color:#333;} .label .lt{color:#888;font-size:11px;}
      button{background:#E5C05B;border:none;border-radius:6px;padding:8px 14px;font-size:13px;cursor:pointer;margin-top:8px;}
      .pagebreak{page-break-before:always;}
      @media print{
        body{margin:12mm;-webkit-print-color-adjust:exact;print-color-adjust:exact;}
        button{display:none;}
        h1,h2,.summary,.params{break-inside:avoid;}
      }
    </style>"""

    summary_html = (
        "<div class='summary'>"
        f"<div><span>Panels</span><b>{total_pieces}</b></div>"
        f"<div><span>Sheets</span><b>{total_sheets}</b></div>"
        f"<div><span>Board area</span><b>{board_m2:.2f} m&sup2;</b></div>"
        f"<div><span>Used</span><b>{placed_m2:.2f} m&sup2;</b></div>"
        f"<div><span>Yield</span><b>{overall_yield:.0f}%</b></div>"
        f"<div><span>Waste</span><b>{waste_m2:.2f} m&sup2;</b></div>"
        + (f"<div><span>Stock cost</span><b>{total_cost:.2f}</b></div>" if has_cost else '')
        + (f"<div><span>Purchased</span><b>{hw_count}</b></div>" if hw_count else '')
        + (f"<div><span>Hardware cost</span><b>{hw_cost:.2f}</b></div>" if has_hw_cost else '')
        + (f"<div><span>Unmatched</span><b style='color:#b00'>{total_unmatched}</b></div>"
           if total_unmatched else '')
        + "</div>")

    hardware_html = _hardware_section_html(hardware_rows, has_hw_cost, hw_cost)

    # Legend: one swatch per material/thickness group (matched first).
    legend_html = "<div class='legend'>" + ''.join(
        f"<span>{_swatch(s['color'])}{_esc(s['material'])} {s['thickness']:.0f}mm</span>"
        for s in sections) + "</div>"

    display_w = 380.0
    blocks = []
    for s in sections:
        blocks.append("<div class='section'>")
        if s['matched']:
            sheet = s['sheet']
            cost_txt = f" &nbsp;|&nbsp; cost {s['cost']:.2f}" if has_cost else ''
            blocks.append(
                f"<h2>{_swatch(s['color'])}{_esc(s['material'])} &mdash; {s['thickness']:.1f} mm "
                f"&nbsp;|&nbsp; {s['pieces']} pcs &nbsp;|&nbsp; {s['num_sheets']} sheet(s) "
                f"&nbsp;|&nbsp; {s['yield']:.0f}% yield &nbsp;|&nbsp; {s['area_m2']:.2f} m&sup2;{cost_txt}</h2>")
            blocks.append(
                f"<div class='sub'>Stock: {_esc(sheet.get('name', 'Sheet'))} "
                f"{sheet['length']:.0f} &times; {sheet['width']:.0f} mm &nbsp;|&nbsp; "
                f"gap {s['gap']:.0f} mm &nbsp;|&nbsp; trim {s['trim']:.0f} mm &nbsp;|&nbsp; "
                f"rotation {'on' if s['allow_rot'] else 'off (grain)'}</div>")
        else:
            blocks.append(
                f"<h2 style='border-color:#b00'>{_swatch(s['color'])}{_esc(s['material'])} &mdash; "
                f"{s['thickness']:.1f} mm &nbsp;|&nbsp; {s['pieces']} pcs &nbsp;|&nbsp; "
                f"<span style='color:#b00'>no matching stock material</span></h2>")
            blocks.append(f"<div class='warn'>&#9888; No stock material for "
                          f"<b>{_esc(s['material'])}</b> ({s['thickness']:.0f} mm). Add it in the "
                          f"Sheets palette (match the material name + thickness), then re-run.</div>")

        blocks.append("<table><thead><tr><th>Qty</th><th>Length (mm)</th><th>Width (mm)</th>"
                      "<th>Area (m&sup2;)</th><th class='l'>Parts</th></tr></thead><tbody>")
        for r in s['rows']:
            names = ', '.join(f"{_esc(n)}&times;{c}" if c > 1 else _esc(n)
                              for n, c in sorted(r['names'].items()))
            row_area = r['L'] * r['W'] * r['qty'] / 1.0e6
            blocks.append(f"<tr><td>{r['qty']}</td><td>{r['L']:.1f}</td><td>{r['W']:.1f}</td>"
                          f"<td>{row_area:.2f}</td><td class='l'>{names}</td></tr>")
        blocks.append("</tbody></table>")

        if s['unplaced']:
            names = ', '.join(_esc(u['label']) for u in s['unplaced'])
            blocks.append(f"<div class='warn'>&#9888; {len(s['unplaced'])} part(s) larger "
                          f"than the sheet: {names}</div>")
        blocks.append("</div>")  # /section — keep heading + table together

        if s['matched']:
            sheet = s['sheet']
            scale = (display_w / sheet['length']) if sheet['length'] > 0 else 0.1
            blocks.append("<div class='sheets'>")
            for i, layout in enumerate(s['sheets']):
                used = nesting.sheet_used_area(layout)
                sy = (used / (s['usable_w'] * s['usable_h']) * 100.0) if (s['usable_w'] and s['usable_h']) else 0.0
                svg = nesting.sheet_svg(layout['placements'], sheet['length'], sheet['width'],
                                        s['trim'], scale, fill=s['color'])
                blocks.append(f"<div class='sheet'><div class='cap'>Sheet {i + 1} &mdash; "
                              f"{len(layout['placements'])} parts &nbsp;|&nbsp; {sy:.0f}% used</div>{svg}</div>")
            blocks.append("</div>")

    # Labels (colour bar per material on the left edge).
    color_by_key = {(s['material'].lower(), s['thickness']): s['color'] for s in sections}
    label_cards = []
    for it in instances:
        L, W = max(it['L'], it['W']), min(it['L'], it['W'])
        c = color_by_key.get(((it.get('material') or 'Unassigned').strip().lower() or 'unassigned',
                              round(it['T'], 1)), UNMATCHED_COLOR)
        parent = (it.get('parent') or '').strip()
        parent_html = f"<div class='lc'>{_esc(parent)}</div>" if parent else ''
        label_cards.append(
            f"<div class='label' style='border-left-color:{_esc(c)}'>"
            f"{parent_html}"
            f"<div class='ln'>{_esc(it['comp_name'])}</div>"
            f"<div class='ld'>{L:.0f} &times; {W:.0f} mm</div>"
            f"<div class='lt'>{it['T']:.1f} mm thick</div></div>")
    labels_html = (f"<div class='pagebreak'></div><h2>Labels &mdash; {total_pieces} pieces</h2>"
                   f"<div class='labels'>{''.join(label_cards)}</div>")

    params_html = (
        f"Stock from the <b>Sheets</b> library (<b>{len(materials)}</b> material(s)). "
        f"Matched by material&nbsp;+&nbsp;thickness; gap / trim / rotation come from each "
        f"chosen sheet.")

    warn_bits = []
    if total_unmatched:
        warn_bits.append(f'{total_unmatched} unmatched')
    if total_unplaced:
        warn_bits.append(f'{total_unplaced} oversized')
    warn = (f' &nbsp;|&nbsp; <span style="color:#b00">&#9888; {", ".join(warn_bits)}</span>'
            if warn_bits else '')

    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<title>WoodCraft Cut List</title>' + css + '</head><body>'
        f'<h1>WoodCraft Cut List &mdash; {_esc(title)}</h1>'
        f'<div class="sub">Generated {now}{warn}</div>'
        f'<div class="params">{params_html}</div>'
        + legend_html + summary_html + ''.join(blocks) + hardware_html + labels_html +
        '<button onclick="window.print()">Print / Save PDF</button>'
        '</body></html>')


def command_destroy(args: adsk.core.CommandEventArgs):
    futil.log(f'{CMD_NAME} Command Destroy Event')
    global local_handlers, _pickers, _dialog_found
    local_handlers = []
    _pickers = []
    _dialog_found = None
