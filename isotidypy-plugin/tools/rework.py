"""
Fleet rework pass (2026-09-07): every defect class the 470M01 / 164M05 /
202M01 sessions named, applied per sheet with per-stage verification.

Stages, each reverted alone if it makes the audit worse:
  1  DOGLEG HYGIENE   vestigial doglegs (running through the own label,
     pointing away from it, or being a leader's ONLY attachment) are
     stripped and the leader re-landed on the label's near edge.  A
     dogleg pointing into its text start -- ISOGEN's proper pattern --
     is kept.
  2  FAR-EDGE LANDINGS  leader (dogleg included) runs >2 mm through its
     own label -> re-land on the nearest clean edge.
  3  FRAMES            AnnoRect rect sized for the RENDER width
     (metric / width factor + 2 mm margins), attrib re-centred; welded
     stacks keep the rect's left edge on the column line.
  4  TABLE             if any label sits on the title-block DESIGN DATA
     table (rect measured from the block's own rule lines), the table is
     injected as a priced obstacle + free-grid stamp and the standard
     solve->apply rounds run (the 202M01 recipe).

Never-worse: arrow tips are never touched; every stage must not increase
measured overlap or audit B/C/D counts, or it is rolled back.

Run:  python tools/rework.py <TIDIED.dxf> [--site=jp1071]
"""

from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ezdxf
from ezdxf import bbox as ezb
from ezdxf.math import Vec3
from shapely.geometry import LineString, Point, box as sbox
from shapely.ops import unary_union
from shapely.strtree import STRtree

from isotidy import DEFAULT_ROLES, JP1071_ROLES, Tuning, detect
from isotidy.writeback import save_dxf  # noqa: E402
from isotidy.extract import component_symbols, extract
from isotidy.leaders import symbol_hits
from isotidy.solve import solve
from isotidy.writeback import apply as wb_apply
from tools.mess_map import audit

SITES = {"default": DEFAULT_ROLES, "jp1071": JP1071_ROLES}
HUB, SEP, OWN_TOL = 3.0, 1.5, 2.0
COMFORT, THREAD_MAX = 2.5, 8.0   # aesthetic bar: no leader runs >8 mm
                                 # inside a text's 2.5 mm comfort zone
FRAME_MARGIN = 2.0
STACK_ALIGN = 0.3


# ---------------------------------------------------------------------------
def _leader_data(msp):
    """handle -> (ml, tip, path-with-dogleg, dogleg_len, dogleg_end)."""
    out = {}
    for ml in msp.query("MULTILEADER"):
        try:
            ld = ml.context.leaders[0]
            lp = ld.last_leader_point
            pts = [(v.x, v.y) for v in ld.lines[0].vertices] + [(lp.x, lp.y)]
            dl = getattr(ld, "dogleg_length", 0) or 0
            end = None
            if ml.dxf.get("has_dogleg", 0) and dl > 0.01:
                dv = ld.dogleg_vector
                end = (lp.x + dv.x * dl, lp.y + dv.y * dl)
                pts = pts + [end]
            out[ml.dxf.handle] = (ml, pts[0], LineString(pts), dl, end)
        except Exception:
            pass
    return out


def _score(path, roles, cfg):
    _d, s = extract(path, roles, cfg)
    rep = detect(s, cfg)
    found, _c = audit(path, roles, cfg)
    bcd = sum(1 for c, *_ in found if c in "BCD")
    return rep.overlap_area, bcd


HAIRLINE = 0.5   # mm2 -- same slack the ink veto allows (Tuning.ink_tolerance)


def _worse(base, new) -> bool:
    """The never-worse gate, judged lexicographically.

    Structural defects (leader crossings, weak associations, dimension
    drift) outrank overlap area: a stage that REMOVES some may cost up to a
    hairline of overlap; a stage that removes none may cost nothing.  The
    literal rule ('any overlap increase is worse') rolled back a realign on
    165M05_r0-2 that cleared two weak associations because the frame's new
    spot measured 0.008 mm2 more overlap -- the rule defeating its purpose.
    """
    ov0, bcd0 = base
    ov1, bcd1 = new
    if bcd1 > bcd0:
        return True
    if bcd1 < bcd0:
        return ov1 > ov0 + HAIRLINE
    return ov1 > ov0 + 1e-9


