# FCC Export — the machine format, and what the exporter does with it

`FCC Export` (Output panel) writes the XML job packet a **Nanxing** panel line
imports: the nesting layout for the router, the drilling and grooving for the
CNC borer, the edgebanding spec for the bander, and the order data the labels
print — one file for the whole cell.

Everything below was read off a production file (`FccForNesting-FccPattern-…​.xml`)
rather than a vendor specification. Where the sample left something ambiguous it
says so, and names the constant to change once the machine builder confirms it.

---

## 1 · What the exporter writes, and what it doesn't

A Nanxing job file exists in two states. The exporter writes the **pre-nest**
one.

| | pre-nest (what we write) | post-nest (what the line produces) |
|---|---|---|
| Panels placed on sheets | yes | yes |
| Holes, grooves, edgebanding | yes | **dropped** in the sample |
| `Pattern/@NcFileName` | — | the generated G-code program |
| `Pattern/@ToolName` `@ToolNo` | — | the tool the line picked |
| `Labels` + `LabelImageFileName` | — | label images and placements |
| Corner arcs on the cut path | — | added at tool radius |
| `CutInfo` speeds / `CutThickness` | — | added |

Fusion has no business inventing any of the right-hand column: the tool table,
the post-processor and the labeller live on the line. So the exporter stops at
the point the sample's un-processed patterns stop, which is exactly the form the
production order in that file arrived in.

---

## 2 · Coordinates

**Everything in the file is absolute sheet millimetres.** There is no per-part
local frame in the geometry — holes and slots carry sheet X/Y — so the exporter
bakes the nesting placement into every feature.

Sheet origin is the corner; X runs along the sheet's length, Y along its width.

Each panel carries three different sizes, and confusing them is the fastest way
to scrap a sheet:

| | meaning |
|---|---|
| `CutLength` / `CutWidth` | what the router actually cuts — the modelled panel |
| `Length` / `Width` | the tool-centre footprint = cut + `ToolDiameter` |
| `ProLength` / `ProWidth` | the **finished** panel, edgebanding included (`BenchmarkInfo`, and the extent of `FccOutline`) |

`Lineament` is the tool-centre path: the cut rectangle grown by the tool radius.
The datum every hole and slot is measured from is

```
Lineament.X + ProOffsetX ,  Lineament.Y + ProOffsetY
```

and that corner belongs to the **finished** panel. `ProOffset` is the tool radius
minus whatever the band on the low-side edge stands proud by — 3.0 mm on a bare
edge with a 6 mm bit, 2.5 mm where that edge is pre-milled and banded.

The growth cancels out in practice, which is worth knowing when reading the code:

```
sheet_X  =  cut origin X  +  the hole's distance from the cut edge
```

`Point/@Angle` is the included angle of the arc **arriving** at that point
(0 = straight, negative = clockwise). The pre-nest form has no corner arcs, so
the exporter writes 0 throughout.

---

## 3 · Faces

| Face | Where | Edgeband attribute |
|---|---|---|
| 1 | Y = Width | `EBL2` |
| 2 | Y = 0 | `EBL1` |
| 3 | X = Length | `EBW2` |
| 4 | X = 0 | `EBW1` |
| 5 | top | — |
| 6 | bottom | — |

`Hole/@Type` is **1 = horizontal** (drilled into an edge, Face 1–4, carrying `Z`
— the height of the hole's axis above Face 6) and **2 = vertical** (into Face 5
or 6, no `Z`).

Face numbers are **placement-frame** numbers: Face 3 is whichever edge ends up at
the far end of the sheet's X axis. When the nester turns a part 90°, the
exporter re-keys its faces and its feature coordinates and records
`RotateAngle="90"`.

> The sample carries `RotateAngle="90"` on panels whose length still lies along
> X, so its own convention for that attribute could not be pinned down. The
> exporter uses the unambiguous one — the flag means "the nester turned this part
> from its design orientation" — and every face number and coordinate in the file
> is consistent with the placement. If the line disagrees, the mapping is
> `ROTATED_FACE_MAP` and `place_point()` in `fcc_writer.py`.

---

## 4 · Sizing, and the one open question

Per axis, the finished dimension grows by `Thickness − Pre_Milling` for each
banded edge — **but in the sample, only for edges whose `Pre_Milling` is
non-zero.** A banded edge with no pre-milling adds nothing at all. With 0.8 mm
tape and 0.3 mm pre-mill that is +0.5 mm per edge, and it holds on every
production panel in the file:

