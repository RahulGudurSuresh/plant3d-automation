"""
Dry placement under a different quality gate, no write-back.

    python tools/sweep_gate.py tests/_cache/PlanView.dxf inputs/PID-Binder.pdf 3.0 6.0

Prints, per view, lines present / named / callouts and the forced count,
so the gate can be tuned on numbers before anyone looks at a picture.
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from callout_annotations import match as matching
from callout_annotations import ortho as ortho_mod
from callout_annotations import pid as pid_mod
from callout_annotations import place
from callout_annotations.config import DEFAULT_TUNING, OD_MM


def main() -> None:
    dxf, pdf = Path(sys.argv[1]), Path(sys.argv[2])
    max_under, max_leader = float(sys.argv[3]), float(sys.argv[4])
    cfg = replace(DEFAULT_TUNING, max_under_h=max_under, max_leader_h=max_leader)
    pid = pid_mod.read_pid(pdf)
    o = ortho_mod.load(dxf)
    est = {t: ortho_mod.estimate_tag_size(o, t, OD_MM) for t in o.tags()}
    matches = matching.match_all(o.tags(), pid, est)
    callouts = place.plan(o, matches, cfg)
    per_view = Counter(c.view for c in callouts)
    print(f"gate under<={max_under}h leader<={max_leader}h : {len(callouts)} callouts, "
          f"{sum(c.forced for c in callouts)} forced")
    for v in sorted(o.frames):
        present = {t for (vv, t) in o.runs if vv == v}
        named = {c.tag for c in callouts if c.view == v}
        print(f"   {v:14s} present {len(present):3d}  named {len(named):3d}  callouts {per_view.get(v, 0):3d}")


if __name__ == "__main__":
    main()
