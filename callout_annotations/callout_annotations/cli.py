"""
Command line entry point.

    python -m callout_annotations.cli "inputs/Plan view.dwg" inputs/PID-Binder.pdf --out out

Produces, in --out:
    <name>_CALLOUTS.dwg   the ortho with full-name callouts (and .dxf)
    report.csv            one row per line tag
    report.md             the summary a person reads first
    <view>.png            one picture per view, if --png (tools/render_views.py)

Accepts a .dxf directly (skips the ODA round trip) for tests and fixtures.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# JP1071 text carries non-cp1252 glyphs; never let the console kill a run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from . import match as matching
from . import ortho as ortho_mod
from . import pid as pid_mod
from . import place, report, writeback
from .config import DEFAULT_TUNING, OD_MM, SITES


def run(dwg: Path, pdf: Path, out_dir: Path, site_name: str = "jp1071",
        png: bool = False, keep_dxf: bool = True, label: str = "",
        mode: str = "inline") -> dict:
    site = SITES[site_name]
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_CALLOUTS" + (f"_{label}" if label else "")
    t0 = time.time()

    # 1. P&ID
    pid = pid_mod.read_pid(pdf, site)
    if not pid.names:
        raise SystemExit(f"no line names found in {pdf} -- scanned PDF?  "
                         f"pages without text: {pid.pages_without_text}")
    print(f"P&ID: {len(pid.sheets)} pages, {len(pid.names)} line names "
          f"({len(pid.duplicate_seqs)} sequences named more than one way)")

    # 2. Ortho
    if dwg.suffix.lower() == ".dwg":
        from .oda import convert_file
        dxf_in = out_dir / (dwg.stem + "_in.dxf")
        convert_file(dwg, dxf_in)
    else:
        dxf_in = dwg
    ortho = ortho_mod.load(dxf_in, site)
    print(f"ortho: {len(ortho.frames)} views, {len(ortho.runs)} (view, tag) runs, "
          f"{len(ortho.tags())} line tags   [{time.time() - t0:.0f}s]")
    for v, fr in sorted(ortho.frames.items()):
        print(f"   {v:16s} n={tuple(round(c, 2) for c in fr.n)} scale 1:{fr.scale:g}"
              f" text h={fr.text_height:g}")

    # 3. Match (size estimate from the plan view first, any view otherwise)
    est = {tag: ortho_mod.estimate_tag_size(ortho, tag, OD_MM) for tag in ortho.tags()}
    matches = matching.match_all(ortho.tags(), pid, est, site)

    # 4. Place + 5. Write
    if mode == "margin":
        from . import margin
        callouts, windows = margin.plan_margins(ortho, matches, DEFAULT_TUNING)
        n_vp = writeback.set_windows(ortho, windows)
        print(f"margin layout: {len(callouts)} callouts, {n_vp} viewport(s) enlarged")
    else:
        callouts = place.plan(ortho, matches, DEFAULT_TUNING)
    texts, leaders = writeback.apply(ortho, callouts, site)
    dxf_out = out_dir / (dwg.stem + suffix + ".dxf")
    ortho.doc.saveas(dxf_out)
    dwg_out = None
    if dwg.suffix.lower() == ".dwg":
        from .oda import convert_file
        dwg_out = convert_file(dxf_out, out_dir / (dwg.stem + suffix + ".dwg"))
        dxf_in.unlink(missing_ok=True)      # 57 MB of intermediate, nobody's deliverable
    print(f"placed {texts} callouts, {leaders} leaders   [{time.time() - t0:.0f}s]")

    # 6. Report
    rws = report.rows(ortho, matches, callouts)
    report.write_csv(out_dir / "report.csv", rws)
    report.write_markdown(out_dir / "report.md", ortho, pid, matches, callouts,
                          rws, str(dwg), str(pdf))
    if png:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from tools.render_views import render_all
        render_all(ortho, callouts, out_dir)
    print(f"-> {dwg_out or dxf_out}\n-> {out_dir / 'report.md'}")
    return {"ortho": ortho, "pid": pid, "matches": matches, "callouts": callouts,
            "dxf_out": dxf_out, "dwg_out": dwg_out}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dwg", type=Path)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument("--site", default="jp1071", choices=sorted(SITES))
    ap.add_argument("--png", action="store_true", help="render one PNG per view")
    ap.add_argument("--label", default="", help="version label appended to output names, e.g. 01")
    ap.add_argument("--mode", default="inline", choices=("inline", "margin"),
                    help="inline: beside the pipe where clean; margin: all labels "
                         "outside the drawing in an enlarged viewport")
    a = ap.parse_args(argv)
    run(a.dwg, a.pdf, a.out, a.site, a.png, label=a.label, mode=a.mode)


if __name__ == "__main__":
    main()
