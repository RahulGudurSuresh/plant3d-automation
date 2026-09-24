"""
Draw OLD and NEW versions of a sheet TOGETHER, ringing everything that moved.

Exists because of hard rule 5: when the solver's moves are millimetres on an
841 mm sheet, a before/after pair of full-sheet PNGs is indistinguishable to
the eye and therefore is NOT evidence.  (Delivered exactly that twice, then a
third time with only the leader change diffed -- this tool is the correction,
kept as a tool so it never has to be re-improvised.)

What it shows, in one image:
  - the NEW file rendered native, exactly as AutoCAD would;
  - every entity that moved, ghosted in BLUE at its OLD position
    (old leader paths dashed, matching the LEADERS_DIFF convention);
  - a dashed ring round each cluster of change, labelled with the measured
    move in mm -- numbers from the DXF entity diff, not from pixels.

Full sheet, nothing cropped.  A zoom of the busiest cluster is written as a
separate `*_zoom.png`, never instead of the full sheet.

Run:
    python tools/visual_diff.py OLD.dxf NEW.dxf OUT.png [--site=jp1071]
"""

from __future__ import annotations

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

from dataclasses import dataclass, field
from math import hypot
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ezdxf
import matplotlib.pyplot as plt
from ezdxf import bbox as ezb
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
from ezdxf.addons.drawing.properties import LayoutProperties
from matplotlib.patches import Rectangle

import mess_map
import render as render_mod
from isotidy import DEFAULT_ROLES, JP1071_ROLES, Tuning, detect, extract

SITES = {"default": DEFAULT_ROLES, "jp1071": JP1071_ROLES}

#: The one hue ISOGEN leaves free on these sheets -- measured in render.py's
#: native-theme survey (dE 24.6 worst-case against the sheet's own colours).
#: Blue here never means "bad"; it means "markup: the old position".
BLUE = "#3987e5"
BLUE_RGB = (0x39, 0x87, 0xE5)

MOVE_EPS = 0.01     # mm; below this an entity is "did not move"
CLUSTER_GAP = 25.0  # mm; changes closer than this share one ring
RING_PAD = 8.0      # mm of air between a change and its ring


@dataclass
class Moved:
    handle: str
    kind: str            # MTEXT | INSERT | DIMENSION | MULTILEADER
    name: str            # human label for the caption/table
    old: tuple[float, float]
    new: tuple[float, float]
    dist: float
    old_path: list[tuple[float, float]] = field(default_factory=list)  # leaders


# --------------------------------------------------------------------------
# 1. What moved, measured from the files by handle.  The file is the truth.
# --------------------------------------------------------------------------

def _positions(doc) -> dict:
    pos = {}
    for e in doc.modelspace():
        t, h = e.dxftype(), e.dxf.handle
        if t == "MTEXT":
            words = e.plain_text().split()
            pos[h] = (t, (e.dxf.insert.x, e.dxf.insert.y),
                      " ".join(words[:2]) or "text", [])
        elif t == "INSERT" and e.dxf.name.startswith("Anno"):
            tag = next((a.dxf.text for a in e.attribs), "")
            pos[h] = (t, (e.dxf.insert.x, e.dxf.insert.y),
                      f"balloon {tag}".strip(), [])
        elif t == "DIMENSION":
            m = e.dxf.get("text_midpoint", None)
            if m is not None:
                # The printed value is the text OVERRIDE (real pipe length);
                # get_measurement() returns sheet-scale mm on an NTS iso and
                # names the dimension something no engineer will recognise.
                shown = e.dxf.get("text", "") or f"{e.get_measurement():.0f}"
                pos[h] = (t, (m.x, m.y), f"dim {shown}", [])
        elif t == "MULTILEADER":
            # Full vertex chain, elbow-aware -- the endpoint shortcut is the
            # exact audit bug logged in CONTEXT.md section 12.
            try:
                ld = e.context.leaders[0]
                lp = ld.last_leader_point
                for ln in ld.lines:
                    if ln.vertices:
                        pts = [(v.x, v.y) for v in ln.vertices]
                        pts.append((lp.x, lp.y))
                        pos[h] = (t, pts[-1], "leader", pts)
                        break
            except Exception:
                continue
    return pos


