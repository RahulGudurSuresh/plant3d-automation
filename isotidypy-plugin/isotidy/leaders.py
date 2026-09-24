"""
Leader re-routing.

The solver moves LABELS.  This moves the LINE BETWEEN a label and its part,
which is a different problem and the one left over once overlap is gone:
crossed leaders read as swapped item numbers -- a traceability defect, not an
untidy one.

THE ARROW TIP NEVER MOVES.  Hard rule from the design engineer, learned the
expensive way: a first version slid the tip along the component it already
identified, and even that reads as "now you are pointing at something new."
ISOGEN's tip position is ground truth.  What may change:

  LANDING       the tail end may move around the perimeter of its own label
                box.  The label itself never moves -- it has already been
                placed overlap-free and re-opening that would trade one
                defect for another.

  ELBOWS        the path may bend -- at most TWO elbows -- so a leader can
                route AROUND an obstacle instead of through it.  This is what
                a draftsman does by hand.

Everything is scored against the leaders that have already been routed, so
the pass is greedy and order-dependent -- worst offender first, then repeat
sweeps while anything improves.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.ops import unary_union
from shapely.geometry import LineString, Point, Polygon

Vec2 = tuple[float, float]

#: Perpendicular offsets tried for elbows, in mm.
ELBOW_OFFSETS = (3.0, -3.0, 6.0, -6.0, 10.0, -10.0, 15.0, -15.0)
#: Elbow positions along the tip->landing chord, as fractions.
#: THE EARLY ONES MATTER MOST.  Two leaders that leave nearly the same point
#: in diverging directions cross within a millimetre or two of their arrows --
#: on 353M05 a pair whose tips are 2.1 mm apart crossed at (135.5, 357.2),
#: while the earliest bend on offer was 35% of the way along, roughly 20 mm
#: too late to help.  A bend just past the arrow lets a leader step aside
#: before it can foul its neighbour, which is exactly what a drafter draws.
ELBOW_AT = (0.12, 0.22, 0.35, 0.5, 0.65)
#: Two-elbow variants: (position1, position2) fractions along the chord.
TWO_ELBOWS_AT = ((0.3, 0.7), (0.25, 0.55), (0.45, 0.75))
#: Leader shorter than this looks like a tick rather than a leader.
MIN_LENGTH = 4.0
#: Contact this close to the frozen tip is the arrow TOUCHING the part it
#: tags -- pointing, not crossing.  Matches mess_map.TIP_TOLERANCE.
TIP_ZONE = 3.0
GRAZE = 0.5        # mm: a leader this close to a symbol curve reads as on it
GRAZE_RUN = 1.5    # mm of such contact beyond the tip zone = a hit
FILL_TIP = 2.2     # mm: tip allowance over a SOLID fill (weld dot radius 1.6 + GRAZE)


@dataclass
class Leader:
    handle: str                 # MULTILEADER handle
    tip: Vec2                   # arrow tip -- FROZEN, never changes
    landing: Vec2               # tail end, on/at the label
    label_box: object           # the label's box; landing lives on its edge
    #: Indices into the caller's `label_boxes` that this leader is ALLOWED to
    #: touch: its own label, plus its group mates.  Identity (`b is own_box`)
    #: cannot do this job -- `Label.box()` builds a fresh polygon on every
    #: call, so the leader's own box in `label_boxes` is never the same object
    #: as `self.label_box`, and every leader was silently charged 800 for
    #: landing on the label it belongs to.  Group mates matter for the same
    #: reason grouping matters everywhere else here: a balloon tucked on its
    #: size text shares ONE leader, which must reach through the pair.
    exempt: frozenset = frozenset()
    path: LineString | None = None
    moved: bool = False


def _perimeter_points(geom, n: int = 12) -> list[Vec2]:
    """Sample points around a geometry's boundary."""
    try:
        ring = geom.exterior if hasattr(geom, "exterior") else geom
        return [(p.x, p.y) for p in
                (ring.interpolate(i / n, normalized=True) for i in range(n))]
    except Exception:
        return []


