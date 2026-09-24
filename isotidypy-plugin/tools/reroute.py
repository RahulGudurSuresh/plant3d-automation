"""
Re-route the leaders on an isometric so they stop crossing each other.

Labels are NOT moved -- they have already been placed overlap-free and
re-opening that would trade one defect for another.  Only the line between a
label and its part changes: which point on the component the arrow touches,
where it lands on the label, and whether it bends once on the way.

Run:
    python tools/reroute.py in.dxf out.dxf [--site=jp1071]
"""

from __future__ import annotations

import shutil
import sys

# The console must never kill the pipeline: JP1071 labels contain the
# centreline symbol (U+2104), which Windows' cp1252 stdout cannot encode --
# one print() of a label name crashed two production runs.  Degrade the
# glyph, not the run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ezdxf
from ezdxf.math import Vec3
from shapely.geometry import Point

from isotidy import DEFAULT_ROLES, JP1071_ROLES, Tuning, detect
from isotidy.writeback import save_dxf  # noqa: E402
from isotidy.extract import extract, _to_geometry
from isotidy.leaders import Leader, reroute

SITES = {"default": DEFAULT_ROLES, "jp1071": JP1071_ROLES}

#: mm.  For a leader with no classified label, a label box this close to the
#: landing is taken to BE its label, and is exempt from crossing checks.
UNMAPPED_OWN = 2.0


def build(path: Path, roles, cfg):
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    _d, scene = extract(path, roles, cfg)

    by_handle = {l.leader_handle: l for l in scene.labels if l.leader_handle}

    leaders, mls = [], {}
    for ml in msp.query("MULTILEADER"):
        try:
            ctx = ml.context
            ld = ctx.leaders[0]
            line = ld.lines[0]
            if not line.vertices:
                continue
            tip = (line.vertices[0].x, line.vertices[0].y)
            landing = (ld.last_leader_point.x, ld.last_leader_point.y)
        except Exception:
            continue
        lab = by_handle.get(ml.dxf.handle)
        if lab is not None:
            # Boxes this leader may touch: its own label, plus its group
            # mates -- a balloon tucked on its size text shares ONE leader,
            # so the path necessarily reaches through the pair.  Indices are
            # positions in the `label_boxes` list built by main(), which is
            # scene.labels order.
            box = lab.box()
            exempt = frozenset(
                i for i, other in enumerate(scene.labels)
                if other.index == lab.index
                or (lab.group is not None and other.group == lab.group))
        else:
            # A leader whose label the extractor did not classify.  Skipping
            # these entirely is how ISOGEN's own Floor_Symbol crossing
            # survived every pass on 545M05: 19 leaders in the file, 15
            # mapped, and the defect sat in the other four.  They are still
            # routable -- with NO label box the landing simply cannot move,
            # so the only freedom left is a bend, which is the safe subset of
            # what is permitted anyway.  Whatever it lands on is exempted by
            # proximity so the router never "fixes" a leader for touching its
            # own text.
            box = None
            lp = Point(landing)
            exempt = frozenset(i for i, other in enumerate(scene.labels)
                               if other.box().distance(lp) <= UNMAPPED_OWN)

        # NOTE: no slide zone.  The arrow tip is frozen at ISOGEN's position
        # -- hard rule from the design engineer; only the tail may move.
        leaders.append(Leader(handle=ml.dxf.handle, tip=tip, landing=landing,
                              label_box=box, exempt=exempt))
        mls[ml.dxf.handle] = ml
    return doc, msp, scene, leaders, mls


