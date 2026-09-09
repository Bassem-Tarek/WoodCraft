# WoodCraft

A lightweight Autodesk **Fusion** add-in for cabinetmaking — a lean, open
alternative to tools like JoinerCAD. It turns a simple "skeleton" body into
parametric panel components, joins and machines them, and produces material‑aware
cut lists with colour‑coded nesting diagrams — without the bloat or a subscription.

> **Status:** active development. Twenty commands across two toolbar tabs —
> **WoodCraft** (modelling, hardware, kitchen, output, dev) and **Kitchen**
> (per-job project management). Pure Python + the Fusion API —
> **no external packages, no build step**.

---

## What's inside

Two tabs in the **Design** workspace. The **WoodCraft** tab holds everything that
models or reports on cabinets, split into panels that read as a workflow
(design → hardware → kitchen → output). The **Kitchen** tab holds the two
per-job commands that manage cloud folders rather than geometry.

### Cabinet Builder (modelling)
| Command | What it does |
|---|---|
| **Carcass Maker** | Select the flat faces of a solid "skeleton" box; each becomes its own panel component, extruded **directly from the face** so it stays associative. Global thickness / direction (Inside·Outside·Symmetric) / offset, plus a per‑panel override table, a "Collect all flat faces" button, and on‑screen labels + build‑direction arrows. |
| **Trim** | Combine‑cuts panels so they fit against each other, with an optional uniform **gap** (clearance/reveal; 0 = flush). |
| **Edit Thickness** | Re‑thickness existing panels by editing the extrude extent **in place** (preserves direction + offset). Prefills the current thickness. |
| **Shelf Creator** | Builds a parametric shelf on a chosen plane bounded by four faces, each with its own offset (positive, negative or uneven), kept associative to the walls. |
| **Line Boring** | Bores the shelf‑pin hole pattern into a side/gable panel from a pluggable **rule** (default **Emaar**: three‑hole sets in two columns, set centres dividing the panel height into N+1 equal gaps). Builds **live‑parametric** — `wc_lb_*` user parameters driving a sketch → seed hole → 2‑direction pattern — so editing the parameters reflows the holes; falls back to explicit holes if any step of the parametric build fails. Geometry and the rule registry live in `commands/boring.py`. |
| **Edgeband** | Tags a panel's thin **edge faces** with an edgeband type from the Sheets library's band catalogue, and **tints** them in the viewport so banded edges read at a glance. The BOM sums tagged length per band type (face area ÷ thickness, so curves count their true arc length) and prices the metres from the catalogue. An "only exposed edges" detector probes each face to skip edges butted against another panel. |
| **Set Type** | Classifies selected components/bodies as **panels** or **purchased hardware** (with a unit cost) so the cut list, nesting and BOM can sort them. Hardware assemblies can be priced as a **complete pack** (one price, children not billed again) or as **separate parts** (sum of contents). Use it for hand‑modelled or imported parts. |
| **Set Finish** | Gives selected cabinets their **finish spec** in one step. Two radio groups (**Carcass Type**, **Door Type**) write attributes of exactly those names — `Painted` or `Veneer` — onto the components you picked. Four dropdowns (**Carcass / Door Material**, **Carcass / Door Appearance**) each list their **own** curated set of names — a carcass is MDF or melamine, a door front is a decor, so one shared shortlist for all four was just noise. The names are resolved against every loaded material library (Emaar, Fusion Material, Assets, Favorites …) and are edited with **Finish Lists**; on Apply the command walks inside each selected cabinet and applies the carcass pair to every component named *Left/Right/Bottom/Back/Top Panel, Rail, Front/Back Rail, Bottom/Top Back Rail, Shelf, Fixed Shelf, Oven Shelf, Oven Support* and the door pair to every *Front Panel, Door Panel, Door, Left/Right/Top/Bottom Door, Fixed Panel, Drawer, Drawer Face, Drawer Face Top/Middle/Bottom, Top/Bottom Drawer, Filler*. Matching ignores case and Fusion’s copy/occurrence suffixes (`Left Panel (2)`, `Left Panel:1`). Alongside those exact names there are **keyword** rules (`WC_*_PART_KEYWORDS`): a component whose name merely *contains* the phrase joins the group, which is how every `Drawer Face …` variant is caught without listing them all. Exact names are resolved before keywords, so a broad phrase can never hijack a listed part; anything matching neither is left untouched. **Material and appearance are independent:** every dropdown starts on *leave unchanged*, so you can set a material without touching the look, recolour without re-specifying the material, do both, or neither and just tag the types. Fusion normally makes a material drag its own appearance along — the command suppresses that by noting each body’s appearance before the material lands and pinning it back as a body override, unless you picked an appearance, which becomes the override instead. Bodies that already carry their own override are left alone; an override outranks a material’s appearance anyway. |
| **Finish Lists** | Edits what Set Finish offers, from inside Fusion. Pick which **list** you are editing (Carcass Material, Carcass Appearance, Door Material, Door Appearance), optionally narrow to one **library**, and **search** by name — matches anywhere in the name, ignoring case, which is the practical way to find one entry among the 461 materials your libraries hold. Tick items in the checkbox list; only entries *of the right kind* are ever offered (materials for a material list, appearances for an appearance one). All four lists are edited in one sitting and written to `%APPDATA%/WoodCraft/finish_lists.json` on OK. Only what is currently displayed is read back, so a name hidden by the filters keeps its place — which is also why a configured name that no loaded library offers any more is **kept and named in the summary** rather than silently dropped: the usual cause is a library not yet imported on this machine. Not promoted — it’s a setup command. |