class Ctx:
    def __init__(self, path, roles, cfg):
        self.doc = ezdxf.readfile(path)
        self.msp = self.doc.modelspace()
        _d, self.sc = extract(path, roles, cfg)
        self.symbols = component_symbols(self.doc)
        self.lds = _leader_data(self.msp)
        self.hub = unary_union([Point(*v[1]).buffer(HUB)
                                for v in self.lds.values()]) \
            if self.lds else None
        self.cfg = cfg
        self.by_leader = {l.leader_handle: l for l in self.sc.labels
                          if l.leader_handle}

    def line_clean(self, pts, own, skip_handles):
        cfg = self.cfg
        line = LineString(pts)
        t = line.difference(self.hub) if self.hub is not None else line
        tip = pts[0]
        for h2, (m2, t2, p2, _dl, _e) in self.lds.items():
            if h2 in skip_handles:
                continue
            if t.crosses(p2):
                return False
            twins = abs(t2[0] - tip[0]) < 0.5 and abs(t2[1] - tip[1]) < 0.5
            if not twins and t.distance(p2.difference(self.hub)) < SEP:
                return False
        og = own.group if own is not None else None
        for o in self.sc.labels:
            if own is not None and (o.handle == own.handle or
                                    (og is not None and o.group == og)):
                continue
            if line.intersects(o.box(cfg.clearance + cfg.geom_buffer)):
                return False
            # THREADING: a leader may cross a text's comfort zone briefly,
            # never run alongside it (202M01: 50 mm through a 2 mm gap
            # passed every touch test and read as garbage).
            soft = o.box(COMFORT)
            if getattr(t.intersection(soft), "length", 0) > THREAD_MAX:
                return False
        if own is not None:
            b = own.box().bounds
            run = line.intersection(sbox(b[0] + .05, b[1] + .05,
                                         b[2] - .05, b[3] - .05))
            if getattr(run, "length", 0) > OWN_TOL:
                return False
        if symbol_hits(line, tip, pts[-1], self.symbols):
            return False
        return True

    def reland(self, handle, land):
        ml = self.lds[handle][0]
        ld = ml.context.leaders[0]
        tip = ld.lines[0].vertices[0]
        ld.lines[0].vertices = [tip]
        ld.last_leader_point = Vec3(land[0], land[1], 0)
        ld.dogleg_length = 0.0
        # BOTH copies of the dogleg state, context AND entity: a record
        # saying has_dogleg=0 with a stored length is exactly where
        # AutoCAD's regeneration and our renderer can disagree.
        if ml.dxf.hasattr("has_dogleg"):
            ml.dxf.has_dogleg = 0
        if ml.dxf.hasattr("dogleg_length"):
            ml.dxf.dogleg_length = 0.0
        ml.context.base_point = Vec3(land[0], land[1], 0)
        ml.proxy_graphic = None

    def near_edge_reland(self, handle, lab):
        """Try edge midpoints + corners of lab's box, nearest tip first."""
        tip = self.lds[handle][1]
        x0, y0, x1, y1 = lab.box().bounds
        cands = [(x0, (y0 + y1) / 2), (x1, (y0 + y1) / 2),
                 ((x0 + x1) / 2, y0), ((x0 + x1) / 2, y1),
                 (x0, y0), (x1, y0), (x0, y1), (x1, y1)]
        cands.sort(key=lambda c: (c[0] - tip[0]) ** 2 + (c[1] - tip[1]) ** 2)
        for c in cands:
            if self.line_clean([tip, c], lab, {handle}):
                self.reland(handle, c)
                self.lds[handle] = (self.lds[handle][0], tip,
                                    LineString([tip, c]), 0.0, None)
                return c
        return None


