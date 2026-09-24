"""
Margin layout: every callout OUTSIDE the drawing, in cleared space around it.

User directive 2026-09-17: "all the annotations out of the drawing, no
overlap on the drawing, horizontal, leader fixed at the head (the pipe),
its length can change".  So:

  1. The view's window is ENLARGED around the linework by a margin wide
     enough for a column of labels left and right and a few rows top and
     bottom.  (writeback.set_windows pushes the new window into the
     paper-space VIEWPORT -- same scale, bigger viewport.)

  2. One callout per P&ID line per view, anchored on the line's longest
     run.  Each anchor goes to the NEAREST side of the drawing.

  3. On each side the labels are stacked in slots -- a column with one
     label per row (left/right), rows of labels (top/bottom) -- ordered
     by where their pipes are, so leaders from one side never cross each
     other.  A label wants the slot level with its pipe; when two want
     the same slot the later one steps down.  No label ever touches
     linework (it is outside all of it) or another label (slots are
     disjoint by construction).

  4. The leader runs from the label's near edge to the anchor on the
     pipe, arrowhead on the pipe, however long that is.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from shapely.ops import unary_union

from .config import DEFAULT_TUNING, Tuning
from .match import Match
from .ortho import Ortho, Run, Vec2
from .place import Callout, _longest_segment, clusters, text_width

Window = tuple[float, float, float, float]

#: Vertical pitch between stacked labels, in text heights.
ROW_PITCH_H = 1.7
#: Gap between the drawing's linework and the first label, in h.
GAP_H = 2.0
#: Rows of labels above and below the drawing.
TOP_ROWS = 2


@dataclass
class _Slotted:
    callout: Callout
    side: str          # L R T B
    key: float         # anchor coordinate along the side


def _anchor_for(run: Run, h: float) -> Vec2:
    group = clusters(run, 3.0 * h)[0]
    (ax, ay), (bx, by) = _longest_segment(group)
    return ((ax + bx) / 2, (ay + by) / 2)


def plan_margins(ortho: Ortho, matches: dict[str, Match],
                 cfg: Tuning = DEFAULT_TUNING) -> tuple[list[Callout], dict[str, Window]]:
    """All callouts in the margins, and the enlarged window per view."""
    out: list[Callout] = []
    windows: dict[str, Window] = {}

    for view, fr in ortho.frames.items():
        h = fr.text_height
        lines = ortho.linework.get(view, [])
        if not lines:
            continue
        xs, ys = [], []
        for ln in lines:
            b = ln.bounds
            xs += [b[0], b[2]]
            ys += [b[1], b[3]]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)

        # One callout per P&ID name in this view: the longest run wins.
        best_run: dict[str, Run] = {}
        for (v, tag), run in ortho.runs.items():
            if v != view or tag not in matches:
                continue
            label = matches[tag].label
            if label not in best_run or sum(l.length for l in run.lines) > \
                    sum(l.length for l in best_run[label].lines):
                best_run[label] = run
        if not best_run:
            continue

        wmax = max(text_width(lbl, h, cfg) for lbl in best_run)
        pitch = ROW_PITCH_H * h
        side_margin = wmax + (GAP_H + 2) * h
        top_margin = TOP_ROWS * pitch + (GAP_H + 1) * h
        window = (x0 - side_margin, y0 - top_margin, x1 + side_margin, y1 + top_margin)
        windows[view] = window

        # Assign each callout to its nearest side.
        by_side: dict[str, list[_Slotted]] = defaultdict(list)
        for label, run in best_run.items():
            m = matches[run.tag]
            ax, ay = _anchor_for(run, h)
            w = text_width(label, h, cfg)
            c = Callout(view, run.tag, label, m.review, (ax, ay), (0.0, 0.0), 0.0,
                        h, w, leader=True)
            d = {"L": ax - x0, "R": x1 - ax, "B": ay - y0, "T": y1 - ay}
            side = min(d, key=d.get)
            by_side[side].append(_Slotted(c, side, ay if side in "LR" else ax))

        # Columns: one label per row, ordered top-down by pipe height.
        for side in ("L", "R"):
            items = sorted(by_side.get(side, []), key=lambda s: -s.key)
            n_slots = max(1, int((y1 - y0) / pitch) + 1)
            wanted = [min(n_slots - 1, max(0, int(round((y1 - s.key) / pitch)))) for s in items]
            slots = _monotone(wanted, n_slots)
            for s, k in zip(items, slots):
                c = s.callout
                y = y1 - k * pitch - h / 2
                x = (x0 - GAP_H * h - c.width) if side == "L" else (x1 + GAP_H * h)
                c.pos = (x, y)
                out.append(c)

        # Rows: labels side by side, ordered left-right; overflow to the
        # next row out.
        for side in ("T", "B"):
            items = sorted(by_side.get(side, []), key=lambda s: s.key)
            if not items:
                continue
            cell = wmax + 2 * h
            n_cols = max(1, int((x1 - x0) / cell) + 1)
            wanted = [min(n_cols - 1, max(0, int((s.key - x0) / cell))) for s in items]
            rows: list[list[int | None]] = []
            for s, k in zip(items, wanted):
                placed = False
                for r, row in enumerate(rows):
                    kk = _next_free(row, k)
                    if kk is not None:
                        row[kk] = 1
                        _put_row(s.callout, side, r, kk, cell, h, pitch, x0, y0, y1)
                        placed = True
                        break
                if not placed:
                    rows.append([None] * n_cols)
                    rows[-1][k] = 1
                    _put_row(s.callout, side, len(rows) - 1, k, cell, h, pitch, x0, y0, y1)
                out.append(s.callout)
            extra = max(0, len(rows) - TOP_ROWS) * pitch
            if extra:
                wx0, wy0, wx1, wy1 = windows[view]
                windows[view] = (wx0, wy0 - (extra if side == "B" else 0),
                                 wx1, wy1 + (extra if side == "T" else 0))
    return out, windows


def _put_row(c: Callout, side: str, row: int, col: int, cell: float, h: float,
             pitch: float, x0: float, y0: float, y1: float) -> None:
    x = x0 + col * cell
    if side == "T":
        y = y1 + GAP_H * h + row * pitch
    else:
        y = y0 - GAP_H * h - h - row * pitch
    c.pos = (x, y)


def _next_free(row: list, k: int) -> int | None:
    for kk in range(k, len(row)):
        if row[kk] is None:
            return kk
    return None


def _monotone(wanted: list[int], n_slots: int) -> list[int]:
    """Distinct, strictly increasing slot indices close to `wanted`."""
    out: list[int] = []
    for k in wanted:
        k = max(k, (out[-1] + 1) if out else 0)
        out.append(k)
    # Pull back anything pushed past the last slot.
    for i in range(len(out) - 1, -1, -1):
        limit = n_slots - 1 - (len(out) - 1 - i)
        if out[i] > limit:
            out[i] = limit
    return out