### Hardware (machining)
| Command | What it does |
|---|---|
| **Insert Hardware** | Inserts parts from a Fusion **cloud library** (top‑level folders = categories) linked at the origin, then launches Fusion's Move gizmo to position them. Thumbnail preview, module cache, and a Refresh button. *(Needs a cloud project — see setup.)* |
| **Sculpt** | Combine‑cuts panels with hardware "tool" bodies that intersect them (e.g. hinge cups, dowel holes), keeping the tool bodies. The productised "machining" step. |

### Kitchen (whole-assembly)
These act on a **finished kitchen** — a run of cabinets already assembled — rather
than on one cabinet at a time. **Set Finish** and **Finish Lists** live here too:
the finish spec is a property of a whole kitchen, not of a cabinet being modelled.

| Command | What it does |
|---|---|
| **Countertop** | Builds the worktop over assembled cabinets. **The wall is the reference:** the slab's back edge lies on the picked wall face and its front edge is that face offset by the **cabinet depth + a 20 mm overhang** (editable). The **side panels supply only the two end lines** — where the run starts and stops along the wall, taken from their outer faces. The underside lands on the **top of the tallest selected side panel**, so you never measure the plinth + carcass height. Tick **Backsplash** for an upstand as well, with its own **thickness** and **height**: it runs the full length of each run, hard against the wall, standing on the worktop. A **live preview** draws every piece as a wireframe box (slab in cyan, upstand in amber) labelled with its run length, so you see it before you commit. One wall face = one run = one component with one body (`Countertop` / `Countertop 1..N`, plus `Backsplash N`), each tagged with its own **countertop** category — costed by area and edgebandable exactly like a panel, but kept **out of the cut list and the nest**, because a worktop is bought as a slab or a cut length rather than nested out of a stock sheet. **L‑ and U‑shaped kitchens in one go:** select every wall and every end panel — each run claims the panels standing in front of *its* wall, is **extended and then clipped to the other walls' planes** so it ends exactly where the walls meet — neither short of the corner nor through the wall — and where two runs still overlap the command finishes with a **Combine cut** (keeping the tool) so the corner is solid once, not twice. |
| **Skirting** | Builds the **plinth** under an assembled kitchen — deliberately its own command, not part of Countertop: a worktop is referenced off the **wall behind** the cabinets, a plinth off the **cabinet fronts**. Pick the **front face** of each run (its plane is the front, its normal says which way the run faces), the **side panels of the end cabinets** (which set how far the run reaches and how high the carcass underside is), optionally an **island** selection for a block skirted all the way round, and optionally a **ground** face or plane (default Z = 0). Give a **thickness** and a **setback** — the toe recess — and each run gets a board from the floor to the underside of the carcass, set back from the fronts. **Corners are mitred:** the run lines are offset and intersected, so an L, a U and a closed island loop all fall out of one construction and the material at a corner is counted once rather than twice. A galley deliberately does *not* join up — parallel runs never meet. Runs longer than **3 m** are split into equal boards, each its own sub-component tagged as a WoodCraft panel so the cut list counts it as a real part. Output is one component per run (`Skirting`, `Skirting 2` …) holding one sub-component per board. |