# ---------------------------------------------------------------------------
def stage_doglegs(ctx) -> int:
    fixed = 0
    for h in list(ctx.lds):
        ml, tip, path, dl, end = ctx.lds[h]
        if dl <= 0.1 or end is None:
            continue
        lab = ctx.by_leader.get(h)
        land = path.coords[-2]                      # before dogleg end
        if lab is None:
            # attachment-by-dogleg (202M01 C-case): a label hugging the
            # dogleg END is the intended owner
            target = None
            for l in ctx.sc.labels:
                if l.leader_handle is None and \
                        sbox(*l.box().bounds).distance(Point(*end)) < 3.0:
                    target = l           # 355M01_r0-1: 1.9 mm, missed at 1.5
                    break
            if target is None:
                # A second connector leader whose dogleg points at NOTHING
                # (469M01_r0-3: 41.7 mm, nearest text 22 mm away) is a
                # phantom line.  Strip the dogleg, keep the leader: never
                # worse.  A dogleg ending within 3 mm of some text is an
                # attachment and is left alone.
                if dl > 5.0 and all(
                        sbox(*l.box().bounds).distance(Point(*end)) > 3.0
                        for l in ctx.sc.labels):
                    ml.context.leaders[0].dogleg_length = 0.0
                    if ml.dxf.hasattr("has_dogleg"):
                        ml.dxf.has_dogleg = 0
                    if ml.dxf.hasattr("dogleg_length"):
                        ml.dxf.dogleg_length = 0.0
                    ml.proxy_graphic = None
                    ctx.lds[h] = (ml, tip, LineString([tip, land]), 0.0, None)
                    print(f"    {h}: {dl:.1f} mm dogleg on an unclaimed "
                          f"leader points at nothing -- stripped")
                    fixed += 1
                continue                            # true connector: leave
            if ctx.near_edge_reland(h, target):
                print(f"    {h}: dogleg was the only attachment -- "
                      f"re-landed ON {target.text.strip()[:14]!r}")
                fixed += 1
            continue
        b = sbox(*lab.box().bounds)
        run = path.intersection(sbox(*[v + s for v, s in
                                       zip(lab.box().bounds,
                                           (.05, .05, -.05, -.05))]))
        through_own = getattr(run, "length", 0) > OWN_TOL
        away = b.distance(Point(*end)) > b.distance(Point(*land)) + 0.5
        if not (through_own or away):
            continue                                # proper pattern: keep
        if ctx.near_edge_reland(h, lab):
            why = "runs through its own text" if through_own \
                else "points away from its label"
            print(f"    {h}: dogleg {why} -- stripped, re-landed "
                  f"({lab.text.strip()[:14]!r})")
            fixed += 1
        elif away:
            # No clean edge to re-land on, but a dogleg pointing AWAY from
            # its own label is a phantom line whatever else is true
            # (166M05_r0-3: 47 mm over a dimension text).  Removing it
            # and keeping the landing is never worse.
            ml.context.leaders[0].dogleg_length = 0.0
            if ml.dxf.hasattr("has_dogleg"):
                ml.dxf.has_dogleg = 0
            if ml.dxf.hasattr("dogleg_length"):
                ml.dxf.dogleg_length = 0.0
            ml.proxy_graphic = None
            ctx.lds[h] = (ml, tip, LineString(list(path.coords[:-1])), 0.0, None)
            print(f"    {h}: dogleg points away from its label and no clean "
                  f"reland exists -- dogleg stripped, landing kept")
            fixed += 1
        else:
            print(f"    {h}: bad dogleg but no clean reland -- left")
    return fixed


def stage_realign(ctx) -> int:
    """Frames whose rect drifted off their stack's column line.

    The 2026-09-03 frame widening grew rects about their CENTRE, so a tag
    that had been left-aligned with its OPERATOR text and balloon ended up
    3-6 mm off the column; the welder (0.3 mm tolerance) then saw two
    things instead of one stack, and the leader on the tag stopped
    'speaking for' the text and balloon -- 12 phantom C findings on 6
    sheets.  Shift the FRAME (rect, attrib and its leader landing together)
    back onto the column; the texts stay where ISOGEN put them."""
    cfg = ctx.cfg
    pad = cfg.clearance + cfg.geom_buffer
    fixed = 0
    for e in ctx.msp.query("INSERT"):
        if not (e.dxf.name or "").startswith("AnnoRect") or not e.attribs:
            continue
        lab = next((l for l in ctx.sc.labels if l.handle == e.dxf.handle),
                   None)
        if lab is None:
            continue
        rects = [ve for ve in e.virtual_entities()
                 if ve.dxftype() == "LWPOLYLINE"]
        if not rects:
            continue
        rb = ezb.extents(rects, fast=False)
        fb = lab.box().bounds
        cands = []
        for o in ctx.sc.labels:
            if o.handle == lab.handle or o.kind == "dimension":
                continue
            ob = o.box().bounds
            vgap = max(ob[1] - fb[3], fb[1] - ob[3])
            if vgap < -0.5 or vgap > 4.0:          # STACK_GAP
                continue
            if ob[2] < rb.extmin.x - 2 or ob[0] > rb.extmax.x + 2:
                continue
            dx = ob[0] - rb.extmin.x
            if STACK_ALIGN < abs(dx) <= 12.0:
                cands.append((abs(dx), dx, o))
        if not cands:
            continue
        # the re-welded stack must end up with exactly ONE leader, whichever
        # member holds it (168M05_r0-2: the OPERATOR text owned it, not the
        # frame); two distinct leaders = two callouts, not a stack
        owners = {l.leader_handle for l in [lab] + [c[2] for c in cands]
                  if l.leader_handle}
        if len(owners) != 1:
            continue
        cands.sort(key=lambda t: t[0])
        dx = cands[0][1]
        mates = {c[2].handle for c in cands} | {lab.handle}
        bx = sbox(rb.extmin.x + dx - pad, rb.extmin.y - pad,
                  rb.extmax.x + dx + pad, rb.extmax.y + pad)
        blocked = any(bx.intersects(o.box(cfg.clearance))
                      for o in ctx.sc.labels if o.handle not in mates)
        blocked = blocked or any(
            bx.intersects(g) for g, own in zip(ctx.sc.obstacles, ctx.sc.owners)
            if own not in (lab.handle, lab.leader_handle))
        if ctx.sc.allowed is not None and not ctx.sc.allowed.contains(bx):
            blocked = True
        if blocked:
            print(f"    frame {e.attribs[0].dxf.text!r}: off its column by "
                  f"{dx:+.1f} mm but the aligned spot collides -- left (stated)")
            continue
        e.dxf.insert = Vec3(e.dxf.insert.x + dx, e.dxf.insert.y, e.dxf.insert.z)
        for a in e.attribs:
            a.dxf.insert = Vec3(a.dxf.insert.x + dx, a.dxf.insert.y, a.dxf.insert.z)
            if a.dxf.hasattr("align_point"):
                ap = a.dxf.align_point
                a.dxf.align_point = Vec3(ap.x + dx, ap.y, ap.z)
        # if the FRAME carries the leader its landing rides along (same rect
        # edge); a leader on a mate stays where it is
        if lab.leader_handle and lab.leader_handle in ctx.lds:
            ml = ctx.lds[lab.leader_handle][0]
            ld = ml.context.leaders[0]
            lp = ld.last_leader_point
            ld.last_leader_point = Vec3(lp.x + dx, lp.y, lp.z)
            ml.context.base_point = ld.last_leader_point
            ml.proxy_graphic = None
        print(f"    frame {e.attribs[0].dxf.text!r}: re-aligned {dx:+.1f} mm "
              f"onto its stack column ({len(cands)} mate(s) re-welded)")
        fixed += 1
    return fixed


