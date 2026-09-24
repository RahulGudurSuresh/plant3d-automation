"""
Solve a drawing REPEATEDLY until the file on disk stops improving.

Why a loop is needed at all: when a balloon moves, write-back re-lands its
leader, and that new leader path is geometry the solver never scored against
-- it only saw the old one.  So a single pass can hand back a file slightly
worse than it claimed, and the round-trip check says so honestly instead of
hiding it.

Re-reading the written file makes the new leaders real obstacles, so the next
pass sees the world as it now is.  The loop stops when the FILE score stops
improving, and the file score -- never the in-memory claim -- is the product.

Run:
    python tools/tidy.py in.dxf out.dxf [--site=jp1071] [--rounds=6]
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

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from isotidy import DEFAULT_ROLES, JP1071_ROLES, Tuning, detect
from isotidy.writeback import save_dxf  # noqa: E402
from isotidy.extract import extract
from isotidy.solve import solve
from isotidy.writeback import apply

SITES = {"default": DEFAULT_ROLES, "jp1071": JP1071_ROLES}


def score(path: Path, roles, cfg) -> tuple[int, float, int]:
    """(collisions, overlap mm2, leader crossings).

    LEADER CROSSINGS ARE IN THE OBJECTIVE because detect() does not score
    them -- it measures area, and a crossing has none.  Optimising on area
    alone let a round that traded two overlaps for a new crossing look like
    an improvement.  What is not in the objective does not get fixed.
    """
    _doc, scene = extract(path, roles, cfg)
    rep = detect(scene, cfg)
    return len(rep.collisions), rep.overlap_area, _crossings(path, roles, cfg)


def _crossings(path: Path, roles, cfg) -> int:
    import ezdxf
    from isotidy.extract import component_symbols
    from isotidy.leaders import symbol_hits
    from tools.mess_map import TIP_TOLERANCE, _leaders  # one definition
    doc = ezdxf.readfile(path)
    _d, scene = extract(path, roles, cfg)
    lds = _leaders(doc.modelspace())
    owner = {l.leader_handle: (l.group, l.index)
             for l in scene.labels if l.leader_handle}
    n = 0
    for i in range(len(lds)):
        for j in range(i + 1, len(lds)):
            if lds[i][2].crosses(lds[j][2]):
                n += 1
    for h, _tip, line in lds:
        grp, oidx = owner.get(h, (None, None))
        for lab in scene.labels:
            if lab.index == oidx or (grp is not None and lab.group == grp):
                continue
            if line.crosses(lab.box()):
                n += 1
    # Leader over a drawn component symbol counts too -- what is not in the
    # objective does not get fixed, and this one was invisible until the user
    # saw a leader cut across the valve it pointed at (B said 0 throughout).
    symbols = component_symbols(doc)
    for h, tip, line in lds:
        n += len(symbol_hits(line, tip, line.coords[-1], symbols,
                             TIP_TOLERANCE))
    return n


def tidy(src: Path, dst: Path, site: str = "jp1071", rounds: int = 6,
         solve_cfg: Tuning | None = None) -> None:
    """`solve_cfg` may relax the weights the SOLVER optimises (see
    Tuning.free_move).  Scoring always uses the standard Tuning: whoever is
    being marked, the examiner does not change, or a run could 'improve' by
    lowering its own bar."""
    roles = SITES[site]
    cfg = Tuning()
    solve_cfg = solve_cfg or cfg
    n0, a0, x0 = score(src, roles, cfg)
    print(f"  start                {n0:3d} collisions  {a0:8.2f} mm2  "
          f"{x0:2d} leader crossings")

    work = dst.with_suffix(".round.dxf")
    shutil.copy(src, work)
    best = (a0, x0, n0)
    shutil.copy(src, dst)

    for r in range(1, rounds + 1):
        doc, scene = extract(work, roles, solve_cfg)
        stats = solve(scene, solve_cfg)
        if stats.moved == 0:
            print(f"  round {r}: nothing left to move - converged")
            break
        apply(doc, scene, roles, solve_cfg)
        save_dxf(doc, work)
        n, a, x = score(work, roles, cfg)
        moved = stats.moved
        # Overlap area first, then crossings, then collision count.
        cand = (round(a, 6), x, n)
        better = cand < best
        flag = "kept" if better else "rejected (no better)"
        print(f"  round {r}: moved {moved:3d}  ->  {n:3d} collisions  "
              f"{a:8.2f} mm2  {x:2d} crossings   {flag}")
        if better:
            best = cand
            shutil.copy(work, dst)
        else:
            # A round that did not improve the FILE is not kept, and there is
            # no point continuing from it -- restart the next round from the
            # best file we actually have.
            shutil.copy(dst, work)
            break

    work.unlink(missing_ok=True)
    ba, bx, bn = best
    print(f"\n  BEST FILE            {bn:3d} collisions  {ba:8.2f} mm2  "
          f"{bx:2d} leader crossings"
          f"   ({100 * (a0 - ba) / a0 if a0 else 0:.1f}% overlap reduction)")
    print(f"  wrote {dst}")


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    site = next((a.split("=", 1)[1] for a in sys.argv
                 if a.startswith("--site=")), "jp1071")
    rounds = int(next((a.split("=", 1)[1] for a in sys.argv
                       if a.startswith("--rounds=")), "6"))
    tidy(Path(argv[0]), Path(argv[1]), site, rounds)
