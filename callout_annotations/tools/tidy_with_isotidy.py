"""
Trial: let isotidy's solver de-conflict the callouts, one view at a time.

isotidy is a 2D isometric-sheet tool: it reads insert.x/.y, knows nothing
about the three planes an ortho's views sit in, and sizes MTEXT boxes
axis-aligned.  So we do not hand it the ortho.  For each view we build a
throw-away sheet in PAPER millimetres:

    Pipe          every curve of the view's linework      (fixed)
    Annotation    one MTEXT per callout, 2.5 mm high      (movable)
                  with the true anchor in isotidy's own XDATA slot, so it
                  does not have to guess what the label points at.
                  A VERTICAL callout becomes a multi-line proxy of the same
                  footprint (isotidy cannot rotate a box); it is moved as a
                  rectangle and re-drawn rotated afterwards.

isotidy extracts, scores, solves, scores again.  Each label's (dx, dy) on
paper is scaled back into the view plane and the callout re-written by
writeback.apply(), which also re-derives whether it still needs a leader.

    python tools/tidy_with_isotidy.py "inputs/Plan view.dwg" inputs/PID-Binder.pdf --out out

Writes <name>_CALLOUTS_TIDY.dwg/.dxf and tidy/<view>.png next to the
untidied output.  The untidied result is kept: the two are meant to be
compared.
"""

from __future__ import annotations

import argparse
import math
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))          # isotidy lives one level up

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import ezdxf
from shapely.ops import unary_union

from isotidy.config import LayerRoles, Tuning as IsoTuning
from isotidy.detect import detect
from isotidy.extract import extract
from isotidy.solve import solve

from callout_annotations import cli, writeback
from callout_annotations.config import DEFAULT_TUNING
from callout_annotations.ortho import Ortho
from callout_annotations.place import Callout
from tools.render_views import render_all

PAPER_H = 2.5
#: "Frame" carries one closed polyline: the viewport window.  isotidy
#: treats closed frame polygons as the sheet border and keeps every label
#: inside it (less frame_margin).  Without it the first trial "solved" the
#: crowding by parking 40 % of the Back View's labels outside the window
#: -- empty space, zero cost, and invisible through the viewport.
ROLES = LayerRoles(movable={"Annotation": "annotation"},
                   fixed=frozenset({"Pipe"}), frame=frozenset({"Frame"}),
                   constrained=frozenset(), leader="ISOTIDY_LEADER")


def _sheet(ortho: Ortho, view: str, callouts: list[Callout], path: Path) -> dict[str, Callout]:
    """Write the view as a flat paper-mm DXF; return temp handle -> Callout."""
    fr = ortho.frames[view]
    k = 1.0 / fr.scale
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    for name in ("Pipe", "Annotation", "Frame", ROLES.leader):
        doc.layers.add(name)
    doc.appids.add("ISOTIDY")
    if fr.window is not None:
        x0, y0, x1, y1 = (v * k for v in fr.window)
        msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True,
                           dxfattribs={"layer": "Frame"})
    for ln in ortho.linework.get(view, []):
        pts = [(x * k, y * k) for x, y in ln.coords]
        if len(pts) >= 2:
            msp.add_lwpolyline(pts, dxfattribs={"layer": "Pipe"})
    mapping: dict[str, Callout] = {}
    for c in callouts:
        if c.view != view:
            continue
        bx0, by0, bx1, by1 = c.box().bounds
        if abs(math.sin(c.angle)) > 0.5:
            # Vertical: a column of one-character lines whose stacked height
            # (h + (n-1) * 1.667 h, AutoCAD single spacing) matches the
            # rotated label's length.  Width comes out ~one glyph, as it
            # should.
            n = max(2, round(((by1 - by0) * k / PAPER_H - 1.0) / 1.667 + 1.0))
            text = "\\P".join(["M"] * n)
        else:
            text = c.text
        mt = msp.add_mtext(text, dxfattribs={
            "layer": "Annotation", "char_height": PAPER_H,
            "attachment_point": 7, "insert": (bx0 * k, by0 * k), "width": 0.0})
        mt.set_xdata("ISOTIDY", [(1000, "ANCHOR"),
                                 (1010, (c.anchor[0] * k, c.anchor[1] * k, 0.0))])
        mapping[mt.dxf.handle] = c
    doc.saveas(path)
    return mapping


def tidy_view(ortho: Ortho, view: str, callouts: list[Callout],
              cfg: IsoTuning, tmpdir: Path) -> dict:
    fr = ortho.frames[view]
    sheet = tmpdir / f"{view.replace(' ', '_')}.dxf"
    mapping = _sheet(ortho, view, callouts, sheet)
    if not mapping:
        return {"view": view, "labels": 0}
    _, scene = extract(sheet, ROLES, cfg)
    before = detect(scene, cfg)
    stats = solve(scene, cfg)
    after = detect(scene, cfg)

    # Pull the moves back into the view plane.
    pipes = {}
    moved = 0
    h = fr.text_height
    for lab in scene.labels:
        c = mapping.get(lab.handle)
        if c is None or not lab.moved():
            continue
        dx, dy = lab.delta()
        c.pos = (c.pos[0] + dx * fr.scale, c.pos[1] + dy * fr.scale)
        moved += 1
        run = ortho.runs.get((view, c.tag))
        if run is not None:
            if c.tag not in pipes:
                pipes[c.tag] = unary_union(run.lines)
            c.leader = c.box(DEFAULT_TUNING.pad_h * h).distance(pipes[c.tag]) \
                > DEFAULT_TUNING.leader_threshold_h * h
    return {"view": view, "labels": len(scene.labels), "moved": moved,
            "before": before, "after": after, "stats": stats}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dwg", type=Path)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--out", type=Path, default=Path("out"))
    a = ap.parse_args(argv)
    t0 = time.time()

    print("== placing (callout_annotations) ==")
    res = cli.run(a.dwg, a.pdf, a.out, png=False)
    ortho, callouts = res["ortho"], res["callouts"]

    print("\n== tidying (isotidy, per view, on paper mm) ==")
    cfg = IsoTuning()
    rows = []
    with tempfile.TemporaryDirectory() as td:
        for view in sorted(ortho.frames):
            r = tidy_view(ortho, view, callouts, cfg, Path(td))
            rows.append(r)
            if r["labels"]:
                b, af = r["before"], r["after"]
                print(f"  {view:14s} {r['labels']:3d} labels  moved {r['moved']:3d}   "
                      f"overlap {b.overlap_area:8.1f} -> {af.overlap_area:8.1f} mm2 "
                      f"(collisions {len(b.collisions)} -> {len(af.collisions)})   "
                      f"[{time.time() - t0:.0f}s]")

    print("\n== writing ==")
    texts, leaders = writeback.apply(ortho, callouts)
    dxf_out = a.out / (a.dwg.stem + "_CALLOUTS_TIDY.dxf")
    ortho.doc.saveas(dxf_out)
    dwg_out = None
    if a.dwg.suffix.lower() == ".dwg":
        from callout_annotations.oda import convert_file
        dwg_out = convert_file(dxf_out, a.out / (a.dwg.stem + "_CALLOUTS_TIDY.dwg"))
    render_all(ortho, callouts, a.out / "tidy")
    print(f"{texts} callouts, {leaders} leaders   [{time.time() - t0:.0f}s]")
    print(f"-> {dwg_out or dxf_out}\n-> {a.out / 'tidy'}/<view>.png")


if __name__ == "__main__":
    main()
