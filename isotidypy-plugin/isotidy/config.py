"""
Configuration: layer roles and solver tuning.

Layer names are NEVER constants elsewhere in this package.  Every site
customises the ISOGEN style's "Default Model.dwg", so a hardcoded layer name
is a tool that works at exactly one company.  Everything downstream asks
LayerRoles what a layer means.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

#: Short-edge-up sheet widths, mm.  Used to work out how much a drawing
#: shrinks between the sheet ISOGEN drew and the paper the engineer holds.
PAPER_WIDTH_MM = {"A0": 1189.0, "A1": 841.0, "A2": 594.0,
                  "A3": 420.0, "A4": 297.0}

#: ISO 3098 minimum lettering height on the FINISHED print.  Below this,
#: text is out of spec however well it is spaced.
ISO3098_MIN_TEXT_MM = 2.5

#: Dimension drift: text further than max(FLOOR, FACTOR x the sheet's own
#: median offset) from its dimension line is a defect.  ONE definition,
#: shared by the auditor (tools/mess_map.py category D) and the extractor
#: (Label.slide_limit) -- the solver must not be able to place text where
#: the audit will flag it.
DIM_DRIFT_FLOOR = 3.0
DIM_DRIFT_FACTOR = 2.0


@dataclass(frozen=True)
class LayerRoles:
    """What each layer *means*, independent of what it is called."""

    movable: dict[str, str]        # layer -> class name (for reporting/colour)
    fixed: frozenset[str]          # geometry the solver must not touch
    frame: frozenset[str]          # sheet border, title block, BOM -- hard bounds
    constrained: frozenset[str]    # text that may only slide along its own line
    leader: str                    # layer generated leaders are written to

    #: Block-name prefixes whose attributed INSERTs are movable annotations
    #: (item balloons).  ISOGEN names them AnnoCircle, AnnoGroupThreeCircle,
    #: AnnoRectWide2 ... -- a prefix survives new balloon variants, an
    #: explicit list does not.  'title block' deliberately does not match.
    balloon_prefixes: tuple[str, ...] = ()

    #: Read DIMENSION entities as movable dimension text.
    read_dimensions: bool = False

    def label_class(self, layer: str) -> str | None:
        return self.movable.get(layer)

    def is_movable(self, layer: str) -> bool:
        return layer in self.movable

    def is_balloon(self, block_name: str) -> bool:
        return bool(self.balloon_prefixes) and \
            block_name.startswith(self.balloon_prefixes)


#: Matches the synthetic fixture.  Real projects override this.
DEFAULT_ROLES = LayerRoles(
    movable={
        "ISO_ANNOTATION": "annotation",
        "ISO_WELD": "weld",
        "ISO_DIMENSION": "dimension",
    },
    fixed=frozenset({"ISO_PIPE", "ISO_SYMBOL"}),
    frame=frozenset({"ISO_FRAME"}),
    constrained=frozenset({"ISO_DIMENSION"}),
    leader="ISO_LEADER",
)

#: Project JP1071 -- measured from the delivered
#: DWGs with tools/survey.py.  This is what a real site config looks like:
#: layer names from their ISOGEN style, plus the entity kinds their style
#: actually emits.
JP1071_ROLES = LayerRoles(
    movable={
        "Annotation": "annotation",
        "Continuation": "continuation",
        "Dimension": "dimension",
    },
    fixed=frozenset({
        "Pipe", "Valve", "Symbol", "Weld", "Supports",
        "Instruments", "Insulation Line", "Fitting", "Flange",
        # Seen 2026-09-17 on the "difficult_iso" sheet: drawn linework a
        # label must not sit on, and a leader must not cross.
        "Heat Tracing Line", "Skew Box",
    }),
    frame=frozenset({"0"}),          # title block + both BOM tables live here
    constrained=frozenset({"Dimension"}),
    leader="ISOTIDY_LEADER",         # our own layer -- never one of theirs
    balloon_prefixes=("Anno",),
    read_dimensions=True,
)


@dataclass(frozen=True)
class Tuning:
    """Solver knobs.  All distances in drawing units (mm on an A3 sheet).

    The weights are a *preference ordering* expressed as numbers, not physical
    quantities.  Read them as: never leave the sheet (w_frame dwarfs
    everything); overlapping another label is worse than sitting on a pipe;
    both are far worse than drifting a few mm from the anchor; and all else
    equal, don't move at all (w_stay).
    """

    # --- geometry ------------------------------------------------------
    # Breathing room added around every label box, per side.  Raised
    # 0.6 -> 0.8 on 2026-08-18 by user directive after reviewing 545M05:
    # callouts that cleared numerically still read as crowded on the sheet.
    # It pads BOTH boxes, so the minimum gap between two labels is twice this
    # -- 1.6 mm at A1, 0.8 mm on the A3 print.
    clearance: float = 0.8
    geom_buffer: float = 0.35     # half the apparent line width of geometry
    ink_halfwidth: float = 0.35   # same, for Scene.ink (real drawn curves)
    # A move may leave at most this much MORE drawn ink under a label than
    # ISOGEN did.  Not a taste parameter -- it is the "never make a spot
    # worse" rule, and 0.5 mm2 is float/rounding slack, not licence.  With no
    # guard, 545M05's 4/6/7 callout moved 37 mm from 5.83 mm2 of ink onto
    # 22.05 mm2 of dimension grid while every score reported an improvement.
    ink_tolerance: float = 0.5
    # A move is judged by WHERE IT LANDS, not by how far it went.  The brake
    # is meant to be a neighbourhood test: destination no busier than home,
    # measured as drawn ink within a halo plus other labels touching it.
    # NOT CURRENTLY ENFORCED: the 2026-08-22 attempt (solve._crowding as a
    # veto) blocked moves the drawing needs and was reverted -- the fixture
    # fell from 99.9% to 87.7% overlap reduction.  The halo is kept because
    # solve._crowding still measures with it; re-introduce as a veto only
    # with thresholds proven across the fleet, not by taste.
    crowd_halo: float = 4.0
    # A move should not leave a callout further from the part it describes
    # than ISOGEN left it (plus this slack).  NOT CURRENTLY ENFORCED --
    # unit_cost prices excess drift (w_distance) instead of vetoing it;
    # 353M05's OFFSET 13 (8.8 -> 40.8 mm, rejected on sight) is the case a
    # future veto must catch without failing the fixture regression.
    reach_tolerance: float = 1.0
    frame_margin: float = 2.0     # keep labels this far inside the border
    # How much daylight ISOGEN may leave between a balloon and its own text
    # and still have them count as one callout.  Measured on JP1071: six of
    # the seven pairs overlap outright, the seventh sits 1.20 mm apart.
    pair_gap: float = 2.0
    # ISOGEN callout STACKS (reducer/offset callouts): a column of entities
    # left-aligned to the same x -- exactly, it is a layout rule -- with a
    # uniform ~2.5 mm gap, one leader for the whole column.  Alignment is
    # kept strict on purpose: two unrelated callouts coinciding within
    # 0.3 mm AND sitting within stack_gap is not a pattern ISOGEN produces.
    stack_align: float = 0.3
    stack_gap: float = 4.0
    # How far a balloon may be eased off the size text it is tucked onto.
    # ISOGEN's tuck is a convention, NOT a defect -- prising the pair apart is
    # what made 209M05's first "fix" look worse than the input, and that
    # lesson stands.  But the user, looking at 545M05's bottom callout, asked
    # for the overlap REDUCED, not removed: the balloon may slide a couple of
    # millimetres so the number and the text both read, and it must stay well
    # inside pair_gap of its text so the two still read as one callout.
    tuck_relief: float = 2.5

    # --- candidate generation ------------------------------------------
    radii: tuple[float, ...] = (3.0, 6.0, 10.0, 15.0)
    n_directions: int = 12        # 30-degree steps -> includes the iso axes
    # Free-space search (isotidy/freespace.py).  The ring above can only ever
    # reach 15 mm, and only along 12 spokes, so a label whose nearest honest
    # gap is 20 mm away in some other direction had NO clean candidate at all
    # -- the "there was obviously room right there" complaint.  These slots
    # are found by asking the sheet where the box actually fits.
    free_cell: float = 0.5        # occupancy grid resolution, mm
    free_step: float = 2.0        # lattice spacing of tested positions, mm
    free_reach: float = 30.0      # how far from home to look, mm
    free_slots: int = 24          # how many nearest gaps to offer per label
    free_halo: float = 1.5        # extra air that marks a slot as "roomy"
    slide_steps: tuple[float, ...] = (0.0, 4.0, -4.0, 8.0, -8.0, 13.0, -13.0)

    # --- cost weights ---------------------------------------------------
    w_label: float = 120.0        # mm^2 of overlap with another label
    w_geometry: float = 80.0      # mm^2 of overlap with fixed geometry
    # Near-miss pricing between labels (raw boxes, no padding).  The band is
    # set from the sheet itself: genuine ISOGEN tucks sit at <= 1.20 mm and
    # its tightest unrelated pair at 2.50 mm, so 2.0 separates "belongs
    # together" from "merely close" -- the same constant as pair_gap, and not
    # by coincidence: landing an unrelated label inside the tuck-recognition
    # band is exactly what makes it read as someone else's callout.
    proximity_band: float = 2.0   # raw-box gap below this is charged
    # Per mm^2 of half-band-grown box intersection (see solve.unit_trouble).
    # Swept on 209M05 (2026-08-13), goal: no unrelated pair tighter than the
    # sheet's own ISOGEN floor (2.50 mm), after the audit caught the solver
    # parking a balloon 1.27 mm from a stranger dimension:
    #   w_prox   min unrelated gap   residual
    #       0        1.271 mm        3.82 mm2   <- the near-miss
    #      30        1.872 mm        1.91 mm2   <- balloon on a dimline
    #      60        2.500 mm        3.82 mm2   <- ISOGEN floor restored
    #     120        2.500 mm        3.82 mm2
    # 60 is the smallest value that restores the floor.  The 3.82 residual it
    # reports is the moved balloon's own OLD leader path over a stationary
    # dim -- stale by construction, because writeback re-lands the leader
    # with a crossing-checked landing; the round-trip score is the honest one.
    w_proximity: float = 60.0
    # Contact with the annotation's OWN dimension line / own leader.  ISOGEN
    # parks 10 of 18 dimension texts on their own line and that is conventional,
    # so this is NOT a defect and is deliberately not in detect().  But it is
    # still contact the reader has to look through, so the solver shaves it
    # WHERE THERE IS ROOM.  Priced an order of magnitude under w_geometry and
    # well under w_displacement (16/mm) on purpose: reducing self-contact must
    # never buy a new overlap with someone else, and must never be worth
    # dragging a number away from the dimension it belongs to.
    #
    # Swept on 209M05 (own-line contact starts at 124.83 mm2 over 36 labels):
    #   w_own   overlap   own-line   moved   mean move   max move
    #       8     0.004     116.96       7       4.6         6.0
    #      25     0.000      22.84      26       3.7        20.4
    #      60     0.000      13.77      29       4.6        20.8   <- chosen
    #     120     0.000      10.98      39       5.3        26.1
    # 60 removes 89% of the self-contact at zero overlap and a mean travel of
    # 4.6 mm.  Past it the curve flattens and the churn does not: 120 buys
    # 2.8 mm2 more for ten more disturbed labels and a longer worst move,
    # which is the "do not drag it far from what it labels" limit talking.
    w_own_line: float = 60.0
    w_frame: float = 1.0e6        # outside the drawable region -- effectively banned
    # w_distance and w_stay were chosen by sweep, not by taste -- see
    # tools/tune.py.  Raising w_distance 0.8 -> 12 cut the worst displacement
    # from 31.5mm to 25.0mm with no loss of cleanliness; past ~30 the solver
    # starts accepting overlaps to stay close, and quality falls off.
    w_distance: float = 12.0      # per mm of anchor-distance BEYOND home's --
                                  # drifting farther from the part is priced,
                                  # getting closer earns nothing (see
                                  # unit_cost: ISOGEN's placement is the prior)
    w_stay: float = 10.0          # flat penalty for moving at all
    # Per mm of travel from home.  Chosen by sweep on 209M05 (2026-08-13):
    #   w_disp   overlap   moved   max-travel   mean
    #      0.0      0.16      15      60.23    13.85   <- the 60mm teleport
    #      1.0      0.16      13      19.09     8.56
    #     16.0      0.16      13      10.36     7.71   <- chosen
    #     24.0      0.16      13      10.36     7.71
    #     32.0      7.81      11       9.55     7.19   <- starts refusing
    #     64.0     40.36       6       8.00     4.73      needed moves
    # 16 is the largest value that still clears the sheet, with margin from
    # the 32 cliff.  What it buys: escapes stay local, so ISOGEN's peripheral
    # callout placement survives the solve instead of being hauled into the
    # congested centre the periphery exists to avoid.
    w_displacement: float = 16.0
    # Per label/geometry crossing of the label's own leader path.  Raised
    # 30 -> 180 by sweep (2026-08-13) against the POST-WRITEBACK score, after
    # the audit caught the tool re-landing a moved balloon's leader straight
    # through a stationary dimension text (8.2 mm chord) -- at 30, one
    # crossing cost less than 3 mm of displacement, so the solver knowingly
    # bought placements whose leader slices text:
    #   w_leader   in-memory   in-file(true)
    #        30        3.82        6.66      <- leader through '134'
    #        90        3.82        6.66
    #       180        1.91        1.94      <- clean corridor, scores agree
    #       360        1.91        1.94
    # The 1.91 that remains is a hairline dimline graze at the balloon's only
    # workable pocket -- all 97 candidates enumerated, none is trouble-free.
    w_leader: float = 180.0

    # --- search ---------------------------------------------------------
    max_passes: int = 12
    leader_threshold: float = 3.5  # move further than this and it needs a leader

    # --- print scale -----------------------------------------------------
    # THE ACCEPTANCE TEST IS THE PRINTOUT, NOT THE SHEET.
    #
    # These drawings are DIN A1 and get printed on A3 -- everything shrinks
    # 0.5x.  So "no overlap at drawing scale" is the wrong bar: a 0.6
    # clearance meant two labels 1.2 mm apart passed, and 1.2 mm at A1 is
    # 0.6 mm of white on the paper the engineer is holding.  Two marks 0.6 mm
    # apart read as one.  (At the current 0.8 the same sum is 0.8 mm printed,
    # which is why the directive was to raise it.)  That is precisely the complaint -- "they're not
    # overlapping, they just LOOK like it" -- and it is a real defect, because
    # the print is the deliverable.
    #
    # So separation is specified in PRINTED mm and back-derived per drawing
    # via for_sheet().  Change the paper, and the geometry follows.
    print_target: str = "A3"
    min_printed_gap: float = 1.0   # mm of white space required ON THE PRINT

    def for_sheet(self, sheet_width_mm: float) -> "Tuning":
        """Re-derive the separation constants for this sheet's print scale.

        clearance pads BOTH boxes, so a required gap G needs G/2 each side.
        Constants only ever tighten: a print target must not licence a looser
        drawing than the hand-tuned values already demand.
        """
        target = PAPER_WIDTH_MM.get(self.print_target.upper())
        if not target or sheet_width_mm <= 0:
            return self
        scale = target / sheet_width_mm          # A1 -> A3 = 0.5
        required = self.min_printed_gap / scale  # drawing-unit gap needed
        return replace(
            self,
            clearance=max(self.clearance, required / 2.0),
            proximity_band=max(self.proximity_band, required),
        )

    def free_move(self) -> "Tuning":
        """Weights with the travel brakes largely off.

        The brakes exist because escapes used to be dangerous: with no map of
        the sheet and no notion of "worse", a label that wandered far usually
        landed somewhere bad, so travel was priced at 16/mm and drift from the
        component at 12/mm to keep moves local.

        Once the solver could see drawn ink (`Scene.ink`) and free space
        (`freespace.py`), those brakes started costing more than they saved.
        Measured on 545M05, relaxed against standard, both SCORED WITH THE
        STANDARD WEIGHTS so the comparison is honest:

            overlap   8.88 -> 0.03 mm2      A hits   5 -> 1
            labels left on more ink than ISOGEN had them: 0
            median travel 3.6 mm (most labels barely move)

        The one long move -- a 3-balloon callout, 71 mm -- crossed the pipe
        into clear space and finished CLOSER to the part it labels than
        ISOGEN had it (33.6 -> 15.0 mm).  Distance was never the risk;
        destination was, and the ink veto now guards that directly.

        Not the default yet: proven on one sheet, and it needs to be run
        across more of the 44 before it earns that.
        """
        return replace(
            self,
            w_displacement=2.0,     # was 16.0 per mm travelled
            w_distance=2.0,         # was 12.0 per mm of drift from the part
            w_stay=1.0,             # was 10.0 flat, for moving at all
            free_reach=60.0,        # was 30.0 mm of free-space search
            free_slots=60,          # was 24 gaps offered per label
        )

    def print_scale(self, sheet_width_mm: float) -> float:
        target = PAPER_WIDTH_MM.get(self.print_target.upper())
        if not target or sheet_width_mm <= 0:
            return 1.0
        return target / sheet_width_mm
