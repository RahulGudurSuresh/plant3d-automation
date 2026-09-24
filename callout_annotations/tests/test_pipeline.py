"""
Regression over the real JP1071 inputs (skipped when they are absent).

The DWG->DXF conversion is cached in tests/_cache so the ODA converter runs
once per checkout, not once per test session.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ezdxf

from callout_annotations import match as matching
from callout_annotations import ortho as ortho_mod
from callout_annotations import pid as pid_mod
from callout_annotations import place, writeback
from callout_annotations.config import DEFAULT_TUNING, JP1071, OD_MM

DWG = ROOT / "inputs" / "Plan view.dwg"
PDF = ROOT / "inputs" / "PID-Binder.pdf"
CACHE = ROOT / "tests" / "_cache"

pytestmark = pytest.mark.skipif(not (DWG.exists() and PDF.exists()),
                                reason="real inputs not present")


@pytest.fixture(scope="module")
def pid():
    return pid_mod.read_pid(PDF)


@pytest.fixture(scope="module")
def ortho():
    dxf = CACHE / "PlanView.dxf"
    if not dxf.exists():
        from callout_annotations.oda import convert_file
        convert_file(DWG, dxf)
    return ortho_mod.load(dxf)


@pytest.fixture(scope="module")
def result(ortho, pid):
    est = {tag: ortho_mod.estimate_tag_size(ortho, tag, OD_MM) for tag in ortho.tags()}
    matches = matching.match_all(ortho.tags(), pid, est)
    callouts = place.plan(ortho, matches, DEFAULT_TUNING)
    return matches, callouts


def test_pid_extraction(pid):
    assert len(pid.sheets) == 22
    assert len(pid.names) == 500
    assert not pid.pages_without_text
    assert "217-209-080-WCG-SFSA-NN" in {n.text for n in pid.names}


def test_ortho_views_and_frames(ortho):
    assert set(ortho.frames) == {"Front View", "Right View", "Left View", "Back View",
                                 "Right View2", "Right View3", "Front View2", "Front View3"}
    fr = ortho.frames
    assert fr["Front View"].n.isclose((0, 0, 1)) and fr["Front View"].scale == pytest.approx(30)
    assert fr["Left View"].n.isclose((-1, 0, 0)) and fr["Right View"].n.isclose((1, 0, 0))
    assert fr["Front View2"].n.isclose((0, -1, 0)) and fr["Front View2"].scale == pytest.approx(20)
    for f in fr.values():
        assert f.window is not None and abs(f.plane) < 1.0
    assert len(ortho.tags()) == 115


def test_join_outcomes(ortho, pid, result):
    matches, _ = result
    st = {t: m.status for t, m in matches.items()}
    assert st["209M01"] == "unique" and matches["209M01"].label == "217-209-080-WCG-SFSA-NN"
    assert st["808"] == "missing" and st["542M02"] == "missing"
    assert st["247M01"] == "ambiguous"          # WE vs WN: size cannot decide
    assert st["540M02"] == "by-size" and matches["540M02"].resolved.dn == 80
    assert st["541M02"] == "by-size" and matches["541M02"].resolved.dn == 50
    assert st["310M02"] == "by-size" and matches["310M02"].resolved.dn == 80


def test_every_pid_line_is_named_somewhere(ortho, result):
    """The rule since 2026-09-17: a line is named ONCE where it is clean,
    not in every view it appears in.  Coverage is per P&ID name (a branch
    tag shares its line's name), and only a line with no room in any
    window may go unnamed -- and there must be very few of those."""
    matches, callouts = result
    names = {matches[t].label for t in ortho.tags()}
    named = {c.text for c in callouts}
    unnamed = sorted(names - named)
    assert len(unnamed) <= 3, unnamed


def test_callouts_are_clean_or_forced(ortho, result):
    """Pass 1 places only clean callouts; anything else is flagged forced."""
    _, callouts = result
    dirty = [c for c in callouts if not c.forced and not c.clean(DEFAULT_TUNING)]
    assert not dirty, [(c.view, c.tag, c.under) for c in dirty]
    assert sum(c.forced for c in callouts) < 0.4 * len(callouts)


def test_callouts_stay_inside_their_window(ortho, result):
    _, callouts = result
    for c in callouts:
        x0, y0, x1, y1 = ortho.frames[c.view].window
        bx0, by0, bx1, by1 = c.box().bounds
        assert bx0 >= x0 - 1e-6 and by0 >= y0 - 1e-6 and bx1 <= x1 + 1e-6 and by1 <= y1 + 1e-6, c


def test_callouts_do_not_stack(ortho, result):
    _, callouts = result
    by_view = {}
    for c in callouts:
        by_view.setdefault(c.view, []).append(c)
    bad = []
    for cs in by_view.values():
        for i, a in enumerate(cs):
            for b in cs[i + 1:]:
                if a.box().intersects(b.box()):
                    bad.append((a.view, a.tag, b.tag))
    assert not bad, bad      # label-on-label is a hard ban now


def test_writeback_and_freeze(ortho, result, tmp_path):
    _, callouts = result
    texts, leaders = writeback.apply(ortho, callouts)
    assert texts == len(callouts) and 0 < leaders <= texts
    out = tmp_path / "out.dxf"
    ortho.doc.saveas(out)
    doc = ezdxf.readfile(out)
    assert len(doc.modelspace().query("MTEXT")) == texts
    assert "Autodesk_PNP" in doc.rootdict
    for vp in doc.layouts.get("Title Block").query("VIEWPORT"):
        if vp.dxf.id == 1:
            continue
        frozen = set(vp.frozen_layers)
        visible = {l.dxf.name.replace("-CALLOUT-REVIEW", "").replace("-CALLOUT", "")
                   for l in doc.layers
                   if l.dxf.name.endswith(("-CALLOUT", "-CALLOUT-REVIEW"))
                   and l.dxf.name not in frozen}
        assert len(visible) == 1, (vp.dxf.id, visible)
    # idempotent
    assert writeback.clear(ortho) == texts + leaders
