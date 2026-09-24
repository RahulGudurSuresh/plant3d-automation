"""
DXF -> views, runs and obstacles.

This is the dirty layer: everything CAD-specific lives here so match.py and
place.py stay pure 2D geometry.

WHAT A PLANT 3D ORTHO LOOKS LIKE (measured on JP1071 "Plan view.dwg"):

  * Model space holds one anonymous-block INSERT per projected component,
    at (0,0,0), scale 1, on layer "<view>-<3D layer>".  The 3D layer is
    the LineNumberTag for piping ("Front View-209M01"), or CENTER / HIDDEN
    / Pipe Support / 9 ... for everything else.

  * Each view's linework is FLATTENED INTO A WCS PLANE at real model
    coordinates: the plan lies in Z=0, the side views in X=0, the front and
    back views in Y=0.  It is 2D drawing with 3D coordinates.

  * A paper-space VIEWPORT shows each view.  Its view_direction_vector says
    which side of the plane we look from (Left and Right views share the
    X=0 plane, mirrored), and its frozen-layer list hides every other
    view's layers.  The viewport window is exactly the view's extents, so
    anything we draw outside it is clipped.

  * The PNP cache blob (a gzip'd SQLite in the Autodesk_PNP dictionary) has
    the same view definitions, but no piping properties -- size and spec
    are NOT in the ortho.  That is why the P&ID is needed at all.

ViewFrame turns a view into a 2D drawing board: (u, v) axes from AutoCAD's
arbitrary-axis algorithm on the viewport's view direction, which is exactly
the DCS the viewport displays, so text laid along +u reads left-to-right on
paper.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

import ezdxf
from ezdxf.math import Vec3
from ezdxf.path import make_path
from shapely.geometry import LineString, box as shapely_box
from shapely.strtree import STRtree

from .config import JP1071, TEXT_PAPER_MM, Site

#: Entities whose flattened path is worth drawing / avoiding.
CURVE_TYPES = {"LINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE", "LWPOLYLINE",
               "POLYLINE"}
FLATTEN_MM = 2.0

Vec2 = tuple[float, float]


def arbitrary_axes(n: Vec3) -> tuple[Vec3, Vec3]:
    """AutoCAD's arbitrary axis algorithm: OCS/DCS X and Y for normal n."""
    n = n.normalize()
    if abs(n.x) < 1 / 64 and abs(n.y) < 1 / 64:
        ax = Vec3(0, 1, 0).cross(n)
    else:
        ax = Vec3(0, 0, 1).cross(n)
    ax = ax.normalize()
    ay = n.cross(ax).normalize()
    return ax, ay


@dataclass
class ViewFrame:
    """A view as a 2D board: (u, v) in the plane, n toward the viewer."""

    name: str
    n: Vec3
    u: Vec3
    v: Vec3
    #: Constant coordinate of the view plane along n (0 on JP1071).
    plane: float = 0.0
    #: Model units per paper mm (30 for 1:30).
    scale: float = 1.0
    #: Viewport window in (u, v): text outside it is clipped.  None = no
    #: viewport found, use the linework extents.
    window: tuple[float, float, float, float] | None = None
    viewport_handles: list[str] = field(default_factory=list)

    def to2d(self, p: Vec3) -> Vec2:
        return (p.dot(self.u), p.dot(self.v))

    def to_wcs(self, x: float, y: float) -> Vec3:
        return self.u * x + self.v * y + self.n * self.plane

    def direction_wcs(self, angle: float) -> Vec3:
        """WCS unit vector for an in-plane direction (radians from +u)."""
        return self.u * math.cos(angle) + self.v * math.sin(angle)

    @property
    def text_height(self) -> float:
        return TEXT_PAPER_MM * self.scale


