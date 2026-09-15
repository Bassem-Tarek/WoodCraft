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

"""FCC nesting-XML writer — pure math + string building, no Fusion API.

Produces the `FccRoot / Patterns / Pattern / Workpiece` file a Nanxing panel line
imports: the nesting layout for the router, the drilling and grooving for the CNC
borer, and the edgebanding spec, all in one job packet. Everything here works on
plain dicts, so the whole format can be exercised (and unit-tested) outside
Fusion — see tests/test_fcc_writer.py.

WHAT THE MACHINE EXPECTS (derived from a production file, FccForNesting-
FccPattern-20260723.xml; see docs/FCC_EXPORT.md for the full reading):

- Every coordinate in the file is ABSOLUTE SHEET millimetres. There is no
  per-part local frame in the geometry: holes and slots carry sheet X/Y.
- `Lineament` is the TOOL-CENTRE path — the cut rectangle grown by the tool
  radius — so `Length` = `CutLength` + tool diameter.
- The datum every hole and slot is measured from is
  `Lineament.X + ProOffsetX`, `Lineament.Y + ProOffsetY`, and that corner
  belongs to the FINISHED (edgebanded) panel, whose size is `ProLength` /
  `ProWidth` on `BenchmarkInfo` and the extent of `FccOutline`.
- Faces: 1 = Y max, 2 = Y = 0, 3 = X max, 4 = X = 0, 5 = top, 6 = bottom.
  Edgeband attributes map EBL1→Face 2, EBL2→Face 1, EBW1→Face 4, EBW2→Face 3.
- A `Point`'s `Angle` is the included angle of the arc ARRIVING at it (0 = line,
  negative = clockwise). This writer emits the pre-nest form, whose paths are
  plain rectangles, so every angle is 0.

WHAT THIS WRITER DELIBERATELY DOES NOT EMIT: `NcFileName`, `ToolName`/`ToolNo`,
`Labels`, corner arcs and the post-nest `CutInfo` fields. Those are produced by
the line's own nesting/NC step; a file that carries them is its OUTPUT, not its
input. See docs/FCC_EXPORT.md.
"""

from xml.sax.saxutils import quoteattr


# ---------------------------------------------------------------------------
# Format constants — the values a machine integrator may need to change
# ---------------------------------------------------------------------------
FCC_VERSION = '2'
DATA_VALID = '5'

# How a banded edge changes the finished dimension.
#
#   'premilled_only'  an edge grows the panel by (thickness - pre_milling) ONLY
#                     when pre_milling > 0; a banded edge with no pre-milling
#                     adds nothing. This is what the production sample does, on
#                     every panel, and is the default.
#   'all_banded'      every banded edge grows the panel by
#                     (thickness - pre_milling), pre-milled or not.
#
# The two differ by the full band thickness per bare-banded edge (1.6 mm on a
# panel banded all round with 0.8 mm tape), so CONFIRM WITH THE MACHINE BUILDER
# before running production through it. docs/FCC_EXPORT.md tracks the question.
SIZE_POLICY_PREMILLED_ONLY = 'premilled_only'
SIZE_POLICY_ALL_BANDED = 'all_banded'
SIZE_POLICY = SIZE_POLICY_PREMILLED_ONLY

# Order the edgebander works the four edges; the trailing field of each EB code
# numbers only the edges that are actually banded, consecutively from 1. Ends
# first, then sides — the usual return-line route, and what the sample's
# four-sided panels do.
BANDING_SEQUENCE = (3, 4, 2, 1)

# Trailing-field profile code in the EB string (field 5). The sample uses 'SS'
# and 'CC'; which edge treatment each selects is not documented, so this writer
# emits one configurable value rather than guessing a rule.
DEFAULT_EDGE_PROFILE = 'SS'
DEFAULT_BANDER_PROGRAM = 'P01'

# Ramp-in length, mm: each candidate lead-in point sits this far back from a
# corner. Clamped per side so it never passes the far corner on a small part.
SLOPE_LEN = 70.0

# Which side's lead-in point to use (index into ToolPointList: 0 bottom,
# 1 right, 2 top, 3 left).
DEFAULT_TOOL_POINT = 0