def relieve(doc, msp, scene, leaders, symbols, cfg, reach: float = 60.0):
    """Move LABELS so their leaders can run straight and clean.

    The routing step can only bend the line.  When no straight route exists
    it bends -- and on 209M05 that meant arcing the A13217TV6301 leader over
    the valve cluster, cutting the GlobeCV and FlangeWN glyphs to dodge two
    leader crossings.  ISOGEN's straight line crossed two leaders; the bent
    one crossed two symbols.  The engineer rejected both.  What he drew by
    hand instead moved TWO labels: the tag itself, and balloon 10, whose box
    and leader spoiled every clean spot near the valve.

    So this works in two stages, single then pair:

      SINGLE  find the nearest spot for the leader's own label from which a
              straight (or, failing that, once-bent) leader crosses nothing.

      PAIR    when a candidate spot is spoiled by exactly ONE other movable
              label -- its box in the way, or its leader lying across the
              routes -- relocate that blocker to ITS nearest clean spot
              first, re-land its leader straight, then place the target.
              This is the general form of the drafter's fix: clear the
              neighbour out of the way, then run the line.

    Grouped callouts (balloon welded to its size text) are never moved here
    -- moving one member tears the weld -- and dimension text is never moved
    off its line.  Every change is still subject to main()'s verdict: relief
    is scored against the no-relief file and thrown away if it costs overlap.
    """
    from shapely.geometry import LineString
    from shapely.ops import nearest_points

    from isotidy.detect import overlap_area
    from isotidy.leaders import (_candidate_paths, _perimeter_points,
                                 symbol_hits)

    if scene.free is None:
        return 0

    by_handle = {l.leader_handle: l for l in scene.labels if l.leader_handle}
    paths = {}
    for ld in leaders:
        paths[ld.handle] = (LineString(ld.path.coords) if ld.path is not None
                            else LineString([ld.tip, ld.landing]))

    def defects_of(handle, path, moving=()):
        """Crossings this path makes, ignoring labels being relocated in the
        same operation.  Foreign labels are tested with the CLEARANCE-PADDED
        box and `intersects`, because that is detect()'s bar: a route passing
        0.5 mm from a label is 'not crossing' by the raw-box test yet scores
        as collision, which is how every relief pass ended up discarded."""
        n = sum(1 for h, q in paths.items() if h != handle and path.crosses(q))
        n += len(symbol_hits(path, path.coords[0], path.coords[-1], symbols))
        moving_ids = {m.index for m in moving}
        own = by_handle.get(handle)
        for lab in scene.labels:
            if lab.index in moving_ids or lab.leader_handle == handle:
                continue
            if own is not None and (lab.index == own.index or
                    (own.group is not None and lab.group == own.group)):
                continue
            # clearance pads the box AND geom_buffer widens the drawn
            # leader when detect() scores it -- so the no-go band around a
            # label is the SUM.  Checking clearance alone left a 0.35 mm
            # sliver where a route was 'clean' here and 0.007 mm2 of
            # collision to the scorer, and that dust discarded a relief pass
            # that had solved every leader on the sheet.
            if path.intersects(lab.box(cfg.clearance + cfg.geom_buffer)):
                n += 1
        return n

    def slot_box(lab, cand, moving=(), own_handle=None):
        """The label's box at `cand` if the spot is honest, else None."""
        box = lab.box(pos=cand)
        if scene.ink_under(box) > scene.ink_under(lab.box()) + cfg.ink_tolerance:
            return None
        padded = lab.box(cfg.clearance, pos=cand)
        exempt = scene.own_exempt(lab.index)
        if overlap_area(padded, scene.obstacles_near(padded, exempt)) > 1e-9:
            return None
        moving_ids = {m.index for m in moving}
        for o in scene.labels:
            if o is lab or o.index in moving_ids:
                continue
            if padded.intersects(o.box(cfg.clearance)):
                return None
        # nobody else's leader may lie within the clearance band either
        for h, q in paths.items():
            if h == own_handle:
                continue
            owner = by_handle.get(h)
            if owner is not None and (owner is lab
                                      or owner.index in moving_ids):
                continue
            if q.intersects(lab.box(cfg.clearance + cfg.geom_buffer,
                                    pos=cand)):
                return None
        return box

    def route_to(ld, box, moving=()):
        """Cleanest route from the frozen tip to `box`: straight if possible,
        else one bend; None when nothing clean exists.

        FIRST CLEAN ROUTE WINS.  Candidates are generated straight-first,
        then one-elbow by ascending detour, so accepting the first clean one
        is the same preference order the exhaustive sort had -- at a fraction
        of the geometry calls.  Two-elbow shapes are not offered here at all:
        relief exists to make leaders SIMPLER, and a spot that needs two
        bends is a spot not worth moving a label to."""
        landing = nearest_points(Point(ld.tip), box)[1]
        landings = [(landing.x, landing.y)] + _perimeter_points(box, 4)
        tip = ld.tip
        straights, bent = [], []
        for l in landings:
            straights.append(LineString([tip, l]))
            dx, dy = l[0] - tip[0], l[1] - tip[1]
            length = (dx * dx + dy * dy) ** 0.5
            if length < 4.0:
                continue
            px, py = -dy / length, dx / length
            for f in (0.15, 0.4):
                bx, by = tip[0] + dx * f, tip[1] + dy * f
                for k in (3.0, -3.0, 6.0, -6.0, 10.0, -10.0):
                    bent.append(LineString(
                        [tip, (bx + px * k, by + py * k), l]))
        bent.sort(key=lambda r: r.length)
        for r in straights + bent:
            if defects_of(ld.handle, r, moving) == 0:
                return r
        return None

    def blockers_at(lab, cand, ld):
        """Movable labels spoiling this spot -- by box, or by their leader
        lying across it."""
        box = lab.box(pos=cand)
        padded = lab.box(cfg.clearance, pos=cand)
        out = {}                      # keyed by index: Label is unhashable
        for o in scene.labels:
            if o is lab:
                continue
            if padded.intersects(o.box(cfg.clearance)):
                out[o.index] = o
        for h, q in paths.items():
            if h == ld.handle:
                continue
            if q.crosses(box) or q.intersects(padded):
                owner = by_handle.get(h)
                if owner is not None:
                    out[owner.index] = owner
        return list(out.values())

    def commit(lab, ld, cand, route):
        entity = doc.entitydb.get(lab.handle)
        if entity is None:
            return False
        dx, dy = cand[0] - lab.pos[0], cand[1] - lab.pos[1]
        try:
            entity.translate(dx, dy, 0)
        except Exception:
            return False
        ml = doc.entitydb.get(ld.handle)
        try:
            ctx = ml.context
            pts = [Vec3(x, y, 0) for x, y in route.coords]
            ctx.leaders[0].lines[0].vertices = pts[:-1]
            ctx.leaders[0].last_leader_point = pts[-1]
            ctx.base_point = pts[-1]
            ml.proxy_graphic = None        # stale cache, see CONTEXT.md 9.4
        except Exception:
            entity.translate(-dx, -dy, 0)
            return False
        lab.pos = cand
        ld.landing = tuple(route.coords[-1])
        ld.path = route
        paths[ld.handle] = route
        return True

    def slots_for(lab, ld):
        """Candidate spots ringing the ARROW TIP, nearest first.

        Centred on the tip, not on where the label happens to sit now: the
        whole point of relief is a short straight leader, and short means
        near the component.  Centring on the label's current position made
        the search spend its 120 nearest slots in the wrong neighbourhood --
        A13217TV6301's only workable spot sits below the valve, 57 mm from
        the label but 25 mm from the tip, and was never even offered."""
        centre = (ld.tip[0] - lab.size[0] / 2.0,
                  ld.tip[1] - lab.size[1] / 2.0)
        return scene.free.open_slots(centre, lab.size, cfg.clearance,
                                     max_reach=reach, step=cfg.free_step,
                                     limit=150, halo=cfg.free_halo)

    def unit_of(b):
        """The blocker and everyone welded to it -- a balloon tucked on its
        size text moves as one thing or not at all."""
        if b.group is None:
            return [b]
        return [x for x in scene.labels if x.group == b.group]

    def move_unit(members, delta):
        """Translate every member entity; True only if all moved."""
        done = []
        for m in members:
            ent = doc.entitydb.get(m.handle)
            if ent is None:
                break
            try:
                ent.translate(delta[0], delta[1], 0)
            except Exception:
                break
            m.pos = (m.pos[0] + delta[0], m.pos[1] + delta[1])
            done.append((m, ent))
        else:
            return True
        for m, ent in done:                      # roll back the partial move
            ent.translate(-delta[0], -delta[1], 0)
            m.pos = (m.pos[0] - delta[0], m.pos[1] - delta[1])
        return False

    def try_pair(ld, lab, cand, blockers):
        """Move the blocking unit aside, then place the target at `cand`."""
        members = unit_of(blockers[0])
        if any(m.kind == "dimension" or
               m.text.strip().startswith("CONNECTED") for m in members):
            return False
        member_ids = {m.index for m in members}
        if {b.index for b in blockers} - member_ids:
            return False                          # blockers span two units
        x0 = min(m.box().bounds[0] for m in members)
        y0 = min(m.box().bounds[1] for m in members)
        x1 = max(m.box().bounds[2] for m in members)
        y1 = max(m.box().bounds[3] for m in members)
        m_lds = [(m, next(x for x in leaders if x.handle == m.leader_handle))
                 for m in members if m.leader_handle]
        for slot in scene.free.open_slots((x0, y0), (x1 - x0, y1 - y0),
                                          cfg.clearance, max_reach=reach,
                                          step=cfg.free_step, limit=40,
                                          halo=cfg.free_halo):
            delta = (slot[0] - x0, slot[1] - y0)
            moving = tuple(members) + (lab,)
            spots = [slot_box(m, (m.pos[0] + delta[0], m.pos[1] + delta[1]),
                              moving=moving, own_handle=m.leader_handle)
                     for m in members]
            if any(sp is None for sp in spots):
                continue
            routes = []
            ok = True
            for (m, m_ld), sp in zip(m_lds, [sp for m, sp in
                                             zip(members, spots)
                                             if m.leader_handle]):
                r = route_to(m_ld, sp, moving=moving)
                if r is None:
                    ok = False
                    break
                routes.append((m_ld, r))
            if not ok:
                continue
            # tentative: place the unit, then check the target spot
            olds = [(m, m.pos) for m in members]
            for m in members:
                m.pos = (m.pos[0] + delta[0], m.pos[1] + delta[1])
            old_paths = {m_ld.handle: paths[m_ld.handle] for m_ld, _r in routes}
            for m_ld, r in routes:
                paths[m_ld.handle] = r
            box = slot_box(lab, cand, moving=tuple(members),
                           own_handle=ld.handle)
            route = (route_to(ld, box, moving=tuple(members))
                     if box is not None else None)
            for m, old in olds:
                m.pos = old
            paths.update(old_paths)
            if box is None or route is None:
                continue
            # commit: unit first, then its leaders, then the target
            if not move_unit(members, delta):
                continue
            for m_ld, r in routes:
                ml = doc.entitydb.get(m_ld.handle)
                try:
                    ctx = ml.context
                    pts = [Vec3(x, y, 0) for x, y in r.coords]
                    ctx.leaders[0].lines[0].vertices = pts[:-1]
                    ctx.leaders[0].last_leader_point = pts[-1]
                    ctx.base_point = pts[-1]
                    ml.proxy_graphic = None
                except Exception:
                    pass
                m_ld.landing = tuple(r.coords[-1])
                m_ld.path = r
                paths[m_ld.handle] = r
            if commit(lab, ld, cand, route):
                kind = "straight" if len(route.coords) == 2 else "clean"
                names = "/".join(m.text.strip()[:10] for m in members)
                print("    moved %r aside and %r so its leader runs %s"
                      % (names, lab.text.strip()[:18], kind))
                return True
            return False
        return False

    moved = 0
    for _sweep in range(2):
        order = sorted(
            (ld for ld in leaders
             if defects_of(ld.handle, paths[ld.handle]) > 0),
            key=lambda ld: -defects_of(ld.handle, paths[ld.handle]))
        progress = False
        for ld in order:
            if defects_of(ld.handle, paths[ld.handle]) == 0:
                continue
            lab = by_handle.get(ld.handle)
            if lab is None or lab.kind == "dimension" \
                    or lab.group is not None:
                continue
            done = False
            pair_options = []
            for cand in slots_for(lab, ld):
                box = slot_box(lab, cand, own_handle=ld.handle)
                route = route_to(ld, box) if box is not None else None
                if route is None:
                    blk = blockers_at(lab, cand, ld)
                    if blk and len(blk) <= 2:
                        pair_options.append((cand, blk))
                    continue
                if commit(lab, ld, cand, route):
                    moved += 1
                    progress = True
                    kind = ("straight" if len(route.coords) == 2
                            else "clean")
                    print("    moved %r so its leader runs %s"
                          % (lab.text.strip()[:18], kind))
                    done = True
                break
            if done:
                continue
            for cand, blk in pair_options[:12]:
                if try_pair(ld, lab, cand, blk):
                    moved += 2
                    progress = True
                    break
        if not progress:
            break
    return moved