def _candidate_paths(ld: Leader, landings: list[Vec2]) -> list[LineString]:
    """Every route worth considering: the tip is FIXED; only the landing and
    the bends vary.  Straight, one elbow, or two elbows."""
    t = ld.tip
    out = []
    for l in landings:
        dx, dy = l[0] - t[0], l[1] - t[1]
        length = math.hypot(dx, dy)
        if length < MIN_LENGTH:
            continue
        ux, uy = dx / length, dy / length
        px, py = -uy, ux                          # unit normal to the chord

        out.append(LineString([t, l]))            # straight

        for f in ELBOW_AT:                        # one elbow
            bx, by = t[0] + dx * f, t[1] + dy * f
            for k in ELBOW_OFFSETS:
                out.append(LineString([t, (bx + px * k, by + py * k), l]))

        for f1, f2 in TWO_ELBOWS_AT:              # two elbows, parallel offset
            b1 = (t[0] + dx * f1, t[1] + dy * f1)
            b2 = (t[0] + dx * f2, t[1] + dy * f2)
            for k in ELBOW_OFFSETS:
                out.append(LineString([
                    t, (b1[0] + px * k, b1[1] + py * k),
                    (b2[0] + px * k, b2[1] + py * k), l]))
    return out


def symbol_hits(path: LineString, tip: Vec2, landing: Vec2,
                symbols, tol: float = TIP_ZONE) -> list[tuple[str, object]]:
    """Component symbols this leader cuts across, as (name, where).

    Two exemptions, both because contact there is the leader doing its job
    rather than failing at it:

      TIP      the arrow must touch the part it tags.  Contact within `tol`
               of the tip is pointing, not crossing.  Contact FURTHER along
               the path is a leader slicing over a glyph -- the defect that
               had to be caught by eye on 209M05 because nothing priced it.

      LANDING  a leader that TERMINATES inside a symbol is attached to it and
               cannot get there without piercing its outline.  ISOGEN draws
               exactly this on 545M05: four text-less connector leaders, one
               ending inside a Floor_Symbol, which the first version of this
               check reported as a permanent unfixable defect.  A checker
               that reports the impossible teaches people to ignore it.

    One hit per SYMBOL, not per curve: "the leader crosses this valve" is one
    fact about the drawing, however many little arcs the glyph is made of.
    """
    if not symbols:
        return []
    tp, lp = Point(tip), Point(landing)
    # The part of the path that is NOT pointing: everything beyond the tip
    # zone.  Judged separately for grazing (below).
    far_path = path.difference(tp.buffer(tol))
    hits = []
    for name, extent, curves in symbols:
        hull = unary_union(curves).convex_hull
        if hull.geom_type == "Polygon" and hull.contains(lp):
            continue                      # leader attaches INSIDE this glyph
            # The DRAWN hull, not the bbox: a balloon landing beside a tall
            # support's stem is inside the support's bbox but nowhere near
            # its glyph, and the leader was then free to lie along the
            # triangle edge (2026-09-17).  'within tol of the bbox' was
            # looser still -- it exempted a landing 2 mm from a reducer's
            # box while the leader ran across the reducer.
        if extent.distance(tp) <= tol:
            # The ARROW is on (or in) this symbol.  An instrument tip sits
            # DEEP inside its gauge glyph, so the leader cannot leave without
            # crossing the symbol's own linework -- that is pointing, not
            # crossing (202M01_r0-1 reported two permanent 'crossings' a
            # drafter could never remove).  But 'near the bbox' exempted far
            # too much: a tip on the pipe beside a 35 mm support symbol sat
            # inside that symbol's EXTENT, and the leader was then free to
            # run the whole length of the support (2026-09-17, difficult
            # sheet: two leaders straight through the yellow glyphs, B=0).
            # Exempt the whole glyph only when the tip is inside its drawn
            # hull by more than `tol`; otherwise the per-curve tip zone
            # below is the only allowance, as for every other symbol.
            if hull.geom_type == "Polygon" and hull.contains(tp)                     and hull.exterior.distance(tp) > tol:
                continue
        for curve in curves:
            # A SOLID FILL is ink wherever it is: a leader clipping the corner
            # of a support triangle 2 mm from its tip is a visible slash, not
            # pointing.  Closed rings (HATCH boundaries) therefore get only
            # FILL_TIP of allowance -- enough to leave a 3.2 mm weld dot from
            # its centre, not enough to cross a 7 mm support glyph.
            cc = list(curve.coords)
            if len(cc) >= 4 and cc[0] == cc[-1]:
                # ... and a leader skimming a fill's edge within GRAZE reads
                # as cutting it (0.3 mm outside a support triangle looked
                # like a slash through it in TrueView).
                poly = Polygon(cc).buffer(GRAZE)
                run = path.difference(tp.buffer(FILL_TIP)).intersection(poly)
                if getattr(run, "length", 0) > 0.3:
                    hits.append((name, run.centroid))
                    break
            # GRAZING is crossing to the eye: a leader laid along a support's
            # triangle edge within 0.3 mm never 'intersects' it, and shipped
            # (2026-09-17).  Any run of more than GRAZE_RUN alongside a curve
            # beyond the tip zone is a hit.
            if far_path.intersection(curve.buffer(GRAZE)).length > GRAZE_RUN:
                hits.append((name, far_path.intersection(curve.buffer(GRAZE)).centroid))
                break
            inter = path.intersection(curve)
            if inter.is_empty:
                continue
            # PER CONTACT, not per curve: a closed ring (support triangle,
            # weld dot) touched at the tip AND crossed 6 mm further along
            # gave one MultiPoint whose distance to the tip was 0 -- the
            # apex contact masked the real crossing.
            far = [g for g in getattr(inter, "geoms", [inter])
                   if g.distance(tp) > tol]
            if far:
                hits.append((name, far[0].centroid))
                break
    return hits