def stage_landings(ctx) -> int:
    fixed = 0
    for h in list(ctx.lds):
        ml, tip, path, dl, end = ctx.lds[h]
        if dl > 0.1:
            continue                                # handled in stage 1
        lab = ctx.by_leader.get(h)
        if lab is None:
            continue
        b = lab.box().bounds
        run = path.intersection(sbox(b[0] + .05, b[1] + .05,
                                     b[2] - .05, b[3] - .05))
        if getattr(run, "length", 0) <= OWN_TOL:
            continue
        if ctx.near_edge_reland(h, lab):
            print(f"    {h}: leader ran {run.length:.1f} mm through its "
                  f"own {lab.text.strip()[:14]!r} -- re-landed")
            fixed += 1
        else:
            print(f"    {h}: own-run but no clean reland -- left")
    return fixed


CLAIM_TOL = 8.0        # an orphan leader landing this close to a leaderless
                       # stack was meant for it (JP1070-028LL: 5.5 mm off)


def _is_frame(ctx, lab) -> bool:
    e = ctx.doc.entitydb.get(lab.handle)
    return e is not None and e.dxftype() == "INSERT" \
        and (e.dxf.name or "").startswith("AnnoRect")


def stage_claims(ctx) -> int:
    """Attach orphan MULTILEADERs to the leaderless stack they were aimed at.

    ISOGEN can land a stack's leader on the corner of the stack's OVERALL
    bounding box -- a point no member touches -- so no label claims it, the
    audit calls the whole stack a weak association (C), and every later
    stage treats the stack as leaderless.  Re-land on the near edge of the
    frame member (or the label itself) so the attachment is real."""
    claimed = {l.leader_handle for l in ctx.sc.labels if l.leader_handle}
    fixed = 0
    for h in list(ctx.lds):
        if h in claimed:
            continue
        ml, tip, path, dl, end = ctx.lds[h]
        land = path.coords[-2] if end is not None else path.coords[-1]
        best = None
        for l in ctx.sc.labels:
            if l.leader_handle or l.kind not in ("balloon", "text"):
                continue
            mates = [m for m in ctx.sc.labels if m.group == l.group] \
                if l.group is not None else [l]
            if any(m.leader_handle for m in mates):
                continue
            # ISOGEN lands a stack's leader on the corner of the STACK's
            # overall extent -- often a point no single member touches
            # (JP1070-028LL: 11.4 mm from the nearest member, 0 mm from
            # the union).  Judge the claim against the union.
            ub = (min(m.box().bounds[0] for m in mates),
                  min(m.box().bounds[1] for m in mates),
                  max(m.box().bounds[2] for m in mates),
                  max(m.box().bounds[3] for m in mates))
            d = sbox(*ub).distance(Point(*land))
            if d <= CLAIM_TOL and (best is None or d < best[0]):
                best = (d, l)
        if best is None:
            continue
        target = best[1]
        if target.group is not None:
            mates = [m for m in ctx.sc.labels if m.group == target.group]
            target = next((m for m in mates if _is_frame(ctx, m)), target)
        if ctx.near_edge_reland(h, target):
            target.leader_handle = h
            ctx.by_leader[h] = target
            claimed.add(h)
            print(f"    {h}: orphan leader ({best[0]:.1f} mm off) claimed by "
                  f"{target.text.strip()[:16]!r}")
            fixed += 1
    return fixed