@dataclass
class Run:
    """The linework of one tag in one view: what a callout points at."""

    view: str
    tag: str
    lines: list[LineString]          # in view (u, v) coordinates
    handles: list[str]               # the INSERTs it came from

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        xs, ys = [], []
        for ln in self.lines:
            b = ln.bounds
            xs += [b[0], b[2]]
            ys += [b[1], b[3]]
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class Ortho:
    doc: ezdxf.document.Drawing
    site: Site
    frames: dict[str, ViewFrame]
    runs: dict[tuple[str, str], Run]                  # (view, tag) -> Run
    linework: dict[str, list[LineString]]             # view -> every curve
    #: Cost weight per curve, aligned with `linework` (Site.curve_weight).
    weights: dict[str, list[float]] = field(default_factory=dict)
    _trees: dict[str, STRtree] = field(default_factory=dict)

    def tags(self) -> list[str]:
        return sorted({t for _, t in self.runs})

    def views_for(self, tag: str) -> list[str]:
        return sorted(v for v, t in self.runs if t == tag)

    def tree(self, view: str) -> STRtree:
        if view not in self._trees:
            self._trees[view] = STRtree(self.linework.get(view, []))
        return self._trees[view]


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def _flatten(entity) -> list[Vec3] | None:
    """An entity's drawn curve as WCS points, or None if it has no path."""
    t = entity.dxftype()
    if t == "LINE":
        return [Vec3(entity.dxf.start), Vec3(entity.dxf.end)]
    if t not in CURVE_TYPES:
        return None
    try:
        pts = list(make_path(entity).flattening(FLATTEN_MM))
    except Exception:
        return None
    return pts if len(pts) >= 2 else None


def _split_layer(name: str, site: Site) -> tuple[str | None, str]:
    m = site.view_layer_re.match(name)
    if not m:
        return None, name
    return m.group("view"), m.group("rest")


def _live_views(doc) -> set[str] | None:
    """View names from Plant 3D's DwgViewList xrecord; None if absent."""
    try:
        xr = doc.rootdict["DwgViewList"]
    except Exception:
        return None
    if xr.dxftype() != "XRECORD":
        return None
    return {t.value for t in xr.tags if t.code == 1}


def _frames_from_viewports(doc, site: Site, view_names: set[str]) -> dict[str, ViewFrame]:
    """One ViewFrame per view, from the paper-space viewport that shows it.

    A viewport 'shows' a view when it does not freeze that view's layers.
    Every ortho viewport freezes every OTHER view's layers, so the mapping
    is unambiguous; a view with no viewport gets a frame later from its
    own linework with the default (+) normal.
    """
    all_layers: dict[str, list[str]] = defaultdict(list)
    for layer in doc.layers:
        view, _ = _split_layer(layer.dxf.name, site)
        if view in view_names:
            all_layers[view].append(layer.dxf.name)

    frames: dict[str, ViewFrame] = {}
    for layout in doc.layouts:
        if layout.name == "Model":
            continue
        for vp in layout.query("VIEWPORT"):
            if vp.dxf.id == 1:          # the paper-space "viewport" itself
                continue
            frozen = set(vp.frozen_layers)
            shown = [v for v in view_names
                     if all_layers[v] and not all(l in frozen for l in all_layers[v])]
            if len(shown) != 1:
                continue
            view = shown[0]
            n = Vec3(vp.dxf.view_direction_vector)
            if n.is_null:
                n = Vec3(0, 0, 1)
            u, v = arbitrary_axes(n)
            vh = float(vp.dxf.view_height)
            aspect = float(vp.dxf.width) / float(vp.dxf.height)
            cx, cy = vp.dxf.view_center_point.x, vp.dxf.view_center_point.y
            scale = vh / float(vp.dxf.height)
            window = (cx - vh * aspect / 2, cy - vh / 2,
                      cx + vh * aspect / 2, cy + vh / 2)
            if view in frames:
                # Two viewports on one view (unlikely); keep the first
                # window, remember both handles for the freeze bookkeeping.
                frames[view].viewport_handles.append(vp.dxf.handle)
                continue
            frames[view] = ViewFrame(name=view, n=n.normalize(), u=u, v=v,
                                     scale=scale, window=window,
                                     viewport_handles=[vp.dxf.handle])
    return frames