def diff(old_doc, new_doc) -> list[Moved]:
    old, new = _positions(old_doc), _positions(new_doc)
    out = []
    for h, (kind, oxy, name, opath) in old.items():
        if h not in new:
            continue
        _k, nxy, _n, npath = new[h]
        d = hypot(nxy[0] - oxy[0], nxy[1] - oxy[1])
        if kind == "MULTILEADER":
            # A leader can gain an elbow without its landing moving.
            if len(opath) == len(npath) and all(
                    hypot(a[0] - b[0], a[1] - b[1]) <= MOVE_EPS
                    for a, b in zip(opath, npath)):
                continue
        elif d <= MOVE_EPS:
            continue
        out.append(Moved(h, kind, name, oxy, nxy, d, opath))
    out.sort(key=lambda m: -m.dist)
    return out


# --------------------------------------------------------------------------
# 2. Ghost document: the OLD file stripped to only what moved, painted blue.
# --------------------------------------------------------------------------

def _paint_blue(entity) -> None:
    try:
        entity.rgb = BLUE_RGB
    except Exception:
        pass


def build_ghost(old_path: Path, moved: list[Moved]):
    doc = ezdxf.readfile(old_path)
    msp = doc.modelspace()
    keep = {m.handle for m in moved if m.kind in ("MTEXT", "INSERT")}
    dims = {m.handle for m in moved if m.kind == "DIMENSION"}

    # A dimension's ghost is its VALUE TEXT only.  The dim lines did not move
    # (the text slides along them), so ghosting the whole dimension would
    # paint blue over lines that are identical in both files -- a diff image
    # claiming a change that did not happen.
    for h in dims:
        dim = doc.entitydb.get(h)
        block_name = dim.dxf.get("geometry", None)
        if not block_name:
            continue
        try:
            block = doc.blocks.get(block_name)
        except ezdxf.DXFKeyError:
            continue
        for e in block:
            if e.dxftype() == "MTEXT":
                ghost = e.copy()
                msp.add_entity(ghost)
                keep.add(ghost.dxf.handle)
                break

    for e in list(msp):
        if e.dxf.handle not in keep:
            msp.delete_entity(e)

    render_mod._drop_wipeouts(doc)
    # Renderer traps (CONTEXT.md section 9): attribs render pre-translation,
    # MTEXT masks render as solid slabs.
    mess_map.flatten_attribs(msp)
    for e in msp:
        render_mod._drop_background_mask(e)
        _paint_blue(e)
    for block in doc.blocks:
        if block.name.lower().startswith(("*model_space", "*paper_space")):
            continue
        for e in block:
            render_mod._drop_background_mask(e)
            _paint_blue(e)
    return doc


# --------------------------------------------------------------------------
# 3. Rings: one per cluster of nearby changes, labelled from the file diff.
# --------------------------------------------------------------------------

def _clusters(moved: list[Moved]) -> list[list[Moved]]:
    def pts(m):
        return [m.old, m.new] + m.old_path

    def near(a, b):
        return any(hypot(p[0] - q[0], p[1] - q[1]) <= CLUSTER_GAP
                   for p in pts(a) for q in pts(b))

    groups: list[list[Moved]] = []
    for m in moved:
        joined = [g for g in groups if any(near(m, o) for o in g)]
        merged = [m]
        for g in joined:
            merged += g
            groups.remove(g)
        groups.append(merged)
    return groups


def _ring_box(cluster):
    xs, ys = [], []
    for m in cluster:
        for x, y in [m.old, m.new] + m.old_path:
            xs.append(x)
            ys.append(y)
    return (min(xs) - RING_PAD, min(ys) - RING_PAD,
            max(xs) + RING_PAD, max(ys) + RING_PAD)


