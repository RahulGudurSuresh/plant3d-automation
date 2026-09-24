"""
Full-sheet BEFORE/AFTER PNGs for every sheet the batch processed.

    python tools/batch_png.py [<dxf_in_dir> <tidied_dir> <png_out_dir>]

Defaults to the 2026-08-27 fleet layout: work/batch/dxf (originals),
work/batch/out_final/dxf (tidied), work/batch/png (output).  Captions are
measured from each file at draw time (sheet_png), full sheet, nothing
cropped.  Resumable: pairs whose two PNGs already exist are skipped.

Also writes <png_out_dir>/index.html -- open it in a browser to page
through every before/after pair side by side.
"""

from __future__ import annotations

import html
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.sheet_png import sheet_png

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "JP1071-000-PP-ISO-24-"


def main(dxf_dir: Path, tidied_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    pairs = []
    tidied = sorted(tidied_dir.glob("*_TIDIED.dxf"))
    for i, t in enumerate(tidied, 1):
        tag = t.stem.replace("_TIDIED", "")
        src = dxf_dir / f"{PREFIX}{tag}.dxf"
        if not src.exists():
            print(f"[{i}/{len(tidied)}] {tag}: no original, skipped")
            continue
        b_png = out_dir / f"{tag}_BEFORE.png"
        a_png = out_dir / f"{tag}_AFTER.png"
        pairs.append((tag, b_png.name, a_png.name))
        if b_png.exists() and a_png.exists():
            print(f"[{i}/{len(tidied)}] {tag}: already rendered")
            continue
        print(f"[{i}/{len(tidied)}] {tag}:")
        sheet_png(src, b_png, "BEFORE")
        sheet_png(t, a_png, "AFTER", compare_tips_with=src)

    rows = "\n".join(
        f'<section><h2 id="{html.escape(tag)}">{html.escape(tag)}</h2>'
        f'<div class="pair">'
        f'<figure><figcaption>before</figcaption>'
        f'<a href="{b}"><img loading="lazy" src="{b}"></a></figure>'
        f'<figure><figcaption>after</figcaption>'
        f'<a href="{a}"><img loading="lazy" src="{a}"></a></figure>'
        f"</div></section>"
        for tag, b, a in pairs)
    toc = " | ".join(f'<a href="#{html.escape(t)}">{html.escape(t)}</a>'
                     for t, _b, _a in pairs)
    (out_dir / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'>"
        "<title>isotidy fleet - before/after</title>"
        "<style>body{font-family:monospace;background:#1a1a19;color:#f5f4f0;"
        "margin:1rem}h2{margin:2rem 0 .3rem}img{width:100%;height:auto;"
        "border:1px solid #444}.pair{display:grid;"
        "grid-template-columns:1fr 1fr;gap:.5rem}figcaption{color:#c3c2b7}"
        "a{color:#7ab3f0}nav{line-height:1.9}</style>"
        f"<h1>isotidy fleet — {len(pairs)} sheets, before/after</h1>"
        f"<nav>{toc}</nav>{rows}",
        encoding="utf-8")
    print(f"\n{len(pairs)} pairs -> {out_dir / 'index.html'}")


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if len(a) >= 3:
        main(Path(a[0]), Path(a[1]), Path(a[2]))
    else:
        main(ROOT / "work/batch/dxf", ROOT / "work/batch/out_final/dxf",
             ROOT / "work/batch/png")