# Order / MES fields a Workpiece may carry. The machine ignores all of them;
# they travel to the label printer and back to the ERP. Written in the order the
# production file uses, and only when the job supplies a value, so an export
# carries exactly the traceability the customer asked for and no empty noise.
# `Info13` is absent on purpose — the production file skips that number.
META_KEYS = (
    'BatchNo', 'BatchIndex', 'OrderNo', 'GroupName', 'ContractNo',
    'Customer', 'Dealer', 'AssembleNo', 'AssembleAdress', 'Designer',
    'OrderDate', 'DeliveryDate', 'PanelNo',
    'ProdutionNo', 'ProductionName', 'OrderDescription',
    'Info1', 'Info2', 'Info3', 'Info4', 'Info5', 'Info6', 'Info7', 'Info8',
    'Info9', 'Info10', 'Info11', 'Info12', 'Info14', 'Info15', 'Info16',
    'Info17', 'Info18',
)

# Face numbers, named.
FACE_YMAX, FACE_YMIN, FACE_XMAX, FACE_XMIN, FACE_TOP, FACE_BOTTOM = 1, 2, 3, 4, 5, 6

# EB attribute for each face.
EB_ATTR_FOR_FACE = {FACE_YMIN: 'EBL1', FACE_YMAX: 'EBL2',
                    FACE_XMIN: 'EBW1', FACE_XMAX: 'EBW2'}

# Local (design-frame) face -> placement-frame face when the nester turned the
# part 90 degrees CCW. Local u (length) then runs along sheet Y.
ROTATED_FACE_MAP = {FACE_XMIN: FACE_YMIN, FACE_XMAX: FACE_YMAX,
                    FACE_YMIN: FACE_XMAX, FACE_YMAX: FACE_XMIN}


# ---------------------------------------------------------------------------
# Number / string formatting
# ---------------------------------------------------------------------------
def num(value, places=3):
    """A millimetre value as the file writes them: no trailing zeros, no
    exponent, and '0' rather than '-0'."""
    try:
        rounded = round(float(value), places)
    except (TypeError, ValueError):
        return '0'
    if rounded == 0:
        return '0'
    text = f'{rounded:.{places}f}'.rstrip('0').rstrip('.')
    return text or '0'


def _attrs(pairs):
    """Attribute string from (name, value) pairs, skipping None values. Values
    are XML-escaped and double-quoted, so part names may hold any text."""
    out = []
    for name, value in pairs:
        if value is None:
            continue
        out.append(f'{name}={quoteattr(str(value))}')
    return ' '.join(out)


def _tag(name, pairs, indent, close=True):
    body = _attrs(pairs)
    space = ' ' if body else ''
    end = ' />' if close else '>'
    return f'{indent}<{name}{space}{body}{end}'


def _bool(flag):
    return 'true' if flag else 'false'


# ---------------------------------------------------------------------------
# Edge helpers
# ---------------------------------------------------------------------------
def edge_growth(edge, policy=None):
    """How much ONE banded edge adds to the finished dimension, in mm.

    `edge` is None (bare) or {'thickness', 'pre_milling'}. See SIZE_POLICY for
    why a bare-banded edge may contribute nothing."""
    if not edge:
        return 0.0
    thickness = float(edge.get('thickness') or 0.0)
    if thickness <= 0:
        return 0.0
    premill = float(edge.get('pre_milling') or 0.0)
    if (policy or SIZE_POLICY) == SIZE_POLICY_PREMILLED_ONLY and premill <= 0:
        return 0.0
    return thickness - premill


def edgeband_code(edge, sequence):
    """The 7-field edgeband string, e.g. `0.8*22*PVCW*A*SS*P01*3`:

        thickness * width * tape code * grade * profile * bander program * order

    Fields 1 and 2 are the physical tape; 3 and 4 identify the stock; 5 selects
    the edge treatment; 6 is the edgebander program; 7 is the banding order of
    this edge among the panel's banded edges."""
    if not edge:
        return ''
    return '*'.join((
        num(edge.get('thickness') or 0.0, 2),
        num(edge.get('width') or 0.0, 1),
        str(edge.get('code') or 'BAND'),
        str(edge.get('grade') or 'A'),
        str(edge.get('profile') or DEFAULT_EDGE_PROFILE),
        str(edge.get('program') or DEFAULT_BANDER_PROGRAM),
        str(sequence),
    ))


