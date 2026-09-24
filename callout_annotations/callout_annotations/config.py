"""
Site configuration: the line-number grammar, layer naming, and sizing.

Everything that is a *convention of one project* lives here, so a second
client is a new `Site` instance and not a code change.  Nothing else in the
package hardcodes a regex or a layer name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: ISO 3098 minimum lettering height on the finished print, mm.  Same bar
#: isotidy enforces.  Callout text is this tall on paper, whatever the view
#: scale.
TEXT_PAPER_MM = 2.5

#: Outside diameter per nominal size (DN, mm), EN 10220 / ASME B36.10.  Used
#: only as a tie-breaker when a sequence number maps to two P&ID names that
#: differ in size -- the ortho linework shows the pipe's OD as two parallel
#: lines, and that spacing picks the candidate.
OD_MM: dict[int, float] = {
    15: 21.3, 20: 26.9, 25: 33.7, 32: 42.4, 40: 48.3, 50: 60.3, 65: 76.1,
    80: 88.9, 100: 114.3, 125: 139.7, 150: 168.3, 200: 219.1, 250: 273.0,
    300: 323.9, 350: 355.6, 400: 406.4, 450: 457.0, 500: 508.0, 600: 610.0,
}


@dataclass(frozen=True)
class Site:
    """One project's naming conventions."""

    #: Full P&ID line name.  Named groups are mandatory: seq is the join
    #: key, dn is used for size tie-breaks, the rest are carried verbatim.
    pid_line_re: re.Pattern = field(default=re.compile(
        r"\b(?P<area>\d{3})-(?P<seq>\d{3})-(?P<dn>\d{3})"
        r"-(?P<service>[A-Z]{2,4})-(?P<spec>[A-Z]{2,4})"
        r"(?:-(?P<suffix>[A-Z]{2,3}))?\b"))

    #: Sheet number printed on each P&ID page (report only).
    pid_sheet_re: re.Pattern = field(default=re.compile(
        r"\b[A-Z]\d{2}-[A-Z]-\d{3}-\d{2}-PX-PID-\d{4}\b"))

    #: Plant 3D LineNumberTag as it appears in the ortho layer name.  JP1071
    #: tags are <seq><M|L><digits> (209M01, 209L0401), bare <seq> (412) or
    #: <seq><letters> (028LL).  Group `seq` is the join key.
    tag_re: re.Pattern = field(default=re.compile(
        r"^(?P<seq>\d{3})(?P<rest>[A-Z]\d{2,4}|[A-Z]{2})?$"))

    #: Ortho layer = "<view name>-<3D model layer>".  Plant 3D names views
    #: "Front View", "Right View2", "SW Isometric View" ...
    view_layer_re: re.Pattern = field(default=re.compile(
        r"^(?P<view>.+? View\d*)-(?P<rest>.+)$"))

    #: Layers the tool writes to, one pair per view: "<view>-<suffix>".
    callout_layer: str = "CALLOUT"
    review_layer: str = "CALLOUT-REVIEW"
    callout_color: int = 7      # ACI white/black -- reads like other text
    review_color: int = 1       # ACI red -- a human must look at these

    #: How much a curve under a label costs, by the 3D layer it came from.
    #: Ortho linework is wall-to-wall on a module GA: the plan's best spot
    #: for any label had 20-60 text-heights of curves under it.  What a
    #: reader minds is text over a PIPE or a VALVE, not over grating, so
    #: the score weighs by kind.  (None = the tag layers = pipe.)
    #: Names not listed fall to `default_weight` (equipment, vendor items,
    #: supports, trays).
    linework_weights: tuple[tuple[str, float], ...] = (
        ("CENTER", 0.1), ("HIDDEN", 0.1), ("HATCH", 0.1), ("INSULATION", 0.2),
        ("0", 0.3), ("000-CONSTRUCTION", 0.3),
    )
    steel_weight: float = 0.3        # purely numeric layer names: 9, 11, 99, 6, 2
    pipe_weight: float = 1.0
    default_weight: float = 0.8

    def curve_weight(self, rest: str) -> float:
        if self.tag_re.match(rest):
            return self.pipe_weight
        for name, w in self.linework_weights:
            if rest == name:
                return w
        if rest.isdigit():
            return self.steel_weight
        return self.default_weight

    #: Text style for the callouts.  Falls back to Standard if absent.
    #: JP1071's Standard is txt.shx -- wide and crude; their 'arial' style
    #: is what the title block uses and reads far better at 2.5 mm.
    text_style: str = "arial"


