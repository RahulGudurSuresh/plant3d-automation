"""Run sheet_health on every TIDIED sheet and tabulate findings by class.

    python tools/fleet_health.py [work/batch/out_final/dxf]

Per sheet the title-block table rect is measured (rework.table_rect), so
ON-TABLE findings are real.  Output: one line per sheet with residuals,
then a fleet total by finding class -- the honest "what is left" list.
"""
import subprocess
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ezdxf

from tools.rework import table_rect

PY = sys.executable
HEALTH = Path(__file__).resolve().parent / "sheet_health.py"
folder = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    Path("work/batch/out_final/dxf")

fleet = Counter()
dirty = []
sheets = sorted(folder.glob("*_TIDIED.dxf"))
for p in sheets:
    args = [PY, str(HEALTH), str(p)]
    try:
        rect = table_rect(ezdxf.readfile(p))
        if rect:
            args += [f"{v:.1f}" for v in rect]
    except Exception:
        pass
    out = subprocess.run(args, capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    lines = [l.strip() for l in out.splitlines()
             if l.startswith("  ") and not l.strip().startswith("clean")]
    # AUDIT-A duplicates OVERLAP; count each class once per line kind
    kinds = Counter(l.split()[0].rstrip(":") for l in lines)
    for k, n in kinds.items():
        fleet[k] += n
    if lines:
        tag = p.name.replace("_TIDIED.dxf", "")
        dirty.append((tag, lines))

print(f"{len(sheets)} sheets, {len(sheets) - len(dirty)} clean by every rule, "
      f"{len(dirty)} with residuals\n")
for tag, lines in dirty:
    print(f"{tag}")
    for l in lines:
        print(f"    {l}")
print("\nFLEET TOTAL BY CLASS:")
for k, n in fleet.most_common():
    print(f"  {k:12s} {n}")
