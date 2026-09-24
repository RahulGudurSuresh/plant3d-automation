"""
Render one DXF as a FULL-SHEET PNG whose caption is measured, not typed.

Every number in the caption is computed from the file being drawn at the
moment it is drawn.  That is the whole point: a before/after pair carrying
hand-copied numbers is a claim, and this project has been burned by claims
that outlived the file they described.

FULL SHEET, NOTHING CROPPED -- hard rule 1.  There is deliberately no window
or zoom parameter here; a zoom is a separate `*_zoom.png`, never a
replacement.

Run:
    python tools/sheet_png.py in.dxf out.png BEFORE [--site=jp1071]
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

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ezdxf
import matplotlib.pyplot as plt
from ezdxf import bbox as ezb

from isotidy import DEFAULT_ROLES, JP1071_ROLES, Tuning

from tools import render as render_mod
from tools.mess_map import audit, own_line_contact
from tools.visual_diff import _draw_doc

SITES = {"default": DEFAULT_ROLES, "jp1071": JP1071_ROLES}


def _tips(path: Path) -> dict:
    """handle -> arrow tip.  Keyed by HANDLE, not by position: three pairs of
    leaders on 545M05 share a tip point, so a set of positions counted 16
    where there are 19 leaders, and the caption under-reported what had been
    verified."""
    out = {}
    for ml in ezdxf.readfile(path).modelspace().query("MULTILEADER"):
        try:
            v = ml.context.leaders[0].lines[0].vertices[0]
            out[ml.dxf.handle] = (round(v.x, 3), round(v.y, 3))
        except Exception:
            pass
    return out


def sheet_png(dxf: Path, out: Path, title: str, site: str = "jp1071",
              compare_tips_with: Path | None = None) -> dict:
    roles, cfg = SITES[site], Tuning()
    found, notes = audit(dxf, roles, cfg)
    counts = {k: sum(1 for f in found if f[0] == k) for k in "ABCDE"}
    _n_own, own_mm2 = own_line_contact(dxf, roles, cfg)

    # Overlap area, scored like tidy.py -- plain Tuning(), never for_sheet().
    from isotidy import detect
    from isotidy.extract import extract
    _d, scene = extract(dxf, roles, cfg)
    overlap = detect(scene, cfg).overlap_area

    tip_line = ""
    if compare_tips_with is not None:
        orig, now = _tips(compare_tips_with), _tips(dxf)
        same = sum(1 for h, t in orig.items() if now.get(h) == t)
        tip_line = f"   arrowheads: {same}/{len(orig)} untouched"

    th = render_mod.THEMES["native"]
    doc = ezdxf.readfile(dxf)
    render_mod._prepare_native(doc)

    fig = plt.figure(figsize=(16.5, 11.7), facecolor=th.surface)  # A3
    ax = fig.add_axes([0, 0.072, 1, 0.855])
    ax.set_facecolor(th.surface)
    ax.set_axis_off()
    _draw_doc(ax, doc, th.surface)

    sheet = ezb.extents(doc.modelspace(), fast=True)
    if sheet.has_data:
        px, py = sheet.size.x * 0.01, sheet.size.y * 0.01
        ax.set_xlim(sheet.extmin.x - px, sheet.extmax.x + px)
        ax.set_ylim(sheet.extmin.y - py, sheet.extmax.y + py)
    ax.set_aspect("equal", adjustable="datalim")

    fig.text(0.012, 0.992, f"{title}   full sheet - nothing cropped",
             va="top", ha="left", fontsize=13, family="monospace",
             color=th.ink, weight="bold")
    fig.text(0.012, 0.969,
             f"{dxf.name}\n"
             f"A={counts['A']} B={counts['B']} C={counts['C']} "
             f"D={counts['D']}   E={counts['E']} flagged (not touched)   "
             f"overlap {overlap:.2f} mm2   own-line {own_mm2:.1f} mm2"
             f"{tip_line}",
             va="top", ha="left", fontsize=11, family="monospace",
             color=th.ink_dim, linespacing=1.6)

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=180, facecolor=th.surface)
    plt.close(fig)
    print(f"  {title}: A={counts['A']} B={counts['B']} C={counts['C']} "
          f"D={counts['D']} E={counts['E']}  overlap {overlap:.2f} mm2 "
          f"-> {out.name}")
    return {"counts": counts, "overlap": overlap, "labels": notes.get("labels", 0)}


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    site = next((x.split("=", 1)[1] for x in sys.argv
                 if x.startswith("--site=")), "jp1071")
    sheet_png(Path(a[0]), Path(a[1]), a[2] if len(a) > 2 else "SHEET", site)
