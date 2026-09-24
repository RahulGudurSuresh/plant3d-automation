"""
Label placement.

This is the Map Labelling Problem (point-feature label placement).  It is
NP-hard, so nobody solves it exactly -- you pick candidate positions and search.
The approach here is the standard one, and deliberately the simplest thing that
works:

    1. CANDIDATES   each label gets a fixed menu of legal positions
    2. GREEDY       place the worst-offending label first, at its cheapest slot
    3. HILL-CLIMB   sweep repeatedly, re-placing any label that can improve,
                    until a pass changes nothing

Why not simulated annealing or an ILP?  Because in drafting, *predictable* beats
*optimal*.  An engineer who reruns the tool and gets a different-but-equally-good
drawing has lost trust in it.  This search is fully deterministic: same input,
same output, every time.  Reach for annealing only if the greedy result provably
leaves collisions on real sheets.

CANDIDATE DIRECTIONS ARE AT 30-DEGREE STEPS.  That is not arbitrary -- it makes
the ring include the isometric axes (30/90/150/210/270/330), so a relocated
label sits parallel to the pipe run it belongs to and reads as intentional
rather than as something a script nudged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry import LineString, Point
from shapely.ops import nearest_points
from shapely.strtree import STRtree

from .config import Tuning
from .detect import detect, overlap_area
from .model import Label, Scene, Vec2


@dataclass
class SolveStats:
    passes: int
    moved: int
    cost_before: float
    cost_after: float


# --------------------------------------------------------------------------
# Candidate generation
# --------------------------------------------------------------------------
def candidates(lab, cfg: Tuning, scene: Scene | None = None) -> list[Vec2]:
    """The legal positions for one movable thing.  Home is always first, so
    ties resolve in favour of not moving.

    `lab` is a `Label` or a `Unit` -- it needs only home/size/anchor/slide,
    and a grouped callout offers exactly those for its combined box, so the
    same candidate ring serves both.
    """
    out: list[Vec2] = [lab.home]
    w, h = lab.size

    if lab.slide is not None:
        # CONSTRAINED: dimension text belongs on its dimension line.  It may
        # slide along that line, and it may flip to the other side of it --
        # but it may not wander off into free space, or it stops reading as a
        # dimension.
        sx, sy = lab.slide
        px, py = -sy, sx                      # unit normal to the dim line
        # Signed offset of home from the dim line (anchor sits ON the line).
        # A candidate at normal offset k sits at |h0 + k| -- and it may not
        # exceed slide_limit, the auditor's own drift bar, or the solver
        # "fixes" an overlap by manufacturing a category-D defect (it did:
        # 21 of 71 sheets on the first honest fleet run).
        hcx, hcy = lab.home[0] + w / 2.0, lab.home[1] + h / 2.0
        h0 = (hcx - lab.anchor[0]) * px + (hcy - lab.anchor[1]) * py
        limit = getattr(lab, "slide_limit", None)
        # The flip step is the BOX EXTENT ALONG THE NORMAL, not the height.
        # Rotated dimension text on a vertical line crosses that line via its
        # width; using h put the two vertical-line dims 4.7 mm clear of their
        # line where every horizontal one landed ~1 mm clear.  |px|*w + |py|*h
        # is the axis-aligned box's span along the normal, so a flip lands
        # just across the line whatever the text's orientation.
        gap = abs(px) * w + abs(py) * h + 1.6
        # FINE perpendicular steps as well as the full flip.  A flip lands the
        # text on the far side of its line; what is usually wanted to clear a
        # graze is a nudge of a millimetre or two, still beside the same line.
        # With flips only, the solver had to choose between touching the line
        # and jumping across it, so it kept the touch.
        offsets = (0.0, 1.2, -1.2, 2.4, -2.4, 3.6, -3.6,
                   gap, -gap, 2 * gap, -2 * gap)
        for s in cfg.slide_steps:
            for k in offsets:
                if s == 0.0 and k == 0.0:
                    continue
                if limit is not None and abs(h0 + k) > limit:
                    continue
                out.append((lab.home[0] + sx * s + px * k,
                            lab.home[1] + sy * s + py * k))
        return out

    # FREE: two rings of positions -- one around HOME, one around the anchor.
    #
    # The home ring comes first and it is the one that matters.  ISOGEN parks
    # congested callouts in the periphery on a long leader ON PURPOSE; for a
    # label like that, an anchor-only menu contains nothing but positions deep
    # in the congestion its placement was escaping.  On 209M05 that is exactly
    # what happened: balloon '7' (home 40 mm out) collected 6.8 mm^2 of
    # trouble, found only anchor-ring candidates on the menu, and was
    # teleported 60 mm into the middle of the valve cluster.  A label in
    # trouble must be able to make a 3 mm move before it is offered a 30 mm
    # one.
    #
    # The anchor ring stays because it is right for the opposite case -- a
    # label already near its part that needs to hop to the other side of it.
    # The box is pushed clear by `r` along each direction, so `r` is true
    # clearance rather than centre-to-centre distance -- otherwise wide labels
    # sit closer than narrow ones at the same nominal radius.
    hx, hy = lab.home[0] + w / 2.0, lab.home[1] + h / 2.0
    for k in range(cfg.n_directions):
        theta = 2.0 * math.pi * k / cfg.n_directions
        ux, uy = math.cos(theta), math.sin(theta)
        for r in cfg.radii:
            out.append((hx + ux * r - w / 2.0, hy + uy * r - h / 2.0))
    ax, ay = lab.anchor
    for k in range(cfg.n_directions):
        theta = 2.0 * math.pi * k / cfg.n_directions
        ux, uy = math.cos(theta), math.sin(theta)
        for r in cfg.radii:
            cx = ax + ux * (r + w / 2.0)
            cy = ay + uy * (r + h / 2.0)
            out.append((cx - w / 2.0, cy - h / 2.0))

    # GAPS THE RING CANNOT REACH.  The two rings above are guesses at 12
    # angles and 4 radii; this asks the sheet itself where the box actually
    # fits, nearest home first.  Without it a label whose only honest gap was
    # 20 mm away in an unsampled direction had no clean candidate at all --
    # it stayed in trouble and the sheet kept a defect that a drafter would
    # have fixed by looking.
    if scene is not None and scene.free is not None:
        out.extend(scene.free.open_slots(
            lab.home, lab.size, cfg.clearance,
            max_reach=cfg.free_reach, step=cfg.free_step,
            limit=cfg.free_slots, halo=cfg.free_halo))
    return out


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------
def _leader_crossings(scene: Scene, lab: Label, box, neighbours,
                      boxes, self_index: int, skip: set[int] | None = None,
                      exempt: set | None = None) -> int:
    """How many things a leader from the anchor to this box would cross.

    A label you cannot trace back to its component is worse than a label that
    overlaps something, so this is priced in from the start rather than left
    for the leader-routing step to discover.
    """
    p = Point(lab.anchor)
    if box.contains(p):
        return 0
    q = nearest_points(p, box)[1]
    leader = LineString([(p.x, p.y), (q.x, q.y)])

    n = 0
    # `exempt` carries the callout's own MULTILEADER handles: the hypothetical
    # leader starts at the same arrow tip as the real one, so without the
    # exemption every ballooned label would "cross" its own arrow everywhere.
    for o in scene.obstacles_near(leader, exempt):
        # The anchor sits ON the geometry it describes; touching your own
        # component is not a crossing.
        if not o.contains(p) and leader.intersects(o):
            n += 1
    for j in neighbours:
        if j == self_index or (skip is not None and j in skip):
            continue
        if leader.intersects(boxes[j]):
            n += 1
    return n


def trouble(scene: Scene, cfg: Tuning, i: int, pos: Vec2,
            boxes, neighbours) -> float:
    """The COLLISION-ONLY part of the cost: overlaps, frame breaches, leader
    crossings.  Excludes the aesthetic terms (distance from anchor, stay).

    This is what decides whether a label is worth touching at all.  A label
    with zero trouble is already fine, and moving it would be churn -- the
    tool rewriting a sheet the draftsman had no complaint about.
    """
    lab = scene.labels[i]
    b = lab.box(cfg.clearance, pos=pos)
    c = 0.0

    for j in neighbours:
        if j == i:
            continue
        ob = boxes[j]
        if b.intersects(ob):
            c += cfg.w_label * b.intersection(ob).area

    c += cfg.w_geometry * overlap_area(b, scene.obstacles_near(b))

    if scene.allowed is not None:
        outside = b.difference(scene.allowed).area
        if outside > 1e-9:
            c += cfg.w_frame * outside

    c += cfg.w_leader * _leader_crossings(scene, lab, b, neighbours, boxes, i)
    return c


def cost(scene: Scene, cfg: Tuning, i: int, pos: Vec2,
         boxes, neighbours) -> float:
    """Full cost of placing label `i` at `pos`, given where everything else is."""
    lab = scene.labels[i]
    c = trouble(scene, cfg, i, pos, boxes, neighbours)

    c += cfg.w_distance * lab.anchor_distance(pos)

    if abs(pos[0] - lab.home[0]) > 1e-9 or abs(pos[1] - lab.home[1]) > 1e-9:
        c += cfg.w_stay

    return c


# --------------------------------------------------------------------------
# Rigid units
#
# The solver moves UNITS, not labels.  A unit is usually one label; where
# ISOGEN has tucked an item balloon onto its own size text it is both of them,
# and they translate together by one delta.  Everything below is written
# against units so the single-label case needs no special handling -- it is
# just a unit with one member.
# --------------------------------------------------------------------------
@dataclass
class Unit:
    members: list[int]        # label indices, in scene order
    offsets: list[Vec2]       # each member's home, relative to the unit's
    home: Vec2                # lower-left of the members' combined home bbox
    size: Vec2                # combined size -- what must fit in a gap
    anchor: Vec2              # the point the whole callout describes
    slide: Vec2 | None
    slide_limit: float | None # drift bar for slide-constrained text
    lead: int                 # position in `members` of the leader-owner
    home_anchor_dist: float = 0.0   # how far ISOGEN itself parked the callout

    def positions(self, pos: Vec2) -> list[Vec2]:
        return [(pos[0] + ox, pos[1] + oy) for ox, oy in self.offsets]


def _build_units(scene: Scene) -> list[Unit]:
    units: list[Unit] = []
    for members in scene.rigid_units():
        labs = [scene.labels[i] for i in members]
        x1 = min(l.home[0] for l in labs)
        y1 = min(l.home[1] for l in labs)
        x2 = max(l.home[0] + l.size[0] for l in labs)
        y2 = max(l.home[1] + l.size[1] for l in labs)

        # The balloon carries the leader, so it knows which component the
        # callout points at.  Anchor the whole unit there, or the pair drifts
        # toward the text's centroid and stops aiming at its part.
        lead = next((k for k, l in enumerate(labs)
                     if l.leader_handle is not None), 0)
        anchor = labs[lead].anchor
        units.append(Unit(
            members=list(members),
            offsets=[(l.home[0] - x1, l.home[1] - y1) for l in labs],
            home=(x1, y1), size=(x2 - x1, y2 - y1),
            anchor=anchor,
            # Sliding is a dimension-text rule; a grouped callout is free.
            slide=labs[lead].slide if len(labs) == 1 else None,
            slide_limit=labs[lead].slide_limit if len(labs) == 1 else None,
            lead=lead,
            home_anchor_dist=labs[lead].box(
                pos=labs[lead].home).distance(Point(anchor)),
        ))
    return units


def _unit_pos(scene: Scene, unit: Unit) -> Vec2:
    i0, (ox, oy) = unit.members[0], unit.offsets[0]
    p = scene.labels[i0].pos
    return (p[0] - ox, p[1] - oy)


def _place(scene: Scene, cfg: Tuning, unit: Unit, pos: Vec2, boxes) -> None:
    for idx, mpos in zip(unit.members, unit.positions(pos)):
        scene.labels[idx].pos = mpos
        boxes[idx] = scene.labels[idx].box(cfg.clearance)


def unit_trouble(scene: Scene, cfg: Tuning, unit: Unit, pos: Vec2,
                 boxes, neigh) -> float:
    """Collision-only cost of putting the whole callout at `pos`.

    Members are never scored against each other: the tuck is how ISOGEN draws
    a callout, so charging for it would make every grouped unit permanently
    "in trouble" and the solver would shove it around forever.
    """
    member_set = set(unit.members)
    exempt = scene.own_exempt(unit.members[0])
    c = 0.0
    positions = unit.positions(pos)

    for idx, mpos in zip(unit.members, positions):
        lab = scene.labels[idx]
        b = lab.box(cfg.clearance, pos=mpos)
        raw = lab.box(pos=mpos)
        for j in neigh[idx]:
            if j in member_set:
                continue
            ob = boxes[j]
            if b.intersects(ob):
                c += cfg.w_label * b.intersection(ob).area
            # NEAR-MISSES COST TOO.  Hard detection stops at the clearance
            # pad; a label parked a hair beyond it scores zero yet reads as
            # touching -- the solver once landed a balloon 1.27 mm above an
            # unrelated dimension text, under the sheet's tightest ISOGEN
            # spacing (2.50 mm) and inside the tuck-recognition band, so it
            # looked like the balloon belonged to that dimension.
            #
            # The charge is EXTENT-AWARE, not gap-only: each raw box grows by
            # half the band (mitred, so rectangles stay rectangles) and the
            # intersection area of the grown boxes is priced.  A long slide-by
            # at 1.3 mm costs far more than a corner graze at the same gap --
            # which is also how a human reads crowding -- and a gap-only ramp
            # measurably failed here: its penalty was flat per candidate, so
            # it could nudge the balloon from 1.27 to only 1.47 mm before
            # displacement pricing won.  Zero exactly at gap >= band.
            other = scene.labels[j].box()
            if raw.distance(other) < cfg.proximity_band:
                half = cfg.proximity_band / 2.0
                inter = raw.buffer(half, join_style=2).intersection(
                    other.buffer(half, join_style=2)).area
                c += cfg.w_proximity * inter
        # Someone else's geometry and this callout's OWN dimension line are
        # both contact, but they are not the same defect.  Sitting on another
        # dimension's line is wrong; sitting on your own is how ISOGEN draws
        # a dimension.  Price them separately so the solver clears the first
        # absolutely and merely SHAVES the second when a free slot is nearby.
        others_ov = overlap_area(b, scene.obstacles_near(b, exempt))
        all_ov = overlap_area(b, scene.obstacles_near(b))
        c += cfg.w_geometry * others_ov
        c += cfg.w_own_line * max(0.0, all_ov - others_ov)
        if scene.allowed is not None:
            outside = b.difference(scene.allowed).area
            if outside > 1e-9:
                c += cfg.w_frame * outside

    # One leader per callout, so price it once -- against the member that
    # actually owns it.
    li = unit.members[unit.lead]
    lead_lab = scene.labels[li]
    lead_box = lead_lab.box(cfg.clearance, pos=positions[unit.lead])
    c += cfg.w_leader * _leader_crossings(
        scene, lead_lab, lead_box, neigh[li], boxes, li, skip=member_set,
        exempt=exempt)
    return c


def _ink_of(scene: Scene, cfg: Tuning, unit: Unit, pos: Vec2) -> float:
    """Drawn ink under the whole callout if placed at `pos`."""
    total = 0.0
    for idx, mpos in zip(unit.members, unit.positions(pos)):
        total += scene.ink_under(scene.labels[idx].box(pos=mpos))
    return total


def _crowding(scene: Scene, cfg: Tuning, unit: Unit, pos: Vec2,
              neigh) -> tuple[float, int]:
    """How busy is the neighbourhood if the callout sits at `pos`?

    Returns (drawn ink within a halo of the box, number of other labels
    touching that halo).  This is the brake that replaced "do not travel far".

    THE JOURNEY WAS NEVER THE PROBLEM, THE DESTINATION IS.  Distance was only
    ever a proxy: a long move usually ended somewhere bad, so pricing travel
    hid the real test.  It also blocked good moves, and once relaxed it let a
    callout cross the sheet into a pile of other text -- which is what the
    user rejected on 353M05 (OFFSET 13 at 48 mm, balloon 4 at 45 mm), even
    though both finished CLOSER to the component they label than ISOGEN had
    them.  A move may go as far as it likes, provided where it lands is no
    busier than where it left.
    """
    halo = cfg.crowd_halo
    members = set(unit.members)
    ink = 0.0
    seen: set[int] = set()
    for idx, mpos in zip(unit.members, unit.positions(pos)):
        lab = scene.labels[idx]
        grown = lab.box(pos=mpos).buffer(halo, join_style=2)
        ink += scene.ink_under(grown)
        for j2 in neigh[idx]:
            if j2 in members or j2 in seen:
                continue
            if grown.intersects(scene.labels[j2].box()):
                seen.add(j2)
    return ink, len(seen)


def unit_cost(scene: Scene, cfg: Tuning, unit: Unit, pos: Vec2,
              boxes, neigh) -> float:
    c = unit_trouble(scene, cfg, unit, pos, boxes, neigh)

    # ISOGEN'S PLACEMENT IS THE PRIOR, NOT A DEFECT TO CORRECT.  The anchor
    # term only charges for drifting FARTHER from the part than ISOGEN left
    # it; getting closer earns nothing.  The old symmetric term paid 12/mm
    # for approaching the anchor, which read ISOGEN's deliberate peripheral
    # placement (long leader, clear space) as 480 points of standing error --
    # and the solver "fixed" it by hauling the callout 60 mm into the exact
    # congestion the periphery exists to avoid.
    li = unit.members[unit.lead]
    lead_box = scene.labels[li].box(pos=unit.positions(pos)[unit.lead])
    excess = lead_box.distance(Point(unit.anchor)) - unit.home_anchor_dist
    if excess > 0.0:
        c += cfg.w_distance * excess

    # What pulls a move back toward home is the displacement term: every mm
    # of travel must buy its keep in resolved collisions, so escapes stay
    # local and the sheet keeps ISOGEN's layout everywhere trouble does not
    # force a change.
    dx, dy = pos[0] - unit.home[0], pos[1] - unit.home[1]
    d = (dx * dx + dy * dy) ** 0.5
    if d > 1e-9:
        c += cfg.w_stay + cfg.w_displacement * d
    return c


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
def _neighbour_lists(scene: Scene, cfg: Tuning, boxes) -> list[list[int]]:
    """For each label, the labels close enough to possibly interact with it.

    Rebuilt once per pass.  Slightly stale during a pass -- a label may move
    into range after the list was built -- which is harmless because actual
    costs are always computed against the live boxes; the list only decides
    who gets *considered*.  The generous radius makes a miss very unlikely.
    """
    if not boxes:
        return []
    tree = STRtree(boxes)
    reach = max(cfg.radii) + max(
        (max(l.size) for l in scene.labels), default=0.0)
    out = []
    for i, b in enumerate(boxes):
        idx = tree.query(b.buffer(reach), predicate="intersects")
        out.append([int(j) for j in idx])
    return out


def _shave_tucks(scene: Scene, cfg: Tuning, units, boxes, neigh) -> None:
    """Ease each balloon off the size text it is tucked onto -- a little.

    ISOGEN sets the balloon down ON its own text.  That tuck is convention,
    and treating it as a defect is what made this tool's first attempt at
    209M05 look worse than the drawing it started from; the pair still moves
    as one unit everywhere else in this file, and that stays.

    What this does is narrower: with the callout already placed, slide the
    balloon by up to `tuck_relief` mm directly away from the text's centre,
    keeping the two within `pair_gap` so they still read as one callout, and
    only when the move costs nothing elsewhere -- no new overlap with other
    labels or geometry, and no more ink underneath.  Anything it cannot do
    for free, it does not do.
    """
    for unit in units:
        if len(unit.members) < 2:
            continue
        balloons = [i for i in unit.members
                    if scene.labels[i].cls == "balloon"]
        texts = [i for i in unit.members if i not in balloons]
        if len(balloons) != 1 or not texts:
            continue
        bi = balloons[0]
        lab = scene.labels[bi]
        raw = lab.box()
        worst = max((raw.intersection(scene.labels[t].box()).area
                     for t in texts), default=0.0)
        if worst <= 1e-9:
            continue                       # nothing tucked, nothing to ease

        tx = sum(scene.labels[t].center()[0] for t in texts) / len(texts)
        ty = sum(scene.labels[t].center()[1] for t in texts) / len(texts)
        cx, cy = lab.center()
        dx, dy = cx - tx, cy - ty
        norm = (dx * dx + dy * dy) ** 0.5
        if norm < 1e-9:
            continue
        ux, uy = dx / norm, dy / norm

        base_trouble = trouble(scene, cfg, bi, lab.pos, boxes, neigh[bi])
        base_ink = scene.ink_under(raw)
        start = lab.pos
        best = None
        step = 0.5
        k = step
        while k <= cfg.tuck_relief + 1e-9:
            pos = (start[0] + ux * k, start[1] + uy * k)
            cand = lab.box(pos=pos)
            gap = min(cand.distance(scene.labels[t].box()) for t in texts)
            if gap > cfg.pair_gap:
                break                      # would stop reading as one callout
            ov = max(cand.intersection(scene.labels[t].box()).area
                     for t in texts)
            if (trouble(scene, cfg, bi, pos, boxes, neigh[bi])
                    <= base_trouble + 1e-9
                    and scene.ink_under(cand) <= base_ink + cfg.ink_tolerance
                    and ov < worst - 1e-9):
                best, worst = pos, ov
            k += step
        if best is not None:
            lab.pos = best
            boxes[bi] = lab.box(cfg.clearance)


def solve(scene: Scene, cfg: Tuning = Tuning()) -> SolveStats:
    """Improve label placement in place.  Mutates Label.pos; Label.home is
    left untouched so the move can always be reported or undone."""
    labels = scene.labels
    if not labels:
        return SolveStats(0, 0, 0.0, 0.0)

    units = _build_units(scene)
    boxes = [l.box(cfg.clearance) for l in labels]
    neigh = _neighbour_lists(scene, cfg, boxes)
    cost_before = sum(unit_cost(scene, cfg, u, _unit_pos(scene, u), boxes, neigh)
                      for u in units)

    passes = 0
    for _ in range(cfg.max_passes):
        passes += 1
        neigh = _neighbour_lists(scene, cfg, boxes)

        # DO NOT TOUCH WHAT IS NOT BROKEN.  Only units actually in collision
        # are candidates for relocation; the rest stay exactly where ISOGEN
        # put them (and still act as obstacles).  Without this the solver
        # drags every label toward its anchor to shave the distance term, and
        # rewrites a sheet nobody complained about -- which is how drafters
        # stop trusting the tool.
        in_trouble = [
            u for u in units
            if unit_trouble(scene, cfg, u, _unit_pos(scene, u),
                            boxes, neigh) > 1e-9
        ]

        # MOST-CONSTRAINED-FIRST: deal with the worst offender while the sheet
        # still has empty space.  Placing easy labels first fills the gaps the
        # hard ones needed, and the hard ones then have nowhere to go.
        order = sorted(
            in_trouble,
            key=lambda u: (-unit_cost(scene, cfg, u, _unit_pos(scene, u),
                                      boxes, neigh), u.members[0]),
        )

        gain = 0.0
        for unit in order:
            at = _unit_pos(scene, unit)
            here = unit_cost(scene, cfg, unit, at, boxes, neigh)
            # NEVER LEAVE A CALLOUT SITTING ON MORE INK THAN ISOGEN DID.
            # Judged against HOME, not against the current position, so a
            # chain of individually-tolerable steps cannot walk a label onto
            # a dimension grid.  This is a veto, not a weight: it cannot be
            # outbid by a big enough collision saving, because "the tool made
            # this worse" is not a trade a drafter accepts.
            # NEVER LEAVE A CALLOUT ON MORE INK THAN ISOGEN DID.  Judged
            # against HOME, so a chain of small steps cannot walk a label
            # onto a dimension grid.  A veto, not a weight: "the tool made
            # this worse" is not a trade a drafter accepts.
            #
            # Tried and REVERTED 2026-08-22: adding crowding and
            # association vetoes alongside this one.  They express the right
            # idea -- judge the destination, not the distance -- but as
            # written they blocked moves the drawing needs: the fixture fell
            # from 99.9% to 87.7% overlap reduction and a second pass churned
            # 5 labels.  The idea is sound; these thresholds were not, and a
            # tool that fails its own regression bar does not ship.
            budget = _ink_of(scene, cfg, unit, unit.home) + cfg.ink_tolerance
            best_pos, best_cost = at, here
            for cand in candidates(unit, cfg, scene):
                c = unit_cost(scene, cfg, unit, cand, boxes, neigh)
                if c < best_cost - 1e-9 and                         _ink_of(scene, cfg, unit, cand) <= budget:
                    best_cost, best_pos = c, cand
            if best_pos != at:
                # THE SCOREKEEPER HAS THE LAST WORD.  The cost function is a
                # weighted blend -- overlap, near-misses, leader crossings,
                # travel -- so a move can lower the cost while raising the
                # one number this tool reports.  Every accepted move is
                # therefore checked against `detect`, the same measure the
                # CLI and the tests use, and reverted if the sheet got worse.
                # Without this, re-running the tool on its own output could
                # walk the drawing backwards, which is the churn that makes
                # drafters stop trusting automation.
                before_area = detect(scene, cfg).overlap_area
                _place(scene, cfg, unit, best_pos, boxes)
                if detect(scene, cfg).overlap_area > before_area + 1e-9:
                    _place(scene, cfg, unit, at, boxes)
                else:
                    gain += here - best_cost

        if gain < 1e-6:      # a pass that changes nothing means we converged
            break

    # ZERO-RESIDUAL SWEEP.  The hill-climb weighs a move's benefit against
    # travel, so a 1.8 mm2 graze can be "not worth" a 10 mm slide and survive
    # every pass -- '134' on 209M05 did exactly that, inside the very region
    # the user circled.  The requirement is not "cheap moves", it is "no
    # overlap": any unit still in trouble now takes its NEAREST zero-trouble
    # candidate whatever the travel cost, ink-vetoed as always, and the move
    # is kept only if the measured overlap strictly drops.
    for unit in units:
        at = _unit_pos(scene, unit)
        if unit_trouble(scene, cfg, unit, at, boxes, neigh) <= 1e-9:
            continue
        budget = _ink_of(scene, cfg, unit, unit.home) + cfg.ink_tolerance
        clean = []
        for cand in candidates(unit, cfg, scene):
            if unit_trouble(scene, cfg, unit, cand, boxes, neigh) <= 1e-9                     and _ink_of(scene, cfg, unit, cand) <= budget:
                d = math.hypot(cand[0] - at[0], cand[1] - at[1])
                clean.append((round(d, 6), cand))
        clean.sort()
        for _d, cand in clean[:5]:
            before_area = detect(scene, cfg).overlap_area
            _place(scene, cfg, unit, cand, boxes)
            if detect(scene, cfg).overlap_area < before_area - 1e-9:
                break
            _place(scene, cfg, unit, at, boxes)

    _shave_tucks(scene, cfg, units, boxes, neigh)

    cost_after = sum(unit_cost(scene, cfg, u, _unit_pos(scene, u), boxes, neigh)
                     for u in units)
    return SolveStats(
        passes=passes,
        moved=sum(1 for l in labels if l.moved()),
        cost_before=cost_before,
        cost_after=cost_after,
    )