def band_sequence(edges):
    """{face: banding order} numbering only the banded edges, consecutively from
    1, in BANDING_SEQUENCE order."""
    out = {}
    nxt = 1
    for face in BANDING_SEQUENCE:
        if edges.get(face):
            out[face] = nxt
            nxt += 1
    return out


def place_edges(edges, rotated):
    """Re-key a panel's {local face: edge} onto the placement frame. A part the
    nester turned 90 degrees has its design-frame edges land on different sheet
    sides, and every face number in the file is a PLACEMENT-frame number."""
    if not rotated:
        return dict(edges)
    return {ROTATED_FACE_MAP[face]: edge for face, edge in edges.items()
            if face in ROTATED_FACE_MAP}


# ---------------------------------------------------------------------------
# Local -> placement frame
# ---------------------------------------------------------------------------
def place_point(u, v, length, width, rotated):
    """A point in the panel's design frame (u along its length, v along its
    width) expressed in the placement frame, where p runs along sheet X.

    Unrotated this is the identity. Rotated it is a 90-degree CCW turn:
    (u, v) -> (width - v, u), so the footprint becomes width x length."""
    if not rotated:
        return float(u), float(v)
    return float(width) - float(v), float(u)


def place_face(face, rotated):
    """A design-frame face number in the placement frame."""
    if not rotated or face not in ROTATED_FACE_MAP:
        return face
    return ROTATED_FACE_MAP[face]