def _label_hits(path: LineString, label_boxes, exempt) -> int:
    """Foreign label boxes this path cuts through.

    `crosses`, not `intersects`, and exempt-aware -- so this counts exactly
    what mess_map's category-B audit counts.  A router scored by a looser
    rule than the auditor will always end up arguing with it."""
    return sum(1 for i, b in enumerate(label_boxes)
               if i not in exempt and path.crosses(b))


def _cost(path: LineString, others: list[LineString],
          obstacles, label_boxes, exempt, symbols=None,
          tip: Vec2 | None = None, landing: Vec2 | None = None) -> float:
    """Crossings dominate; then bends (fewer is calmer); then length; then a
    mild preference for the isometric axes so a re-routed leader still looks
    drawn, not computed.

    Ordering of the crossing weights is deliberate: another LEADER is worst
    (reads as swapped callouts), a foreign LABEL next, then drawn component
    SYMBOLS, then bbox obstacles (which near a cluster are unavoidable and
    barely discriminate)."""
    c = 0.0
    for o in others:
        if path.crosses(o):
            c += 1000.0
    c += 800.0 * _label_hits(path, label_boxes, exempt)
    if symbols and tip is not None and landing is not None:
        # The candidate's OWN endpoint, not the leader's current one: a route
        # is judged by where it actually ends.
        c += 500.0 * len(symbol_hits(path, tip, path.coords[-1], symbols))
    for g in obstacles:
        if path.crosses(g):
            c += 400.0
    c += 25.0 * (len(path.coords) - 2)        # per elbow
    c += 2.0 * path.length
    for a, b in zip(path.coords[:-1], path.coords[1:]):
        ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0
        best = min(abs(ang - t) for t in (0.0, 30.0, 90.0, 150.0, 180.0))
        c += 0.35 * best
    return c


