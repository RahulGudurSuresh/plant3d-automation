"""
Polish pass: the two defect classes named by the engineer on 1028M01_r0
(2026-08-28), applied generically.

1. FAR-EDGE LANDINGS.  A leader that lands on the far edge of its own label
   runs straight through the text it belongs to before stopping -- "goes over
   the annotation, then points somewhere else".  It was structurally
   invisible: every checker exempts own-label contact, and the exemption was
   written too coarsely to tell touching from crossing.  Rule: if the path's
   run through its own label's interior exceeds OWN_CROSS_TOL, re-land on the
   nearest edge that gives a clean line.

2. TIGHT TAG FRAMES.  ISOGEN's AnnoRect* blocks draw one fixed-width
   rectangle whatever the tag length; long tags overflow it (mildly in
   AutoCAD, badly in the PNG's substituted font).  A label was never checked
   against ITSELF, so this had no category.  Rule: when attrib text fills
   >= FRAME_RATIO of its frame, widen the frame (per-instance xscale; text
   and height untouched) to FRAME_FACTOR x the text extent plus margin.

Both are verified per stage against detect(): a stage that increases the
sheet's measured overlap is rolled back, reported, and the sheet ships
without it -- no defect trades.

Run:
    python tools/polish.py in.dxf [out.dxf] [--site=jp1071]
"""

from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ezdxf
from ezdxf import bbox as ezb
from ezdxf.math import Vec3
from shapely.geometry import LineString, box as sbox

from isotidy import DEFAULT_ROLES, JP1071_ROLES, Tuning, detect
from isotidy.writeback import save_dxf  # noqa: E402
from isotidy.extract import component_symbols, extract
from isotidy.leaders import symbol_hits

SITES = {"default": DEFAULT_ROLES, "jp1071": JP1071_ROLES}

OWN_CROSS_TOL = 2.0    # mm of run through the own box that counts as crossing
FRAME_RATIO = 0.85     # text/frame width ratio that counts as tight
FRAME_FACTOR = 1.2     # widened frame = text extent * this + 2*margin
FRAME_MARGIN = 1.0     # mm
FRAME_PREFIXES = ("AnnoRect",)


def _leader_paths(msp):
    out = {}
    for ml in msp.query("MULTILEADER"):
        try:
            ld = ml.context.leaders[0]
            lp = ld.last_leader_point
            out[ml.dxf.handle] = [(v.x, v.y) for v in ld.lines[0].vertices] \
                + [(lp.x, lp.y)]
        except Exception:
            pass
    return out


def fix_far_edge_landings(doc, scene, cfg) -> int:
    msp = doc.modelspace()
    paths = _leader_paths(msp)
    symbols = component_symbols(doc)
    by_handle = {l.leader_handle: l for l in scene.labels if l.leader_handle}
    fixed = 0
    for handle, pts in paths.items():
        lab = by_handle.get(handle)
        if lab is None:
            continue
        line = LineString(pts)
        x0, y0, x1, y1 = lab.box().bounds
        inner = sbox(x0 + 0.05, y0 + 0.05, x1 - 0.05, y1 - 0.05)
        run = line.intersection(inner)
        if getattr(run, "length", 0.0) <= OWN_CROSS_TOL:
            continue

        ml = doc.entitydb.get(handle)
        ctx = ml.context
        tip = ctx.leaders[0].lines[0].vertices[0]      # exact, never retyped
        cands = [(x0, (y0 + y1) / 2), (x1, (y0 + y1) / 2),
                 ((x0 + x1) / 2, y1), ((x0 + x1) / 2, y0)]
        best = None
        for c in cands:
            cand = LineString([(tip.x, tip.y), c])
            if getattr(cand.intersection(inner), "length", 0.0) \
                    > OWN_CROSS_TOL:
                continue
            if symbol_hits(cand, (tip.x, tip.y), c, symbols):
                continue
            bad = False
            for h2, p2 in paths.items():
                if h2 == handle:
                    continue
                if cand.crosses(LineString(p2)):
                    bad = True
                    break
            if not bad:
                for o in scene.labels:
                    if o.index == lab.index or \
                            (lab.group is not None and o.group == lab.group):
                        continue
                    if cand.intersects(
                            o.box(cfg.clearance + cfg.geom_buffer)):
                        bad = True
                        break
            if bad:
                continue
            if best is None or cand.length < best[0]:
                best = (cand.length, c)
        if best is None:
            print(f"    own-label crossing on {handle} "
                  f"({lab.text.strip()[:16]!r}): no clean near edge -- left")
            continue
        c = best[1]
        v = Vec3(c[0], c[1], 0)
        ctx.leaders[0].lines[0].vertices = [tip]
        ctx.leaders[0].last_leader_point = v
        ctx.base_point = v
        ml.proxy_graphic = None
        paths[handle] = [(tip.x, tip.y), c]
        fixed += 1
        print(f"    re-landed {lab.text.strip()[:18]!r}: leader no longer "
              f"runs through its own text")
    return fixed