JP1071 = Site()

SITES: dict[str, Site] = {"jp1071": JP1071, "default": JP1071}


@dataclass(frozen=True)
class Tuning:
    """Placement knobs.  Distances are multiples of the text height h."""

    text_paper_mm: float = TEXT_PAPER_MM
    #: Average glyph advance as a fraction of cap height.  MEASURED with
    #: ezdxf's font engine on the real labels: 0.77-0.81 for Arial-like
    #: fonts, 0.95 for txt.shx.  The first guess (0.72) was 10-30 % short
    #: and every "clean" label collided on paper.  Generous is right: an
    #: over-estimate leaves a gap, an under-estimate leaves text on text.
    char_aspect: float = 0.85
    #: Clearance the label keeps from linework and other labels.
    pad_h: float = 0.35

    #: Pipe runs shorter than this (in h) are not worth a callout -- a stub
    #: in a section view gets its name from the plan.
    min_run_h: float = 4.0
    #: Most callouts one tag gets in one view (largest runs first).  Three
    #: printed 217-350-050 three times in one 170 mm elevation; two is
    #: enough for a line that leaves the frame and comes back.
    max_per_view: int = 2
    #: A second run of the same tag in one view only gets its own callout
    #: if it is at least this fraction of the longest run's linework.
    #: Without it every elbow that re-enters the frame was labelled and
    #: the plan view carried 104 callouts for 60 lines.
    secondary_run_ratio: float = 0.5
    #: Two fragments of the same tag closer than this (in h) are one run.
    cluster_gap_h: float = 3.0

    #: Perpendicular offsets tried from the run, in h, outermost last.
    offsets_h: tuple[float, ...] = (1.0, 2.0, 3.5, 5.0, 7.5, 10.0, 14.0)
    #: Along-run shifts tried, as a fraction of the run length.
    shifts: tuple[float, ...] = (0.0, 0.25, -0.25, 0.5, -0.5, 0.75, -0.75)
    #: Radial distances (in h) for horizontal, leadered fallbacks.
    radial_h: tuple[float, ...] = (3.0, 5.0, 8.0, 12.0, 16.0, 20.0, 24.0)

    #: A label further than this (in h) from its run gets a leader.
    leader_threshold_h: float = 1.2

    #: The quality gate (place.Callout.clean).  A callout is placed in a
    #: view only if it overlaps no other callout, has at most this much
    #: linework under it (in text heights -- 1.0 is one thin line crossing
    #: the box), and stands within this many text heights of its pipe.
    #: Anything else is skipped in that view; the line is named where it
    #: IS clean, or forced into the least-bad view if nowhere is.
    max_under_h: float = 6.0         # WEIGHTED by Site.curve_weight
    max_leader_h: float = 12.0       # user (2026-09-17): leaders may be long

    #: Never rotate text.  A riser's label sits beside it, horizontal,
    #: with a leader.  User directive 2026-09-17 ("all annotations
    #: horizontal, change the leader length if you must").
    horizontal_only: bool = True

    #: MTEXT background mask: blank the linework behind the text (AutoCAD
    #: "use drawing background").  The standard trick on a crowded GA, and
    #: with the weighted cost keeping labels off pipes and equipment, what
    #: the mask hides is grating.  `mask_scale` is the border factor.
    mask: bool = True
    mask_scale: float = 1.15

    # Cost weights.  Units: linework length inside the box is in h,
    # overlap areas in h^2, distances in h.  Read as a preference order:
    # never leave the window; covering another callout is far worse than
    # covering linework (a label on a label is two lost labels); a long
    # leader is worse than a short one but better than either.
    w_linework: float = 1.0
    w_label_overlap: float = 20.0
    w_distance: float = 0.4
    w_leader: float = 1.5
    w_not_parallel: float = 1.0
    w_outside: float = 1e6


DEFAULT_TUNING = Tuning()
