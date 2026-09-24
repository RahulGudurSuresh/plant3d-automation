"""
Where each callout goes.  Pure 2D: nothing here knows what a DXF is.

THE RULE (learned on the Back View, 2026-09-17): a callout that cannot be
placed legibly is worse than no callout.  The first version labelled every
run in every view -- 62 full names in a 170 x 121 mm elevation -- and the
user's verdict was "can't see a thing".  The elevation had more text than
white space; no solver fixes that.

So placement is now two passes over ALL views:

  pass 1  plan view first, then the others by size.  Each run gets a
          callout only if the best spot is CLEAN (no label overlap, almost
          no linework under the text, a short leader).  Otherwise the run
          is skipped in that view -- the line is still named wherever it
          IS clean, usually the plan.

  pass 2  every tag that pass 1 left unlabelled in every view is FORCED
          into the one view where it costs least, and flagged so the
          report says so.

Per run:
  1. Split into clusters (a line can surface twice in one view).
  2. Anchor on the longest straight edge; text reads along the pipe.
  3. Score candidates: linework under the text, overlap with callouts
     already placed, distance, needing a leader, not parallel, and --
     absolutely -- leaving the viewport window, which would clip the text.

The obstacle test is "length of linework inside the label box", not a
boolean hit: text over the tip of one line is a nuisance, text over a
valve is unreadable, and the score should know the difference.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points, unary_union

from .config import DEFAULT_TUNING, Tuning
from .match import Match
from .ortho import Ortho, Run, Vec2, ViewFrame


@dataclass
class Callout:
    view: str
    tag: str
    text: str
    review: bool
    anchor: Vec2                 # on the pipe
    pos: Vec2                    # text bottom-left, view coordinates
    angle: float                 # radians from +u
    height: float
    width: float
    leader: bool
    cost: float = 0.0
    under: float = 0.0           # linework length inside the box, in h
    overlap: float = 0.0         # overlap with other callouts, in h^2
    forced: bool = False         # pass 2: nowhere was clean, this was cheapest

    def box(self, pad: float = 0.0) -> Polygon:
        return _rect(self.pos, self.angle, self.width, self.height, pad)

    def leader_points(self) -> tuple[Vec2, Vec2] | None:
        """(start on the label edge, end on the pipe) or None."""
        if not self.leader:
            return None
        p_label, p_anchor = nearest_points(self.box().exterior, Point(self.anchor))
        return ((p_label.x, p_label.y), (p_anchor.x, p_anchor.y))

    def clean(self, cfg: Tuning) -> bool:
        return (self.overlap == 0.0
                and self.under <= cfg.max_under_h
                and self.box().distance(Point(self.anchor)) <= cfg.max_leader_h * self.height)


def _rect(pos: Vec2, angle: float, w: float, h: float, pad: float = 0.0) -> Polygon:
    c, s = math.cos(angle), math.sin(angle)
    ux, uy = c, s
    vx, vy = -s, c
    x0 = pos[0] - ux * pad - vx * pad
    y0 = pos[1] - uy * pad - vy * pad
    W, H = w + 2 * pad, h + 2 * pad
    return Polygon([
        (x0, y0),
        (x0 + ux * W, y0 + uy * W),
        (x0 + ux * W + vx * H, y0 + uy * W + vy * H),
        (x0 + vx * H, y0 + vy * H),
    ])


def _upright(angle: float) -> float:
    """Fold a direction into (-90, 90] degrees so text never reads upside down."""
    a = math.atan2(math.sin(angle), math.cos(angle))
    if a <= -math.pi / 2:
        a += math.pi
    elif a > math.pi / 2:
        a -= math.pi
    return a


def _longest_segment(lines: list[LineString]) -> tuple[Vec2, Vec2]:
    best, best_len = None, -1.0
    for ln in lines:
        c = list(ln.coords)
        for a, b in zip(c, c[1:]):
            d = math.hypot(b[0] - a[0], b[1] - a[1])
            if d > best_len:
                best, best_len = (a, b), d
    return best


def clusters(run: Run, gap: float) -> list[list[LineString]]:
    """Connected groups of the run's lines, closer than `gap`, largest first."""
    if len(run.lines) == 1:
        return [run.lines]
    blobs = unary_union([ln.buffer(gap / 2, cap_style=2) for ln in run.lines])
    parts = list(blobs.geoms) if hasattr(blobs, "geoms") else [blobs]
    groups: list[list[LineString]] = [[] for _ in parts]
    for ln in run.lines:
        p = ln.representative_point()
        for i, blob in enumerate(parts):
            if blob.intersects(p):
                groups[i].append(ln)
                break
    groups = [g for g in groups if g]
    groups.sort(key=lambda g: -sum(l.length for l in g))
    return groups


