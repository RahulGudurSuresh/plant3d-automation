"""
Join the 3D model's line tags to the P&ID's full line names.

The join key is the 3-digit sequence number: tag 209M01 <-> P&ID
217-209-080-WCG-SFSA-NN.  The tag's suffix (M01 = module 01, L0401 = a
branch) is Plant 3D bookkeeping the P&ID never sees.

Four outcomes, and the drawing must show which one it got:

  unique     one P&ID name for the sequence.            -> callout
  by-size    two names, sizes differ, the linework's OD
             agrees with exactly one.                    -> callout
  ambiguous  two names, cannot tell them apart.          -> REVIEW callout
             The label shows the agreeing fields and
             lists the differing ones as (A|B).
  missing    sequence not in the P&ID at all.            -> REVIEW callout
             The label is the tag plus "NOT IN P&ID".

Never guess silently: an ambiguous line gets a red label a human has to
clear, not a coin flip that looks like a fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import JP1071, Site
from .pid import LineName, PidIndex


@dataclass
class Match:
    tag: str
    seq: str | None
    candidates: list[LineName]
    resolved: LineName | None
    status: str                       # unique | by-size | ambiguous | missing
    est_dn: int | None = None
    note: str = ""

    @property
    def review(self) -> bool:
        return self.status in ("ambiguous", "missing")

    @property
    def label(self) -> str:
        """What goes on the drawing.

        Resolved lines get the P&ID name verbatim.  Review labels are kept
        SHORT on purpose: the first cut printed every differing field as
        (A|B) and produced 47-character red strings that covered more pipe
        than they explained.  The tag plus the candidates' area-seq-size
        is enough to find the row in report.csv, which has the rest.
        """
        if self.resolved is not None:
            return self.resolved.text
        if self.status == "missing":
            return f"{self.tag}  NOT IN P&ID"
        alts = " / ".join(f"{c.area}-{c.seq}-{c.dn:03d}" for c in self.candidates)
        return f"{self.tag} ?  {alts}"

    @property
    def size_check(self) -> str:
        """Does the linework's size agree with the P&ID's?  Report only."""
        if self.resolved is None or self.est_dn is None:
            return ""
        return "ok" if self.resolved.dn == self.est_dn else \
            f"P&ID {self.resolved.dn:03d} vs model ~{self.est_dn:03d}"


def merged_label(cands: list[LineName]) -> str:
    """Fields the candidates agree on verbatim; the rest as (A|B)."""
    cols = list(zip(*(c.fields for c in cands)))
    out = []
    for col in cols:
        vals = sorted(set(col))
        if vals == [""]:
            continue
        out.append(vals[0] if len(vals) == 1 else "(" + "|".join(v or "-" for v in vals) + ")")
    return "-".join(out)


def match_tag(tag: str, pid: PidIndex, est_dn: int | None = None,
              site: Site = JP1071) -> Match:
    m = site.tag_re.match(tag)
    seq = m.group("seq") if m else None
    cands = list(pid.by_seq.get(seq, [])) if seq else []
    if not cands:
        return Match(tag, seq, [], None, "missing", est_dn)
    if len(cands) == 1:
        return Match(tag, seq, cands, cands[0], "unique", est_dn)

    sizes = {c.dn for c in cands}
    if est_dn is not None and len(sizes) > 1:
        hit = [c for c in cands if c.dn == est_dn]
        if len(hit) == 1:
            return Match(tag, seq, cands, hit[0], "by-size", est_dn,
                         note=f"linework OD matches DN{est_dn}")
    return Match(tag, seq, cands, None, "ambiguous", est_dn,
                 note="P&ID names " + " / ".join(c.text for c in cands))


def match_all(tags: list[str], pid: PidIndex, est_dn: dict[str, int | None],
              site: Site = JP1071) -> dict[str, Match]:
    return {t: match_tag(t, pid, est_dn.get(t), site) for t in tags}
