"""
Roadmap 5b: run the solve chain over EVERY drawing and tabulate what happens.

    python tools/batch_report.py work/batch/dxf work/batch/out

Per sheet: audit BEFORE -> tidy (A) -> reroute (B) -> audit AFTER, all on the
DXF (the ODA round-trip is a separate, one-shot pass at the end of the fleet
-- conversion fidelity is its own axis and should not blur the solver's).

A crash is a RESULT, not an interruption: it is caught, logged to
<out>/log/<tag>.log with the traceback, and the batch continues.  Rows land
in <out>/report.csv as they finish, so a killed batch still reports.

Columns: tag, status, labels, then before/after for A B C D overlap, seconds.
status: CRASH | CLEAN (nothing to do) | OK (improved to zero) |
        RESIDUAL (improved, defects left) | WORSE (any category grew).
"""

from __future__ import annotations

import contextlib
import csv
import sys
import time
import traceback

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from isotidy import DEFAULT_ROLES, JP1071_ROLES, Tuning, detect
from isotidy.extract import extract
from tools.mess_map import audit
from tools.reroute import main as reroute_main
from tools.tidy import tidy

SITES = {"default": DEFAULT_ROLES, "jp1071": JP1071_ROLES}


def score(path: Path, roles, cfg):
    found, notes = audit(path, roles, cfg)
    counts = Counter(code for code, _x, _y, _d in found)
    _doc, scene = extract(path, roles, cfg)
    overlap = sum(c.area for c in detect(scene, cfg).collisions)
    return counts, overlap, notes


def run_sheet(src: Path, out_dir: Path, roles, cfg) -> dict:
    tag = src.stem.replace("JP1071-000-PP-ISO-24-", "")
    log = out_dir / "log" / f"{tag}.log"
    row: dict = {"tag": tag}
    t0 = time.time()
    try:
        with open(log, "w", encoding="utf-8") as lf, \
                contextlib.redirect_stdout(lf), \
                contextlib.redirect_stderr(lf):
            bc, bo, bn = score(src, roles, cfg)
            tidied = out_dir / "tmp" / f"{tag}_tidy.dxf"
            final = out_dir / "dxf" / f"{tag}_TIDIED.dxf"
            tidy(src, tidied, "jp1071")
            reroute_main(tidied, final, "jp1071")
            ac, ao, an = score(final, roles, cfg)
        row.update({
            "labels": bn.get("labels", 0),
            "A0": bc.get("A", 0), "A1": ac.get("A", 0),
            "B0": bc.get("B", 0), "B1": ac.get("B", 0),
            "C0": bc.get("C", 0), "C1": ac.get("C", 0),
            "D0": bc.get("D", 0), "D1": ac.get("D", 0),
            "ov0": round(bo, 2), "ov1": round(ao, 2),
        })
        worse = any(row[f"{k}1"] > row[f"{k}0"] for k in "ABCD") \
            or ao > bo + 1e-6
        fixable0 = sum(bc.get(k, 0) for k in "ABCD") + (bo > 1e-6)
        fixable1 = sum(ac.get(k, 0) for k in "ABCD") + (ao > 1e-6)
        row["status"] = ("WORSE" if worse else
                         "CLEAN" if fixable0 == 0 else
                         "OK" if fixable1 == 0 else "RESIDUAL")
    except BaseException:
        with open(log, "a", encoding="utf-8") as lf:
            lf.write("\n" + traceback.format_exc())
        row["status"] = "CRASH"
        row["error"] = traceback.format_exc(limit=1).strip().splitlines()[-1]
    row["sec"] = round(time.time() - t0, 1)
    return row


FIELDS = ["tag", "status", "labels", "A0", "A1", "B0", "B1", "C0", "C1",
          "D0", "D1", "ov0", "ov1", "sec", "error"]


def main(in_dir: Path, out_dir: Path) -> None:
    for d in ("log", "tmp", "dxf"):
        (out_dir / d).mkdir(parents=True, exist_ok=True)
    roles, cfg = JP1071_ROLES, Tuning()
    report = out_dir / "report.csv"
    done = set()
    if report.exists():
        with open(report, newline="", encoding="utf-8") as f:
            done = {r["tag"] for r in csv.DictReader(f)}
    new = not report.exists()
    with open(report, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        srcs = sorted(in_dir.glob("*.dxf"))
        for i, src in enumerate(srcs, 1):
            tag = src.stem.replace("JP1071-000-PP-ISO-24-", "")
            if tag in done:
                continue
            row = run_sheet(src, out_dir, roles, cfg)
            w.writerow(row)
            f.flush()
            print(f"[{i:2d}/{len(srcs)}] {row['tag']:14s} "
                  f"{row['status']:8s} {row.get('sec', 0):6.1f}s  "
                  + (row.get("error", "") if row["status"] == "CRASH" else
                     f"A {row.get('A0')}->{row.get('A1')} "
                     f"B {row.get('B0')}->{row.get('B1')} "
                     f"C {row.get('C0')}->{row.get('C1')} "
                     f"D {row.get('D0')}->{row.get('D1')} "
                     f"ov {row.get('ov0')}->{row.get('ov1')}"))
    print(f"\nreport -> {report}")


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    main(Path(a[0]), Path(a[1]))