def reroute(leaders: list[Leader], obstacles, label_boxes,
            symbols=None, sweeps: int = 3) -> tuple[int, int]:
    """Re-route in place.  Returns (defects_before, defects_after).

    A defect is exactly what mess_map calls category B: this leader crossing
    another LEADER, a foreign LABEL box, or a component SYMBOL it neither
    points at nor lands in (`symbols`, optional -- see `symbol_hits`).

    THE COUNTER MUST MATCH THE AUDITOR.  It once counted only leader-leader
    and linework hits, so on 545M05 the router reported "0 defects, 0
    re-routed" while the audit of the very same file reported B=1: a leader
    crossing a label was never counted, therefore never triggered a re-route,
    therefore was never fixed.  A defect outside the trigger is a defect that
    survives, however good the cost function is.

    Only the landing end and the bends of a path ever change; every
    candidate starts at the leader's original tip.
    """
    for ld in leaders:
        if ld.path is None:
            ld.path = LineString([ld.tip, ld.landing])

    def defects_of(i):
        ld = leaders[i]
        return (sum(1 for j, o in enumerate(leaders)
                    if j != i and ld.path.crosses(o.path))
                + _label_hits(ld.path, label_boxes, ld.exempt)
                + len(symbol_hits(ld.path, ld.tip, ld.landing, symbols)))

    def total_defects():
        # Leader-leader pairs counted once; the rest are per-leader.
        n = sum(1 for i in range(len(leaders))
                for j in range(i + 1, len(leaders))
                if leaders[i].path.crosses(leaders[j].path))
        for i, ld in enumerate(leaders):
            n += _label_hits(ld.path, label_boxes, ld.exempt)
            n += len(symbol_hits(ld.path, ld.tip, ld.landing, symbols))
        return n

    before = total_defects()

    for _ in range(sweeps):

        order = sorted(range(len(leaders)), key=lambda i: -defects_of(i))
        changed = False
        for i in order:
            ld = leaders[i]
            if defects_of(i) == 0:
                continue                      # do not disturb what is fine
            others = [l.path for j, l in enumerate(leaders) if j != i]
            landings = [ld.landing] + _perimeter_points(ld.label_box, 12)
            # AN ELBOW IS A LAST RESORT, NOT A TIE-BREAKER.  Hard user rule:
            # "there is no need to add an elbow, only add when necessary."
            # So candidates are ranked lexicographically -- defects first,
            # then BENDS, then cost -- which means a bent path is chosen only
            # when it removes a defect no straight path can, never because it
            # shaved a few points of length or angle.
            def rank(p):
                # RANKED BY KIND, NOT BY A SUM.  Summing the three defect
                # types made them interchangeable, so a route that removed a
                # leader-leader crossing but clipped a label box scored the
                # same as the crossing it fixed -- and the straight path then
                # won the "fewer bends" tiebreak.  353M05 had 78 candidate
                # routes that cleared its crossing and the router took none of
                # them.  A leader crossing another leader is the defect the
                # engineer named: it reads as swapped item numbers.  It goes
                # first, and nothing else may outvote it.
                return (sum(1 for o in others if p.crosses(o)),
                        _label_hits(p, label_boxes, ld.exempt),
                        len(symbol_hits(p, ld.tip, p.coords[-1], symbols)),
                        len(p.coords) - 2,
                        _cost(p, others, obstacles, label_boxes, ld.exempt,
                              symbols, ld.tip, ld.landing))

            best, best_key = ld.path, rank(ld.path)
            for p in _candidate_paths(ld, landings):
                k = rank(p)
                if k < best_key:
                    best_key, best = k, p
            # ONLY A CLEAN ROUTE MAY REPLACE THE CURRENT ONE.  The ranking
            # would otherwise trade defect kinds -- one leader crossing for a
            # pass through three labels, or over a valve glyph -- and both
            # trades shipped and were rejected on sight.  If no candidate is
            # clean, the leader stays as drawn and the defect stays reported:
            # fixing it is then the label-mover's job, not the bender's.
            if best_key[:3] != (0, 0, 0):
                continue
            if best is not ld.path:
                assert best.coords[0] == (ld.tip[0], ld.tip[1]), \
                    "arrow tip moved -- forbidden"
                ld.path = best
                ld.landing = (best.coords[-1][0], best.coords[-1][1])
                ld.moved = True
                changed = True
        if not changed:
            break

    after = total_defects()
    return before, after
