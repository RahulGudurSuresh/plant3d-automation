"""
Command line entry point.

    python -m isotidy.cli fixtures/iso_congested.dxf fixtures/iso_tidied.dxf

Prints the before/after score.  The score is the product -- "it looks better"
is not a claim you can put in front of a piping lead.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from ezdxf import bbox as ezbbox

from .config import (DEFAULT_ROLES, ISO3098_MIN_TEXT_MM, JP1071_ROLES,
                     Tuning)

#: Site configurations.  Adding a new client is a dict entry, not a code
#: change -- see tools/survey.py for how to discover the values.
SITES = {"default": DEFAULT_ROLES, "jp1071": JP1071_ROLES}
from .detect import detect
from .extract import extract
from .solve import solve
from .writeback import apply


class ConfigMismatch(RuntimeError):
    """The drawing's layer scheme does not match the configured LayerRoles."""


def _guard(src: Path, scene) -> None:
    """Refuse to score a drawing we clearly cannot read.

    Without this the tool reports "0 collisions -- drawing is clean" on a
    drawing whose annotations all sit on layers the config never mentioned.
    A checking tool that returns a false PASS is worse than no tool: it does
    not merely fail, it certifies the failure.
    """
    if scene.labels:
        return
    found = sorted(scene.unmapped_text.items(), key=lambda kv: -kv[1])
    lines = [
        "",
        "  !! NO MANAGED ANNOTATIONS FOUND -- refusing to report a score.",
        f"     {src}",
        "",
        "     Every text entity in this drawing sits on a layer that is not",
        "     declared movable in LayerRoles.  This is normally a layer-name",
        "     mismatch: each site customises its ISOGEN style's",
        "     'Default Model.dwg'.",
        "",
    ]
    if found:
        lines.append("     Text was found on these layers:")
        lines += [f"       {n:6d}  {layer}" for layer, n in found[:20]]
        if len(found) > 20:
            lines.append(f"       ... and {len(found) - 20} more")
    else:
        lines.append("     No text entities at all in modelspace.")
        lines.append("     (Paper-space layouts are not read yet -- see roadmap.)")
    lines += [
        "",
        "     Fix: map these onto isotidy/config.py -> LayerRoles.",
        "",
    ]
    raise ConfigMismatch("\n".join(lines))


class UnsupportedFormat(RuntimeError):
    """Input is not a DXF file."""


def _read(src: Path, cfg: Tuning, roles=DEFAULT_ROLES):
    """Read the input, turning ezdxf's IOError into a message that tells the
    user what to actually do about it.  DWG is the format they will have."""
    try:
        return extract(src, roles, cfg)
    except IOError as exc:
        if "not a DXF" not in str(exc):
            raise
        raise UnsupportedFormat(
            f"\n  !! NOT A DXF FILE: {src}\n\n"
            f"     isotidy reads DXF only.  It cannot read DWG, PDF, DWF or\n"
            f"     images -- DWG is a closed binary format and ezdxf does not\n"
            f"     parse it.\n\n"
            f"     If this is a DWG, convert it first:\n"
            f"       ODA File Converter (free)  DWG -> DXF, batch, headless\n"
            f"       or AutoCAD:  DXFOUT / accoreconsole.exe\n\n"
            f"     Production will not convert at all -- the C# plugin reads\n"
            f"     DWG in-process (roadmap step 6).\n"
        ) from None


def _coverage_banner(scene) -> str | None:
    """State how much of the drawing was actually scored.

    A score over 9% of the annotations is not a score, it is an anecdote.
    Printing this above the numbers keeps the reader from mistaking one for
    the other.
    """
    cov = scene.coverage()
    if cov >= 0.999:
        return None
    missed = sorted(scene.unsupported.items(), key=lambda kv: -kv[1])
    lines = [
        f"  !! PARTIAL COVERAGE -- {cov:.0%} of annotations scored "
        f"({len(scene.labels)} read, {sum(scene.unsupported.values())} skipped)",
        "     Unsupported annotation entities in this drawing:",
    ]
    lines += [f"       {n:5d}  {t}" for t, n in missed]
    lines.append("     The numbers below describe only the annotations "
                 "isotidy can read.")
    return "\n".join(lines)