# ---------------------------------------------------------------------------
# One workpiece
# ---------------------------------------------------------------------------
class Workpiece:
    """One panel placed on one sheet, with every derived dimension resolved.

    Input (all millimetres, all in the panel's OWN design frame):
      length, width, thickness   the modelled panel — this writer treats the
                                 modelled size as the CUT size, with edgebanding
                                 added on top, because WoodCraft models the bare
                                 panel and tags banding onto faces.
      x, y                       sheet position of the cut rectangle's min corner
      rotated                    the nester turned the part 90 degrees
      edges                      {face 1-4: None | {thickness, pre_milling,
                                 width, code, grade, profile, program}}
      holes                      [{type, face, u, v, z, diameter, depth, tool}]
      slots                      [{face, u1, v1, u2, v2, width, depth,
                                 tool_offset, tool}]
    """

    def __init__(self, data, tool_diameter, index, policy=None):
        self.data = data
        self.index = index
        self.policy = policy or SIZE_POLICY
        self.tool_diameter = float(tool_diameter)
        self.radius = self.tool_diameter / 2.0

        self.rotated = bool(data.get('rotated'))
        design_l = float(data.get('length') or 0.0)
        design_w = float(data.get('width') or 0.0)
        self.design_length, self.design_width = design_l, design_w
        # In the placement frame the long axis is whatever lies along sheet X.
        self.cut_length, self.cut_width = (design_w, design_l) if self.rotated \
            else (design_l, design_w)

        self.thickness = float(data.get('thickness') or 0.0)
        self.x = float(data.get('x') or 0.0)
        self.y = float(data.get('y') or 0.0)

        self.edges = place_edges(data.get('edges') or {}, self.rotated)
        self.sequence = band_sequence(self.edges)

        gx0 = edge_growth(self.edges.get(FACE_XMIN), self.policy)
        gx1 = edge_growth(self.edges.get(FACE_XMAX), self.policy)
        gy0 = edge_growth(self.edges.get(FACE_YMIN), self.policy)
        gy1 = edge_growth(self.edges.get(FACE_YMAX), self.policy)
        self.growth = {FACE_XMIN: gx0, FACE_XMAX: gx1,
                       FACE_YMIN: gy0, FACE_YMAX: gy1}

        # Finished (edgebanded) panel.
        self.pro_length = self.cut_length + gx0 + gx1
        self.pro_width = self.cut_width + gy0 + gy1

        # Tool-centre path: the cut rectangle grown by the tool radius.
        self.lin_x = self.x - self.radius
        self.lin_y = self.y - self.radius
        self.lin_length = self.cut_length + self.tool_diameter
        self.lin_width = self.cut_width + self.tool_diameter

        # Datum for every hole and slot: the finished panel's min corner, given
        # relative to the tool path's corner.
        self.pro_offset_x = self.radius - gx0
        self.pro_offset_y = self.radius - gy0

    # -- geometry ----------------------------------------------------------
    def sheet_xy(self, u, v):
        """A design-frame point as absolute sheet coordinates.

        The banding growth cancels out here: datum + finished-frame offset
        reduces to the cut corner plus the local coordinate."""
        p, q = place_point(u, v, self.design_length, self.design_width, self.rotated)
        return self.x + p, self.y + q

    def tool_point_list(self):
        """Four candidate lead-in points, one per side, each SLOPE_LEN back from
        a corner — written as `X,Y,side$` and joined with `;`. Side indices are
        0 bottom, 1 right, 2 top, 3 left."""
        x0, y0 = self.lin_x, self.lin_y
        x1, y1 = x0 + self.lin_length, y0 + self.lin_width
        slope_x = min(SLOPE_LEN, self.lin_length)
        slope_y = min(SLOPE_LEN, self.lin_width)
        points = ((x1 - slope_x, y0, 0), (x1, y1 - slope_y, 1),
                  (x0 + slope_x, y1, 2), (x0, y0 + slope_y, 3))
        return ';'.join(f'{num(px)},{num(py)},{side}$' for px, py, side in points)

    def outline_points(self):
        """The finished ring, with each point carrying the banding of the
        segment ARRIVING at it. The first point repeats the closing segment's
        banding so the ring reads consistently from either end."""
        length, width = self.pro_length, self.pro_width
        faces = (FACE_XMIN, FACE_YMIN, FACE_XMAX, FACE_YMAX, FACE_XMIN)
        coords = ((0, 0), (length, 0), (length, width), (0, width), (0, 0))
        return [(cx, cy, self.edges.get(face)) for (cx, cy), face in zip(coords, faces)]

    # -- serialisation -----------------------------------------------------
    def to_xml(self, indent, job_meta):
        pad = indent
        inner = pad + '  '
        eb = {}
        for face, attr in EB_ATTR_FOR_FACE.items():
            edge = self.edges.get(face)
            eb[attr] = edgeband_code(edge, self.sequence.get(face, 1)) if edge else ''

        holes = self.data.get('holes') or []
        slots = self.data.get('slots') or []
        has_horizontal = any(h.get('type') == 1 for h in holes)
        has_top = any(h.get('face') == FACE_TOP for h in holes) or \
            any(s.get('face') == FACE_TOP for s in slots)
        has_bottom = any(h.get('face') == FACE_BOTTOM for h in holes) or \
            any(s.get('face') == FACE_BOTTOM for s in slots)

        meta = dict(job_meta or {})
        meta.update(self.data.get('meta') or {})

        pairs = [
            ('ID', self.index),
            ('WorkpieceId', self.data.get('workpiece_id') or f'WP{self.index:05d}'),
            ('CuttingOrderNo', self.data.get('cutting_order', self.index)),
            ('Qty', 1),
            ('IsProduce', 'true'),
            ('Name', self.data.get('name') or ''),
            ('Type', 1),
            ('Material', self.data.get('material') or ''),
            ('Length', num(self.lin_length)),
            ('Width', num(self.lin_width)),
            ('Thickness', num(self.thickness)),
            ('CutLength', num(self.cut_length)),
            ('CutWidth', num(self.cut_width)),
            ('MachiningPoint', self.data.get('machining_point', 1)),
            ('Grain', self.data.get('grain') or 'N'),
            ('Memo', self.data.get('memo') or ''),
            ('HasHorizontalHole', _bool(has_horizontal)),
            ('HasFace5', _bool(has_top)),
            ('HasFace6', _bool(has_bottom)),
            ('OnlyHasFace6', _bool(has_bottom and not has_top)),
            ('EBL1', eb['EBL1']), ('EBL2', eb['EBL2']),
            ('EBW1', eb['EBW1']), ('EBW2', eb['EBW2']),
        ]
        if self.rotated:
            pairs.append(('RotateAngle', 90))
        for key in META_KEYS:
            if meta.get(key):
                pairs.append((key, meta[key]))

        lines = [_tag('Workpiece', pairs, pad, close=False)]

        # Edge banding, per face.
        lines.append(f'{inner}<EdgeGroup X1="0" Y1="0">')
        for face in (1, 2, 3, 4):
            edge = self.edges.get(face)
            lines.append(_tag('Edge', [
                ('Face', face),
                ('Thickness', num(edge.get('thickness') if edge else 0, 2)),
                ('Pre_Milling', num(edge.get('pre_milling') if edge else 0, 2)),
                ('X', 0), ('Y', 0), ('CentralAngle', 0),
            ], inner + '  '))
        lines.append(f'{inner}</EdgeGroup>')

        # Tool-centre path, then the same ring without arcs.
        ring = ((self.lin_x, self.lin_y),
                (self.lin_x + self.lin_length, self.lin_y),
                (self.lin_x + self.lin_length, self.lin_y + self.lin_width),
                (self.lin_x, self.lin_y + self.lin_width),
                (self.lin_x, self.lin_y))
        rot = -90 if self.rotated else 0
        lines.append(_tag('Lineament', [
            ('RotationAngle', rot), ('X', num(self.lin_x)), ('Y', num(self.lin_y)),
            ('ProOffsetX', num(self.pro_offset_x)),
            ('ProOffsetY', num(self.pro_offset_y)),
        ], inner, close=False))
        lines.append(f'{inner}  <Points>')
        for i, (px, py) in enumerate(ring, start=1):
            lines.append(_tag('Point', [('Index', i), ('X', num(px)), ('Y', num(py)),
                                        ('Angle', 0)], inner + '    '))
        lines.append(f'{inner}  </Points>')
        lines.append(_tag('CutInfos', [
            ('SamllWorkpieceFlg', _bool(self.data.get('small'))),
            ('ToolPoint', self.data.get('tool_point', DEFAULT_TOOL_POINT)),
            ('ToolPointList', self.tool_point_list()),
        ], inner + '  ', close=False))
        lines.append(_tag('CutInfo', [('CutNo', 1), ('ToolDirection', 0),
                                      ('SlopeLen', num(SLOPE_LEN))], inner + '    '))
        lines.append(f'{inner}  </CutInfos>')
        lines.append(f'{inner}</Lineament>')

        lines.append(f'{inner}<Lineament2>')
        lines.append(f'{inner}  <Points>')
        for i, (px, py) in enumerate(ring, start=1):
            lines.append(_tag('Point', [('Index', i), ('X', num(px)), ('Y', num(py)),
                                        ('Angle', 0)], inner + '    '))
        lines.append(f'{inner}  </Points>')
        lines.append(f'{inner}</Lineament2>')

        # The finished outline, carrying per-segment banding.
        lines.append(f'{inner}<FccOutline>')
        for cx, cy, edge in self.outline_points():
            lines.append(_tag('FccOutlinePoint', [
                ('X', num(cx)), ('Y', num(cy)), ('Angle', 0),
                ('EdgeThickness', num(edge.get('thickness') if edge else 0, 2)),
                ('EdgePreMilling', num(edge.get('pre_milling') if edge else 0, 2)),
            ], inner + '  '))
        lines.append(f'{inner}</FccOutline>')

        bench = [('ProLength', num(self.pro_length)), ('ProWidth', num(self.pro_width))]
        if holes:
            first = holes[0]
            fp, fq = place_point(first.get('u', 0), first.get('v', 0),
                                 self.design_length, self.design_width, self.rotated)
            bench += [('ProMachiningId', first.get('id', 1)),
                      ('ProMachiningX', num(fp + self.growth[FACE_XMIN])),
                      ('ProMachiningY', num(fq + self.growth[FACE_YMIN]))]
        lines.append(_tag('BenchmarkInfo', bench, inner))

        if holes:
            lines.append(f'{inner}<Holes>')
            for i, hole in enumerate(holes, start=1):
                hx, hy = self.sheet_xy(hole.get('u', 0), hole.get('v', 0))
                pairs = [('ID', hole.get('id', i)), ('Type', hole.get('type', 2)),
                         ('IsGenCode', 2),
                         ('Face', place_face(hole.get('face', FACE_TOP), self.rotated)),
                         ('X', num(hx)), ('Y', num(hy))]
                if hole.get('type') == 1:
                    pairs.append(('Z', num(hole.get('z', self.thickness / 2.0))))
                pairs += [('Diameter', num(hole.get('diameter', 0), 2)),
                          ('Depth', num(hole.get('depth', 0), 2)),
                          ('ToolName', hole.get('tool') or '')]
                lines.append(_tag('Hole', pairs, inner + '  '))
            lines.append(f'{inner}</Holes>')

        if slots:
            lines.append(f'{inner}<Slots>')
            for i, slot in enumerate(slots, start=1):
                sx, sy = self.sheet_xy(slot.get('u1', 0), slot.get('v1', 0))
                ex, ey = self.sheet_xy(slot.get('u2', 0), slot.get('v2', 0))
                lines.append(_tag('Slot', [
                    ('ID', slot.get('id', i)), ('IsGenCode', 2),
                    ('Face', place_face(slot.get('face', FACE_TOP), self.rotated)),
                    ('X', num(sx)), ('Y', num(sy)),
                    ('EndX', num(ex)), ('EndY', num(ey)),
                    ('Width', num(slot.get('width', 0), 2)),
                    ('Depth', num(slot.get('depth', 0), 2)),
                    ('ToolOffset', slot.get('tool_offset', 0)),
                    ('ToolName', slot.get('tool') or ''),
                ], inner + '  '))
            lines.append(f'{inner}</Slots>')

        lines.append(f'{pad}</Workpiece>')
        return lines


