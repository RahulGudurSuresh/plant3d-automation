"""
DWG <-> DXF batch conversion via ODA File Converter.

isotidy reads DXF; Plant 3D emits DWG.  This wraps the free ODA File
Converter so the round trip is one command instead of a GUI.

    winget install ODA.ODAFileConverter

GOTCHA, learned the hard way: the file filter is CASE SENSITIVE.  Passing
"*.dwg" silently converts nothing and exits 0 -- no error, no output, no
warning.  Always pass it uppercase, and always check the output count.

Run:
    python tools/convert.py to-dxf "<in dir>" "<out dir>"
    python tools/convert.py to-dwg "<in dir>" "<out dir>"
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ODA_CANDIDATES = [
    Path(r"C:\Program Files\ODA"),
    Path(r"C:\Program Files (x86)\ODA"),
]
OUT_VERSION = "ACAD2018"


def find_converter() -> Path:
    for root in ODA_CANDIDATES:
        if not root.exists():
            continue
        for exe in sorted(root.glob("*/ODAFileConverter.exe"), reverse=True):
            return exe
    raise SystemExit(
        "ODA File Converter not found.\n"
        "  Install it with:  winget install ODA.ODAFileConverter"
    )


def convert(src: Path, dst: Path, to: str) -> list[Path]:
    """to = 'DXF' or 'DWG'.  Returns the files produced."""
    exe = find_converter()
    src, dst = src.resolve(), dst.resolve()
    dst.mkdir(parents=True, exist_ok=True)
    src_ext = "DWG" if to == "DXF" else "DXF"
    before = {p.name for p in dst.glob(f"*.{to.lower()}")}

    subprocess.run(
        [str(exe), str(src), str(dst), OUT_VERSION, to, "0", "1",
         f"*.{src_ext}"],          # uppercase -- see module docstring
        check=False,
    )

    made = [p for p in sorted(dst.glob(f"*.{to.lower()}"))
            if p.name not in before]
    n_in = len(list(src.glob(f"*.{src_ext.lower()}")))
    if n_in and not made:
        print(f"  !! {n_in} input file(s) but nothing converted -- "
              f"check the filter case and that {src} contains .{src_ext} files",
              file=sys.stderr)
    return made


def main() -> None:
    if len(sys.argv) < 4 or sys.argv[1] not in ("to-dxf", "to-dwg"):
        print(__doc__)
        raise SystemExit(1)
    to = "DXF" if sys.argv[1] == "to-dxf" else "DWG"
    made = convert(Path(sys.argv[2]), Path(sys.argv[3]), to)
    for p in made:
        print(f"  {p.name}  ({p.stat().st_size // 1024} KB)")
    print(f"converted {len(made)} file(s) -> {to}")


if __name__ == "__main__":
    main()
