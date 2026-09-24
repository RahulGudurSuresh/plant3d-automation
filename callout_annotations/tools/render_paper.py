"""
Render ONE view the way its viewport shows it -- from the DXF entities
themselves, not from callout_annotations' model of them.

tools/render_views.py draws the placer's rectangles; it can only ever
agree with the placer.  This draws the real MTEXT and LEADER entities with
ezdxf's drawing add-on (real font metrics, real attachment points, real
text direction), so it shows what AutoCAD shows -- including any mistake in
how the text was written.  It is the check the user asked for.

How: keep only the view's entities (its INSERTs and its callout layers),
rotate the whole view plane onto XY with the frame's (u, v, n), and draw
model space clipped to the viewport window at the viewport's scale.

    python tools/render_paper.py "out/Plan view_CALLOUTS_TIDY.dxf" "Back View" out/paper_back.png
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ezdxf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from ezdxf.addons.drawing import Frontend, RenderContext, config
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
from ezdxf.math import Matrix44

from callout_annotations import ortho as ortho_mod
from callout_annotations.config import JP1071


def render_paper(dxf: Path, view: str, out: Path, dpi: int = 400,
                 site=JP1071) -> Path:
    t0 = time.time()
    o = ortho_mod.load(dxf, site)
    fr = o.frames[view]
    doc = o.doc
    msp = doc.modelspace()

    keep_prefix = view + "-"
    for e in list(msp):
        if not e.dxf.layer.startswith(keep_prefix):
            msp.delete_entity(e)
    # Rotate the view plane onto XY: p' = (p.u, p.v, p.n).
    u, v, n = fr.u, fr.v, fr.n
    m = Matrix44((u.x, v.x, n.x, 0.0,
                  u.y, v.y, n.y, 0.0,
                  u.z, v.z, n.z, 0.0,
                  0.0, 0.0, 0.0, 1.0))
    for e in list(msp):
        try:
            e.transform(m)
        except Exception as ex:
            print("  transform failed:", e.dxftype(), ex)
    # Thaw everything: layer state is the viewport's business, we already
    # selected the view's entities by name.
    for layer in doc.layers:
        layer.thaw()
        layer.on()

    x0, y0, x1, y1 = fr.window
    w_in = (x1 - x0) / fr.scale / 25.4
    h_in = (y1 - y0) / fr.scale / 25.4
    fig = plt.figure(figsize=(w_in, h_in), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1])
    ctx = RenderContext(doc)
    cfg = config.Configuration(background_policy=config.BackgroundPolicy.WHITE,
                               color_policy=config.ColorPolicy.BLACK,
                               min_lineweight=0.05)
    Frontend(ctx, MatplotlibBackend(ax), config=cfg).draw_layout(msp, finalize=False)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=dpi, facecolor="white")
    plt.close(fig)
    print(f"{view}: {out}  ({(x1 - x0) / fr.scale:.0f} x {(y1 - y0) / fr.scale:.0f} mm "
          f"on paper, 1:{fr.scale:g})  [{time.time() - t0:.0f}s]")
    return out


if __name__ == "__main__":
    dxf, view, out = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    render_paper(dxf, view, out)
