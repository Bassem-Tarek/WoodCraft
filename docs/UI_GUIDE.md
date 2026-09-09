# WoodCraft — UI / Icon Guide for Agents

This document is for agents (or people) working on the **visual side** of the
WoodCraft add-in: command icons, the toolbar tab, and the Sheets palette. It
describes what the code already wires up so you know exactly which files to touch
and which names/IDs to honour. **Do not rename IDs** — they are how the runtime
finds and removes UI elements.

---

## 1. Where the UI lives

WoodCraft adds **two** tabs to Fusion's **Design** workspace. Everything is built
at runtime from Python; there is no static UI definition file.

The **WoodCraft** tab is split into five panels (the gap between panels reads as a
workflow separator). The **Kitchen** tab is separate because its commands manage
cloud folders for a job rather than geometry — a designer starting a kitchen
shouldn't have to scroll past twenty modelling buttons to find the one they want.

Every id below is built from `config.COMPANY_NAME` — never hard-code the prefix.

```
Design workspace (FusionSolidEnvironment)
└── Tab: "WoodCraft"                       id = WoodCraft_tab
    ├── Panel: "Cabinet Builder"           id = WoodCraft_cabinet_panel
    │   ├── Carcass Maker    (button)      id = WoodCraft_carcassMaker
    │   ├── Trim             (button)      id = WoodCraft_trim
    │   ├── Edit Thickness   (button)      id = WoodCraft_editThickness
    │   ├── Shelf Creator    (button)      id = WoodCraft_shelf
    │   ├── Line Boring      (button)      id = WoodCraft_lineBoring
    │   ├── Edgeband         (button)      id = WoodCraft_edgeband
    │   └── Set Type         (button)      id = WoodCraft_convertPanel
    ├── Panel: "Hardware"                  id = WoodCraft_hardware_panel
    │   ├── Insert Hardware  (button)      id = WoodCraft_insertHardware
    │   └── Sculpt           (button)      id = WoodCraft_sculpt
    ├── Panel: "Kitchen"                   id = WoodCraft_kitchen_panel
    │   ├── Countertop       (button)      id = WoodCraft_countertop
    │   ├── Skirting         (button)      id = WoodCraft_skirting
    │   ├── Set Finish       (button)      id = WoodCraft_setFinish
    │   └── Finish Lists     (button)      id = WoodCraft_finishLists   (not promoted)
    ├── Panel: "Output"                    id = WoodCraft_output_panel
    │   ├── Sheets           (palette)     id = WoodCraft_sheets
    │   ├── Cut List & Nest  (button)      id = WoodCraft_cutList
    │   ├── BOM              (palette)     id = WoodCraft_bom
    │   └── Settings         (button)      id = WoodCraft_settings   (not promoted)
    └── Panel: "Dev"                       id = WoodCraft_dev_panel
        └── Inspect Panels   (button)      id = WoodCraft_inspectPanels   (removable)

Design workspace (FusionSolidEnvironment)
└── Tab: "Kitchen"                         id = WoodCraft_kitchen_tab
    └── Panel: "Kitchen Project"           id = WoodCraft_kitchen_project_panel
        ├── Create Kitchen Template        id = WoodCraft_createKitchenTemplate
        ├── New Kitchen      (button)      id = WoodCraft_newKitchen
        └── Finish Kitchen   (button)      id = WoodCraft_finishKitchen
```

> ⚠️ **"Kitchen" names two different things — don't merge them.**
> `WoodCraft_kitchen_panel` is a panel of whole-kitchen **modelling** commands
> inside the WoodCraft tab (Countertop, Skirting, Set Finish). `WoodCraft_kitchen_tab`
> is the separate **job-management** tab above. Both sets of constants live in
> `config.py` under their own headings.

`ui_helpers.get_panel()` and `remove_command()` both take an optional `tab_id`
(and `tab_name`) that defaults to the WoodCraft tab — a command only names a tab
when it wants a different one:

```python
panel = ui_helpers.get_panel(PANEL_ID, PANEL_NAME, tab_id=TAB_ID, tab_name=TAB_NAME)
...
ui_helpers.remove_command(PANEL_ID, CMD_ID, tab_id=TAB_ID)   # same tab_id, or the
                                                             # button never goes away
```

All panel IDs/names are defined in [`config.py`](../config.py); each command's
`CMD_ID` is in its own `entry.py`. The tab and panels are created lazily by the
first command that starts and removed when the last command stops — see
[`commands/ui_helpers.py`](../commands/ui_helpers.py). When you add a panel/tab,
add its ID/name constants to `config.py`; never hard-code strings in command files.

---

