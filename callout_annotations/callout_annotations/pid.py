"""
P&ID PDF -> line names.

The binder is a vector PDF (exported from CAD), so every string on every
page is in the text layer and pypdfium2 hands it over verbatim.  We keep
the strings that match the site's line-name grammar and remember which
sheets each one appears on.  One line appears on several sheets
(continuation arrows, off-page connectors); it is still one line.

A scanned PDF would need OCR first -- read_pid() reports zero names on a
page with no text layer, and the CLI refuses to proceed on an empty index
rather than silently annotate nothing.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium

from .config import JP1071, Site


@dataclass(frozen=True)
class LineName:
    """One full line name from the P&ID, split into its fields."""

    text: str          # verbatim, e.g. 217-209-080-WCG-SFSA-NN
    area: str
    seq: str           # the join key to the 3D model's LineNumberTag
    dn: int            # nominal size, mm
    service: str
    spec: str
    suffix: str        # insulation/tracing code; may be ""
    pages: tuple[int, ...] = ()

    @property
    def fields(self) -> tuple[str, ...]:
        return (self.area, self.seq, f"{self.dn:03d}", self.service,
                self.spec, self.suffix)


@dataclass
class PidIndex:
    names: list[LineName]
    by_seq: dict[str, list[LineName]]
    sheets: dict[int, str]           # page number -> sheet number
    pages_without_text: list[int]

    @property
    def duplicate_seqs(self) -> dict[str, list[LineName]]:
        """Sequence numbers the P&ID itself names more than one way.

        Two causes seen on JP1071: a size change within the line
        (214-310-050 and 214-310-080 -- legitimate) and a suffix
        inconsistency between sheets (217-247-...-WE on one, -WN on
        another -- a P&ID error).  Both are worth telling the engineer.
        """
        return {s: v for s, v in self.by_seq.items() if len(v) > 1}


def parse_line_name(text: str, site: Site = JP1071) -> LineName | None:
    m = site.pid_line_re.fullmatch(text.strip())
    if not m:
        return None
    return LineName(text=m.group(0), area=m.group("area"), seq=m.group("seq"),
                    dn=int(m.group("dn")), service=m.group("service"),
                    spec=m.group("spec"), suffix=m.group("suffix") or "")


def read_pid(pdf_path: Path | str, site: Site = JP1071) -> PidIndex:
    pdf = pdfium.PdfDocument(str(pdf_path))
    hits: dict[str, set[int]] = defaultdict(set)
    sheets: dict[int, str] = {}
    empty: list[int] = []
    for i in range(len(pdf)):
        page_no = i + 1
        text = pdf[i].get_textpage().get_text_range()
        if not text.strip():
            empty.append(page_no)
            continue
        sm = site.pid_sheet_re.search(text)
        sheets[page_no] = sm.group(0) if sm else "?"
        for m in site.pid_line_re.finditer(text):
            hits[m.group(0)].add(page_no)

    names: list[LineName] = []
    by_seq: dict[str, list[LineName]] = defaultdict(list)
    for text in sorted(hits):
        base = parse_line_name(text, site)
        if base is None:
            continue
        ln = LineName(**{**base.__dict__, "pages": tuple(sorted(hits[text]))})
        names.append(ln)
        by_seq[ln.seq].append(ln)
    return PidIndex(names=names, by_seq=dict(by_seq), sheets=sheets,
                    pages_without_text=empty)