### Kitchen tab (per-job project management)
A **separate tab**, because these three commands don't touch geometry at all: they
create, name and clear the cloud folders for one customer's job. Between them they
book-end the design.

The shape on the cloud:

```
Clients/                              ← KITCHENS_PROJECT_NAME (project)
└── Kitchens/                         ← KITCHENS_FOLDER_NAME
    ├── Kitchen Template 1/           ← a spare, waiting
    │   ├── Kitchen Template 1        ← hybrid design
    │   └── Library/                  ← copy of Emaar Library / Kitchen Library
    └── Al Rashid_Kitchen_2026-09-09/ ← a template New Kitchen has renamed
        ├── Al Rashid_Kitchen_2026-09-09
        └── Library/
```

| Command | What it does |
|---|---|
| **Create Kitchen Template** | Builds a spare: a folder **`Kitchen Template <n>`** under `Clients/Kitchens` holding a **`Library`** sub-folder — a full copy of the master cabinet library (`LIBRARY_PROJECT_NAME` / `LIBRARY_SOURCE_FOLDER`, default **Emaar Library / Kitchen Library**), sub-folders and all — plus a new **hybrid** design named after the folder, saved and closed. No customer is asked for, because there isn't one yet: **that is the point.** Copying a library is slow and network-bound, so templates get stocked in a quiet moment. Build several at once with the **How many** spinner. The number is the **lowest unused** one, not the highest plus one — templates are consumed by being renamed, so gaps open constantly and filling them keeps the numbers small. |
| **New Kitchen** | Type the customer's name and you get `Clients/Kitchens/<Customer>_Kitchen_<date>` — a design of that name plus its own `Library` copy — opened ready to work in. Two routes to that same result, and the designer needn't care which: if a template is **waiting**, it renames it (folder *and* design), which is two metadata writes and therefore instant; if **nothing is waiting**, it builds the kitchen from scratch exactly as Create Kitchen Template would, straight under the customer's name. The fallback matters because "no templates available" is a dead end at the worst possible moment — a fresh install, or a busy week that drained the spares — so the dialog just says this one will take a few minutes and suggests stocking up afterwards. Both routes produce an identical folder with identical stamps, so nothing downstream can tell them apart. There's deliberately **no template picker**: templates are interchangeable, so choosing would be a question with no wrong answer. The literal "Kitchen" in the name marks the job type, so the Clients project can hold other kinds of project later without the names turning ambiguous. |
| **Finish Kitchen** | Run it on the finished design: it clears every file in that kitchen's **`Library`** the design doesn't reference, and prunes the sub-folders that leaves empty — what survives is exactly the cabinets in this kitchen. **Used means referenced at any depth**, so a placed cabinet's own hardware is safe too. It lists everything it is about to delete *before* you press OK, and Fusion's own `deleteMe()` refuses any file still referenced or open — those are reported as **kept**, so even a wrong answer from the reference scan can't orphan a cabinet. Deleted files land in the project's deleted items and can be restored from the Data Panel. |

