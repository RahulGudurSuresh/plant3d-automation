"""
Regression tests for extract -> detect -> solve -> write-back.

These encode the properties that must hold for ANY placement the solver
produces, not the specific answer it happens to give today.  Asserting exact
coordinates would freeze the algorithm; asserting invariants lets you replace
greedy hill-climbing with annealing tomorrow and still know it is correct.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from isotidy import Tuning, detect, extract, solve  # noqa: E402
from isotidy.writeback import apply  # noqa: E402

FIXTURE = ROOT / "fixtures" / "iso_congested.dxf"


@pytest.fixture(scope="module")
def solved():
    doc, scene = extract(FIXTURE)
    before = detect(scene)
    stats = solve(scene)
    after = detect(scene)
    return doc, scene, before, after, stats


# --------------------------------------------------------------------------
# The headline claim
# --------------------------------------------------------------------------
def test_fixture_actually_has_the_bug():
    """If the fixture stops reproducing the problem, every other test below
    passes vacuously.  Guard the guard."""
    _doc, scene = extract(FIXTURE)
    report = detect(scene)
    assert len(report.of_kind("label")) >= 10
    assert report.overlap_area > 100.0


def test_no_label_on_label_overlap_remains(solved):
    _doc, _scene, _before, after, _stats = solved
    assert after.of_kind("label") == []


def test_overlap_area_drops_by_at_least_95_percent(solved):
    _doc, _scene, before, after, _stats = solved
    reduction = (before.overlap_area - after.overlap_area) / before.overlap_area
    assert reduction >= 0.95, f"only {reduction:.1%}"


# --------------------------------------------------------------------------
# Hard constraints -- these may never be traded away for a better score
# --------------------------------------------------------------------------
def test_no_label_leaves_the_drawable_region(solved):
    _doc, _scene, _before, after, _stats = solved
    assert after.of_kind("frame") == []


def test_labels_stay_near_their_anchor(solved):
    """A label that wanders across the sheet is unreadable even with a leader."""
    _doc, scene, _before, _after, _stats = solved
    worst = max(l.anchor_distance() for l in scene.labels)
    assert worst < 60.0, f"a label sits {worst:.1f}mm from its anchor"


def test_dimension_text_stays_on_its_dimension_line(solved):
    """Constrained labels may slide and flip, never wander off."""
    _doc, scene, _before, _after, _stats = solved
    for lab in scene.labels:
        if lab.slide is None:
            continue
        dx, dy = lab.delta()
        sx, sy = lab.slide
        # decompose the move into along-line and across-line components
        across = abs(-sy * dx + sx * dy)
        assert across <= 4 * (lab.size[1] + 1.6) + 1e-6, lab.text


def test_text_content_is_never_altered(solved):
    """The tool moves text. It must never rewrite it."""
    _doc, scene, _before, _after, _stats = solved
    _doc2, fresh = extract(FIXTURE)
    assert ([l.text for l in scene.labels] == [l.text for l in fresh.labels])


# --------------------------------------------------------------------------
# Properties an engineer will rely on
# --------------------------------------------------------------------------
def test_solver_is_deterministic():
    """Same drawing in, same drawing out -- every time.

    Non-determinism is why drafters stop trusting automation: rerun the tool,
    get a different sheet, and now you cannot tell your change from its churn.
    """
    runs = []
    for _ in range(2):
        _doc, scene = extract(FIXTURE)
        solve(scene)
        runs.append([l.pos for l in scene.labels])
    assert runs[0] == runs[1]


def test_rerunning_on_the_output_is_stable(tmp_path):
    """Idempotence: a tidied drawing must not be reshuffled by a second pass."""
    doc, scene = extract(FIXTURE)
    solve(scene)
    apply(doc, scene)
    out = tmp_path / "pass1.dxf"
    doc.saveas(out)

    doc2, scene2 = extract(out)
    before2 = detect(scene2)
    solve(scene2)
    after2 = detect(scene2)
    assert after2.overlap_area <= before2.overlap_area + 1e-9
    moved = sum(1 for l in scene2.labels if l.moved())
    assert moved <= 2, f"second pass churned {moved} labels"


def test_unmoved_labels_need_no_leader(solved):
    """Leaders are only drawn for labels that actually travelled."""
    _doc, scene, _before, _after, _stats = solved
    cfg = Tuning()
    for lab in scene.labels:
        if lab.displacement() < cfg.leader_threshold:
            assert lab.displacement() < cfg.leader_threshold


# --------------------------------------------------------------------------
# Anchor inference -- the thing that must work on REAL drawings
# --------------------------------------------------------------------------
def test_anchor_inference_matches_ground_truth():
    """Real ISOGEN output has no XDATA, so production runs infer the anchor
    from nearby geometry.  Measure that fallback against the fixture's truth
    BEFORE trusting it on a live drawing."""
    from isotidy.extract import _infer_anchor, _to_geometry
    import ezdxf
    from isotidy.config import DEFAULT_ROLES

    doc = ezdxf.readfile(FIXTURE)
    msp = doc.modelspace()
    fixed = [g for g in (_to_geometry(e) for e in msp
                         if e.dxf.layer in DEFAULT_ROLES.fixed) if g]

    _doc, scene = extract(FIXTURE)
    errors = []
    for lab in scene.labels:
        guess = _infer_anchor(lab.center(), fixed)
        errors.append(((guess[0] - lab.anchor[0]) ** 2
                       + (guess[1] - lab.anchor[1]) ** 2) ** 0.5)

    within_5mm = sum(1 for e in errors if e <= 5.0) / len(errors)
    # Not a quality bar -- a tripwire.  If this regresses, the fallback broke.
    assert within_5mm >= 0.5, f"only {within_5mm:.0%} of anchors within 5mm"


def test_multiline_attrib_embedded_mtext_follows_balloon(tmp_path):
    """ISOGEN balloons carry multi-line ATTRIBs whose embedded MTEXT is where
    AutoCAD draws the number.  translate() leaves it behind; save_dxf must
    re-pin it (2026-09-17, empty balloons in TrueView)."""
    import ezdxf
    from ezdxf.math import Vec3
    from isotidy.writeback import save_dxf, sync_embedded_attribs

    doc = ezdxf.new("R2018")
    blk = doc.blocks.new("AnnoCircle")
    blk.add_circle((0, 0), 3)
    blk.add_attdef("ID", (0, 0), dxfattribs={"height": 2.5})
    msp = doc.modelspace()
    ins = msp.add_blockref("AnnoCircle", (100, 100))
    a = ins.add_attrib("ID", "15", (100, 100))
    a.dxf.halign, a.dxf.valign = 1, 2
    a.dxf.align_point = Vec3(100, 100)
    m = ezdxf.entities.MText.new(dxfattribs={"insert": Vec3(100, 100), "char_height": 2.5})
    m.text = "15"
    a.set_mtext(m)
    assert a.has_embedded_mtext_entity

    ins.translate(20, -10, 0)
    assert a.dxf.align_point.isclose((120, 90))
    assert a._embedded_mtext.dxf.insert.isclose((100, 100))   # the bug
    assert sync_embedded_attribs(doc) == 1
    assert a._embedded_mtext.dxf.insert.isclose((120, 90))
    assert sync_embedded_attribs(doc) == 0                     # idempotent

    out = tmp_path / "b.dxf"
    ins.translate(0, 5, 0)
    save_dxf(doc, out)
    back = ezdxf.readfile(out).modelspace().query("INSERT")[0].attribs[0]
    assert back._embedded_mtext.dxf.insert.isclose((120, 95))


def test_symbol_hits_tip_beside_support_is_not_exempt():
    """A tip on the pipe at the apex of a support triangle is NOT 'inside'
    the support: a leader running down through the triangle and stem must
    be a hit.  A tip deep inside a gauge circle still is exempt."""
    from shapely.geometry import LineString, Point, box
    from isotidy.leaders import symbol_hits
    # support: triangle apex at (0,0) pointing up, base 6 mm below, stem to -20
    tri = LineString([(0, 0), (-3, -6), (3, -6), (0, 0)])
    stem = LineString([(0, -6), (0, -20)])
    support = ("Support", box(-3, -20, 3, 0), [tri, stem])
    path = LineString([(0, 0), (-1, -30)])            # straight down through it
    assert symbol_hits(path, (0, 0), (-1, -30), [support])
    # gauge: tip at the centre of a 10 mm circle, leader leaves through the rim
    rim = LineString([(5 * __import__("math").cos(a / 20 * 6.2832),
                       5 * __import__("math").sin(a / 20 * 6.2832)) for a in range(21)])
    gauge = ("Instrument", box(-5, -5, 5, 5), [rim])
    path2 = LineString([(0, 0), (30, 30)])
    assert symbol_hits(path2, (0, 0), (30, 30), [gauge]) == []


def test_flatten_entity_sees_hatch_fills():
    import ezdxf
    from isotidy.extract import _flatten_entity
    doc = ezdxf.new("R2018")
    blk = doc.blocks.new("Sup")
    h = blk.add_hatch(color=2)
    h.paths.add_polyline_path([(0, 0), (-3, -6), (3, -6)], is_closed=True)
    ins = doc.modelspace().add_blockref("Sup", (100, 100))
    chains = _flatten_entity(ins)
    assert chains and len(chains[0]) == 4
    assert abs(chains[0][0][0] - 100) < 1e-9 and abs(chains[0][1][1] - 94) < 1e-9


def test_symbol_hits_grazing_and_near_landing():
    from shapely.geometry import LineString, box
    from isotidy.leaders import symbol_hits
    tri = LineString([(0, 0), (-3, -6), (3, -6), (0, 0)])
    support = ("Support", box(-3, -20, 3, 0), [tri, LineString([(0, -6), (0, -20)])])
    # laid exactly along the left edge (direction (-3,-6)), 0.2 mm outside it
    graze = LineString([(0, 0), (-6.2, -12)])
    assert symbol_hits(graze, (0, 0), (-6.2, -12), [support])
    # landing 2 mm OUTSIDE the box is not 'attached': crossing counts
    reducer = ("Reducer", box(10, 10, 20, 20), [LineString([(10, 10), (20, 20)]), LineString([(10, 20), (20, 10)])])
    path = LineString([(25, 5), (8, 22)])
    assert symbol_hits(path, (25, 5), (8, 22), [reducer])
    # landing INSIDE the box is attached: exempt
    path2 = LineString([(25, 5), (15, 15)])
    assert symbol_hits(path2, (25, 5), (15, 15), [reducer]) == []


def test_symbol_hits_landing_beside_support_stem_is_not_attached():
    from shapely.geometry import LineString, box
    from isotidy.leaders import symbol_hits
    tri = LineString([(0, 0), (-3, -6), (3, -6), (0, 0)])
    stem = LineString([(0, -6), (0, -20)])
    support = ("Support", box(-3, -20, 3, 0), [tri, stem])
    # lands at (-2.8, -12): inside the bbox, beside the stem, along the edge
    path = LineString([(0, 0), (-4.3, -10.3)])
    assert symbol_hits(path, (0, 0), (-4.3, -10.3), [support])


def test_symbol_hits_solid_fill_gets_only_a_small_tip_allowance():
    from shapely.geometry import LineString, box
    from isotidy.leaders import symbol_hits
    # filled support triangle, apex at the tip, 7 mm tall
    fill = LineString([(0, 0), (-2.25, -7), (2.25, -7), (0, 0)])
    support = ("Support", box(-2.25, -7, 2.25, 0), [fill])
    # leader leaves almost straight down: inside the fill 2-6 mm from the tip
    path = LineString([(0, 0), (-1, -10)])
    assert symbol_hits(path, (0, 0), (-1, -10), [support])
    # weld dot, tip at centre, radius 1.6: leaving it is pointing
    import math
    dot = LineString([(1.6 * math.cos(a / 24 * 2 * math.pi), 1.6 * math.sin(a / 24 * 2 * math.pi)) for a in range(25)])
    weld = ("Buttweld", box(-1.6, -1.6, 1.6, 1.6), [dot])
    assert symbol_hits(path, (0, 0), (-1, -10), [weld]) == []


def test_symbol_hits_leader_skimming_a_fill_edge():
    from shapely.geometry import LineString, box
    from isotidy.leaders import symbol_hits
    fill = LineString([(0, 0), (-2.25, -7), (2.25, -7), (0, 0)])
    support = ("Support", box(-2.25, -7, 2.25, 0), [fill])
    # the real case: 0.3 mm outside the left edge, diverging slowly
    path = LineString([(0, 0), (-4.3, -10.3)])
    assert symbol_hits(path, (0, 0), (-4.3, -10.3), [support])
