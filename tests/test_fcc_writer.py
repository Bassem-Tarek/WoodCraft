"""Checks the FCC writer against the rules read off a production file.

Runs anywhere — no Fusion, no dependencies:  python3 tests/test_fcc_writer.py
"""
import importlib.util
import os
import sys
import xml.etree.ElementTree as ET

# Load the writer straight from its file. Importing it as `commands.fcc_writer`
# would execute the add-in's package __init__, which imports every command and
# therefore the Fusion API — the one thing this test exists to avoid needing.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    'fcc_writer', os.path.join(os.path.dirname(_HERE), 'commands', 'fcc_writer.py'))
W = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(W)

FAILS = []


def check(label, condition, detail=''):
    if condition:
        print(f'  ok   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILS.append(label)


def eq(label, got, want, tol=1e-6):
    ok = abs(float(got) - float(want)) <= tol
    check(label, ok, f'got {got}, want {want}')


BAND = {'name': 'PVC White 0.8', 'thickness': 0.8, 'pre_milling': 0.3,
        'width': 22.0, 'code': 'PVCW08', 'grade': 'A'}
BARE_BAND = {'name': 'PVC White 0.8', 'thickness': 0.8, 'pre_milling': 0.0,
             'width': 22.0, 'code': 'PVCW08', 'grade': 'A'}

JOB = {
    'meta': {'OrderNo': 'EMAAR-0042', 'Customer': 'Emaar', 'Designer': 'BT'},
    'materials': [{
        'name': 'MR MDF White 18mm', 'length': 2440.0, 'width': 1220.0,
        'thickness': 18.0, 'grain': 'N',
        'tool_diameter': 6.0, 'separation': 7.0, 'trim': 10.0,
        'sheets': [{
            'workpieces': [
                {   # all four edges banded and pre-milled -> +0.5 per edge
                    'workpiece_id': 'WC00001', 'name': 'Base 600 / Bottom',
                    'material': 'MR MDF White 18mm',
                    'length': 561.0, 'width': 567.0, 'thickness': 18.0,
                    'x': 10.0, 'y': 10.0, 'rotated': False, 'grain': 'N',
                    'edges': {1: BAND, 2: BAND, 3: BAND, 4: BAND},
                    'holes': [
                        {'id': 1, 'type': 2, 'face': 5, 'u': 60.0, 'v': 50.0,
                         'diameter': 8.0, 'depth': 12.0},
                        {'id': 2, 'type': 1, 'face': 4, 'u': 0.0, 'v': 100.0,
                         'z': 9.0, 'diameter': 8.0, 'depth': 30.0},
                    ],
                    'slots': [{'id': 3, 'face': 6, 'u1': 0.0, 'v1': 80.0,
                               'u2': 561.0, 'v2': 80.0, 'width': 6.0, 'depth': 8.0,
                               'tool_offset': 0}],
                },
                {   # one edge banded, bare (no pre-mill) -> no growth by default
                    'workpiece_id': 'WC00002', 'name': 'Base 600 / Back rail',
                    'material': 'MR MDF White 18mm',
                    'length': 600.0, 'width': 150.0, 'thickness': 18.0,
                    'x': 600.0, 'y': 10.0, 'rotated': False, 'grain': 'L',
                    'edges': {3: BARE_BAND},
                    'holes': [],
                },
                {   # turned 90 by the nester
                    'workpiece_id': 'WC00003', 'name': 'Tall 400 / Side',
                    'material': 'MR MDF White 18mm',
                    'length': 700.0, 'width': 300.0, 'thickness': 18.0,
                    'x': 1300.0, 'y': 10.0, 'rotated': True, 'grain': 'W',
                    'edges': {2: BAND, 4: BAND},
                    'holes': [{'id': 1, 'type': 1, 'face': 2, 'u': 35.0, 'v': 0.0,
                               'z': 9.0, 'diameter': 8.0, 'depth': 30.0}],
                },
            ],
            'oddments': [{'x': 1700.0, 'y': 10.0, 'length': 700.0, 'width': 400.0,
                          'keep': True}],
        }],
    }],
}

xml_text = W.build(JOB)
root = ET.fromstring(xml_text)
wps = root.findall('.//Workpiece')
wp1, wp2, wp3 = wps

print('\nStructure')
check('parses as XML', root.tag == 'FccRoot')
check('three workpieces', len(wps) == 3, f'got {len(wps)}')
check('root flags match the machine file',
      root.get('Version') == '2' and root.get('CreateG') == 'true'
      and root.get('WorkpieceCutting') == 'true')
check('no post-nest fields leak in',
      root.find('.//Pattern').get('NcFileName') is None
      and root.find('.//Labels') is None)

print('\nSizes (tool diameter 6)')
eq('Length = CutLength + tool diameter',
   float(wp1.get('Length')), float(wp1.get('CutLength')) + 6.0)
eq('Width = CutWidth + tool diameter',
   float(wp1.get('Width')), float(wp1.get('CutWidth')) + 6.0)
bench1 = wp1.find('BenchmarkInfo')
eq('four pre-milled edges add 0.5 each on length',
   float(bench1.get('ProLength')), 561.0 + 1.0)
eq('four pre-milled edges add 0.5 each on width',
   float(bench1.get('ProWidth')), 567.0 + 1.0)
bench2 = wp2.find('BenchmarkInfo')
eq('a banded edge with no pre-milling adds nothing (default policy)',
   float(bench2.get('ProLength')), 600.0)

print('\nDatum')
lin1 = wp1.find('Lineament')
eq('tool path starts a radius before the cut', float(lin1.get('X')), 10.0 - 3.0)
eq('ProOffsetX = radius - growth on the X-min edge',
   float(lin1.get('ProOffsetX')), 3.0 - 0.5)
eq('ProOffsetY = radius - growth on the Y-min edge',
   float(lin1.get('ProOffsetY')), 3.0 - 0.5)
datum_x = float(lin1.get('X')) + float(lin1.get('ProOffsetX'))
datum_y = float(lin1.get('Y')) + float(lin1.get('ProOffsetY'))
eq('BenchmarkInfo names the first hole, measured from the datum',
   datum_x + float(bench1.get('ProMachiningX')), 10.0 + 60.0)
eq('...and on Y', datum_y + float(bench1.get('ProMachiningY')), 10.0 + 50.0)

hole1 = wp1.findall('Holes/Hole')[0]
eq('vertical hole sheet X = cut origin + local U', float(hole1.get('X')), 70.0)
eq('vertical hole sheet Y = cut origin + local V', float(hole1.get('Y')), 60.0)
check('vertical hole carries no Z', hole1.get('Z') is None)
hole2 = wp1.findall('Holes/Hole')[1]
check('horizontal hole carries Z', hole2.get('Z') == '9')
check('horizontal hole is Type 1 on an edge face',
      hole2.get('Type') == '1' and hole2.get('Face') == '4')

print('\nEdgebanding')
check('EBL1 is Face 2', wp2.get('EBL1') == '')
check('EBW2 is Face 3', wp2.get('EBW2').startswith('0.8*22*PVCW08*A*SS*P01*'))
check('the only banded edge is banded first', wp2.get('EBW2').endswith('*1'))
codes = [wp1.get(a) for a in ('EBW2', 'EBW1', 'EBL1', 'EBL2')]
check('four-sided panel bands ends first, then sides',
      [c.rsplit('*', 1)[1] for c in codes] == ['1', '2', '3', '4'],
      f'got {[c.rsplit("*", 1)[1] for c in codes]}')
edges1 = {e.get('Face'): e for e in wp1.findall('EdgeGroup/Edge')}
check('EdgeGroup lists all four faces', sorted(edges1) == ['1', '2', '3', '4'])
check('bare faces are written as zero thickness',
      {e.get('Face'): e.get('Thickness') for e in wp2.findall('EdgeGroup/Edge')}
      == {'1': '0', '2': '0', '3': '0.8', '4': '0'})
outline1 = wp1.findall('FccOutline/FccOutlinePoint')
eq('outline spans the finished length', float(outline1[1].get('X')), 562.0)
eq('outline spans the finished width', float(outline1[2].get('Y')), 568.0)
check('outline points carry the arriving segment\'s banding',
      all(p.get('EdgeThickness') == '0.8' for p in outline1))

print('\nRotated part')
check('rotation is recorded', wp3.get('RotateAngle') == '90')
eq('CutLength is the X extent on the sheet', float(wp3.get('CutLength')), 300.0)
eq('CutWidth is the Y extent on the sheet', float(wp3.get('CutWidth')), 700.0)
check('design-frame Face 4 lands on placement Face 2',
      wp3.get('EBL1') != '' and wp3.get('EBW1') == '')
check('design-frame Face 2 lands on placement Face 3', wp3.get('EBW2') != '')
h3 = wp3.find('Holes/Hole')
eq('rotated hole X = cut origin + (width - v)', float(h3.get('X')), 1300.0 + 300.0)
eq('rotated hole Y = cut origin + u', float(h3.get('Y')), 10.0 + 35.0)
check('its face rotates with it', h3.get('Face') == '3')

print('\nLead-in points')
cut_infos = wp1.find('Lineament/CutInfos')
pts = [p for p in cut_infos.get('ToolPointList').split(';') if p]
check('one candidate per side', len(pts) == 4)
x0, y0 = float(lin1.get('X')), float(lin1.get('Y'))
x1, y1 = x0 + float(wp1.get('Length')), y0 + float(wp1.get('Width'))
on_path = []
for p in pts:
    px, py, _ = p.rstrip('$').split(',')
    px, py = float(px), float(py)
    on_path.append(abs(px - x0) < 1e-6 or abs(px - x1) < 1e-6
                   or abs(py - y0) < 1e-6 or abs(py - y1) < 1e-6)
check('every candidate sits on the tool path', all(on_path))
check('side indices are 0-3',
      [p.rstrip('$').split(',')[2] for p in pts] == ['0', '1', '2', '3'])

print('\nOffcuts')
odd = root.find('.//Oddments')
check('a keepable remnant is Type 1 with a real size',
      odd.get('Type') == '1' and odd.get('Length') == '700')

print('\nPolicy switch')
alt = ET.fromstring(W.build(JOB, policy=W.SIZE_POLICY_ALL_BANDED))
alt2 = alt.findall('.//Workpiece')[1]
eq('under all_banded a bare-banded edge adds its full thickness',
   float(alt2.find('BenchmarkInfo').get('ProLength')), 600.8)

print('\nWarnings')
bad = {'materials': [dict(JOB['materials'][0], separation=3.0, trim=1.0)]}
warnings = W.check(bad)
check('a gap narrower than the tool is reported',
      any('narrower than' in w for w in warnings))
check('a trim smaller than the tool radius is reported',
      any('tool radius' in w for w in warnings))
check('a clean job reports nothing', W.check(JOB) == [], W.check(JOB))

print()
if FAILS:
    print(f'{len(FAILS)} FAILED: ' + '; '.join(FAILS))
    sys.exit(1)
print('all checks passed')