def relocate_and_widen(ctx, e, lab, need, rb) -> bool:
    return relocate_stack(ctx, lab, e=e, need=need, rb=rb)


def relocate_stack(ctx, lab, e=None, need=None, rb=None, why="") -> bool:
    """Move a label's whole stack to the nearest clean slot, re-landing its
    leader; with `e/need/rb` the frame member is widened to `need` there.

    Only for stacks that own a leader -- a relocated stack without one
    would trade the original defect for a weak association.  Used by:
    frames that cannot grow in place (470M01, JP1070-028LL), leaders that
    thread a text and cannot be re-landed clear, and residual grazes."""
    cfg, sc = ctx.cfg, ctx.sc
    members = [l for l in sc.labels
               if l.handle == lab.handle
               or (lab.group is not None and l.group == lab.group)]
    leader_h = next((l.leader_handle for l in members if l.leader_handle),
                    None)
    txt = e.attribs[0].dxf.text if e is not None else lab.text.strip()[:16]
    what = f"frame {txt!r}: needs {need:.1f} mm, cannot grow," \
        if e is not None else f"{txt!r}: {why}"
    if leader_h is None or leader_h not in ctx.lds:
        print(f"    {what} and its stack owns no leader -- left (stated)")
        return False
    tip = ctx.lds[leader_h][1]
    boxes = {}
    for m in members:
        b = m.box().bounds
        if e is not None and m.handle == lab.handle:
            stacked = any(
                abs(o.box().bounds[0] - rb.extmin.x) < STACK_ALIGN + 0.5
                for o in members if o.handle != lab.handle)
            x0 = rb.extmin.x if stacked else \
                (rb.extmin.x + rb.extmax.x) / 2 - need / 2
            b = (x0, rb.extmin.y, x0 + need, rb.extmax.y)
        boxes[m.handle] = b
    gx0 = min(b[0] for b in boxes.values())
    gy0 = min(b[1] for b in boxes.values())
    size = (max(b[2] for b in boxes.values()) - gx0,
            max(b[3] for b in boxes.values()) - gy0)
    own = {m.handle for m in members} | {leader_h}
    pad = cfg.clearance + cfg.geom_buffer
    trect = table_rect(ctx.doc)
    tpoly = sbox(*trect) if trect else None
    ink_tree = STRtree(sc.ink)

    def box_ok(b):
        bx = sbox(b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad)
        if tpoly is not None and tpoly.intersects(bx):
            return False
        if sc.allowed is not None and not sc.allowed.contains(bx):
            return False
        for o in sc.labels:
            if o.handle not in own and bx.intersects(o.box(cfg.clearance)):
                return False
        for g, o in zip(sc.obstacles, sc.owners):
            if o not in own and bx.intersects(g):
                return False
        if any(sc.ink[i].intersects(bx) for i in ink_tree.query(bx)):
            return False
        for h2, (_m, _t, p2, _d, _e) in ctx.lds.items():
            if h2 != leader_h and bx.intersects(p2):
                return False
        return True

    def line_ok(land, cand_boxes):
        line = LineString([tip, land])
        t = line.difference(ctx.hub) if ctx.hub is not None else line
        for h2, (_m, t2, p2, _d, _e) in ctx.lds.items():
            if h2 == leader_h:
                continue
            if t.crosses(p2):
                return False
            twins = abs(t2[0] - tip[0]) < 0.5 and abs(t2[1] - tip[1]) < 0.5
            if not twins and t.distance(p2.difference(ctx.hub)) < SEP:
                return False
        for o in sc.labels:
            if o.handle in own:
                continue
            if line.intersects(o.box(pad)):
                return False
            if getattr(t.intersection(o.box(COMFORT)), "length", 0) \
                    > THREAD_MAX:
                return False          # threading rule, as in Ctx.line_clean
        for b in cand_boxes.values():
            run = line.intersection(sbox(b[0] + .05, b[1] + .05,
                                         b[2] - .05, b[3] - .05))
            if getattr(run, "length", 0) > OWN_TOL:
                return False
        return not symbol_hits(line, tip, land, ctx.symbols)

    best = None
    for sx, sy in sc.free.open_slots((gx0, gy0), size, pad=pad,
                                     max_reach=60, step=2.0, limit=200,
                                     halo=1.5):
        dx, dy = sx - gx0, sy - gy0
        cb = {h: (b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy)
              for h, b in boxes.items()}
        if not all(box_ok(b) for b in cb.values()):
            continue
        fb = cb[lab.handle]
        cands = [(fb[0], (fb[1] + fb[3]) / 2), (fb[2], (fb[1] + fb[3]) / 2),
                 ((fb[0] + fb[2]) / 2, fb[1]), ((fb[0] + fb[2]) / 2, fb[3]),
                 (fb[0], fb[1]), (fb[2], fb[1]), (fb[0], fb[3]), (fb[2], fb[3])]
        cands.sort(key=lambda c: (c[0] - tip[0]) ** 2 + (c[1] - tip[1]) ** 2)
        for c in cands:
            if line_ok(c, cb):
                score = ((c[0] - tip[0]) ** 2 + (c[1] - tip[1]) ** 2) ** .5 \
                    + 2 * (dx * dx + dy * dy) ** .5
                if best is None or score < best[0]:
                    best = (score, dx, dy, c, fb)
                break
    if best is None:
        print(f"    {what} no clean slot within 60 mm for the stack -- "
              f"left (stated)")
        return False
    _s, dx, dy, land, fb = best

    # move every member (INSERT attribs travel explicitly -- never assumed)
    for m in members:
        ent = ctx.doc.entitydb.get(m.handle)
        if ent is None:
            continue
        ent.dxf.insert = Vec3(ent.dxf.insert.x + dx, ent.dxf.insert.y + dy,
                              ent.dxf.insert.z)
        if ent.dxftype() == "INSERT":
            for a in ent.attribs:
                a.dxf.insert = Vec3(a.dxf.insert.x + dx, a.dxf.insert.y + dy,
                                    a.dxf.insert.z)
                if a.dxf.hasattr("align_point"):
                    ap = a.dxf.align_point
                    a.dxf.align_point = Vec3(ap.x + dx, ap.y + dy, ap.z)
    if e is not None:
        # widen the frame about its centre, then pin its left edge
        att = e.attribs[0]
        e.dxf.xscale = e.dxf.xscale * need / rb.size.x
        rb2 = ezb.extents([ve for ve in e.virtual_entities()
                           if ve.dxftype() == "LWPOLYLINE"], fast=False)
        e.dxf.insert = Vec3(e.dxf.insert.x + (fb[0] - rb2.extmin.x),
                            e.dxf.insert.y, e.dxf.insert.z)
        ab = ezb.extents([att], fast=False)
        cx = fb[0] + need / 2
        off = (ab.extmin.x + ab.extmax.x) / 2 - cx
        for a in e.attribs:
            a.dxf.insert = Vec3(a.dxf.insert.x - off, a.dxf.insert.y,
                                a.dxf.insert.z)
            if a.dxf.hasattr("align_point"):
                ap = a.dxf.align_point
                a.dxf.align_point = Vec3(ap.x - off, ap.y, ap.z)
    ctx.reland(leader_h, land)
    ctx.lds[leader_h] = (ctx.lds[leader_h][0], tip, LineString([tip, land]),
                         0.0, None)
    grew = f", frame {rb.size.x:.1f} -> {need:.1f} mm" if e is not None else ""
    print(f"    {txt!r}: stack moved ({dx:+.1f},{dy:+.1f}){grew}, "
          f"leader re-landed")
    return True