# ---------------------------------------------------------------------------
# Whole job
# ---------------------------------------------------------------------------
def build(job, policy=None):
    """The complete FCC XML for `job` as a string.

    job = {
      'meta':      {order fields copied onto every workpiece},
      'materials': [{'name', 'length', 'width', 'thickness', 'grain',
                     'tool_diameter', 'separation', 'trim',
                     'sheets': [{'workpieces': [...], 'oddments': [...]}]}],
    }
    """
    policy = policy or SIZE_POLICY
    meta = job.get('meta') or {}
    out = ['<?xml version="1.0"?>']
    out.append(_tag('FccRoot', [
        ('xmlns:xsi', 'http://www.w3.org/2001/XMLSchema-instance'),
        ('xmlns:xsd', 'http://www.w3.org/2001/XMLSchema'),
        ('Version', FCC_VERSION), ('DataValid', DATA_VALID),
        ('WorkpieceCutting', 'true'), ('CreateG', 'true'),
        ('RefreshCuttingOrder', 'false'), ('RefreshToolPointLst', 'false'),
    ], '', close=False))

    for m_index, material in enumerate(job.get('materials') or [], start=1):
        tool_d = float(material.get('tool_diameter') or 6.0)
        out.append(_tag('Patterns', [
            ('Index', m_index), ('ID', m_index),
            ('Name', material.get('name') or ''),
            ('Length', num(material.get('length'))),
            ('Width', num(material.get('width'))),
            ('Thickness', num(material.get('thickness'))),
            ('Grain', material.get('grain') or 'N'),
        ], '  ', close=False))

        for p_index, sheet in enumerate(material.get('sheets') or [], start=1):
            trim = float(material.get('trim') or 0.0)
            out.append(_tag('Pattern', [
                ('Index', p_index), ('LayoutOrigin', 0),
                ('WorkpieceSpace', num(material.get('separation') or 0.0)),
                ('X', 0), ('Y', 0),
                ('ToolName', ''), ('ToolDiameter', num(tool_d)),
                ('CutOddmentsFlg', 2),
                ('Margin', ','.join([num(trim)] * 4)),
            ], '    ', close=False))

            out.append('      <Workpieces>')
            for w_index, data in enumerate(sheet.get('workpieces') or [], start=1):
                piece = Workpiece(data, tool_d, w_index, policy=policy)
                out.extend(piece.to_xml('        ', meta))
            out.append('      </Workpieces>')

            oddments = sheet.get('oddments') or []
            out.append('      <OddmentsList>')
            for o_index, odd in enumerate(oddments, start=1):
                pairs = [('Index', o_index), ('Type', 1 if odd.get('keep') else 0)]
                if odd.get('keep'):
                    pairs += [('Length', num(odd.get('length'))),
                              ('Width', num(odd.get('width')))]
                out.append(_tag('Oddments', pairs, '        ', close=False))
                ox, oy = float(odd.get('x', 0)), float(odd.get('y', 0))
                ol, ow = float(odd.get('length', 0)), float(odd.get('width', 0))
                ring = ((ox, oy), (ox + ol, oy), (ox + ol, oy + ow), (ox, oy + ow), (ox, oy))
                out.append(_tag('Lineament', [('RotationAngle', 0), ('X', num(ox)),
                                              ('Y', num(oy))], '          ', close=False))
                out.append('            <Points>')
                for i, (px, py) in enumerate(ring, start=1):
                    out.append(_tag('Point', [('Index', i), ('X', num(px)),
                                              ('Y', num(py)), ('Angle', 0)],
                                    '              '))
                out.append('            </Points>')
                out.append(_tag('CutInfos', [('SamllWorkpieceFlg', 'true')],
                                '            ', close=False))
                out.append(_tag('CutInfo', [('CutNo', 1), ('ToolDirection', 0),
                                            ('SlopeLen', num(SLOPE_LEN))],
                                '              '))
                out.append('            </CutInfos>')
                out.append('          </Lineament>')
                out.append('        </Oddments>')
            out.append('      </OddmentsList>')
            out.append('    </Pattern>')
        out.append('  </Patterns>')
    out.append('</FccRoot>')
    return '\n'.join(out) + '\n'