> **Guards worth knowing about**
>
> - **Finish Kitchen refuses to run on an unassigned `Kitchen Template <n>`.** A
>   fresh template references nothing, so "delete everything unused" would wipe
>   the library copy that makes the template worth having. Run New Kitchen on it
>   first.
> - It also **requires the design to be saved**: the cloud works out what is
>   referenced from the saved version, so a cabinet placed but not yet saved
>   would look unused. If the design references *nothing at all*, the dialog says
>   so in red before you commit.
> - **The Library folder is found by ID, not by name.** Create Kitchen Template
>   stamps the folder's id onto the design document, which is what makes the
>   rename workflow safe — the folder *and* the design both change name between
>   creation and Finish Kitchen, and an id doesn't care.
> - **One builder, two callers.** `commands/kitchen_build.py` owns "make a kitchen
>   folder"; Create Kitchen Template and New Kitchen's fallback both call it, so
>   the two paths can't drift and a fallback-built kitchen is indistinguishable
>   from a template-built one.
> - **"Newest template" means newest by creation date, not highest number.**
>   Numbers get reused as gaps open, so `Kitchen Template 2` can easily be newer
>   than `3`. A spare whose Library came out empty (a copy cancelled part-way) is
>   skipped while any stocked one exists.
>
> **A note on wording:** everything these commands show the user says *clear* and
> *tidy*, never *delete* — a designer who is scared of a button won't press it,
> and Finish Kitchen is what pays back the per-job library copy. It isn't spin:
> what it clears goes to the project's deleted items and can be restored.
>
> **Changing the names** (they will change): constants at the top of
> [`config.py`](config.py) — `KITCHENS_PROJECT_NAME`, `KITCHENS_FOLDER_NAME`
> (empty = straight in the project root), `LIBRARY_PROJECT_NAME`,
> `LIBRARY_SOURCE_FOLDER` (empty = copy the whole project),
> `KITCHEN_LIBRARY_FOLDER_NAME`, `KITCHEN_TEMPLATE_PREFIX`,
> `KITCHEN_NAME_PATTERN` (default `{customer}_Kitchen_{date}`) and
> `KITCHEN_DATE_FORMAT` (default `%Y-%m-%d`, which sorts chronologically in the
> Data Panel; empty drops the date, and the dangling separator with it). Both dialogs show a
> **red cross** beside anything they can't find and list the projects they *can*
> see, so a renamed project is obvious before any work starts.

### Output (production)
| Command | What it does |
|---|---|
| **Sheets** | A docked **HTML palette** that edits a global stock‑sheet **library** modelled on Fusion's Nesting *Process Material Library*: **Material → Sheets**. Each material has a name (matching the Fusion material), thickness, category and a display **colour**; each sheet has a size, cost and nesting params (rotation, item separation, edge trim). Save / **Export** / **Import** for sharing. |
| **Cut List & Nest** | Collects all panels, groups them by **(material, thickness)**, matches each group to the Sheets library, and opens a **colour‑coded HTML report**: cut‑list table, per‑sheet **guillotine nesting** diagrams, sheet count, yield, optional cost, a **purchased‑items** list (your hardware + costs), and a printable label sheet. Pick one or more assemblies (or the whole design), and choose which stock sheet to nest on when a material has several. |
| **BOM** | A **docked palette** showing the **assembly hierarchy** — root → components → sub‑components — one row per component with its **type, dimensions, material, quantity and part number** (the native Fusion `Component.partNumber`). Expand/collapse the tree and **Export to Excel** (a native `.xlsx`, written with the stdlib — no add‑on dependency, with **live formulas** so costs recalc when you tweak quantities). Shows the active **configuration** name. This is the structural bill; the cutting/nesting view lives in Cut List & Nest. |
| **Settings** | Add‑on‑wide options shared by every design (stored next to the Sheets library). Today that's the panel‑cost **waste factor** — the percentage added on top of a panel's raw area when the BOM estimates its cost from sheet prices, since nesting never uses 100 % of a sheet. Not promoted to the toolbar; find it in the Output panel's overflow. |