def _pass(src: Path, dst: Path, roles, cfg, use_relief: bool):
    """One attempt.  Returns (leader defects after, overlap mm2, relieved)."""
    from isotidy.extract import component_symbols

    doc, msp, scene, leaders, mls = build(src, roles, cfg)
    # Obstacle layers come from the SITE CONFIG, not a literal tuple -- the
    # old hardcoded list was JP1071's names and would silently vanish for
    # any other site ("layer names are configuration, never constants").
    obstacle_layers = set(roles.fixed) | set(roles.constrained)
    obstacles = [g for e in msp
                 if e.dxf.layer in obstacle_layers
                 and (g := _to_geometry(e)) is not None]
    label_boxes = [l.box() for l in scene.labels]
    symbols = component_symbols(doc)

    relieved = 0
    if use_relief:
        # Try to make a straight leader POSSIBLE by moving its label, before
        # bending anything.  Bending is the last resort.
        relieved = relieve(doc, msp, scene, leaders, symbols, cfg)
        if relieved:
            label_boxes = [l.box() for l in scene.labels]

    before, after = reroute(leaders, obstacles, label_boxes, symbols)
    moved = sum(1 for l in leaders if l.moved)

    for ld in leaders:
        if not ld.moved:
            continue
        ml = mls[ld.handle]
        try:
            ctx = ml.context
            pts = [Vec3(x, y, 0) for x, y in ld.path.coords]
            ctx.leaders[0].lines[0].vertices = pts[:-1]
            ctx.leaders[0].last_leader_point = pts[-1]
            ctx.base_point = pts[-1]
            # Same discipline as writeback.py: the entity caches AutoCAD's
            # rendering as a proxy graphic, and proxy-honouring viewers --
            # including our own render tool -- replay it instead of the
            # context we just edited.  Forgetting this line shipped a DWG
            # whose elbow existed in the file while every PNG still drew the
            # old straight path crossing two leaders.
            ml.proxy_graphic = None
        except Exception as exc:
            print(f"    !! could not write leader {ld.handle}: {exc}")

    save_dxf(doc, dst)
    _d2, s2 = extract(dst, roles, cfg)
    rep = detect(s2, cfg)
    return after, rep.overlap_area, relieved, moved, before