def stage_threads(ctx) -> int:
    """Leaders that thread a text's comfort zone, graze its hard pad, or run
    <1.5 mm from another leader: first try another edge of the same label,
    then move the stack."""
    cfg = ctx.cfg
    fixed = 0
    for h in list(ctx.lds):
        lab = ctx.by_leader.get(h)
        if lab is None:
            continue
        ml, tip, path, dl, end = ctx.lds[h]
        pts = list(path.coords)
        # re-run the shared rule set on the CURRENT path; clean => skip
        if ctx.line_clean(pts, lab, {h}):
            continue
        b = lab.box()
        if ctx.near_edge_reland(h, lab):
            print(f"    {h}: leader re-landed on another edge of "
                  f"{lab.text.strip()[:16]!r} (was threading/grazing)")
            fixed += 1
        elif relocate_stack(ctx, lab, why="leader threads or grazes text"):
            fixed += 1
    return fixed


def stage_hairlines(ctx) -> int:
    """Residual label-on-geometry grazes (< 5 mm2) on stacks that own a
    leader: the solver could not slide them clear; a relocation with the
    full clean-slot test can."""
    rep = detect(ctx.sc, ctx.cfg)
    fixed = 0
    seen = set()
    for c in rep.collisions:
        if c.b is not None or c.area > 5.0:
            continue
        lab = ctx.sc.labels[c.a]
        key = lab.group if lab.group is not None else lab.handle
        if key in seen:
            continue
        seen.add(key)
        if lab.kind == "dimension":
            continue                    # slide-pinned: solver's domain
        if relocate_stack(ctx, lab,
                          why=f"{c.area:.2f} mm2 graze on geometry"):
            fixed += 1
    return fixed


