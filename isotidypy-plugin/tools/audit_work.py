"""
Re-audit every ORIGINAL/TIDIED pair sitting in work/<tag>/ and print a
before -> after table per sheet.  Read-only; no solving, no renders.

    python tools/audit_work.py
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collections import Counter

from isotidy import JP1071_ROLES, Tuning, detect
from isotidy.extract import extract
from tools.mess_map import audit

WORK = Path(__file__).resolve().parents[1] / "work"


def score(path, roles, cfg):
    found, notes = audit(path, roles, cfg)
    counts = Counter(code for code, _x, _y, _d in found)
    _doc, scene = extract(path, roles, cfg)
    overlap = sum(c.area for c in detect(scene, cfg).collisions)
    return counts, overlap, notes


def main() -> None:
    roles, cfg = JP1071_ROLES, Tuning()
    for d in sorted(WORK.iterdir()):
        if not d.is_dir():
            continue
        orig = list(d.glob("*_ORIGINAL.dxf"))
        tidy = list(d.glob("*_TIDIED.dxf"))
        if not orig or not tidy:
            continue
        bc, bo, bn = score(orig[0], roles, cfg)
        ac, ao, an = score(tidy[0], roles, cfg)
        line = [f"{d.name:14s}"]
        for k in "ABCDE":
            line.append(f"{k}:{bc.get(k, 0):3d}->{ac.get(k, 0):3d}")
        line.append(f"overlap {bo:8.2f} -> {ao:8.2f} mm2")
        line.append(f"labels {an.get('labels', '?')}")
        print("  ".join(line))


if __name__ == "__main__":
    main()