def _sheet_width(doc) -> float:
    """Width of the drawn sheet, in drawing units."""
    try:
        b = ezbbox.extents(doc.modelspace(), fast=True)
        return float(b.size.x) if b.has_data else 0.0
    except Exception:
        return 0.0


def _text_heights(doc) -> list[float]:
    """Every annotation lettering height in the drawing."""
    hs = []
    for e in doc.modelspace():
        t = e.dxftype()
        if t == "MTEXT":
            hs.append(float(e.dxf.char_height))
        elif t == "TEXT":
            hs.append(float(e.dxf.height))
        elif t == "INSERT":
            hs += [float(a.dxf.height) for a in e.attribs]
    return [h for h in hs if h > 0]


def _print_banner(cfg: Tuning, doc, sheet_w: float) -> str:
    """What the numbers below actually mean, in paper terms."""
    scale = cfg.print_scale(sheet_w)
    lines = [
        f"  PRINT TARGET {cfg.print_target}   "
        f"sheet {sheet_w:.0f} mm wide -> scale {scale:.3f}",
        f"    requiring {cfg.min_printed_gap:.1f} mm of white on the print "
        f"= {cfg.min_printed_gap / scale:.1f} mm on the drawing "
        f"(clearance {cfg.clearance:.2f} per box)",
    ]
    # Lettering height is NOT a placement problem.  No amount of moving text
    # around fixes characters that print below the legibility floor, so the
    # tool says so rather than quietly reporting a clean sheet the engineer
    # still cannot read.
    hs = _text_heights(doc)
    if hs:
        smallest = min(hs)
        printed = smallest * scale
        if printed < ISO3098_MIN_TEXT_MM:
            lines.append(
                f"    !! LEGIBILITY: smallest text {smallest:.2f} mm prints at "
                f"{printed:.2f} mm, under the ISO 3098 floor of "
                f"{ISO3098_MIN_TEXT_MM} mm.")
            lines.append(
                "       Placement cannot fix this -- it is a style/sheet-size "
                "decision (an A3-native iso style would print ~2.5 mm).")
    return "\n".join(lines)


def run(src: Path, dst: Path | None, cfg: Tuning = Tuning(),
        roles=DEFAULT_ROLES, verbose: bool = True) -> dict:
    t0 = time.perf_counter()
    doc, scene = _read(src, cfg, roles)
    _guard(src, scene)

    # Re-derive separation from the print target now that we know the sheet.
    # extract() only used geom_buffer/frame_margin, neither of which depends
    # on print scale, so nothing already read needs redoing.
    sheet_w = _sheet_width(doc)
    cfg = cfg.for_sheet(sheet_w)

    before = detect(scene, cfg)

    stats = solve(scene, cfg)
    after = detect(scene, cfg)

    moved = leaders = 0
    roundtrip = None
    if dst is not None:
        # MUST be the same roles used to extract: apply() writes leaders to
        # roles.leader, and extract() skips that layer to stay idempotent.
        # Mismatch them and our own leaders come back as obstacles next run.
        moved, leaders = apply(doc, scene, roles, cfg)
        doc.saveas(dst)
        roundtrip = _verify_roundtrip(dst, roles, cfg, after)
    elapsed = time.perf_counter() - t0

    if verbose:
        print(f"\n{src.name}   {len(scene.labels)} labels\n")
        print(_print_banner(cfg, doc, sheet_w) + "\n")
        banner = _coverage_banner(scene)
        if banner:
            print(banner + "\n")
        # Some labels found, but other text is unmanaged.  Usually the title
        # block -- occasionally a whole annotation class nobody mapped.  Say
        # so rather than quietly scoring a partial drawing.
        if scene.unmapped_text:
            top = sorted(scene.unmapped_text.items(), key=lambda kv: -kv[1])
            shown = ", ".join(f"{lyr} ({n})" for lyr, n in top[:4])
            more = f", +{len(top) - 4} more" if len(top) > 4 else ""
            print(f"  note: unmanaged text on {shown}{more}\n")
        print(before.summary("BEFORE"))
        print()
        print(after.summary("AFTER"))
        print(f"\n    passes {stats.passes}   labels moved {stats.moved}"
              f"   leaders {leaders}")
        print(f"    search cost {stats.cost_before:,.0f} -> "
              f"{stats.cost_after:,.0f}")
        _verdict(before, after, scene.coverage(), cfg.print_target)
        if roundtrip is not None:
            print(f"    {roundtrip}")
        print(f"\n    {elapsed * 1000:.0f} ms")
        if dst is not None:
            print(f"    wrote {dst}")

    return {
        "before": before, "after": after, "stats": stats,
        "moved": moved, "leaders": leaders, "seconds": elapsed,
    }