def text_width(text: str, h: float, cfg: Tuning) -> float:
    return len(text) * h * cfg.char_aspect


def _union_bounds(lines: list[LineString]) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for ln in lines:
        b = ln.bounds
        xs += [b[0], b[2]]
        ys += [b[1], b[3]]
    return (min(xs), min(ys), max(xs), max(ys))


class Placer:
    def __init__(self, ortho: Ortho, cfg: Tuning = DEFAULT_TUNING):
        self.ortho = ortho
        self.cfg = cfg
        self.placed: dict[str, list[Callout]] = {}

    # -- scoring -----------------------------------------------------------
    def _score(self, view: str, fr: ViewFrame, poly: Polygon, anchor: Vec2,
               parallel: bool, leader: bool, h: float,
               pending: list[Callout] = ()) -> tuple[float, float, float]:
        """(cost, linework-under in h, label overlap in h^2).

        `pending`: callouts chosen for THIS run but not yet committed --
        a line's second callout must not sit on its first (350M02 did).
        """
        cfg = self.cfg
        if fr.window is not None:
            wx0, wy0, wx1, wy1 = fr.window
            bx0, by0, bx1, by1 = poly.bounds
            if bx0 < wx0 or by0 < wy0 or bx1 > wx1 or by1 > wy1:
                return cfg.w_outside, 0.0, 0.0
        tree = self.ortho.tree(view)
        lines = self.ortho.linework[view]
        weights = self.ortho.weights.get(view)
        under = 0.0
        for i in tree.query(poly):
            under += poly.intersection(lines[i]).length * (weights[i] if weights else 1.0)
        overlap = 0.0
        for other in [*self.placed.get(view, []), *pending]:
            ob = other.box(cfg.pad_h * h)
            if ob.intersects(poly):
                overlap += ob.intersection(poly).area
        if overlap > 0.0:
            # Hard ban, not a price.  Two stacked labels are two lost
            # labels; the forced pass used to pay the price and stack
            # them at the window edge (Back View, right margin).
            return cfg.w_outside, 0.0, overlap / (h * h)
        dist = poly.distance(Point(anchor))
        under_h, overlap_h2 = under / h, overlap / (h * h)
        cost = (cfg.w_linework * under_h
                + cfg.w_label_overlap * overlap_h2
                + cfg.w_distance * dist / h
                + (cfg.w_leader if leader else 0.0)
                + (0.0 if parallel else cfg.w_not_parallel))
        return cost, under_h, overlap_h2

    # -- candidates --------------------------------------------------------
    def _candidates(self, fr: ViewFrame, seg: tuple[Vec2, Vec2], w: float, h: float):
        cfg = self.cfg
        (ax, ay), (bx, by) = seg
        L = math.hypot(bx - ax, by - ay)
        mid = ((ax + bx) / 2, (ay + by) / 2)
        theta = _upright(math.atan2(by - ay, bx - ax))
        t = (math.cos(theta), math.sin(theta))
        p = (-t[1], t[0])
        # Beside the pipe, either side, slid along it.  The OFFSET follows
        # the pipe's direction; the TEXT follows it only when rotation is
        # allowed -- otherwise the box is laid horizontally at that spot
        # (a riser gets its label to its left or right, not along it).
        text_angle = 0.0 if cfg.horizontal_only else theta
        parallel = abs(math.sin(theta - text_angle)) < 0.05
        ta = (math.cos(text_angle), math.sin(text_angle))
        pa = (-ta[1], ta[0])
        for side in (1.0, -1.0):
            for d in cfg.offsets_h:
                for s in cfg.shifts:
                    cx = mid[0] + side * p[0] * (d * h + h / 2) + t[0] * s * L
                    cy = mid[1] + side * p[1] * (d * h + h / 2) + t[1] * s * L
                    if not parallel:
                        # keep the box clear of the pipe: shift it outward
                        # by half its own width along the offset direction
                        cx += side * p[0] * (w / 2)
                        cy += side * p[1] * (w / 2)
                    pos = (cx - ta[0] * w / 2 - pa[0] * h / 2,
                           cy - ta[1] * w / 2 - pa[1] * h / 2)
                    yield pos, text_angle, parallel, mid
        # Horizontal, radiating out: the leadered fallback.
        for r in cfg.radial_h:
            for k in range(8):
                a = k * math.pi / 4
                cx = mid[0] + math.cos(a) * r * h
                cy = mid[1] + math.sin(a) * r * h
                yield (cx - w / 2, cy - h / 2), 0.0, False, mid

    # -- one run -----------------------------------------------------------
    def best_for_run(self, run: Run, m: Match, relax: bool = False) -> list[Callout]:
        """The cheapest callout per cluster.  Nothing is committed.

        `relax`: the line is named nowhere else, so even a stub (below
        min_run_h) may carry the callout -- with a leader, one cluster.
        """
        fr = self.ortho.frames[run.view]
        cfg = self.cfg
        h = fr.text_height
        text = m.label
        w = text_width(text, h, cfg)
        out: list[Callout] = []
        groups = clusters(run, cfg.cluster_gap_h * h)[: 1 if relax else cfg.max_per_view]
        longest = sum(l.length for l in groups[0]) if groups else 0.0
        for i, group in enumerate(groups):
            b = _union_bounds(group)
            if not relax and max(b[2] - b[0], b[3] - b[1]) < cfg.min_run_h * h:
                continue
            if i and sum(l.length for l in group) < cfg.secondary_run_ratio * longest:
                break
            seg = _longest_segment(group)
            pipe = unary_union(group)
            best: Callout | None = None
            for pos, angle, parallel, anchor in self._candidates(fr, seg, w, h):
                poly = _rect(pos, angle, w, h, cfg.pad_h * h)
                leader = poly.distance(pipe) > cfg.leader_threshold_h * h
                c, under, overlap = self._score(run.view, fr, poly, anchor, parallel,
                                                leader, h, pending=out)
                if best is None or c < best.cost:
                    best = Callout(run.view, run.tag, text, m.review, anchor,
                                   pos, angle, h, w, leader, c, under, overlap)
                    if c == 0.0:
                        break
            if best is not None and best.cost < cfg.w_outside:
                out.append(best)
        return out

    def commit(self, c: Callout) -> None:
        self.placed.setdefault(c.view, []).append(c)