def load(dxf_path: Path | str, site: Site = JP1071) -> Ortho:
    doc = ezdxf.readfile(str(dxf_path))
    msp = doc.modelspace()

    # Pass 1: every INSERT's flattened linework, grouped by view and tag.
    per_view_pts: dict[str, list[Vec3]] = defaultdict(list)
    raw_lines: dict[str, list[tuple[list[Vec3], float]]] = defaultdict(list)
    raw_runs: dict[tuple[str, str], tuple[list[list[Vec3]], list[str]]] = \
        defaultdict(lambda: ([], []))
    view_names: set[str] = set()

    for insert in msp.query("INSERT"):
        view, rest = _split_layer(insert.dxf.layer, site)
        if view is None:
            continue
        view_names.add(view)
        tag = rest if site.tag_re.match(rest) else None
        try:
            virtual = list(insert.virtual_entities())
        except Exception:
            continue
        for ve in virtual:
            pts = _flatten(ve)
            if not pts:
                continue
            # The curve's own layer says what it is (a pipe INSERT holds
            # CENTER and HIDDEN curves too); the INSERT's layer says whose.
            _, sub_rest = _split_layer(ve.dxf.layer, site)
            raw_lines[view].append((pts, site.curve_weight(sub_rest if tag is None else
                                                            (rest if site.tag_re.match(sub_rest) else sub_rest))))
            if len(per_view_pts[view]) < 5000:
                per_view_pts[view].append(pts[0])
            if tag is not None:
                lines, handles = raw_runs[(view, tag)]
                lines.append(pts)
                if not handles or handles[-1] != insert.dxf.handle:
                    handles.append(insert.dxf.handle)

    # Only views Plant 3D still lists are real.  GA-24-0004 carried 2521
    # INSERTs of a deleted "Front View" -- no viewport, not even flat --
    # and the linework fallback annotated it at 1:1: 89 callouts nobody
    # could ever see, and every line "named" there instead of the plan.
    live = _live_views(doc)
    orphaned = sorted(v for v in view_names if live is not None and v not in live)
    if orphaned:
        print(f"  !! ignoring {len(orphaned)} view(s) not in DwgViewList "
              f"(deleted in Plant 3D, linework left behind): {orphaned}")
        view_names -= set(orphaned)
        for v in orphaned:
            raw_lines.pop(v, None)
            per_view_pts.pop(v, None)
        for key in [k for k in raw_runs if k[0] in orphaned]:
            raw_runs.pop(key)

    # Pass 2: frames.  Viewports first; linework fallback for the rest.
    frames = _frames_from_viewports(doc, site, view_names)
    for view in view_names:
        pts = per_view_pts[view]
        if view not in frames:
            spans = [max(p[i] for p in pts) - min(p[i] for p in pts) for i in range(3)]
            flat = spans.index(min(spans))
            n = Vec3(*(1.0 if i == flat else 0.0 for i in range(3)))
            u, v = arbitrary_axes(n)
            frames[view] = ViewFrame(name=view, n=n, u=u, v=v)
        fr = frames[view]
        fr.plane = median(p.dot(fr.n) for p in pts) if pts else 0.0

    # Pass 3: project to 2D.
    linework: dict[str, list[LineString]] = {}
    weights: dict[str, list[float]] = {}
    for view, chains in raw_lines.items():
        fr = frames[view]
        ls, ws = [], []
        for pts, w in chains:
            xy = [fr.to2d(p) for p in pts]
            line = LineString(xy)
            if line.length > 0:
                ls.append(line)
                ws.append(w)
        linework[view] = ls
        weights[view] = ws
        if fr.window is None and ls:
            b = shapely_box(*_union_bounds(ls)).buffer(fr.text_height * 4).bounds
            fr.window = tuple(b)

    runs: dict[tuple[str, str], Run] = {}
    for (view, tag), (chains, handles) in raw_runs.items():
        fr = frames[view]
        ls = [LineString([fr.to2d(p) for p in pts]) for pts in chains]
        ls = [l for l in ls if l.length > 0]
        if ls:
            runs[(view, tag)] = Run(view=view, tag=tag, lines=ls, handles=handles)

    return Ortho(doc=doc, site=site, frames=frames, runs=runs, linework=linework,
                 weights=weights)