## 2. Command icons

Each command points at a `resources/` folder next to its `entry.py` via the
`ICON_FOLDER` constant. Fusion looks inside that folder for these exact filenames:

| File        | Used for                                  |
|-------------|-------------------------------------------|
| `16x16.png` | Small toolbar / list views                |
| `32x32.png` | Standard toolbar button                   |
| `64x64.png` | High-DPI displays                         |

Optional but recommended (Fusion uses them if present): `96x96.png` and
`disabled-16x16.png` / `disabled-32x32.png` / `disabled-64x64.png` (greyed-out
state). To restyle a command, replace the PNGs in place using the **same
filenames** — no code change needed.

### ⚠️ Fusion caches icons against the folder PATH

Replacing the PNGs in place often appears to do **nothing**. Fusion reads a
command's artwork when `addButtonDefinition` creates it, then caches the bitmaps
against that icon folder path. Stopping and restarting the add-in reloads the
Python but reuses the cached images, so the *old* icons keep showing — and if the
folder once held placeholder art, the placeholder is what you see.

**Fully quit and reopen Fusion after changing an icon.** If even that doesn't
take, point `ICON_FOLDER` at a path Fusion has never read (e.g. a
`resources/v2/` sub-folder) — a new path has nothing cached against it.

### Icon folders (folder name ≠ display name)

| Command          | Folder (`commands/…/resources/`)   | `CMD_ID`               |
|------------------|------------------------------------|------------------------|
| Carcass Maker    | `carcassMaker/`                    | `WoodCraft_carcassMaker` |
| Trim             | `trim/`                            | `WoodCraft_trim`       |
| Edit Thickness   | `editThickness/`                   | `WoodCraft_editThickness` |
| Shelf Creator    | `shelf/`                           | `WoodCraft_shelf`      |
| Line Boring      | `lineBoring/`                      | `WoodCraft_lineBoring` |
| Edgeband         | `edgeband/`                        | `WoodCraft_edgeband`   |
| Set Type         | `convertPanel/`                    | `WoodCraft_convertPanel` |
| Insert Hardware  | `insertHardware/`                  | `WoodCraft_insertHardware` |
| Sculpt           | `sculpt/`                          | `WoodCraft_sculpt`     |
| Countertop       | `countertop/`                      | `WoodCraft_countertop` |
| Sheets           | `sheets/`                          | `WoodCraft_sheets`     |
| Cut List & Nest  | `cutList/`                         | `WoodCraft_cutList`    |
| BOM              | `bom/`                             | `WoodCraft_bom`        |
| Settings         | `settings/`                        | `WoodCraft_settings`   |
| Inspect Panels   | `inspectPanels/`                   | `WoodCraft_inspectPanels` |
| Create Kitchen Template | `createKitchenTemplate/`    | `WoodCraft_createKitchenTemplate` |
| New Kitchen      | `newKitchen/`                      | `WoodCraft_newKitchen` |
| Finish Kitchen   | `finishKitchen/`                   | `WoodCraft_finishKitchen` |

> The folder name is internal; the display name comes from `CMD_NAME` in each
> `entry.py`. If you find stray icon-only folders named after display names (e.g.
> `panelThickness/`, `shelfCreator/`), they're leftovers — icons must live in the
> folder the command's `ICON_FOLDER` points at.

Every command has custom artwork. A shared accent palette is used so the set feels
like a family: WoodCraft **yellow** `#E5C05B`/`#F3D573` for panels/stock, **red**
`#EF4444` for cut/edit/trim accents (Trim, Edit Thickness, Sculpt, and the Inspect
Panels magnifying glass), **orange** `#F47426` for the Set Type (convertPanel) badge.
Icons are generated by [`generate_icons.py`](../generate_icons.py) (Pillow; a
dev-only script, **`.gitignore`d** and not shipped). To re-render just one icon,
call its `generate_*()` + `save_icon_sizes(img, <its resources folder>)` rather
than running `__main__` (which rewrites every icon and would clobber custom art).

### Two drawing conventions — match the one that fits

Read off the existing set; getting this wrong is what makes a new icon look
foreign:

| Convention | Used by | Rules |
|---|---|---|
| **Isometric solid** | Carcass Maker, Trim, Shelf Creator, Set Type's cube, **Countertop** | **No keyline at all.** Depth comes only from three face shades — top lightest (`#F3D573` / `#E8E8E8`), then the left face (`#E5C05B` / `#BFBFBF`), then the right (`#CFA644` / `#A3A3A3`). 2:1 isometric, x to the lower-right, y to the lower-left, z up. |
| **Flat, front-on** | Cut List, BOM | Thin dark keyline `#555555` at ~2 px (at 64), near-white `#FAFAFA` body, yellow blocks for content. |