| panel | cut | banded edges | finished |
|---|---|---|---|
| shelf | 561 × 567 | all four at 0.8 / 0.3 | 562 × 568 |
| base bottom | 599.5 × 563 | F3 at 0.8/0.3; F1, F2, F4 at 0.8/**0** | 600 × 563 |
| back rail | 149.5 × 563 | F3 only | 150 × 563 |
| wall bottom | 908 × 399.5 | F2 only | 908 × 400 |

Whether the bare-banded edges genuinely contribute nothing, or the sample's
design software had already folded them into the cut size, is **not settled**.
The difference is the full tape thickness per edge — 1.6 mm on a panel banded all
round — so it is worth a written answer from the machine builder.

```python
# commands/fcc_writer.py
SIZE_POLICY = SIZE_POLICY_PREMILLED_ONLY   # matches the sample; the default
# SIZE_POLICY = SIZE_POLICY_ALL_BANDED     # every banded edge grows the panel
```

The exporter treats **the modelled panel as the cut panel**, with banding added
on top, because WoodCraft models the bare panel and tags edgebanding onto faces.

---

## 5 · Edgeband codes

Each `EB*` attribute is seven `*`-separated fields:

```
0.8 * 22 * PVCWHITE08 * A * SS * P01 * 3
 │    │       │         │    │     │    └ banding order among this panel's banded edges
 │    │       │         │    │     └────── edgebander program
 │    │       │         │    └──────────── edge profile  (see below)
 │    │       │         └───────────────── tape grade
 │    │       └─────────────────────────── tape stock code
 │    └─────────────────────────────────── tape width, mm
 └──────────────────────────────────────── tape thickness, mm
```

The stock code is derived from the band's name in the Sheets library
(`PVC White 0.8 mm` → `PVCWHITE08`) — stable, because that name is also what the
face tag stores.

The sample uses `SS` and `CC` in field 5 and never says which edge treatment each
selects, so the exporter emits one configurable value
(`fcc_writer.DEFAULT_EDGE_PROFILE`, default `SS`) rather than guessing a rule.
**Ask for the edgeband material table**, then set it per band.

Banding order is ends first, then sides (`BANDING_SEQUENCE = (3, 4, 2, 1)`),
numbering only the edges that are actually banded. The sample's four-sided panels
do this; a couple of its panels use a different order, which is probably the
bander's own routing rather than something Fusion should predict.

---

## 6 · How a panel becomes a workpiece

`commands/fcc_features.py` reads the B-Rep; `commands/fcc_writer.py` writes the
format. Neither knows about the other's job, and the writer needs no Fusion at
all — `tests/test_fcc_writer.py` exercises the whole format with plain dicts.

**The panel's frame** is the component-space bounding box of its own visible
bodies: thickness is the smallest extent, length the largest. That makes it
independent of how the cabinet is oriented — a side panel standing on edge in a
kitchen reads exactly like the same panel lying flat, which is what a flat-panel
machine wants. The frame is forced right-handed; a left-handed one would mirror
the part and put every hole on the wrong side of it.

A panel modelled at an angle *inside its own component* still produces a frame
(its bounding box) but every coordinate in it would be wrong, so
`frame_is_reliable()` checks that the two big faces really are square to the
thickness axis and the command warns instead of shipping it.

**Holes** are concave cylindrical faces whose axis is parallel to a frame axis.
Faces belonging to one bore — a hole split at a surface seam, or crossing a
groove — are merged; coaxial bores whose spans don't meet stay separate, so a
pair of dowel holes drilled into opposite ends of a panel is two holes and not
one bore straight through the part.

**Grooves** are found by their floor: a planar face parallel to the skins, lying
between them, longer than it is wide. The floor's outward normal points into the
removed material, which is what says whether the groove was cut from the top or
the bottom.

### Known limits of reading geometry

- A bore that runs into another feature reports the depth of **its own
  cylindrical face**, not the nominal drill depth. On the test cabinet a Ø8
  dowel hole breaking into a Ø15 cam housing reads 27.7 mm rather than the ~34 mm
  the drill would be set to.
- Only straight, axis-aligned grooves are reported. Shaped pockets, curved dados
  and rebates that break out through an edge are left out rather than guessed at.
- Holes at a compound angle are skipped — a flat-panel machine can't drill them.

---

## 7 · Using it

1. **Sheets palette** — every panel material needs a stock material at the right
   thickness, or its panels are skipped. `+ From design` seeds them.
2. **Edgeband** — tag the edges that get banded. Panels with no tags export bare;
   the dialog's *Band untagged edges with* applies one band to all four edges of
   every untagged panel, which is the usual case for a single-material carcass.
3. **FCC Export** — set the router tool diameter (must match the machine), the
   pre-milling allowance, a barcode prefix and any order data, pick the cabinets
   (or leave empty for the whole design), and save.

The summary afterwards lists sheets, panels, holes and grooves, plus anything
worth checking: parts too big for the sheet, a part gap narrower than the tool, a
band with no thickness in the library, panels that aren't square to their own
component.

### Settings that must match the machine

| Setting | Where | Note |
|---|---|---|
| Router tool Ø | dialog | drives `ToolDiameter`, the cut-path offset and the lead-ins |
| Part gap | Sheets library (`separation`) | must be **≥ the tool diameter** or neighbouring cuts overlap |
| Edge trim | Sheets library (`trim`) | must be **≥ the tool radius** or the outer cut runs off the sheet |
| Pre-milling | dialog | see §4 |
| Lead-in length | `fcc_writer.SLOPE_LEN` | 70 mm, as the sample |

### Barcodes

`WorkpieceId` is `<prefix><5 digits>`, numbered across the whole job — it is what
every station scans, so it must be unique. The sample's own file has a duplicate;
this exporter cannot produce one.

---

## 8 · Still to confirm with Nanxing

1. The sizing rule in §4 — **in writing**, before production.
2. The edgeband material table: what `SS` / `CC` select, and how tape codes map
   to stock.
3. What `HasFace5` / `HasFace6` actually mean. In the sample they are *not* "has
   features on that face" — panels with Face-6 holes report `HasFace6="false"` —
   so they more likely mean "needs a flip / second setup". The exporter currently
   writes them literally (`HasFace5` = has features on Face 5), which is the
   honest reading; change `Workpiece.to_xml` in `fcc_writer.py` once it's known.
4. Whether the borer reads the pre-nest patterns. The sample's nesting step
   **drops all holes and slots**, so if the line re-writes the file before the
   borer sees it, the drilling has to survive that step.
5. The code tables behind `DataValid="5"`, `FccFreeCombination Type="105"`,
   `IsGenCode="2"`, `MachiningPoint` (1/3/7) and `Slot/@ToolOffset`. The exporter
   copies the sample's constants, which is safe but unexamined.