### Dev
| Command | What it does |
|---|---|
| **Inspect Panels** | Lists every classified WoodCraft component (panel/hardware) with its cut size — a debugging aid. Self‑contained and safe to delete before release. |

---

## How panels & materials work

- **Component classification.** Every WoodCraft component carries invisible custom
  attributes under one group (`WoodCraft`), the key one being `category`
  (`panel`, `hardware` or `countertop`). Carcass Maker and Shelf Creator
  auto‑classify what they build as panels, Countertop stamps its own category;
  **Set Type** classifies existing geometry (and prices hardware).
  Output commands collect strictly by this category — no geometry guessing — so
  panels and purchased items stay cleanly separated, even across referenced cabinets
  in a larger assembly. The scheme is an extensible key/value store
  (`commands/wc_attrs.py`); new commands add keys without migrations.
- **A worktop is not a panel.** `countertop` is its own category because a slab is
  measured, priced by area and edgebanded exactly like a sheet good but is *bought*
  as a slab or a cut length — so it belongs on the BOM and never on a nesting
  diagram. `config.WC_SHEET_LIKE` is the tuple that says "costed like a panel";
  `WC_CAT_PANEL` alone is what says "nested like a panel". Anything else that
  should be billed by area but never nested joins `WC_SHEET_LIKE` rather than
  becoming a second flavour of panel.
- **Material = Fusion's native material.** Cut List reads each panel's Fusion
  physical material name and matches it (plus thickness) to the Sheets library.
  Assign real materials to your parts in Fusion, define matching stock in the
  **Sheets** palette, and nesting/costing follows. Parts with no matching stock
  are listed as a warning rather than nested.

---

## Installation & setup

This is a standard Fusion **Python add-in** — drop the folder in, enable it, done.
There is **nothing to `pip install` and no build step**.

### 1. Get the files
- **Zip:** unzip it, **or**
- **GitHub:** `git clone` the repo, or use **Code → Download ZIP**.

> ⚠️ **The folder must be named exactly `WoodCraft`.** Fusion matches the folder
> name to `WoodCraft.py` / `WoodCraft.manifest`, and the add-in derives its name
> from the folder. GitHub's "Download ZIP" gives you a `WoodCraft-main/` folder —
> **rename it to `WoodCraft`**. (A `git clone` of a repo named `WoodCraft` is
> already correct.)

### 2. Put it in the Fusion add‑ins folder
- **Windows:** `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\WoodCraft`
- **macOS:** `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/WoodCraft`

### 3. Enable it
1. In Fusion, open **Utilities → Add‑Ins** (or press **`Shift+S`**) → **Add‑Ins** tab.
2. Select **WoodCraft** → **Run**. Tick **Run on Startup** to load it automatically.
3. Switch to the **Design** workspace — the **WoodCraft** tab appears.

That's it — the stock‑sheet library auto‑creates with sensible defaults the first
time you open **Sheets** or run **Cut List**, so there's nothing else to configure.

### Notes for testers
- **macOS:** the manifest declares `"supportedOS": "windows"`, so Fusion on a Mac
  won't list the add-in until you change that line in `WoodCraft.manifest` to
  `"mac"` (or remove the key). The code itself is cross‑platform.
- **Insert Hardware** is the only feature needing external setup: it reads a Fusion
  cloud project named **`WoodCraft Hardware`** (set in `config.py` →
  `HARDWARE_PROJECT_NAME`). Without it, that one command just shows an empty
  catalogue and lists the projects it *can* see; **everything else works with zero
  config.**
- **For a "release" build:** set `DEBUG = False` in `config.py` (quiets the Text
  Commands log) and optionally delete `commands/inspectPanels/` (the Dev tool).