def _union_bounds(lines: list[LineString]) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for ln in lines:
        b = ln.bounds
        xs += [b[0], b[2]]
        ys += [b[1], b[3]]
    return (min(xs), min(ys), max(xs), max(ys))


# --------------------------------------------------------------------------
# Pipe size from linework (tie-breaker only)
# --------------------------------------------------------------------------
def estimate_tag_size(ortho: "Ortho", tag: str, od_table: dict[int, float]) -> int | None:
    """The tag's size: from the plan view if it has one, else from all views.

    Measured on JP1071: the plan (view normal along Z) draws the bare pipe
    and gets 540M02 -> 80, 310M02 -> 80, 233M02 -> 40, 155M02 -> 80.  The
    side views carry insulation cladding at OD + 2t, which outvotes the
    pipe (540M02 read DN100 = 88.9 + 2 x 12.7 there).  Only when the tag
    has no plan run -- risers, section-only stubs -- do we pool everything.
    """
    plan = [v for v, fr in ortho.frames.items() if abs(fr.n.z) > 0.99]
    for view in plan:
        run = ortho.runs.get((view, tag))
        if run is not None:
            dn, _ = estimate_od(run.lines, od_table)
            if dn is not None:
                return dn
    lines = [l for v in ortho.views_for(tag) for l in ortho.runs[(v, tag)].lines]
    return estimate_od(lines, od_table)[0]



def estimate_od(lines: list[LineString], od_table: dict[int, float],
                tol: float = 0.01, min_votes: int = 3,
                max_lines: int = 600) -> tuple[int | None, float | None]:
    """Guess the nominal size from parallel-line spacing.

    A pipe drawn in an ortho is two parallel edge lines EXACTLY one
    standard OD apart (88.90, 60.30 ...), and that spacing repeats on every
    straight piece.  We histogram the spacing of every parallel,
    overlapping pair, keep only spacings within `tol` (1 %) of a table OD,
    and take the mode.

    The 1 % is the point: insulation cladding, trays, supports and hatching
    add parallel lines at arbitrary spacings (313.7 on the 540M02 tray),
    and a loose tolerance let them snap to a neighbouring DN.  Measured on
    JP1071: per-pair voting over the whole table at 6 % mis-sized two
    thirds of the lines; the raw mode at 4 % lost 540M02 to its tray; the
    exact-OD filter gets 540M02 -> 80, 541M02 -> 50, 310M02 -> 80 and
    reports nothing it is unsure of.  Still evidence, not proof: match.py
    only trusts it when it agrees with exactly one P&ID candidate.
    """
    segs = []
    for ln in lines:
        c = list(ln.coords)
        if len(c) == 2:
            a, b = Vec3(c[0][0], c[0][1], 0), Vec3(c[1][0], c[1][1], 0)
            if (b - a).magnitude > 5:
                segs.append((a, b, (b - a).normalize()))
    segs = segs[:max_lines]
    spacings: dict[int, int] = defaultdict(int)
    for i, (a1, b1, d1) in enumerate(segs):
        l1 = (b1 - a1).magnitude
        for a2, b2, d2 in segs[i + 1:]:
            if abs(abs(d1.dot(d2)) - 1) > 1e-3:
                continue
            w = a2 - a1
            perp = (w - d1 * w.dot(d1)).magnitude
            if not 10 < perp < 700:
                continue
            t2 = sorted(((a2 - a1).dot(d1), (b2 - a1).dot(d1)))
            if min(l1, t2[1]) - max(0.0, t2[0]) < 20:
                continue
            dn = min(od_table, key=lambda k: abs(od_table[k] - perp))
            if abs(od_table[dn] - perp) <= od_table[dn] * tol:
                spacings[dn] += 1
    if not spacings:
        return None, None
    dn, votes = max(spacings.items(), key=lambda kv: kv[1])
    if votes < min_votes:
        return None, None
    return dn, od_table[dn]