def _text_extent(ax, text: str, fontsize: float) -> tuple[float, float]:
    """Approximate (width, height) of a monospace string in DRAWING units."""
    inv = ax.transData.inverted()
    x0, y0 = inv.transform((0.0, 0.0))
    x1, y1 = inv.transform((100.0, 100.0))
    per_px_x, per_px_y = abs(x1 - x0) / 100.0, abs(y1 - y0) / 100.0
    px = fontsize * ax.figure.dpi / 72.0
    return len(text) * px * 0.62 * per_px_x, px * 1.25 * per_px_y


def _place_ring_labels(ax, rings, texts, fontsize: float = 8.5) -> None:
    """Put each ring's caption somewhere it does not sit on another caption
    or on a ring.

    Shipping a diff whose own captions overlap each other, in a tool whose
    entire purpose is removing overlapping annotation, is the bug performed
    in the act of reporting it -- 545M05 has 13 change clusters and the naive
    above/below alternation stacked five captions into an unreadable heap.
    Same greedy-slot idea as the mess_map badges, which already print
    "none overlapping" for exactly this reason.
    """
    placed: list[tuple[float, float, float, float]] = []
    gap = 2.5

    def overlap(a, b) -> float:
        w = min(a[2], b[2]) - max(a[0], b[0])
        h = min(a[3], b[3]) - max(a[1], b[1])
        return w * h if w > 0 and h > 0 else 0.0

    for (x0, y0, x1, y1), text in zip(rings, texts):
        w, h = _text_extent(ax, text, fontsize)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        cands = []
        for k in (1, 2, 3, 4):
            d = gap * k
            cands += [
                ((cx - w / 2, y1 + d, cx + w / 2, y1 + d + h), cx, y1 + d,
                 "center", "bottom"),
                ((cx - w / 2, y0 - d - h, cx + w / 2, y0 - d), cx, y0 - d,
                 "center", "top"),
                ((x1 + d, cy - h / 2, x1 + d + w, cy + h / 2), x1 + d, cy,
                 "left", "center"),
                ((x0 - d - w, cy - h / 2, x0 - d, cy + h / 2), x0 - d, cy,
                 "right", "center"),
            ]
        best, best_cost = None, None
        for box, tx, ty, ha, va in cands:
            cost = sum(overlap(box, p) for p in placed) \
                + sum(overlap(box, r) for r in rings)
            if best_cost is None or cost < best_cost:
                best, best_cost = (box, tx, ty, ha, va), cost
            if cost == 0:
                break
        box, tx, ty, ha, va = best
        placed.append(box)
        ax.text(tx, ty, text, ha=ha, va=va, fontsize=fontsize,
                family="monospace", color=BLUE, zorder=201)


# --------------------------------------------------------------------------
# 4. Render: NEW native, ghost on top, rings on top of everything.
# --------------------------------------------------------------------------

def _draw_doc(ax, doc, surface: str) -> None:
    msp = doc.modelspace()
    backend = MatplotlibBackend(ax, adjust_figure=False)  # load-bearing
    props = LayoutProperties.from_layout(msp)
    props.set_colors(surface)  # ACI 7 = "contrast with paper" -> white
    Frontend(RenderContext(doc), backend).draw_layout(
        msp, finalize=True, layout_properties=props)


