"""
Generate a synthetic piping-isometric DXF that reproduces the annotation
overlap problem, with ground truth embedded.

Why synthetic?
    Real ISOGEN output is slow to regenerate, unreviewable in diffs, and gives
    you no way to score a solver ("is that text in the RIGHT place now, or just
    a DIFFERENT wrong place?").  Here we know, for every label, which component
    it belongs to -- stored as XDATA -- so the solver can be measured, not
    eyeballed.

Layer names mimic a typical ISOGEN style.  Real projects customise these in the
style's "Default Model.dwg", so nothing downstream may hardcode them; they are
declared once, here and in the extractor config, and nowhere else.

Run:
    .venv/Scripts/python.exe tools/make_fixture.py
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import ezdxf
from ezdxf.enums import TextEntityAlignment

# --------------------------------------------------------------------------
# Layer scheme (see module docstring -- configurable, not law)
# --------------------------------------------------------------------------
LYR_PIPE = "ISO_PIPE"          # centreline / geometry -- FIXED obstacle
LYR_SYMBOL = "ISO_SYMBOL"      # valve, flange, reducer symbols -- FIXED
LYR_ANNO = "ISO_ANNOTATION"    # component tags, line numbers -- MOVABLE
LYR_WELD = "ISO_WELD"          # weld numbers -- MOVABLE
LYR_DIM = "ISO_DIMENSION"      # dimension text -- MOVABLE (constrained)
LYR_FRAME = "ISO_FRAME"        # border, title block, BOM -- FIXED, hard bound
LYR_LEADER = "ISO_LEADER"      # leaders we generate -- created by the solver

APPID = "ISOTIDY"              # XDATA namespace for ground truth

TEXT_HEIGHT = 2.5              # mm on paper -- typical iso annotation height
DIM_OFFSET = 9.0               # mm the dimension line sits clear of geometry

# Sheet: A3 landscape, drawing area excludes the BOM/title block on the right.
SHEET_W, SHEET_H = 420.0, 297.0
MARGIN = 10.0
BOM_X0 = 285.0                 # everything right of this belongs to the BOM
DRAW_AREA = (MARGIN + 5, MARGIN + 5, BOM_X0 - 5, SHEET_H - MARGIN - 5)


# --------------------------------------------------------------------------
# Isometric projection
# --------------------------------------------------------------------------
COS30 = math.cos(math.radians(30.0))
SIN30 = math.sin(math.radians(30.0))


def project(east: float, north: float, up: float) -> tuple[float, float]:
    """Map a 3D model point to 2D paper space using the standard piping
    isometric projection (axes at 30 / 150 / 270 degrees)."""
    return (east - north) * COS30, (east + north) * SIN30 + up


# --------------------------------------------------------------------------
# The model we are going to draw
# --------------------------------------------------------------------------
@dataclass
class Component:
    """A piping component sitting at a point on the route."""
    pos3d: tuple[float, float, float]
    kind: str                       # GATE_VALVE, FLANGE, REDUCER, TEE, ...
    labels: list[str] = field(default_factory=list)


# A route with a deliberately congested valve station.  Coordinates in mm,
# model scale -- they get projected and fitted to the sheet afterwards.
ROUTE = [
    (0, 0, 0),
    (3000, 0, 0),
    (3000, 0, 1500),
    (3000, 2500, 1500),
    (5000, 2500, 1500),
    (5000, 2500, 500),
]

COMPONENTS = [
    # --- the congested station: five items inside 900mm of pipe ------------
    Component((1200, 0, 0), "FLANGE", ['6"-WN-150#-RF']),
    Component((1500, 0, 0), "GATE_VALVE", ["GV-1001", '6"-150#-CS']),
    Component((1800, 0, 0), "FLANGE", ['6"-WN-150#-RF']),
    Component((2100, 0, 0), "REDUCER", ['6"x4"-CONC-RED']),
    Component((2400, 0, 0), "CHECK_VALVE", ["CV-1002", '4"-150#-CS']),
    # --- sparser items elsewhere ------------------------------------------
    Component((3000, 1200, 1500), "TEE", ["T-2001", '4"-EQ-TEE']),
    Component((4200, 2500, 1500), "GATE_VALVE", ["GV-1003", '4"-150#-CS']),
    Component((5000, 2500, 900), "FLANGE", ['4"-WN-150#-RF']),
]

# Welds sit at every fitting and at each route vertex.
WELDS = [
    ((1200, 0, 0), "W01"), ((1500, 0, 0), "W02"), ((1800, 0, 0), "W03"),
    ((2100, 0, 0), "W04"), ((2400, 0, 0), "W05"), ((3000, 0, 0), "W06"),
    ((3000, 0, 1500), "W07"), ((3000, 1200, 1500), "W08"),
    ((3000, 2500, 1500), "W09"), ((4200, 2500, 1500), "W10"),
    ((5000, 2500, 1500), "W11"), ((5000, 2500, 900), "W12"),
]

# Dimension strings: (from3d, to3d, text)
DIMS = [
    ((0, 0, 0), (1500, 0, 0), "1500"),
    ((1500, 0, 0), (2400, 0, 0), "900"),
    ((2400, 0, 0), (3000, 0, 0), "600"),
    ((3000, 0, 0), (3000, 0, 1500), "1500"),
    ((3000, 0, 1500), (3000, 2500, 1500), "2500"),
    ((3000, 2500, 1500), (5000, 2500, 1500), "2000"),
]


# --------------------------------------------------------------------------
# Fit the projected geometry into the drawing area
# --------------------------------------------------------------------------
def build_fitter(points3d):
    """Return a function mapping model 3D -> fitted paper 2D."""
    pts = [project(*p) for p in points3d]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, y0, x1, y1 = DRAW_AREA
    # Leave room around the geometry for annotations to live in.
    pad = 40.0
    sx = (x1 - x0 - 2 * pad) / (max(xs) - min(xs))
    sy = (y1 - y0 - 2 * pad) / (max(ys) - min(ys))
    s = min(sx, sy)
    ox = x0 + pad - min(xs) * s
    oy = y0 + pad - min(ys) * s

    def fit(p3d):
        px, py = project(*p3d)
        return px * s + ox, py * s + oy

    return fit


# --------------------------------------------------------------------------
# Naive annotation placement -- this is the bug we are reproducing
# --------------------------------------------------------------------------
def naive_offset(index: int, stack: int) -> tuple[float, float]:
    """Mimic ISOGEN's local placement rule: push the label a fixed distance
    off the component, alternating side, stacking multi-line labels downward.

    It has no global collision awareness -- which is exactly the point.  Two
    components 300mm apart in the model land ~4mm apart on paper, and their
    labels sit straight on top of each other.
    """
    side = 1 if index % 2 == 0 else -1
    dx = 4.0 * side
    dy = 5.0 * side - stack * (TEXT_HEIGHT * 1.35)
    return dx, dy


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------
def add_layers(doc):
    for name, color in [
        (LYR_PIPE, 7), (LYR_SYMBOL, 3), (LYR_ANNO, 2),
        (LYR_WELD, 6), (LYR_DIM, 4), (LYR_FRAME, 8), (LYR_LEADER, 1),
    ]:
        doc.layers.add(name=name, color=color)


def add_frame(msp):
    """Sheet border, title block and BOM region -- hard obstacles the solver
    must never place a label inside."""
    msp.add_lwpolyline(
        [(MARGIN, MARGIN), (SHEET_W - MARGIN, MARGIN),
         (SHEET_W - MARGIN, SHEET_H - MARGIN), (MARGIN, SHEET_H - MARGIN)],
        close=True, dxfattribs={"layer": LYR_FRAME},
    )
    # BOM / material list block down the right-hand side.
    msp.add_lwpolyline(
        [(BOM_X0, MARGIN), (SHEET_W - MARGIN, MARGIN),
         (SHEET_W - MARGIN, SHEET_H - MARGIN), (BOM_X0, SHEET_H - MARGIN)],
        close=True, dxfattribs={"layer": LYR_FRAME},
    )
    msp.add_text(
        "MATERIAL LIST", height=3.5, dxfattribs={"layer": LYR_FRAME}
    ).set_placement((BOM_X0 + 5, SHEET_H - MARGIN - 8), align=TextEntityAlignment.LEFT)


def add_symbol(msp, xy, kind):
    """A crude but distinguishable component symbol."""
    x, y = xy
    a = {"layer": LYR_SYMBOL}
    if kind in ("GATE_VALVE", "CHECK_VALVE"):
        r = 2.2
        msp.add_lwpolyline(
            [(x - r, y - r), (x + r, y + r), (x + r, y - r), (x - r, y + r)],
            close=True, dxfattribs=a,
        )
    elif kind == "FLANGE":
        msp.add_line((x, y - 2.5), (x, y + 2.5), dxfattribs=a)
        msp.add_line((x + 1.2, y - 2.5), (x + 1.2, y + 2.5), dxfattribs=a)
    elif kind == "REDUCER":
        msp.add_lwpolyline(
            [(x - 2.5, y - 2.2), (x + 2.5, y - 1.2),
             (x + 2.5, y + 1.2), (x - 2.5, y + 2.2)],
            close=True, dxfattribs=a,
        )
    elif kind == "TEE":
        msp.add_line((x - 2.5, y), (x + 2.5, y), dxfattribs=a)
        msp.add_line((x, y), (x, y + 3.0), dxfattribs=a)


def tag_anchor(entity, anchor_xy, owner: str):
    """Stamp ground truth onto the label: which point it describes, and the
    id of the component that owns it.  A solver may move the text anywhere it
    likes, but this tells us what it was ever supposed to point at."""
    entity.set_xdata(
        APPID,
        [
            (1000, "ANCHOR"),
            (1010, (anchor_xy[0], anchor_xy[1], 0.0)),
            (1000, "OWNER"),
            (1000, owner),
        ],
    )


def build(path: Path) -> None:
    doc = ezdxf.new("R2010", setup=True)
    doc.appids.add(APPID)
    add_layers(doc)
    msp = doc.modelspace()

    all_pts = ROUTE + [c.pos3d for c in COMPONENTS] + [w[0] for w in WELDS]
    fit = build_fitter(all_pts)

    add_frame(msp)

    # --- pipe centreline -------------------------------------------------
    msp.add_lwpolyline([fit(p) for p in ROUTE], dxfattribs={"layer": LYR_PIPE})

    # --- components + their labels ---------------------------------------
    for i, comp in enumerate(COMPONENTS):
        xy = fit(comp.pos3d)
        add_symbol(msp, xy, comp.kind)
        for stack, text in enumerate(comp.labels):
            dx, dy = naive_offset(i, stack)
            t = msp.add_text(
                text, height=TEXT_HEIGHT, dxfattribs={"layer": LYR_ANNO}
            )
            t.set_placement((xy[0] + dx, xy[1] + dy),
                            align=TextEntityAlignment.MIDDLE_LEFT)
            tag_anchor(t, xy, f"{comp.kind}@{i}")

    # --- weld numbers ----------------------------------------------------
    for i, (p3d, wid) in enumerate(WELDS):
        xy = fit(p3d)
        msp.add_circle(xy, 0.8, dxfattribs={"layer": LYR_WELD})
        t = msp.add_text(
            wid, height=TEXT_HEIGHT * 0.8, dxfattribs={"layer": LYR_WELD}
        )
        t.set_placement((xy[0] - 3.0, xy[1] - 4.0),
                        align=TextEntityAlignment.MIDDLE_LEFT)
        tag_anchor(t, xy, f"WELD@{wid}")

    # --- dimension lines + text ------------------------------------------
    # ISOGEN offsets the dimension line clear of the geometry and drops
    # witness lines back to the measured points.  Getting this right matters:
    # dimension text is movable but CONSTRAINED -- it may only slide along
    # its own dimension line, so that line has to be a real, separate object.
    for (a3, b3, text) in DIMS:
        ax, ay = fit(a3)
        bx, by = fit(b3)
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy) or 1.0
        # unit normal -- offset side alternates naturally with run direction
        nx, ny = -dy / length * DIM_OFFSET, dx / length * DIM_OFFSET
        p0 = (ax + nx, ay + ny)
        p1 = (bx + nx, by + ny)
        a = {"layer": LYR_DIM}
        msp.add_line(p0, p1, dxfattribs=a)          # dimension line
        msp.add_line((ax, ay), p0, dxfattribs=a)    # witness lines
        msp.add_line((bx, by), p1, dxfattribs=a)
        mx, my = (p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0
        t = msp.add_text(
            text, height=TEXT_HEIGHT, dxfattribs=a
        )
        t.set_placement((mx, my + 1.5), align=TextEntityAlignment.MIDDLE_CENTER)
        tag_anchor(t, (mx, my), "DIM")

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    print(f"wrote {path}")
    print(f"  components : {len(COMPONENTS)}")
    print(f"  welds      : {len(WELDS)}")
    print(f"  dimensions : {len(DIMS)}")
    n_labels = sum(len(c.labels) for c in COMPONENTS) + len(WELDS) + len(DIMS)
    print(f"  labels     : {n_labels}")


if __name__ == "__main__":
    build(Path(__file__).resolve().parents[1] / "fixtures" / "iso_congested.dxf")
