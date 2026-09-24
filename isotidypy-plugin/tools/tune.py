"""
Sweep a cost weight and print the trade-off it controls.

The solver optimises the cost function exactly.  So the cost function -- not
the search -- is what decides whether the output looks like a draftsman did it.
Tune by measurement, never by staring at one drawing.

Run:
    python tools/tune.py
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from isotidy import Tuning, detect, extract, solve  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "iso_congested.dxf"


def sweep(field: str, values) -> None:
    print(f"\n  {field:>12}  overlap mm2   moved   mean move   max move   leaders")
    print("  " + "-" * 68)
    for v in values:
        cfg = replace(Tuning(), **{field: v})
        _doc, scene = extract(FIXTURE, cfg=cfg)
        solve(scene, cfg)
        rep = detect(scene, cfg)
        moves = [l.displacement() for l in scene.labels if l.moved()]
        leaders = sum(1 for l in scene.labels
                      if l.displacement() >= cfg.leader_threshold)
        print(f"  {v:>12}  {rep.overlap_area:11.2f}   {len(moves):5d}   "
              f"{(sum(moves) / len(moves) if moves else 0):9.1f}   "
              f"{(max(moves) if moves else 0):8.1f}   {leaders:7d}")


if __name__ == "__main__":
    sweep("w_distance", [0.2, 0.8, 2.0, 5.0, 12.0, 30.0])
    sweep("w_stay", [0.0, 2.0, 10.0, 40.0])