def fix_tight_frames(doc) -> int:
    fixed = 0
    for e in doc.modelspace().query("INSERT"):
        if not e.dxf.name.startswith(FRAME_PREFIXES) or not e.attribs:
            continue
        fb = ezb.extents([e], fast=False)
        if not fb.has_data or fb.size.x <= 0:
            continue
        frame_w = fb.size.x
        widths = []
        for a in e.attribs:
            ab = ezb.extents([a], fast=False)
            if ab.has_data:
                widths.append(ab.size.x)
        if not widths:
            continue
        text_w = max(widths)
        if text_w / frame_w < FRAME_RATIO:
            continue
        need = text_w * FRAME_FACTOR + 2 * FRAME_MARGIN
        if frame_w >= need - 1e-6:
            continue
        e.dxf.xscale = e.dxf.xscale * need / frame_w
        fixed += 1
        print(f"    widened frame of {e.attribs[0].dxf.text!r}: "
              f"{frame_w:.1f} -> {need:.1f} mm")
    return fixed


def polish(src: Path, dst: Path, site: str = "jp1071") -> dict:
    roles, cfg = SITES[site], Tuning()
    result = {"landings": 0, "frames": 0, "reverted": []}

    doc = ezdxf.readfile(src)
    _d, scene = extract(src, roles, cfg)
    base = detect(scene, cfg).overlap_area
    tmp = dst.with_suffix(".polish.dxf")

    # stage 1: landings
    n = fix_far_edge_landings(doc, scene, cfg)
    if n:
        save_dxf(doc, tmp)
        _d2, s2 = extract(tmp, roles, cfg)
        if detect(s2, cfg).overlap_area > base + 1e-9:
            print("    landings stage increased overlap -- rolled back")
            result["reverted"].append("landings")
            doc = ezdxf.readfile(src)
            _d, scene = extract(src, roles, cfg)
        else:
            result["landings"] = n

    # stage 2: frames
    n = fix_tight_frames(doc)
    if n:
        save_dxf(doc, tmp)
        _d2, s2 = extract(tmp, roles, cfg)
        if detect(s2, cfg).overlap_area > base + 1e-9:
            print("    frames stage increased overlap -- rolled back")
            result["reverted"].append("frames")
            doc = ezdxf.readfile(src)
            _d, scene = extract(src, roles, cfg)
            n2 = fix_far_edge_landings(doc, scene, cfg) \
                if result["landings"] else 0
            result["landings"] = n2
        else:
            result["frames"] = n

    save_dxf(doc, dst)
    tmp.unlink(missing_ok=True)
    return result


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    site = next((x.split("=", 1)[1] for x in sys.argv
                 if x.startswith("--site=")), "jp1071")
    src = Path(a[0])
    dst = Path(a[1]) if len(a) > 1 else src
    print(polish(src, dst, site))
