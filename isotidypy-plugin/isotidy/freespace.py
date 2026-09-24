"""
Where is there actually room on this sheet?

The solver used to answer that by guessing: a ring of 97 positions around the
label and around its component, tested one by one.  If the only clear space
lay 25 mm away, or in a direction the 12-spoke ring did not sample, no
candidate on the menu was clean and the label stayed in trouble.  "There was
obviously room right there" is the complaint that motivated this file.

The method is an occupancy grid plus a summed-area table:

  1. Lay a fine grid over the sheet (0.5 mm cells -- an A1 sheet is about
     1700 x 1200 of them, which costs a few megabytes and a few milliseconds).
  2. Mark every cell that has ink in it: pipe, symbols, dimension and witness
     lines, tables, the frame.
  3. Build the cumulative sum of that grid.  The number of occupied cells
     inside ANY axis-aligned rectangle is then four lookups, regardless of
     size -- so "does a 15 x 4 mm label fit here with clearance?" is answered
     exactly and in constant time, and we can afford to ask it thousands of
     times per label.

Deliberately free of CAD types and of scipy: it takes plain coordinates and
uses numpy only, so it ports to the C# plugin as-is.

WHAT IT DOES NOT KNOW: other labels.  They move while the solver runs, so
baking them into a static grid would make it lie.  The grid answers "is this
spot clear of the DRAWING"; label-versus-label conflict stays with the cost
function, which sees live positions.
"""

from __future__ import annotations

import math

import numpy as np

Vec2 = tuple[float, float]


class FreeSpace:
    """Occupancy of the sheet, queryable as "does this box fit here?"."""

    def __init__(self, bounds: tuple[float, float, float, float],
                 cell: float = 0.5) -> None:
        self.x0, self.y0, x1, y1 = bounds
        self.cell = cell
        self.nx = max(1, int(math.ceil((x1 - self.x0) / cell)))
        self.ny = max(1, int(math.ceil((y1 - self.y0) / cell)))
        self._occ = np.zeros((self.ny, self.nx), dtype=np.uint8)
        self._sat: np.ndarray | None = None

    # -- building ---------------------------------------------------------
    def _cells(self, x: float, y: float) -> tuple[int, int]:
        return (int((x - self.x0) / self.cell), int((y - self.y0) / self.cell))

    def add_rect(self, x0: float, y0: float, x1: float, y1: float,
                 grow: float = 0.0) -> None:
        cx0, cy0 = self._cells(x0 - grow, y0 - grow)
        cx1, cy1 = self._cells(x1 + grow, y1 + grow)
        cx0, cy0 = max(0, cx0), max(0, cy0)
        cx1 = min(self.nx - 1, cx1)
        cy1 = min(self.ny - 1, cy1)
        if cx1 >= cx0 and cy1 >= cy0:
            self._occ[cy0:cy1 + 1, cx0:cx1 + 1] = 1

    def add_segments(self, segments, grow: float = 0.0) -> None:
        """Stamp polylines onto the grid.

        Walked at half-cell steps so no cell is skipped on a diagonal, which
        an isometric drawing is made of.
        """
        r = int(math.ceil(grow / self.cell))
        step = self.cell * 0.5
        for pts in segments:
            for (ax, ay), (bx, by) in zip(pts[:-1], pts[1:]):
                dx, dy = bx - ax, by - ay
                length = math.hypot(dx, dy)
                n = max(1, int(length / step))
                for i in range(n + 1):
                    t = i / n
                    cx, cy = self._cells(ax + dx * t, ay + dy * t)
                    x0, x1 = max(0, cx - r), min(self.nx - 1, cx + r)
                    y0, y1 = max(0, cy - r), min(self.ny - 1, cy + r)
                    if x1 >= x0 and y1 >= y0:
                        self._occ[y0:y1 + 1, x0:x1 + 1] = 1

    def finish(self) -> "FreeSpace":
        """Build the summed-area table.  Call once, after all adds."""
        self._sat = self._occ.cumsum(0, dtype=np.int32).cumsum(1, dtype=np.int32)
        return self

    # -- queries ----------------------------------------------------------
    def occupied_in(self, x0: float, y0: float, x1: float, y1: float) -> int:
        """Occupied cells inside a rectangle -- four lookups, any size."""
        if self._sat is None:
            self.finish()
        cx0, cy0 = self._cells(x0, y0)
        cx1, cy1 = self._cells(x1, y1)
        cx0, cy0 = max(0, cx0), max(0, cy0)
        cx1 = min(self.nx - 1, cx1)
        cy1 = min(self.ny - 1, cy1)
        if cx1 < cx0 or cy1 < cy0:
            return 0
        s = self._sat
        total = int(s[cy1, cx1])
        if cx0 > 0:
            total -= int(s[cy1, cx0 - 1])
        if cy0 > 0:
            total -= int(s[cy0 - 1, cx1])
        if cx0 > 0 and cy0 > 0:
            total += int(s[cy0 - 1, cx0 - 1])
        return total

    def fits(self, pos: Vec2, size: Vec2, pad: float = 0.0) -> bool:
        x, y = pos
        w, h = size
        return self.occupied_in(x - pad, y - pad, x + w + pad, y + h + pad) == 0

    def room_at(self, pos: Vec2, size: Vec2, pad: float, halo: float) -> bool:
        """Does it fit with `halo` mm of extra air?  Used to prefer a roomy
        slot over a merely legal one -- which is most of what makes a
        hand-placed label look right."""
        return self.fits(pos, size, pad + halo)

    def open_slots(self, home: Vec2, size: Vec2, pad: float,
                   max_reach: float, step: float = 2.0,
                   limit: int = 24, halo: float = 1.5) -> list[Vec2]:
        """Positions where the box genuinely fits, NEAREST HOME FIRST.

        Nearest-first matters as much as the search itself: the point is to
        find the closest honest gap, not the emptiest corner of the sheet.
        Roomy slots (those that also clear `halo`) are offered before tight
        ones at a similar distance.
        """
        out: list[tuple[float, int, Vec2]] = []
        n = int(max_reach / step)
        for i in range(-n, n + 1):
            for j in range(-n, n + 1):
                dx, dy = i * step, j * step
                d = math.hypot(dx, dy)
                if d < 1e-9 or d > max_reach:
                    continue
                pos = (home[0] + dx, home[1] + dy)
                if not self.fits(pos, size, pad):
                    continue
                tight = 0 if self.room_at(pos, size, pad, halo) else 1
                out.append((d, tight, pos))
        # distance first, then roominess: a tight slot 5 mm away still beats a
        # luxurious one 30 mm away, because travel is what the reader notices.
        out.sort(key=lambda t: (round(t[0], 3), t[1], t[2]))
        return [p for _d, _t, p in out[:limit]]