def stage_frames(ctx) -> int:
    cfg = ctx.cfg
    fixed = 0
    for e in ctx.msp.query("INSERT"):
        if not (e.dxf.name or "").startswith("AnnoRect") or not e.attribs:
            continue
        rects = [ve for ve in e.virtual_entities()
                 if ve.dxftype() == "LWPOLYLINE"]
        if not rects:
            continue
        rb = ezb.extents(rects, fast=False)
        att = e.attribs[0]
        ab = ezb.extents([att], fast=False)
        wf = att.dxf.width if att.dxf.width else 1.0
        need = ab.size.x / wf + 2 * FRAME_MARGIN
        if rb.size.x >= need - 0.2:
            # rect big enough -- but is the text centred in it?
            off = abs((ab.extmin.x + ab.extmax.x) / 2
                      - (rb.extmin.x + rb.extmax.x) / 2)
            if off > 0.5:
                cx = (rb.extmin.x + rb.extmax.x) / 2
                if att.dxf.hasattr("align_point"):
                    att.dxf.align_point = Vec3(cx, att.dxf.align_point.y,
                                               att.dxf.align_point.z)
                att.dxf.insert = Vec3(cx - ab.size.x / 2, att.dxf.insert.y,
                                      att.dxf.insert.z)
                print(f"    frame {att.dxf.text!r}: re-centred "
                      f"(was {off:.1f} mm off)")
                fixed += 1
            continue
        # find the stack column: welded mates share the rect's left edge
        lab = next((l for l in ctx.sc.labels if l.handle == e.dxf.handle),
                   None)
        col = None
        if lab is not None and lab.group is not None:
            mates = [l for l in ctx.sc.labels
                     if l.group == lab.group and l.handle != lab.handle]
            for m in mates:
                if abs(m.box().bounds[0] - rb.extmin.x) < STACK_ALIGN + 0.5:
                    col = rb.extmin.x
                    break
        grow = need - rb.size.x
        # candidate growths: pinned-left (stack), centred, right-only,
        # left-only -- first one whose new strips are clean wins
        opts = []
        if col is not None:
            opts.append((col, "left edge pinned to stack column"))
        else:
            opts.append((rb.extmin.x - grow / 2, "centred"))
            opts.append((rb.extmin.x, "grown right"))
            opts.append((rb.extmin.x - grow, "grown left"))
        done = False
        for new_x0, how in opts:
            new_rect = (new_x0, rb.extmin.y, new_x0 + need, rb.extmax.y)
            strips = []
            if new_rect[0] < rb.extmin.x:
                strips.append((new_rect[0], rb.extmin.y,
                               rb.extmin.x, rb.extmax.y))
            if new_rect[2] > rb.extmax.x:
                strips.append((rb.extmax.x, rb.extmin.y,
                               new_rect[2], rb.extmax.y))
            ok = True
            pad = cfg.clearance + cfg.geom_buffer
            for s in strips:
                bx = sbox(s[0] - pad, s[1] - pad, s[2] + pad, s[3] + pad)
                for o in ctx.sc.labels:
                    if o.handle == e.dxf.handle or \
                            (lab is not None and lab.group is not None
                             and o.group == lab.group):
                        continue
                    if bx.intersects(o.box(cfg.clearance)):
                        ok = False
                for g, own in zip(ctx.sc.obstacles, ctx.sc.owners):
                    if own == e.dxf.handle or own == \
                            (lab.leader_handle if lab else None):
                        continue
                    if bx.intersects(g):
                        ok = False
                if ctx.sc.allowed is not None and \
                        not ctx.sc.allowed.contains(bx):
                    ok = False
            if not ok:
                continue
            e.dxf.xscale = e.dxf.xscale * need / rb.size.x
            rb2 = ezb.extents([ve for ve in e.virtual_entities()
                               if ve.dxftype() == "LWPOLYLINE"], fast=False)
            e.dxf.insert = Vec3(e.dxf.insert.x + (new_rect[0]
                                                  - rb2.extmin.x),
                                e.dxf.insert.y, e.dxf.insert.z)
            cx = new_rect[0] + need / 2
            if att.dxf.hasattr("align_point"):
                att.dxf.align_point = Vec3(cx, att.dxf.align_point.y,
                                           att.dxf.align_point.z)
            att.dxf.insert = Vec3(cx - ab.size.x / 2, att.dxf.insert.y,
                                  att.dxf.insert.z)
            print(f"    frame {att.dxf.text!r}: {rb.size.x:.1f} -> "
                  f"{need:.1f} mm ({how}), text centred")
            fixed += 1
            done = True
            break
        if not done:
            if lab is not None and relocate_and_widen(ctx, e, lab, need, rb):
                fixed += 1
            elif lab is None:
                print(f"    frame {att.dxf.text!r}: needs {need:.1f} mm but "
                      f"every growth collides -- left (stated)")
    return fixed


