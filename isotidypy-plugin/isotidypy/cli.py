"""The `isotidypy` command.  See package docstring for usage.

This wraps the pipeline exactly as it was proven on 71 sheets -- it adds
no placement logic of its own:

    tools.pipeline.run   DWG -> DXF (ODA), tidy + reroute, DXF -> DWG,
                         round-trip audit, BEFORE/AFTER/DIFF renders
    tools.rework.rework  the polish stages (doglegs, realign, claims,
                         frames, threads, hairlines, table), each verified
    ODA again            the reworked DXF -> the DWG that ships
    tools.sheet_health   every rule, measured on the shipped file

Never-worse is enforced inside those tools; this file only sequences them,
reports, and decides where the finished DWG goes.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from isotidypy import __version__  # noqa: E402

DONE_FILE = ".isotidypy-done.txt"


# --------------------------------------------------------------------------
def _oda_or_die():
    from tools.convert import find_converter
    try:
        return find_converter()
    except SystemExit:
        print("!! ODA File Converter is not installed.  It is free:\n"
              "     winget install ODA.ODAFileConverter\n"
              "   or download from https://www.opendesign.com/guestfiles/oda_file_converter\n"
              "   then run this command again.")
        raise SystemExit(2)


def _table_rect(dxf: Path):
    try:
        import ezdxf
        from tools.rework import table_rect
        return table_rect(ezdxf.readfile(dxf))
    except Exception:
        return None


def _health(dxf: Path) -> tuple[int, list[str]]:
    """Run sheet_health on `dxf`; (issue count, lines)."""
    args = [sys.executable, str(ROOT / "tools" / "sheet_health.py"), str(dxf)]
    rect = _table_rect(dxf)
    if rect:
        args += [f"{v:.1f}" for v in rect]
    out = subprocess.run(args, capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    lines = [l.strip() for l in out.splitlines()
             if l.startswith("  ") and not l.strip().startswith("clean")]
    return len(lines), lines


def _tips_unmoved(orig_dxf: Path, new_dxf: Path) -> tuple[int, int]:
    import ezdxf
    o = {m.dxf.handle: m for m in
         ezdxf.readfile(orig_dxf).modelspace().query("MULTILEADER")}
    n = {m.dxf.handle: m for m in
         ezdxf.readfile(new_dxf).modelspace().query("MULTILEADER")}
    same = total = 0
    for h, m in o.items():
        try:
            a = m.context.leaders[0].lines[0].vertices[0]
            b = n[h].context.leaders[0].lines[0].vertices[0]
            total += 1
            if abs(a.x - b.x) < 1e-6 and abs(a.y - b.y) < 1e-6:
                same += 1
        except Exception:
            pass
    return same, total


# --------------------------------------------------------------------------
def score_one(src: Path, site: str) -> dict:
    """Measure the drawing as it is.  Writes nothing."""
    from tools.convert import convert
    from tools.mess_map import audit
    from isotidy import JP1071_ROLES, DEFAULT_ROLES, Tuning, detect
    from isotidy.extract import extract
    roles = {"jp1071": JP1071_ROLES, "default": DEFAULT_ROLES}[site]
    cfg = Tuning()
    with tempfile.TemporaryDirectory(prefix="isotidypy_") as td:
        tmp = Path(td)
        (tmp / "in").mkdir()
        shutil.copy(src, tmp / "in" / src.name)
        made = convert(tmp / "in", tmp / "dxf", "DXF")
        if not made:
            raise RuntimeError("ODA produced no DXF")
        dxf = made[0]
        _d, sc = extract(dxf, roles, cfg)
        rep = detect(sc, cfg)
        found, notes = audit(dxf, roles, cfg)
        counts = {k: sum(1 for c, *_ in found if c == k) for k in "ABCDE"}
        n_issues, _lines = _health(dxf)
        return {"overlap": rep.overlap_area, "counts": counts,
                "labels": notes.get("labels", 0), "health": n_issues}


def tidy_one(src: Path, evidence: Path, site: str, dest: Path | None,
             inplace: bool) -> dict:
    """The full proven chain on one DWG.  Returns the verdict dict."""
    from tools.pipeline import run as pipeline_run
    from tools.rework import rework
    from tools.convert import convert
    from tools.sheet_png import sheet_png

    tag = src.stem
    out = evidence / tag
    t0 = time.time()
    print(f"\n==== {src.name}")

    # 1. tidy + reroute + round-trip audit + renders (the proven pipeline)
    try:
        res = pipeline_run(src, out, site, tag)
    except SystemExit as exc:
        return {"file": src.name, "ok": False, "why": str(exc)}
    tidied_dxf = out / f"{tag}_TIDIED.dxf"
    orig_dxf = out / f"{tag}_ORIGINAL.dxf"

    # 2. the polish stages, each verified against detect()/audit()
    print(f"[{tag}] rework stages:")
    rw = rework(tidied_dxf, site)

    # 3. the reworked DXF is what ships: convert it, re-render it
    with tempfile.TemporaryDirectory(prefix="isotidypy_") as td:
        tmp = Path(td)
        (tmp / "in").mkdir()
        shutil.copy(tidied_dxf, tmp / "in" / tidied_dxf.name)
        made = convert(tmp / "in", tmp / "dwg", "DWG")
        if not made:
            return {"file": src.name, "ok": False,
                    "why": "ODA produced no DWG for the reworked file"}
        final_dwg = out / f"{tag}_TIDIED.dwg"
        shutil.copy(made[0], final_dwg)
    a = sheet_png(tidied_dxf, out / f"{tag}_AFTER.png", "AFTER", site,
                  compare_tips_with=orig_dxf)

    # 4. measure what ships
    n_issues, lines = _health(tidied_dxf)
    same, total = _tips_unmoved(orig_dxf, tidied_dxf)
    b = res["before"]

    # 5. deliver the DWG
    if inplace:
        backup = src.parent / "original"
        backup.mkdir(exist_ok=True)
        if not (backup / src.name).exists():
            shutil.copy(src, backup / src.name)
        shutil.copy(final_dwg, src)
        delivered = src
    else:
        d = dest or (src.parent / "tidied")
        d.mkdir(parents=True, exist_ok=True)
        delivered = d / src.name
        shutil.copy(final_dwg, delivered)

    print(f"\n[{tag}] VERDICT (measured on the shipped file):")
    for k in "ABCD":
        print(f"  {k}: {b['counts'][k]:3d} -> {a['counts'][k]:3d}")
    print(f"  overlap {b['overlap']:.2f} -> {a['overlap']:.2f} mm2")
    print(f"  rework: {', '.join(f'{k} {v}' for k, v in rw.items() if k not in ('reverted','overlap','bcd') and v)}"
          or "  rework: nothing to do")
    if rw.get("reverted"):
        print(f"  rework stages rolled back (would have been worse): {rw['reverted']}")
    print(f"  arrowheads unmoved: {same}/{total}")
    print(f"  remaining findings by every rule: {n_issues}")
    for l in lines[:8]:
        print(f"      {l}")
    print(f"  wrote {delivered}")
    print(f"  evidence (BEFORE/AFTER/DIFF renders, DXFs): {out}")
    print(f"  {time.time() - t0:.0f} s")
    return {"file": src.name, "ok": True, "before": b["overlap"],
            "after": a["overlap"], "tips": f"{same}/{total}",
            "health": n_issues, "dwg": str(delivered)}


# --------------------------------------------------------------------------
def _dwgs_in(folder: Path) -> list[Path]:
    return sorted(p for p in folder.glob("*.dwg") if p.is_file())


def watch(folder: Path, evidence: Path, site: str, inplace: bool,
          interval: int) -> None:
    """Phase D: tidy every NEW DWG as ISOGEN drops it.  Polling (a file
    watcher misses events on network shares); a file must be the same
    size on two consecutive polls before it is touched (ISOGEN may still
    be writing it)."""
    done_path = folder / DONE_FILE
    done = set(done_path.read_text().split()) if done_path.exists() else set()
    sizes: dict[str, int] = {}
    print(f"watching {folder}  (every {interval} s; Ctrl+C stops)")
    while True:
        for p in _dwgs_in(folder):
            if p.name in done:
                continue
            prev = sizes.get(p.name)
            sizes[p.name] = p.stat().st_size
            if prev is None or prev != p.stat().st_size:
                continue
            try:
                r = tidy_one(p, evidence, site, None, inplace)
                if r["ok"]:
                    done.add(p.name)
                    done_path.write_text("\n".join(sorted(done)))
                else:
                    print(f"  !! {p.name}: {r['why']} -- will retry")
            except Exception as exc:
                print(f"  !! {p.name}: {exc} -- will retry")
        time.sleep(interval)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="isotidypy",
        description="Tidy ISOGEN isometric annotations.  DWG in, DWG out.")
    ap.add_argument("path", nargs="?", help="a .dwg file or a folder of them")
    ap.add_argument("--score", action="store_true",
                    help="measure only; write nothing")
    ap.add_argument("--watch", action="store_true",
                    help="keep watching the folder; tidy each new DWG")
    ap.add_argument("--inplace", action="store_true",
                    help="replace the original (backup kept in original\\)")
    ap.add_argument("--out", type=Path,
                    help="evidence folder (default: <folder>\\isotidypy_out)")
    ap.add_argument("--site", default="jp1071", choices=["jp1071", "default"])
    ap.add_argument("--interval", type=int, default=15,
                    help="watch poll interval, seconds")
    ap.add_argument("--selftest", action="store_true",
                    help="run the fixture regression suite")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)

    if a.selftest:
        return subprocess.call([sys.executable, "-m", "pytest",
                                str(ROOT / "tests"), "-q"])
    if not a.path:
        ap.print_help()
        return 1
    _oda_or_die()
    src = Path(a.path).expanduser().resolve()
    if not src.exists():
        print(f"!! not found: {src}")
        return 2

    if a.score:
        files = [src] if src.is_file() else _dwgs_in(src)
        for f in files:
            s = score_one(f, a.site)
            c = s["counts"]
            print(f"{f.name}: overlap {s['overlap']:.2f} mm2   "
                  f"A={c['A']} B={c['B']} C={c['C']} D={c['D']} E={c['E']}   "
                  f"{s['labels']} labels   {s['health']} findings by every rule")
        return 0

    folder = src if src.is_dir() else src.parent
    evidence = a.out or (folder / "isotidypy_out")
    if a.watch:
        if not src.is_dir():
            print("!! --watch needs a folder")
            return 2
        watch(src, evidence, a.site, a.inplace, a.interval)
        return 0

    files = [src] if src.is_file() else _dwgs_in(src)
    if not files:
        print(f"!! no .dwg files in {src}")
        return 2
    results = [tidy_one(f, evidence, a.site, None, a.inplace) for f in files]
    if len(results) > 1:
        print("\n==== SUMMARY")
        for r in results:
            if r["ok"]:
                print(f"  {r['file']:40s} {r['before']:7.2f} -> {r['after']:6.2f} mm2"
                      f"   tips {r['tips']}   findings {r['health']}")
            else:
                print(f"  {r['file']:40s} FAILED: {r['why']}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