Artwork **fills the frame** — the stock icons run to within ~3 px of the 64 px
edge. Draw oversize (8×) and downsample with LANCZOS so 16 px stays legible.

> The Kitchen panel's icon comes from
> [`generate_icons_kitchen.py`](../generate_icons_kitchen.py) — same idea as
> `generate_icons.py`, also dev-only and `.gitignore`d. It auto-fits each
> composition to the frame, so editing the shapes can't leave the artwork small
> or off-centre.
> Icon: **Countertop** = grey carcass + yellow slab + yellow upstand.
>
> The **Kitchen tab** trio comes from `generate_icons_kitchen_tab.py` — dev-only
> and `.gitignore`d like the others. They follow the **flat, front-on** convention
> and deliberately share one artwork: a WoodCraft-yellow folder with a keylined
> white badge bottom-right. Only the badge differs, and it tracks the workflow in
> **both glyph and colour** so the three stay apart at 16 px, where shape alone
> blurs:
>
> | Command | Badge |
> |---|---|
> | Create Kitchen Template | dark `#555555` **+** — stock a spare |
> | New Kitchen | orange `#F47426` **pencil** — name it for a customer |
> | Finish Kitchen | green `#1A7F37` **tick** — the kitchen is done |
>
> Finish Kitchen deliberately does **not** get a red ✕. It tidies a finished job,
> and a red ✕ reads as *destroy* — which makes designers avoid the one button that
> pays back the whole per-job library copy. Same reason its tooltip and dialog say
> "clear" and "tidy" rather than "delete". If you redraw one, redraw all three.

There is also a top-level [`AddInIcon.svg`](../AddInIcon.svg) (referenced by
`WoodCraft.manifest`) used as the add-in's icon in the Scripts & Add-Ins dialog.

### Dialog action-button icons (Carcass Maker)
Carcass Maker's dialog has three in-dialog action buttons — **Collect all flat
faces**, **Reset rows to defaults**, **Delete selected row** — created as
icon `BoolValueInput`s (non-checkbox) that reset themselves after firing. Each
points at its own sub-folder:

| Button         | Icon sub-folder                              | id (in `entry.py`) |
|----------------|----------------------------------------------|--------------------|
| Collect faces  | `commands/carcassMaker/resources/collect/`   | `COLLECT_BTN_ID`   |
| Reset defaults | `commands/carcassMaker/resources/defaults/`  | `DEFAULTS_BTN_ID`  |
| Delete row     | `commands/carcassMaker/resources/delete/`    | `DELETE_BTN_ID`    |

> A **non-checkbox** `BoolValueInput` *must* have a valid icon folder — an empty
> one throws and silently aborts the dialog (see the project memory on
> `command_created` swallowing errors). **Checkbox-style** (`isCheckBox=True`)
> self-resetting buttons work with an empty `''` folder — that's what the Sheets/
> Cut-List dialogs use where no icon exists.

---

## 3. Adding a new command (for reference)

The pattern every command follows (see `commands/carcassMaker/entry.py` as the
reference implementation):

1. Create `commands/<name>/` with `__init__.py` (empty), `entry.py`, and a
   `resources/` folder holding the three PNGs.
2. In `entry.py` define `CMD_ID`, `CMD_NAME`, `PANEL_ID`, `ICON_FOLDER`, and
   `start()` / `stop()`. Use `ui_helpers.get_panel(...)` in `start()` and
   `ui_helpers.remove_command(...)` in `stop()`.
3. Register the module in [`commands/__init__.py`](../commands/__init__.py).

`CMD_ID` convention: `f'{config.COMPANY_NAME}_<camelCaseName>'`
(e.g. `WoodCraft_carcassMaker`). Keep it globally unique.

---

## 4. The Sheets palette (HTML/JS UI)