def _verify_roundtrip(dst: Path, roles, cfg: Tuning, expected) -> str:
    """Re-read what we just wrote and re-score it.

    The in-memory result is a claim; the file on disk is the product.  They
    diverge for real reasons -- a write-back that silently failed, leaders
    landing on a layer the extractor does not recognise as ours -- and every
    one of those bugs is invisible unless you read the file back.
    """
    try:
        _doc, scene = extract(dst, roles, cfg)
        got = detect(scene, cfg)
    except Exception as exc:
        return f"ROUND-TRIP: FAILED TO RE-READ OUTPUT -- {exc}"
    d_area = abs(got.overlap_area - expected.overlap_area)
    if d_area < 1e-6 and len(got.collisions) == len(expected.collisions):
        return "ROUND-TRIP: output re-scores identically -- verified"
    # Leaders are obstacles, and write-back re-lands the moved balloons'
    # leaders -- so the file's leader paths legitimately differ from the ones
    # the solver scored against.  Small drift in that direction is physics,
    # not a bug, and the FILE is the authoritative number either way.  Only a
    # file that is materially WORSE than the claim is an error.
    if got.overlap_area <= expected.overlap_area + 0.5:
        return (f"ROUND-TRIP: file scores {got.overlap_area:.2f} mm2 "
                f"(in-memory {expected.overlap_area:.2f}) -- drift from "
                f"re-landed leaders; the file score is the product")
    return (f"ROUND-TRIP MISMATCH: file scores "
            f"{len(got.collisions)} collisions / {got.overlap_area:.2f} mm2, "
            f"expected {len(expected.collisions)} / "
            f"{expected.overlap_area:.2f} mm2 -- the written drawing is "
            f"worse than the solve claimed")


def _verdict(before, after, coverage: float = 1.0, target: str = "A3") -> None:
    """State the result as a percentage, and never round a residual to zero.

    The verdict names the PAPER it is true for.  "Clean" on its own invites
    the reader to assume it means clean at whatever size they happen to print,
    which is the assumption that produced the original complaint.
    """
    b, a = before.overlap_area, after.overlap_area
    pct = 100.0 * (b - a) / b if b else 0.0
    print(f"\n    OVERLAP REDUCED {pct:.1f}%   "
          f"({b:.2f} -> {a:.2f} mm2)")
    residual = len(after.collisions)
    if residual:
        kinds = {}
        for c in after.collisions:
            kinds[c.kind] = kinds.get(c.kind, 0) + 1
        detail = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
        print(f"    RESIDUAL: {residual} collisions remain ({detail})")
    elif coverage < 0.999:
        # The single most dangerous sentence this tool could print is "drawing
        # is clean" about a drawing it only partly read.  It is not allowed to.
        print(f"    RESIDUAL: none IN THE {coverage:.0%} SCORED "
              f"-- NOT a clean drawing, coverage is incomplete")
    else:
        print(f"    RESIDUAL: none -- clean at {target}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("src", type=Path)
    ap.add_argument("dst", type=Path, nargs="?")
    ap.add_argument("--dry-run", action="store_true",
                    help="score and solve but write nothing")
    ap.add_argument("--site", default="default", choices=sorted(SITES),
                    help="layer/block scheme for this client's ISOGEN style")
    args = ap.parse_args()
    try:
        run(args.src, None if args.dry_run else args.dst,
            roles=SITES[args.site])
    except (ConfigMismatch, UnsupportedFormat) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2)      # non-zero: a batch run must not swallow this


if __name__ == "__main__":
    main()
