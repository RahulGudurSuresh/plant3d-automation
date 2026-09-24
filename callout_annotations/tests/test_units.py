"""
Unit tests: the grammar, the join, the view frames.  No CAD files needed.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ezdxf.math import Vec3

from callout_annotations.config import JP1071, OD_MM
from callout_annotations.match import match_tag
from callout_annotations.ortho import ViewFrame, arbitrary_axes
from callout_annotations.pid import LineName, PidIndex, parse_line_name
from callout_annotations.place import _rect, _upright


def _ln(text, pages=(1,)):
    ln = parse_line_name(text)
    assert ln is not None, text
    return LineName(**{**ln.__dict__, "pages": pages})


def _pid(*names):
    by = {}
    for n in names:
        by.setdefault(n.seq, []).append(n)
    return PidIndex(names=list(names), by_seq=by, sheets={}, pages_without_text=[])


# -- grammar ---------------------------------------------------------------
def test_pid_grammar_full_and_no_suffix():
    ln = parse_line_name("217-209-080-WCG-SFSA-NN")
    assert (ln.area, ln.seq, ln.dn, ln.service, ln.spec, ln.suffix) == \
        ("217", "209", 80, "WCG", "SFSA", "NN")
    ln = parse_line_name("217-315-025-STL-CBUA")
    assert ln.suffix == "" and ln.spec == "CBUA"
    assert parse_line_name("A13-E-214-24-PX-PID-2001") is None
    assert parse_line_name("MV-214030VPL") is None


def test_tag_grammar():
    for t in ("209M01", "209L0401", "412", "227L53", "028LL"):
        m = JP1071.tag_re.match(t)
        assert m and m.group("seq") == t[:3], t
    assert JP1071.tag_re.match("CENTER") is None
    assert JP1071.tag_re.match("Pipe Support") is None


def test_view_layer_split():
    m = JP1071.view_layer_re.match("Front View-209M01")
    assert (m.group("view"), m.group("rest")) == ("Front View", "209M01")
    m = JP1071.view_layer_re.match("Right View2-065 - Vendor Items")
    assert (m.group("view"), m.group("rest")) == ("Right View2", "065 - Vendor Items")
    m = JP1071.view_layer_re.match("SW Isometric View-CENTER")
    assert m.group("view") == "SW Isometric View"
    assert JP1071.view_layer_re.match("Defpoints") is None


# -- join ------------------------------------------------------------------
def test_match_unique_and_missing():
    pid = _pid(_ln("217-209-080-WCG-SFSA-NN"))
    m = match_tag("209M01", pid)
    assert m.status == "unique" and m.label == "217-209-080-WCG-SFSA-NN" and not m.review
    m = match_tag("808", pid)
    assert m.status == "missing" and m.review and "NOT IN P&ID" in m.label


def test_match_by_size_and_ambiguous():
    pid = _pid(_ln("214-540-080-WCO-CBSA-WN"), _ln("217-540-050-LCO-SFSA-WN"))
    m = match_tag("540M02", pid, est_dn=80)
    assert m.status == "by-size" and m.resolved.text == "214-540-080-WCO-CBSA-WN"
    m = match_tag("540L0201", pid, est_dn=None)
    assert m.status == "ambiguous" and m.review
    assert m.label.startswith("540L0201 ?") and "214-540-080" in m.label and "217-540-050" in m.label
    # same size on both sides: size cannot decide, must stay ambiguous
    pid2 = _pid(_ln("217-247-040-LCC-UCSA-WE"), _ln("217-247-040-LCC-UCSA-WN"))
    assert match_tag("247M01", pid2, est_dn=40).status == "ambiguous"
    # a size that matches neither candidate must not pick one
    assert match_tag("540M02", pid, est_dn=25).status == "ambiguous"


def test_size_check_reports_disagreement():
    pid = _pid(_ln("217-233-040-SCC-UCSB-WN"))
    assert match_tag("233M02", pid, est_dn=40).size_check == "ok"
    assert "vs model" in match_tag("233M02", pid, est_dn=25).size_check


# -- frames ----------------------------------------------------------------
def test_arbitrary_axes_match_autocad_dcs():
    # (normal toward viewer) -> (DCS x, DCS y), validated on JP1071 against
    # every viewport's view_center_point.
    cases = {
        (0, 0, 1): ((1, 0, 0), (0, 1, 0)),      # plan
        (1, 0, 0): ((0, 1, 0), (0, 0, 1)),      # right
        (-1, 0, 0): ((0, -1, 0), (0, 0, 1)),    # left (mirrored)
        (0, 1, 0): ((-1, 0, 0), (0, 0, 1)),     # back
        (0, -1, 0): ((1, 0, 0), (0, 0, 1)),     # front sections
    }
    for n, (u, v) in cases.items():
        ax, ay = arbitrary_axes(Vec3(n))
        assert ax.isclose(Vec3(u)) and ay.isclose(Vec3(v)), n


def test_frame_roundtrip_and_text_direction():
    n = Vec3(-1, 0, 0)
    u, v = arbitrary_axes(n)
    fr = ViewFrame("Left View", n, u, v, plane=0.0, scale=30)
    p = Vec3(0, 27301.7, 5915.0)
    x, y = fr.to2d(p)
    assert (x, y) == (-27301.7, 5915.0)
    assert fr.to_wcs(x, y).isclose(p)
    assert fr.text_height == 75
    assert fr.direction_wcs(0.0).isclose(u)
    assert fr.direction_wcs(math.pi / 2).isclose(v)


# -- placement geometry ----------------------------------------------------
def test_upright_never_reads_upside_down():
    for deg in (0, 45, 90, 135, 180, -135, -90, -45, 270):
        a = math.degrees(_upright(math.radians(deg)))
        assert -90 < a <= 90, deg


def test_rect_orientation_and_pad():
    r = _rect((0, 0), 0.0, 10, 2, pad=0)
    assert r.bounds == (0, 0, 10, 2)
    r = _rect((0, 0), math.pi / 2, 10, 2, pad=0)
    x0, y0, x1, y1 = r.bounds
    assert abs(x0 + 2) < 1e-9 and abs(y1 - 10) < 1e-9
    assert abs(_rect((0, 0), 0.0, 10, 2, pad=1).area - 12 * 4) < 1e-9