### Requirements
- A recent **Autodesk Fusion** (uses standard Design‑workspace APIs — extrude,
  combine, offset‑faces, custom graphics, palettes). Fusion ships its own Python;
  no separate Python install.
- No third‑party Python packages at runtime. *(The optional `generate_icons.py`
  needs Pillow, but it's a dev‑only icon generator and isn't shipped.)*

---

## Quick start

1. **Model a skeleton** box at the cabinet's outer dimensions.
2. **Carcass Maker** → pick faces (or *Collect all flat faces*), set thickness /
   direction / offset, **OK**. → **Trim** to resolve the corner overlaps.
3. **Shelf Creator** for shelves; **Edit Thickness** to re‑thickness anything;
   **Line Boring** for the shelf‑pin holes.
4. Assign **Fusion materials** to your panels, and **Edgeband** the exposed edges.
5. **Sheets** → define your stock (use **+ From design** to pull in the materials
   the design actually uses), set sizes / cost / colour, **Save**.
6. **Cut List & Nest** → pick the assemblies (or leave empty for the whole design)
   → the colour‑coded report opens in your browser.

### …and for a whole kitchen

7. Assemble the cabinets into the kitchen.
8. **Countertop** → pick the wall face(s) and the side panels at each end of each
   run, set cabinet depth + slab thickness, tick **Backsplash** if you want an
   upstand → check the preview → the worktop lands on top, corners already cut.
---

## Sharing your stock library

