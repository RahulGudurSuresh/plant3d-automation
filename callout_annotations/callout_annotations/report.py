"""
The report: what was matched, what was placed, what a human must look at.

Written as CSV (one row per tag) and a short Markdown summary.  The CSV is
the thing the piping lead opens; the drawing is the thing they check it
against.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from .match import Match
from .ortho import Ortho
from .pid import PidIndex
from .place import Callout

COLUMNS = ["tag", "seq", "status", "label", "pid_candidates", "pid_pages",
           "est_dn", "size_check", "views", "labelled_in", "skipped_in",
           "callouts", "forced", "named_via", "note"]


def rows(ortho: Ortho, matches: dict[str, Match], callouts: list[Callout]) -> list[dict]:
    per_tag = Counter(c.tag for c in callouts)
    views_labelled: dict[str, set[str]] = {}
    forced: set[str] = set()
    for c in callouts:
        views_labelled.setdefault(c.tag, set()).add(c.view)
        if c.forced:
            forced.add(c.tag)
    # A branch tag (152L0201) is named by its line's callout (152M02):
    # same P&ID name, one callout.  Say so instead of "0 callouts".
    named_by: dict[str, set[str]] = {}
    for c in callouts:
        named_by.setdefault(c.text, set()).add(c.tag)
    out = []
    for tag in ortho.tags():
        m = matches[tag]
        present = ortho.views_for(tag)
        lab = sorted(views_labelled.get(tag, set()))
        via = sorted(named_by.get(m.label, set()) - {tag})
        out.append({
            "tag": tag, "seq": m.seq or "", "status": m.status, "label": m.label,
            "pid_candidates": " / ".join(c.text for c in m.candidates),
            "pid_pages": " ".join(
                ",".join(str(p) for p in c.pages) for c in m.candidates),
            "est_dn": m.est_dn if m.est_dn is not None else "",
            "size_check": m.size_check,
            "views": "; ".join(present),
            "labelled_in": "; ".join(lab),
            "skipped_in": "; ".join(v for v in present if v not in lab),
            "callouts": per_tag.get(tag, 0),
            "forced": "yes" if tag in forced else "",
            "named_via": "; ".join(via) if not lab else "",
            "note": m.note,
        })
    return out


def write_csv(path: Path, rws: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rws)


def write_markdown(path: Path, ortho: Ortho, pid: PidIndex,
                   matches: dict[str, Match], callouts: list[Callout],
                   rws: list[dict], src_dwg: str, src_pdf: str) -> None:
    st = Counter(m.status for m in matches.values())
    per_view = Counter(c.view for c in callouts)
    L = []
    L.append("# callout_annotations report\n")
    L.append(f"- DWG: `{src_dwg}`")
    L.append(f"- P&ID: `{src_pdf}` -- {len(pid.sheets)} pages, "
             f"{len(pid.names)} distinct line names"
             + (f", pages without text: {pid.pages_without_text}" if pid.pages_without_text else ""))
    L.append(f"- Views: {', '.join(sorted(ortho.frames))}")
    L.append(f"- Line tags in the ortho: {len(matches)}  ->  "
             f"unique {st['unique']}, by-size {st['by-size']}, "
             f"ambiguous {st['ambiguous']}, not in P&ID {st['missing']}")
    L.append(f"- Callouts written: {len(callouts)}\n")

    L.append("## Per view: lines present vs. named there\n")
    L.append("A line is named in a view only where the callout is clean "
             "(no overlap, no linework under the text, short leader). "
             "Skipped lines are named in another view -- see `labelled_in` "
             "in report.csv.\n")
    L.append("| view | lines present | named here | callouts |")
    L.append("|---|---|---|---|")
    for v in sorted(ortho.frames):
        present = {t for (vv, t) in ortho.runs if vv == v}
        named = {c.tag for c in callouts if c.view == v}
        L.append(f"| {v} | {len(present)} | {len(named)} | {per_view.get(v, 0)} |")
    L.append("")

    forced = [r for r in rws if r["forced"]]
    if forced:
        L.append("## Forced placements (clean nowhere, placed where it hurts least)\n")
        for r in forced:
            L.append(f"- **{r['tag']}** in {r['labelled_in']}")
        L.append("")

    rev = [r for r in rws if r["status"] in ("ambiguous", "missing")]
    L.append("## Needs a human (red labels in the drawing)\n")
    if not rev:
        L.append("none\n")
    for r in rev:
        L.append(f"- **{r['tag']}** -- {r['status']}: {r['pid_candidates'] or 'no P&ID line with seq ' + r['seq']}"
                 + (f"  (linework looks like DN{r['est_dn']})" if r['est_dn'] else ""))
    L.append("")

    bys = [r for r in rws if r["status"] == "by-size"]
    if bys:
        L.append("## Resolved by pipe size (worth a glance)\n")
        for r in bys:
            L.append(f"- **{r['tag']}** -> {r['label']}  ({r['note']})")
        L.append("")

    mism = [r for r in rws if r["size_check"] and r["size_check"] != "ok"]
    if mism:
        L.append("## Size disagreement between P&ID and model linework\n")
        L.append("The linework estimate is a heuristic (insulation and supports "
                 "add parallel lines); treat these as prompts, not findings.\n")
        for r in mism:
            L.append(f"- **{r['tag']}** {r['label']}: {r['size_check']}")
        L.append("")

    dups = pid.duplicate_seqs
    if dups:
        L.append("## P&ID names one sequence more than one way\n")
        for seq, names in sorted(dups.items()):
            L.append(f"- {seq}: " + " / ".join(f"{n.text} (p{','.join(map(str, n.pages))})" for n in names))
        L.append("")

    names = {matches[t].label for t in ortho.tags()}
    named = {c.text for c in callouts}
    L.append(f"## Coverage: {len(named)} of {len(names)} P&ID line names carry a callout\n")
    via = [r for r in rws if r["callouts"] == 0 and r["named_via"]]
    if via:
        L.append(f"{len(via)} tags have no callout of their own because a sibling tag "
                 "with the same P&ID name carries it (branches: 152L0201 -> 152M02).\n")
    unplaced = [r for r in rws if r["callouts"] == 0 and not r["named_via"]]
    if unplaced:
        L.append("### Lines with NO callout anywhere\n")
        L.append("No room inside any viewport window without stacking on another label.\n")
        for r in unplaced:
            L.append(f"- {r['tag']} {r['label']} ({r['views']})")
        L.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L), encoding="utf-8")
