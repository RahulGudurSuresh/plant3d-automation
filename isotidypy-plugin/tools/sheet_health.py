"""Every check learned so far, one pass, one sheet.
  1  detect() overlaps                6  own-interior runs (incl dogleg)
  2  audit A-D                        7  threading (>8 mm in comfort zone)
  3  R1/R2/R3                         8  hard line pad (1.0 mm, outside hubs)
  4  vestigial doglegs                9  leader-leader sep/crossings
  5  AnnoRect render-width + center  10  labels on title-block table
                                     11  welds spanning leader-owners
Usage: sheet_health.py <dxf> [table_x0 y0 x1 y1]
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, r"d:\AI_Rahul\Automation\Plant3D")
from pathlib import Path

import ezdxf
from ezdxf import bbox as ezb
from shapely.geometry import LineString, Point, box as sbox
from shapely.ops import unary_union

from isotidy import JP1071_ROLES, Tuning, detect
from isotidy.extract import extract, _flatten_entity
from tools.mess_map import audit

P = Path(sys.argv[1])
TABLE = sbox(*map(float, sys.argv[2:6])) if len(sys.argv) > 5 else None
cfg = Tuning()
LINE_PAD, COMFORT, THREAD_MAX, SEP, HUB = 1.0, 2.5, 8.0, 1.5, 3.0

doc = ezdxf.readfile(P)
msp = doc.modelspace()
_d, sc = extract(P, JP1071_ROLES, cfg)
issues = []

# 1 detect
rep = detect(sc, cfg)
for c in rep.collisions:
    a = sc.labels[c.a]
    other = (f"label {sc.labels[c.b].text.strip()[:16]!r}"
             if c.b is not None else "geometry")
    issues.append(f"OVERLAP {c.area:.2f} mm2: {a.text.strip()[:16]!r} "
                  f"@({a.center()[0]:.0f},{a.center()[1]:.0f}) x {other}")

# 2 audit
found, _c = audit(P, JP1071_ROLES, cfg)
for c, x, y, d in found:
    if c in "ABCD":
        issues.append(f"AUDIT-{c} @({x:.0f},{y:.0f}) {d}")

# leader paths incl doglegs
paths, tips = {}, {}
for m in msp.query("MULTILEADER"):
    try:
        l2 = m.context.leaders[0]
        lp = l2.last_leader_point
        pts = [(v.x, v.y) for v in l2.lines[0].vertices] + [(lp.x, lp.y)]
        dl = getattr(l2, "dogleg_length", 0) or 0
        if m.dxf.get("has_dogleg", 0) and dl > 0.01:
            dv = l2.dogleg_vector
            pts.append((lp.x + dv.x * dl, lp.y + dv.y * dl))
        paths[m.dxf.handle] = LineString(pts)
        tips[m.dxf.handle] = pts[0]
    except Exception:
        pass
hub_all = unary_union([Point(*t).buffer(HUB) for t in tips.values()]) \
    if tips else None
own_of = {l.leader_handle: l for l in sc.labels if l.leader_handle}
group_of = {}
for l in sc.labels:
    group_of[l.handle] = l.group

# 3 R1/R2/R3 (R1/R2 approximated by detect+audit; R3 here)
hs = list(paths)
for i in range(len(hs)):
    for j in range(i + 1, len(hs)):
        if paths[hs[i]].crosses(paths[hs[j]]):
            p = paths[hs[i]].intersection(paths[hs[j]])
            ta, tb = tips[hs[i]], tips[hs[j]]
            c = p.centroid
            # shared-arrowhead float noise is not a crossing
            if (abs(c.x - ta[0]) < 0.5 and abs(c.y - ta[1]) < 0.5
                    and abs(c.x - tb[0]) < 0.5 and abs(c.y - tb[1]) < 0.5):
                continue
            issues.append(f"R3 leaders {hs[i]} x {hs[j]} cross at "
                          f"({c.x:.0f},{c.y:.0f})")

# 4 vestigial doglegs: dogleg pointing away from the leader's own label
for m in msp.query("MULTILEADER"):
    try:
        l2 = m.context.leaders[0]
        dl = getattr(l2, "dogleg_length", 0) or 0
        if not (m.dxf.get("has_dogleg", 0) and dl > 0.1):
            continue
        lab = own_of.get(m.dxf.handle)
        lp = l2.last_leader_point
        dv = l2.dogleg_vector
        end = (lp.x + dv.x * dl, lp.y + dv.y * dl)
        if lab is None:
            # a second connector leader whose dogleg ENDS at some text is
            # an attachment (355M01_r0-1: 1.9 mm); one pointing at nothing
            # is a phantom line
            if all(sbox(*l.box().bounds).distance(Point(*end)) > 3.0
                   for l in sc.labels):
                issues.append(f"DOGLEG {m.dxf.handle}: {dl:.1f} mm on an "
                              f"unclaimed leader points at nothing")
            continue
        b = lab.box().bounds
        d_land = sbox(*b).distance(Point(lp.x, lp.y))
        d_end = sbox(*b).distance(Point(*end))
        if d_end > d_land + 0.5:
            issues.append(f"DOGLEG {m.dxf.handle}: {dl:.1f} mm points AWAY "
                          f"from {lab.text.strip()[:14]!r}")
    except Exception:
        pass

# 5 AnnoRect frames
for e in msp.query("INSERT"):
    if not (e.dxf.name or "").startswith("AnnoRect") or not e.attribs:
        continue
    rects = [ve for ve in e.virtual_entities()
             if ve.dxftype() == "LWPOLYLINE"]
    if not rects:
        continue
    rb = ezb.extents(rects, fast=False)
    a = e.attribs[0]
    ab = ezb.extents([a], fast=False)
    wf = a.dxf.width if a.dxf.width else 1.0
    render_w = ab.size.x / wf
    off = abs((ab.extmin.x + ab.extmax.x) / 2
              - (rb.extmin.x + rb.extmax.x) / 2)
    if render_w > rb.size.x - 0.2:
        issues.append(f"FRAME {a.dxf.text!r}: render {render_w:.1f} vs "
                      f"rect {rb.size.x:.1f} -- clips")
    if off > 0.5:
        issues.append(f"FRAME {a.dxf.text!r}: text center off rect "
                      f"center by {off:.1f} mm")

# 6 own-interior runs
for h, line in paths.items():
    lab = own_of.get(h)
    if lab is None:
        continue
    b = lab.box().bounds
    run = line.intersection(sbox(b[0] + .05, b[1] + .05,
                                 b[2] - .05, b[3] - .05))
    if getattr(run, "length", 0) > 2.0:
        issues.append(f"OWN-RUN {h}: leader runs {run.length:.1f} mm "
                      f"through its own {lab.text.strip()[:14]!r}")

# 7+8 threading and hard pad
for h, line in paths.items():
    t = line.difference(hub_all) if hub_all is not None else line
    own = own_of.get(h)
    og = own.group if own is not None else None
    for l in sc.labels:
        if own is not None and (l.handle == own.handle or
                                (og is not None and l.group == og)):
            continue
        b = l.box().bounds
        hard = sbox(b[0] - LINE_PAD, b[1] - LINE_PAD,
                    b[2] + LINE_PAD, b[3] + LINE_PAD)
        if t.intersects(hard):
            issues.append(f"LINE-PAD {h}: leader within {LINE_PAD} mm of "
                          f"{l.text.strip()[:14]!r} "
                          f"@({l.center()[0]:.0f},{l.center()[1]:.0f})")
            continue
        soft = sbox(b[0] - COMFORT, b[1] - COMFORT,
                    b[2] + COMFORT, b[3] + COMFORT)
        run = t.intersection(soft)
        if getattr(run, "length", 0) > THREAD_MAX:
            issues.append(f"THREAD {h}: leader runs "
                          f"{run.length:.1f} mm alongside "
                          f"{l.text.strip()[:14]!r}")

# 9 separation (twins fanning from ONE shared arrowhead are exempt --
#   they read as a fan, not a near-crossing; crossings are still charged
#   in check 3)
for i in range(len(hs)):
    ti = paths[hs[i]].difference(hub_all)
    for j in range(i + 1, len(hs)):
        ta, tb = tips[hs[i]], tips[hs[j]]
        if abs(ta[0] - tb[0]) < 0.5 and abs(ta[1] - tb[1]) < 0.5:
            continue
        d = ti.distance(paths[hs[j]].difference(hub_all))
        if 0 < d < SEP:
            issues.append(f"SEP leaders {hs[i]} x {hs[j]}: {d:.2f} mm apart")

# 10 table
if TABLE is not None:
    for l in sc.labels:
        if TABLE.intersects(sbox(*l.box().bounds)):
            issues.append(f"ON-TABLE {l.text.strip()[:14]!r}")

# 11 welds
groups = {}
for l in sc.labels:
    if l.group is not None:
        groups.setdefault(l.group, []).append(l)
for g, mem in groups.items():
    owners = {l.leader_handle for l in mem if l.leader_handle}   # DISTINCT
    if len(owners) > 1:
        issues.append(f"WELD g{g} has {len(owners)} leader-owners: "
                      f"{[l.text.strip()[:12] for l in mem]}")

print(f"=== {P.name}: {len(issues)} issue(s)")
for s in issues:
    print("  " + s)
if not issues:
    print("  clean by every rule")