def main(src: Path, dst: Path, site: str = "jp1071") -> None:
    """Route the leaders, moving labels only if that genuinely helps.

    RELIEF IS TRIED, THEN VERIFIED, THEN KEPT OR THROWN AWAY.  Moving a label
    re-lands its leader, and a leader is itself an obstacle -- so on 209M05
    three labels that never moved went into collision because a re-landed
    leader now grazed them, and overlap went 1.84 -> 16.21 mm2 while every
    check inside `relieve` said the move was clean.  Proxy checks kept
    disagreeing with the scorer, so this stops arguing with it: run both
    ways, score both files the way the CLI does, and keep relief only when it
    costs no overlap.
    """
    roles, cfg = SITES[site], Tuning()
    plain = dst.with_suffix(".norelief.dxf")
    d_plain, ov_plain, _r, moved_p, before = _pass(src, plain, roles, cfg, False)
    d_rel, ov_rel, relieved, moved_r, _b = _pass(src, dst, roles, cfg, True)

    keep_relief = relieved and ov_rel <= ov_plain + 1e-9 and d_rel <= d_plain
    if keep_relief:
        print(f"  leaders: B defects {before} -> {d_rel}   "
              f"re-routed {moved_r}, {relieved} label(s) moved to allow a "
              f"straight leader")
        overlap = ov_rel
    else:
        if relieved:
            print(f"  label relief discarded: it would leave "
                  f"{ov_rel:.2f} mm2 of overlap against {ov_plain:.2f} "
                  f"without it")
        shutil.copy(plain, dst)
        print(f"  leaders: B defects {before} -> {d_plain}   "
              f"re-routed {moved_p}")
        overlap = ov_plain
    plain.unlink(missing_ok=True)
    print(f"  annotation overlap after re-route: {overlap:.2f} mm2")
    print(f"  wrote {dst}")


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    site = next((x.split("=", 1)[1] for x in sys.argv
                 if x.startswith("--site=")), "jp1071")
    main(Path(a[0]), Path(a[1]), site)