**Sheets** (`commands/sheets/`, `CMD_ID = WoodCraft_sheets`, Output panel) is a
docked **palette** that edits the global stock-sheet library (Material → Sheets,
mirroring Fusion's Nesting *Process Material Library*). The toolbar button is a
thin launcher: `command_created` opens the palette (`ui.palettes.add`) and adds
**no command inputs**, so the command auto-executes with **no command dialog**
(`Command.isAutoExecute` defaults to True — adding any input would force a dialog).

UI files (vanilla HTML/CSS/JS, no build step) in `commands/sheets/resources/html/`:

| File         | Role                                                              |
|--------------|-------------------------------------------------------------------|
| `index.html` | Layout: header (Save / Refresh / Export / Import), category filter, tree + detail panel |
| `style.css`  | Fusion-dark theme (`--accent` = WoodCraft yellow `#E5C05B`)        |
| `main.js`    | All logic: tree render, edits, the Python bridge                  |

`PALETTE_ID = WoodCraft_sheets_palette` (in `commands/sheets/entry.py`).

> **BOM uses the same palette pattern.** `commands/bom/` is a second palette
> (`PALETTE_ID = WoodCraft_bom_palette`), with its UI in
> `commands/bom/resources/html/{index.html,style.css,main.js}` and the same
> `incomingFromHTML` bridge — actions `ready` (serve the assembly tree) and `export`
> (write a native `.xlsx` via `commands/xlsx_writer.py`). It shares the same dark
> theme variables, so restyle both together.

**Python ↔ JS bridge.** JS calls `adsk.fusionSendData(action, jsonString)`; the
`incomingFromHTML` handler in `entry.py` replies via `args.returnData`:

| Action   | JS sends            | Python returns                                              |
|----------|---------------------|------------------------------------------------------------|
| `ready`  | `{}`                | `{library:{materials:[...]}, designMaterials:[...], designGroups:[...], path, rotations}` |
| `save`   | `{materials:[...]}` | `{ok, count, path}` (writes the global library)            |
| `export` | `{materials:[...]}` | `{ok, path}` / `{cancelled}` (file Save dialog)            |
| `import` | `{}`                | `{ok, materials, path}` / `{cancelled}` (file Open dialog) |

The library is owned by `commands/sheets_store.py` (pure, no Fusion API); the
design's real material names come from `panels.design_panel_materials(design)` and
`panels.design_panel_groups(design)`.

### Fusion webview gotchas (learned the hard way — keep these)
- **Palette URL must be a `file://` URI.** Pass `pathlib.Path(path).as_uri()`, not a
  raw Windows path (a backslash path becomes a broken `file:///C:/%5C…` URL).
- **`<datalist>` popups clip.** Fusion's webview renders datalist suggestions
  *in-page*, so a scrolling `overflow` container crops them. Use a real `<select>`
  (its popup renders above the page). Long-value fields stack the control under the
  label (`.field.wide`) so they don't get cut off horizontally.
- **Pin layout to the viewport** (`body{height:100vh;overflow:hidden}`) and let the
  tree/detail panels scroll, or bottom fields clip.
- If the palette ever renders **blank** on a newer Fusion, switch
  `ui.palettes.add(...)` → `ui.palettes.add2(..., useNewWebBrowser=True)`; the same
  `adsk.fusionSendData` / `window.fusionJavaScriptHandler` bridge applies.

> **Note:** Fusion **command-dialog** text boxes (`addTextBoxCommandInput`) render
> as **plain text** here — HTML tags show up raw. Use plain text + `\n`. (Only the
> *palette* renders real HTML/CSS; so does the browser cut-list report.)

---

## 5. Quick reference — IDs you must not change

| Constant (in `config.py`)     | Value                                       |
|-------------------------------|---------------------------------------------|
| `COMPANY_NAME`                | `WoodCraft`                                 |
| `DESIGN_WORKSPACE_ID`         | `FusionSolidEnvironment`                    |
| `TAB_ID` / `TAB_NAME`         | `WoodCraft_tab` / WoodCraft                 |
| `CABINET_PANEL_ID` / NAME     | `WoodCraft_cabinet_panel` / Cabinet Builder |
| `HARDWARE_PANEL_ID` / NAME    | `WoodCraft_hardware_panel` / Hardware       |
| `KITCHEN_PANEL_ID` / NAME     | `WoodCraft_kitchen_panel` / Kitchen         |
| `OUTPUT_PANEL_ID` / NAME      | `WoodCraft_output_panel` / Output           |
| `DEV_PANEL_ID` / NAME         | `WoodCraft_dev_panel` / Dev                 |

Command `CMD_ID`s (each in its own `entry.py`, all prefixed `WoodCraft`):
`_carcassMaker`, `_trim`, `_editThickness`, `_shelf`, `_lineBoring`, `_edgeband`,
`_convertPanel` (display "Set Type"), `_insertHardware`, `_sculpt`, `_countertop`,
`_sheets` (+ palette `…_sheets_palette`), `_cutList`, `_bom`
(+ palette `…_bom_palette`), `_settings`, `_inspectPanels`.

> `COMPANY_NAME` prefixes every id, so nothing else may hard-code it. The
> component attribute group (`WC_GROUP`) and the `%APPDATA%` data folder are
> deliberately separate literals: they name saved user data, which must not
> move if the id prefix is ever changed.
