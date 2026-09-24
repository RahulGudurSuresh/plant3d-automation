"""
Reconnaissance on a real ISOGEN drawing, before trying to process it.

Run this FIRST on any new site's drawings.  It answers the three questions
that decide whether isotidy can do anything at all:

    Where is the content?      modelspace or paper-space layouts
    What are annotations MADE of?   TEXT / MTEXT / DIMENSION / MULTILEADER /
                                    INSERT+ATTRIB -- each needs its own adapter
    What are the layers called?     so LayerRoles can be written

Guessing any of these costs a day.  Measuring them costs a second.

Run:
    python tools/survey.py <file.dxf> [more.dxf ...]
    python tools/survey.py <folder>
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

import ezdxf
from ezdxf import bbox

# Entity types that can carry annotation text, and how we'd have to reach it.
ANNOTATION_TYPES = {
    "TEXT":        "supported",
    "MTEXT":       "supported",
    "ATTRIB":      "supported",
    "DIMENSION":   "needs adapter: dxf.text_midpoint",
    "MULTILEADER": "needs adapter: context.mtext / leader routing",
    "INSERT":      "needs adapter: recurse into .attribs",
    "ACAD_TABLE":  "fixed region (BOM) -- never movable",
    "LEADER":      "needs adapter: legacy leader",
}


def survey(path: Path) -> dict:
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()

    print(f"\n{'=' * 72}\n{path.name}\n{'=' * 72}")
    print(f"  DXF {doc.dxfversion} ({doc.acad_release})   "
          f"$INSUNITS={doc.header.get('$INSUNITS')} (4=mm)")

    # --- where is the content? ------------------------------------------
    layouts = [(n, len(list(doc.layout(n)))) for n in doc.layout_names()]
    stray = [(n, c) for n, c in layouts if n != "Model" and c > 2]
    print("  layouts: " + ", ".join(f"{n}={c}" for n, c in layouts))
    if stray:
        print("  !! content in paper space -- isotidy reads modelspace only")

    try:
        b = bbox.extents(msp, fast=True)
        print(f"  extents: {b.size.x:.1f} x {b.size.y:.1f} units")
    except Exception:
        pass

    # --- what is it made of? ---------------------------------------------
    types = collections.Counter(e.dxftype() for e in msp)
    print(f"\n  {'entity':<14}{'n':>5}   status")
    print("  " + "-" * 60)
    for t, n in types.most_common():
        note = ANNOTATION_TYPES.get(t, "geometry / obstacle")
        print(f"  {t:<14}{n:>5}   {note}")

    # --- attributed blocks: the hidden annotations ------------------------
    blocks = collections.Counter()
    attr_text = collections.Counter()
    for e in msp.query("INSERT"):
        blocks[e.dxf.name] += 1
        for a in e.attribs:
            attr_text[e.dxf.name] += 1
    hidden = {k: v for k, v in attr_text.items() if v}
    if hidden:
        print(f"\n  attributed blocks (text isotidy cannot currently see):")
        for name, n in sorted(hidden.items(), key=lambda kv: -kv[1]):
            print(f"    {name:<34} {blocks[name]:>3} inserts, {n:>3} attribs")

    # --- layers -----------------------------------------------------------
    layers = collections.Counter(e.dxf.layer for e in msp)
    print(f"\n  layers in use ({len(layers)} of {len(doc.layers)} defined):")
    for lyr, n in layers.most_common():
        print(f"    {lyr:<28} {n:>5}")

    # --- the headline number ---------------------------------------------
    visible = sum(n for t, n in types.items() if t in ("TEXT", "MTEXT"))
    total_anno = visible + types.get("DIMENSION", 0) \
        + types.get("MULTILEADER", 0) + sum(hidden.values())
    print(f"\n  ANNOTATIONS: ~{total_anno} total, "
          f"{visible} readable by isotidy today "
          f"({100 * visible / total_anno if total_anno else 0:.0f}%)")

    return {"types": types, "layers": layers, "hidden": hidden,
            "visible": visible, "total": total_anno}


def main() -> None:
    args = [Path(a) for a in sys.argv[1:]]
    if not args:
        print(__doc__)
        raise SystemExit(1)
    files: list[Path] = []
    for a in args:
        files.extend(sorted(a.glob("*.dxf")) if a.is_dir() else [a])
    for f in files:
        survey(f)


if __name__ == "__main__":
    main()