def check(job, policy=None):
    """Warnings a machinist would want before the file reaches the line: parts
    that overhang the sheet, gaps too narrow for the tool, missing banding data.
    Returns a list of strings; empty means nothing looked wrong."""
    policy = policy or SIZE_POLICY
    problems = []
    for material in job.get('materials') or []:
        tool_d = float(material.get('tool_diameter') or 6.0)
        trim = float(material.get('trim') or 0.0)
        gap = float(material.get('separation') or 0.0)
        sheet_l = float(material.get('length') or 0.0)
        sheet_w = float(material.get('width') or 0.0)
        label = material.get('name') or 'material'
        if gap < tool_d:
            problems.append(
                f'{label}: part gap {num(gap)} mm is narrower than the {num(tool_d)} mm '
                f'tool — neighbouring cuts would overlap. Raise the separation in Sheets.')
        if trim < tool_d / 2.0:
            problems.append(
                f'{label}: edge trim {num(trim)} mm is less than the tool radius '
                f'{num(tool_d / 2.0)} mm — the outermost cut would run off the sheet.')
        for p_index, sheet in enumerate(material.get('sheets') or [], start=1):
            for data in sheet.get('workpieces') or []:
                piece = Workpiece(data, tool_d, 1, policy=policy)
                name = data.get('name') or data.get('workpiece_id') or 'part'
                if piece.lin_x < -1e-6 or piece.lin_y < -1e-6 or \
                        piece.lin_x + piece.lin_length > sheet_l + 1e-6 or \
                        piece.lin_y + piece.lin_width > sheet_w + 1e-6:
                    problems.append(
                        f'{label} sheet {p_index}: the cut path for "{name}" runs past '
                        f'the sheet edge.')
                if piece.thickness <= 0:
                    problems.append(f'{label}: "{name}" has no thickness.')
                for face, edge in piece.edges.items():
                    if edge and not edge.get('thickness'):
                        problems.append(
                            f'{label}: "{name}" face {face} is banded with '
                            f'"{edge.get("name") or "?"}", which has no thickness in the '
                            f'Sheets library — the finished size will be wrong.')
    return problems