def table_rect(doc):
    """DESIGN DATA table rect measured from the title block's rule lines."""
    msp = doc.modelspace()
    tb = None
    for e in msp.query("INSERT"):
        try:
            b = ezb.extents([e], fast=True)
        except Exception:
            continue
        if b.has_data and b.size.x > 700:
            tb = e
            break
    if tb is None:
        return None
    ys, xs = set(), set()
    for ve in tb.virtual_entities():
        try:
            b = ezb.extents([ve], fast=True)
        except Exception:
            continue
        if not b.has_data:
            continue
        if ve.dxftype() in ("LINE", "LWPOLYLINE"):
            if b.size.y < 0.5 and b.size.x > 50 and b.extmax.y < 170 \
                    and b.extmax.x < 340:
                ys.add(round(b.extmin.y, 1))
            if b.size.x < 0.5 and b.size.y > 20 and b.extmax.y < 170 \
                    and b.extmin.x < 340:
                xs.add(round(b.extmin.x, 1))
    if len(ys) < 4 or len(xs) < 2:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def stage_table(ctx, path, roles, cfg) -> int:
    rect = table_rect(ctx.doc)
    if rect is None:
        return 0
    poly = sbox(*rect)
    on = [l for l in ctx.sc.labels
          if poly.intersects(sbox(*l.box().bounds))]
    if not on:
        return 0
    print(f"    {len(on)} label(s) on the DESIGN DATA table "
          f"{[l.text.strip()[:10] for l in on[:6]]}")

    def inject(scene):
        scene.obstacles.append(poly)
        scene.owners.append(None)
        scene._tree = STRtree(scene.obstacles)
        scene.free.add_rect(*rect)
        scene.free.finish()

    def score(p):
        _d, s = extract(p, roles, cfg)
        inject(s)
        r = detect(s, cfg)
        n_on = sum(1 for l in s.labels
                   if poly.intersects(sbox(*l.box().bounds)))
        return n_on, r.overlap_area

    work = path.with_suffix(".tbl.dxf")
    save_dxf(ctx.doc, work)
    best = score(work)
    kept = ezdxf.readfile(work)
    for rnd in range(1, 7):
        doc2, s2 = extract(work, roles, cfg)
        inject(s2)
        stats = solve(s2, cfg)
        if stats.moved == 0:
            break
        wb_apply(doc2, s2, roles, cfg)
        save_dxf(doc2, work)
        cand = score(work)
        if (cand[0], round(cand[1], 6)) < (best[0], round(best[1], 6)):
            best = cand
            kept = ezdxf.readfile(work)
        else:
            save_dxf(kept, work)
            break
    moved = len(on) - best[0]
    save_dxf(kept, work)
    # hand the result back through ctx.doc
    ctx.doc = kept
    ctx.msp = kept.modelspace()
    work.unlink(missing_ok=True)
    print(f"    table cleared: {len(on)} -> {best[0]} label(s) on it")
    return max(moved, 0)


# ---------------------------------------------------------------------------
def rework(src: Path, site: str = "jp1071") -> dict:
    roles, cfg = SITES[site], Tuning()
    res = {"doglegs": 0, "realign": 0, "landings": 0, "claims": 0,
           "frames": 0, "threads": 0, "hairlines": 0, "table": 0,
           "reverted": []}
    base = _score(src, roles, cfg)
    tmp = src.with_suffix(".rw.dxf")
    good = src.with_suffix(".rw.good.dxf")
    shutil.copy(src, good)

    for name, fn in (("doglegs", stage_doglegs),
                     ("realign", stage_realign),
                     ("landings", stage_landings),
                     ("claims", stage_claims),
                     ("frames", stage_frames),
                     ("threads", stage_threads),
                     ("hairlines", stage_hairlines)):
        ctx = Ctx(good, roles, cfg)
        n = fn(ctx)
        if not n:
            continue
        save_dxf(ctx.doc, tmp)
        s = _score(tmp, roles, cfg)
        if _worse(base, s):
            print(f"    stage {name} made it worse "
                  f"({base} -> {s}) -- rolled back")
            res["reverted"].append(name)
        else:
            res[name] = n
            base = s
            shutil.copy(tmp, good)

    ctx = Ctx(good, roles, cfg)
    n = stage_table(ctx, good, roles, cfg)
    if n:
        save_dxf(ctx.doc, tmp)
        s = _score(tmp, roles, cfg)
        if s[1] > base[1]:
            print(f"    stage table raised B/C/D ({base} -> {s}) -- "
                  f"rolled back")
            res["reverted"].append("table")
        else:
            res["table"] = n
            base = s
            shutil.copy(tmp, good)

    shutil.copy(good, src)
    tmp.unlink(missing_ok=True)
    good.unlink(missing_ok=True)
    res["overlap"] = round(base[0], 2)
    res["bcd"] = base[1]
    return res


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    site = next((a.split("=", 1)[1] for a in sys.argv
                 if a.startswith("--site=")), "jp1071")
    print(rework(Path(args[0]), site))