def plan(ortho: Ortho, matches: dict[str, Match],
         cfg: Tuning = DEFAULT_TUNING) -> list[Callout]:
    """Every callout for the drawing -- see the module docstring."""
    placer = Placer(ortho, cfg)
    out: list[Callout] = []
    #: What is "named" is the P&ID LINE, not the Plant 3D tag: 152M02 and
    #: 152L0201 (its branch) both read 214-152-080-LCL-UCSA-WN, and one
    #: callout names them both.  Keyed by label text for exactly that.
    named: set[str] = set()

    def is_plan(view: str) -> bool:
        return abs(ortho.frames[view].n.z) > 0.99

    views = sorted(ortho.frames,
                   key=lambda v: (0 if is_plan(v) else 1,
                                  -sum(l.length for l in ortho.linework.get(v, []))))

    # Pass 1: clean placements only, plan first.
    for view in views:
        runs = sorted((r for (v, _), r in ortho.runs.items() if v == view),
                      key=lambda r: -sum(l.length for l in r.lines))
        for run in runs:
            m = matches.get(run.tag)
            if m is None:
                continue
            for c in placer.best_for_run(run, m):
                if c.clean(cfg):
                    placer.commit(c)
                    out.append(c)
                    named.add(m.label)

    # Pass 2: a line named nowhere is named where it hurts least -- on its
    # longest run across all its tags and views, stubs allowed.
    pending: dict[str, list[Run]] = {}
    for (view, tag), run in ortho.runs.items():
        m = matches.get(tag)
        if m is not None and m.label not in named:
            pending.setdefault(m.label, []).append(run)
    for label, runs in pending.items():
        best: Callout | None = None
        for run in sorted(runs, key=lambda r: -sum(l.length for l in r.lines)):
            for c in placer.best_for_run(run, matches[run.tag], relax=True)[:1]:
                if best is None or c.cost < best.cost:
                    best = c
        if best is not None:
            best.forced = True
            placer.commit(best)
            out.append(best)
            named.add(label)
    return out
