"""
DWG <-> DXF via the free ODA File Converter (winget install ODA.ODAFileConverter).

Same wrapper isotidy uses, kept here so this package stands alone.

GOTCHA, inherited from isotidy: the file filter is CASE SENSITIVE.  "*.dwg"
converts nothing and exits 0.  Always uppercase, always count the output.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ODA_CANDIDATES = [Path(r"C:\Program Files\ODA"), Path(r"C:\Program Files (x86)\ODA")]
OUT_VERSION = "ACAD2018"


def find_converter() -> Path:
    for root in ODA_CANDIDATES:
        if root.exists():
            for exe in sorted(root.glob("*/ODAFileConverter.exe"), reverse=True):
                return exe
    raise SystemExit("ODA File Converter not found.  winget install ODA.ODAFileConverter")


def convert_dir(src: Path, dst: Path, to: str) -> list[Path]:
    exe = find_converter()
    src, dst = src.resolve(), dst.resolve()
    dst.mkdir(parents=True, exist_ok=True)
    src_ext = "DWG" if to == "DXF" else "DXF"
    before = {p.name for p in dst.glob(f"*.{to.lower()}")}
    subprocess.run([str(exe), str(src), str(dst), OUT_VERSION, to, "0", "1",
                    f"*.{src_ext}"], check=False)
    made = [p for p in sorted(dst.glob(f"*.{to.lower()}")) if p.name not in before]
    if not made:
        print(f"  !! nothing converted from {src} -- check the filter case",
              file=sys.stderr)
    return made


def convert_file(src: Path, dst: Path) -> Path:
    """Convert one file; `dst` decides the direction by its suffix."""
    to = dst.suffix.lstrip(".").upper()
    with tempfile.TemporaryDirectory() as tin, tempfile.TemporaryDirectory() as tout:
        tmp_in = Path(tin) / src.name
        shutil.copy2(src, tmp_in)
        made = convert_dir(Path(tin), Path(tout), to)
        if not made:
            raise RuntimeError(f"ODA converter produced no {to} for {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(made[0], dst)
    return dst