def visual_diff(old_path: Path, new_path: Path, out: Path,
                site: str = "jp1071") -> list[Moved]:
    th = render_mod.THEMES["native"]
    roles, cfg = SITES[site], Tuning()

    old_doc = ezdxf.readfile(old_path)
    new_doc = ezdxf.readfile(new_path)
    moved = diff(old_doc, new_doc)
    print(f"moved entities (> {MOVE_EPS} mm): {len(moved)}")
    for m in moved:
        print(f"  {m.kind:12s} {m.handle:>8s}  {m.dist:6.2f} mm  {m.name}")
    if not moved:
        print("nothing moved -- refusing to draw a diff that shows nothing")
        return moved

    # Tie the picture to the CANONICAL score: tidy.py scores with a plain
    # Tuning(), so this must too.  (First version used render.py's
    # for_sheet() scaling and captioned 105.96 mm2 on a sheet every report
    # calls 86.52 -- a diff image that disagrees with the CLI is worse than
    # no diff image.)
    scores = {}
    for tag, p in (("old", old_path), ("new", new_path)):
        _d, scene = extract(p, roles, cfg)
        scores[tag] = detect(scene, cfg).overlap_area
        print(f"  {tag} file overlap: {scores[tag]:.2f} mm2")

    ghost = build_ghost(old_path, moved)
    render_mod._prepare_native(new_doc)

    fig = plt.figure(figsize=(16.5, 11.7), facecolor=th.surface)  # A3
    ax = fig.add_axes([0, 0.072, 1, 0.855])
    ax.set_facecolor(th.surface)
    ax.set_axis_off()

    _draw_doc(ax, new_doc, th.surface)
    _draw_doc(ax, ghost, th.surface)

    # Old leader paths: dashed, matching LEADERS_DIFF's "dashed = old".
    for m in moved:
        if m.old_path:
            ax.plot(*zip(*m.old_path), color=BLUE, lw=1.4,
                    linestyle=(0, (5, 3)), zorder=180)
        elif m.dist >= 3.0:
            ax.annotate("", xy=m.new, xytext=m.old, zorder=190,
                        arrowprops=dict(arrowstyle="->", color=BLUE, lw=1.1))

    clusters = _clusters(moved)
    rings = [_ring_box(c) for c in clusters]
    texts = []
    for cluster in clusters:
        top = max(m.dist for m in cluster)
        texts.append(f"{cluster[0].name}  Δ{top:.1f}mm" if len(cluster) == 1
                     else f"{len(cluster)} moved  Δ≤{top:.1f}mm")
    for x0, y0, x1, y1 in rings:
        ax.add_patch(Rectangle(
            (x0, y0), x1 - x0, y1 - y0, fill=False, edgecolor=BLUE,
            linewidth=1.6, linestyle=(0, (6, 3)), zorder=200))

    # Limits BEFORE the labels: their size in drawing units depends on the
    # data-to-pixel scale, which is not settled until the view is.
    # Full sheet, nothing cropped -- hard rule 1.
    sheet = ezb.extents(new_doc.modelspace(), fast=True)
    pad_x = float(sheet.size.x) * 0.01
    pad_y = float(sheet.size.y) * 0.01
    ax.set_xlim(sheet.extmin.x - pad_x, sheet.extmax.x + pad_x)
    ax.set_ylim(sheet.extmin.y - pad_y, sheet.extmax.y + pad_y)
    ax.set_aspect("equal", adjustable="datalim")
    _place_ring_labels(ax, rings, texts)

    leaders = sum(1 for m in moved if m.kind == "MULTILEADER")
    fig.text(0.012, 0.992, "FINAL DIFF   full sheet - nothing cropped",
             va="top", ha="left", fontsize=13, family="monospace",
             color=th.ink, weight="bold")
    fig.text(0.012, 0.969,
             f"BLUE GHOST = old position (as delivered)   "
             f"sheet colours = final   dashed = old leader path\n"
             f"{len(moved)} entities moved "
             f"({len(moved) - leaders} annotations + {leaders} leader)   "
             f"largest Δ {moved[0].dist:.1f} mm   "
             f"overlap {scores['old']:.2f} -> {scores['new']:.2f} mm²",
             va="top", ha="left", fontsize=11, family="monospace",
             color=th.ink_dim, linespacing=1.6)

    fig.savefig(out, dpi=180, facecolor=th.surface)

    # Zoom is an EXTRA file on the busiest cluster, never a replacement.
    busiest = max(clusters, key=len)
    x0, y0, x1, y1 = _ring_box(busiest)
    m = 18.0
    ax.set_xlim(x0 - m, x1 + m)
    ax.set_ylim(y0 - m, y1 + m)
    zoom = out.with_name(out.stem + "_zoom.png")
    fig.savefig(zoom, dpi=180, facecolor=th.surface)
    plt.close(fig)
    print(f"wrote {out}\nwrote {zoom}")
    return moved


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    site = next((x.split("=", 1)[1] for x in sys.argv
                 if x.startswith("--site=")), "jp1071")
    visual_diff(Path(a[0]), Path(a[1]), Path(a[2]), site)
