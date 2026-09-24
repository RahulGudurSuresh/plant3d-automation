"""
One PNG per ortho view, drawn from the SAME 2D model place.py used.

Diagnostic, not a deliverable.  Linework grey, the annotated pipe runs
blue, callout boxes outlined (black = matched, red = needs review), leaders
dashed.  The box is the exact rectangle the placer scored, so if it sits on
linework here, it sits on linework in AutoCAD.

    python tools/render_views.py out/Plan view_CALLOUTS.dxf out/
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection

from callout_annotations.ortho import Ortho
from callout_annotations.place import Callout


def render_view(ortho: Ortho, view: str, callouts: list[Callout], out: Path,
                dpi: int = 150) -> Path:
    fr = ortho.frames[view]
    lines = ortho.linework.get(view, [])
    x0, y0, x1, y1 = fr.window if fr.window else (0, 0, 1, 1)
    w_mm, h_mm = (x1 - x0) / fr.scale, (y1 - y0) / fr.scale
    fig_w = max(6.0, min(40.0, w_mm / 25.4 * 1.0))
    fig_h = max(4.0, fig_w * (h_mm / w_mm))
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=dpi)
    ax.add_collection(LineCollection([list(l.coords) for l in lines],
                                     colors="#9a9a9a", linewidths=0.35))
    tagged = [l for (v, t), r in ortho.runs.items() if v == view for l in r.lines]
    ax.add_collection(LineCollection([list(l.coords) for l in tagged],
                                     colors="#3b6fd6", linewidths=0.5))
    for c in callouts:
        if c.view != view:
            continue
        col = "#c0392b" if c.review else "#111111"
        xs, ys = c.box().exterior.xy
        ax.plot(xs, ys, color=col, linewidth=0.6)
        ax.text(c.pos[0], c.pos[1], c.text, fontsize=max(3.0, c.height / fr.scale * 2.2),
                rotation=math.degrees(c.angle), rotation_mode="anchor",
                ha="left", va="bottom", color=col, family="DejaVu Sans Mono")
        lp = c.leader_points()
        if lp:
            (sx, sy), (ex, ey) = lp
            ax.plot([sx, ex], [sy, ey], color=col, linewidth=0.5, linestyle="--")
            ax.plot([ex], [ey], marker="o", markersize=1.5, color=col)
    ax.plot([x0, x1, x1, x0, x0], [y0, y0, y1, y1, y0], color="#cccccc", linewidth=0.4)
    # Clip exactly like the viewport does: a label outside the window is
    # cut off here because it is cut off in AutoCAD.  'box' keeps the
    # limits fixed; the default 'datalim' silently widened them to fit
    # stray labels and hid the problem.
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")
    ax.set_title(f"{view}   1:{fr.scale:g}", fontsize=8)
    fig.tight_layout(pad=0.2)
    path = out / (view.replace(" ", "_") + ".png")
    fig.savefig(path, dpi=dpi)
    plt.close(fig)
    return path


def render_all(ortho: Ortho, callouts: list[Callout], out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    return [render_view(ortho, v, callouts, out) for v in sorted(ortho.frames)]
