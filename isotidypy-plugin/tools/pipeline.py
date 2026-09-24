"""
One command, one drawing: DWG in -> tidied DWG out, with the evidence.

    python tools/pipeline.py "real inputs/<file>.dwg" work/545M05 --tag=545M05

What it does, in order:
  1. DWG -> DXF (ODA)
  2. tidy      -- category A: overlap, solved by moving labels, iterated
                  until the FILE stops improving
  3. reroute   -- category B: leader crossings (leader/label/component
                  linework), tail-only, tips frozen, 1-2 elbows
  4. DXF -> DWG, then the DWG is converted BACK and the round-trip file is
     re-audited: every number printed is measured on what ships, not on any
     in-memory claim
  5. the evidence set, all full-sheet:
       <tag>_BEFORE.png / <tag>_AFTER.png    scored renders
       <tag>_DIFF.png (+ _zoom)              old + new drawn together, ringed
       <tag>_MESSMAP_BEFORE/AFTER.png        A-E badges

What it never does:
  - move an arrow tip (hard rule)
  - touch category E symbol clutter -- counted and reported, never "fixed"
  - print "clean" on a partial read: zero mapped labels aborts loudly
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

import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from isotidy import DEFAULT_ROLES, JP1071_ROLES, Tuning

from tools.convert import convert
from tools.mess_map import audit, render_map
from tools.reroute import main as reroute_main
from tools.sheet_png import sheet_png
from tools.tidy import tidy
from tools.visual_diff import visual_diff

SITES = {"default": DEFAULT_ROLES, "jp1071": JP1071_ROLES}


def run(src: Path, out_dir: Path, site: str = "jp1071",
        tag: str | None = None, free_move: bool = False) -> dict:
    roles, cfg = SITES[site], Tuning()
    solve_cfg = cfg.free_move() if free_move else cfg
    # REBUILD THE OUTPUT FOLDER, never overwrite into it.  Windows Explorer
    # shows a folder's date as the last ADD/REMOVE inside it, not the last
    # overwrite -- so in-place runs left the folder advertising a stale time
    # while every file in it was new, and the user spent a day unable to
    # tell whether anything had actually been regenerated.
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    tag = tag or src.stem
    if free_move:
        print(f"[{tag}] FREE MOVE: travel {cfg.w_displacement}->"
              f"{solve_cfg.w_displacement}/mm, drift {cfg.w_distance}->"
              f"{solve_cfg.w_distance}/mm, search {solve_cfg.free_reach} mm."
              f"  Scored with the standard weights.")

    with tempfile.TemporaryDirectory(prefix="isotidy_") as td:
        tmp = Path(td)
        for d in ("in", "dxf", "out", "dwg", "rt"):
            (tmp / d).mkdir()

        # 1. to DXF (or accept a DXF directly)
        if src.suffix.lower() == ".dwg":
            shutil.copy(src, tmp / "in" / src.name)
            made = convert(tmp / "in", tmp / "dxf", "DXF")
            if not made:
                raise SystemExit(f"ODA produced no DXF for {src}")
            src_dxf = out_dir / f"{tag}_ORIGINAL.dxf"
            shutil.copy(made[0], src_dxf)
        else:
            src_dxf = out_dir / f"{tag}_ORIGINAL.dxf"
            shutil.copy(src, src_dxf)

        # 2. overlaps (A)
        print(f"[{tag}] tidy:")
        tidied = tmp / "out" / f"{tag}_tidy.dxf"
        tidy(src_dxf, tidied, site, solve_cfg=solve_cfg)

        # 3. leader defects (B)
        print(f"[{tag}] reroute:")
        final_dxf = tmp / "out" / f"{tag}_tidied.dxf"
        reroute_main(tidied, final_dxf, site)

        # 4. to DWG, round-trip, audit WHAT SHIPS
        shutil.copy(final_dxf, tmp / "dwg" / final_dxf.name)
        made = convert(tmp / "dwg", tmp / "dwg", "DWG")
        if not made:
            raise SystemExit("ODA produced no DWG for the result")
        out_dwg = out_dir / f"{tag}_TIDIED.dwg"
        shutil.copy(made[0], out_dwg)
        convert(tmp / "dwg", tmp / "rt", "DXF")
        rt_dxf = out_dir / f"{tag}_TIDIED.dxf"
        shutil.copy(next((tmp / "rt").glob("*.dxf")), rt_dxf)

        found, notes = audit(rt_dxf, roles, cfg)
        if notes.get("labels", 0) == 0:
            raise SystemExit(
                "REFUSING VERDICT: 0 labels mapped after round-trip -- layer "
                "mismatch or conversion fault, not a clean drawing")

    # 5. evidence, all full-sheet
    print(f"[{tag}] renders:")
    b = sheet_png(src_dxf, out_dir / f"{tag}_BEFORE.png", "BEFORE", site)
    a = sheet_png(rt_dxf, out_dir / f"{tag}_AFTER.png", "AFTER", site,
                  compare_tips_with=src_dxf)
    render_map(src_dxf, out_dir / f"{tag}_MESSMAP_BEFORE.png", site)
    render_map(rt_dxf, out_dir / f"{tag}_MESSMAP_AFTER.png", site)
    print(f"[{tag}] diff image:")
    visual_diff(src_dxf, rt_dxf, out_dir / f"{tag}_DIFF.png", site)

    print(f"\n[{tag}] VERDICT (measured on the round-tripped shipped file):")
    for k in "ABCD":
        print(f"  {k}: {b['counts'][k]:3d} -> {a['counts'][k]:3d}")
    print(f"  E: {a['counts']['E']:3d} flagged - NOT touched "
          f"(awaiting direction)")
    print(f"  overlap {b['overlap']:.2f} -> {a['overlap']:.2f} mm2   "
          f"{a['labels']} labels mapped")
    print(f"  wrote {out_dwg}")
    return {"before": b, "after": a, "dwg": out_dwg}


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if len(a) < 2:
        print(__doc__)
        raise SystemExit(1)
    site = next((x.split("=", 1)[1] for x in sys.argv
                 if x.startswith("--site=")), "jp1071")
    tag = next((x.split("=", 1)[1] for x in sys.argv
                if x.startswith("--tag=")), None)
    run(Path(a[0]), Path(a[1]), site, tag, "--free-move" in sys.argv)