The library is a single portable JSON file at
`%APPDATA%\WoodCraft\sheets.json` (Windows) — colours, costs and sheets included.
Share it by sending that file (the recipient drops it in their own
`%APPDATA%\WoodCraft\`) or, more conveniently, use the **Export / Import** buttons
in the Sheets palette.

---

## Project layout

```
WoodCraft/
├── WoodCraft.py                # add-in entry point (run / stop)
├── WoodCraft.manifest          # Fusion add-in manifest
├── AddInIcon.svg               # add-in icon
├── config.py                   # shared ids, tab/panel names, DEBUG, hardware project name,
│                               #   kitchen project/folder + library names, template prefix,
│                               #   attribute schema
├── commands/
│   ├── __init__.py             # registers every command (start/stop, per-command error isolation)
│   ├── ui_helpers.py           # shared tab/panel creation, teardown, panel tagging
│   ├── kitchen_build.py        # builds one kitchen folder (folder + Library copy + hybrid
│   │                           #   design) — shared by Create Kitchen Template and New
│   │                           #   Kitchen's from-scratch fallback
│   ├── kitchen_data.py         # cloud-data plumbing for the Kitchen tab: find project/folder,
│   │                           #   recursive copy, template numbering, rename, reference scan,
│   │                           #   delete + prune (no geometry)
│   ├── wc_attrs.py             # component attribute store (category, cost, purchase mode,
│   │                           #   edgeband, carcass/door finish type)
│   ├── panels.py               # shared panel collector, material reading, assembly tree
│   ├── boring.py               # pure-math shelf-pin boring rules + geometry (no Fusion features)
│   ├── countertop_geom.py      # pure-math worktop outline from wall + side panels (no Fusion API)
│   ├── skirting_geom.py        # pure-math plinth outlines: mitred corners, 3 m splitting (no Fusion API)
│   ├── nesting.py              # pure-math guillotine nester + SVG (no Fusion API)
│   ├── sheets_store.py         # global stock-sheet + edgeband library (load/save/match; pure)
│   ├── settings_store.py       # global add-on settings JSON (waste factor)
│   ├── finish_store.py         # per-dropdown material/appearance name lists (pure)
│   ├── material_pool.py        # resolves those names against the loaded material libraries
│   ├── report_utils.py         # shared HTML report shell, CSS, escaping, open-in-browser
│   ├── xlsx_writer.py          # dependency-free .xlsx writer (stdlib zipfile + XML)
│   ├── carcassMaker/  trim/  editThickness/  shelf/  lineBoring/  edgeband/  convertPanel/
│   ├── setFinish/            # Set Finish: type attributes + carcass/door materials
│   ├── finishLists/          # Finish Lists: edits the four dropdown lists
│   ├── insertHardware/  sculpt/
│   ├── countertop/             # Kitchen panel
│   ├── skirting/               # Kitchen panel: the plinth
│   ├── sheets/                 # Sheets palette: entry.py + resources/html/{index,style,main}
│   ├── cutList/                # Cut List & Nest
│   ├── bom/                    # BOM palette: entry.py + resources/html/{index,style,main}
│   ├── settings/               # Add-on settings dialog
│   ├── createKitchenTemplate/  # Kitchen TAB: stock a spare kitchen + library copy
│   ├── newKitchen/             # Kitchen TAB: rename a template into a customer job
│   ├── finishKitchen/          # Kitchen TAB: clear the unused library copy
│   └── inspectPanels/          # Dev tool (removable)
├── lib/fusionAddInUtils/       # Autodesk template helpers (logging, event wiring)
└── docs/UI_GUIDE.md            # icon / UI guide for design work
```

Each command is a self‑contained folder with an `entry.py` exposing `start()` /
`stop()`. To add one: create the folder, import it in `commands/__init__.py`,
append it to the `commands` list, and drop icons in its `resources/`. See
`docs/UI_GUIDE.md` for toolbar/icon conventions and the IDs that must stay stable.

---

## Notes & known limitations

- **Nesting is guillotine** (full edge‑to‑edge cuts, panel‑saw friendly). It tries
  several heuristics and keeps the tightest layout, but won't beat true‑shape
  nesting on yield. Fusion's own Nesting *Process Material Library* is **not**
  API‑readable (paid extension), which is why WoodCraft keeps its own library.
- **Trim gap** is positive (clearance) only — a negative‑offset "groove" mode was
  removed for lack of a clean uniform solid‑offset API. (Shelf offsets *do* allow
  negatives.)
- **Shelf Creator** assumes the four bounding faces form two parallel pairs (the
  normal cabinet case).
- **Countertop runs start as rectangles.** Each wall face gives one slab, then it
  is stretched into any corner it meets and clipped by the other picked walls, so
  a run in an L or U lands exactly on the wall‑to‑wall intersection. Where two
  finished runs still overlap the command Combine‑cuts the later one with the
  earlier one — but the joint is a **butt, not a mitre**, and the first wall you
  pick is the run that stays whole. Cut‑outs (sink, hob) and a nosing profile are
  modelling steps, not part of this command. The slab is also **not
  associative**: it is built from the cabinets' positions at the moment you run
  it, so move a cabinet and you re‑run Countertop.
- **Corners only work with the walls you pick.** A wall that bounds a run but
  isn't selected can neither extend nor clip it — if a run stops short of a
  corner or runs past one, add that wall face to the selection.
- **A run is only stretched into a nearby corner** — a wall further than the
  worktop depth (plus 100 mm) beyond the run's end is treated as a different part
  of the house and ignored, and a wall parallel to the run never extends it. So a
  run that genuinely stops short of a wall (a doorway, an appliance gap) will
  still be pulled to it if that wall is selected and within range; leave it out of
  the selection in that case.
- **The corner cut needs real overlap.** Runs that merely butt end‑to‑end, or that
  sit at different heights (a raised breakfast bar over base units), are left
  alone — only genuinely coincident material is trimmed.
- **UI/icons** are maintained partly by separate passes; see `docs/UI_GUIDE.md`.
  The Kitchen icons are rendered by `generate_icons_kitchen.py` (dev‑only,
  Pillow, `.gitignore`d — the PNGs are what ships).

---

## License

**GNU GPL v3.0 or later** — see [`LICENSE`](LICENSE). You're free to use, modify,
distribute and even sell this, but any copy or derivative you distribute must
also be under the GPL, with its source made available to whoever receives it.
It comes with no warranty.

**Author:** Abdelrahman Youssry
