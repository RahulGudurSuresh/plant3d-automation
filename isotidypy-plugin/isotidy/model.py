"""
The geometric model the solver works on.

Deliberately free of any CAD types.  Once extraction is done, nothing here
knows what a DXF is -- a Label is a rectangle, a size, an anchor point and a
class.  That is what makes the algorithm portable to C# later: this module is
the part you re-implement, and it has no dependency to port.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shapely.geometry import Point, Polygon
from shapely.geometry import box as shapely_box
from shapely.strtree import STRtree

Vec2 = tuple[float, float]


@dataclass
class Label:
    """A movable annotation: an axis-aligned rectangle tied to an anchor."""

    index: int
    handle: str                  # DXF handle -- the only CAD residue, used on write-back
    text: str
    layer: str
    cls: str                     # "annotation" | "weld" | "dimension"
    anchor: Vec2                 # the point this label describes
    size: Vec2                   # (width, height) of the text
    pos: Vec2                    # CURRENT lower-left corner
    home: Vec2                   # original lower-left corner, never mutated
    slide: Vec2 | None = None    # unit vector if the label may only slide

    #: For slide-constrained text: how far its centre may sit from its own
    #: line (perpendicular), in mm.  Set by the extractor to the same limit
    #: the auditor enforces -- max(3.0, 2 x the sheet's median offset) less
    #: a safety margin -- so the solver cannot offer a candidate the audit
    #: will call dimension drift.  The fixer and the checker share one bar.
    slide_limit: float | None = None

    #: How write-back must move this label.  The solver never looks at it --
    #: a rectangle is a rectangle -- but a DIMENSION is relocated by setting
    #: text_midpoint, a balloon by translating its INSERT and re-landing its
    #: leader, and plain text by translating the entity.  One field keeps all
    #: three out of the algorithm.
    kind: str = "text"           # "text" | "dimension" | "balloon"

    #: MULTILEADER handle that must follow this label (balloons only).
    leader_handle: str | None = None

    #: A previous isotidy run drew a plain-LINE leader for this label (on
    #: the tool's own layer).  Distinct from leader_handle: that is ISOGEN's
    #: MULTILEADER, this is ours.  Without the flag, every re-extract forgot
    #: the leader existed -- writeback's clear-and-redraw then deleted it
    #: for good (the label no longer counts as "moved" against its new
    #: home), and the audit kept flagging a weak association the drawing
    #: had already fixed.
    drawn_leader: bool = False

    #: Labels sharing a group id are ONE annotation and move as a rigid body.
    #:
    #: ISOGEN tucks an item balloon onto the corner of its own size text --
    #: measured on JP1071, the overlap is 35.43 mm^2 on every single pair, to
    #: the last decimal.  That is a fixed convention, not a defect.  Scoring
    #: them as two colliding rectangles made the tool chase 309.25 mm^2 of
    #: phantom overlap -- 100% of the label<->label total on 209M05 -- and
    #: "fix" it by prising the number off the text it labels.
    #:
    #: A group therefore has two properties, and both matter:
    #:   1. members never collide with EACH OTHER (the tuck is intended), and
    #:   2. members translate by the SAME delta, so the balloon stays welded
    #:      to its text and the pair keeps reading as one callout.
    group: int | None = None

    def box(self, pad: float = 0.0, pos: Vec2 | None = None) -> Polygon:
        """Axis-aligned bounds, optionally at a hypothetical position."""
        x, y = pos if pos is not None else self.pos
        w, h = self.size
        return shapely_box(x - pad, y - pad, x + w + pad, y + h + pad)

    def center(self, pos: Vec2 | None = None) -> Vec2:
        x, y = pos if pos is not None else self.pos
        w, h = self.size
        return x + w / 2.0, y + h / 2.0

    def delta(self) -> Vec2:
        return self.pos[0] - self.home[0], self.pos[1] - self.home[1]

    def displacement(self) -> float:
        dx, dy = self.delta()
        return (dx * dx + dy * dy) ** 0.5

    def moved(self, eps: float = 1e-6) -> bool:
        return self.displacement() > eps

    def anchor_distance(self, pos: Vec2 | None = None) -> float:
        """Distance from the anchor to the nearest edge of the label box."""
        return self.box(pos=pos).distance(Point(self.anchor))


@dataclass
class Scene:
    """Everything the solver needs: what may move, and what may not."""

    labels: list[Label]
    obstacles: list = field(default_factory=list)   # buffered shapely geoms
    allowed: Polygon | None = None                  # region labels must stay inside

    #: Owner handle per obstacle, aligned with `obstacles`.  None = plain
    #: geometry, owned by nobody.  A MULTILEADER's line is a real obstacle --
    #: labels must not sit on someone else's leader -- but 15 of the 19 on
    #: 209M05 land exactly ON the box edge of the balloon they belong to, so
    #: an ownerless leader obstacle would put every ballooned callout
    #: permanently "in collision" with its own arrow.  The owner handle lets
    #: detection and solving skip exactly the (label, own leader) pairs and
    #: nothing else.
    owners: list = field(default_factory=list)      # str | None per obstacle

    #: layer -> count of text entities found on layers NOT declared movable.
    #: The tool cannot tell "this drawing has no overlaps" from "this drawing
    #: uses layer names I was never told about", so it records the evidence
    #: and lets the caller refuse to report a clean bill of health.
    unmapped_text: dict[str, int] = field(default_factory=dict)

    #: entity type -> count of annotations we could see but cannot yet READ,
    #: because the extractor has no adapter for that type (DIMENSION,
    #: MULTILEADER, attributed INSERTs...).  Distinct from unmapped_text:
    #: there the layer was wrong, here the layer is right and the entity kind
    #: is unsupported.  Both produce the same lie -- "0 collisions" -- so both
    #: are recorded.
    unsupported: dict[str, int] = field(default_factory=dict)

    def coverage(self) -> float:
        """Fraction of the annotations in this drawing that were actually
        scored.  Anything below 1.0 means the score is a partial view."""
        missed = sum(self.unsupported.values()) + sum(self.unmapped_text.values())
        total = len(self.labels) + missed
        return len(self.labels) / total if total else 1.0

    #: REAL DRAWN INK, pre-buffered to the width it prints at -- every curve
    #: actually on the paper (component outlines, dimension and witness lines,
    #: pipe), as opposed to `obstacles`, which are mostly bounding boxes.
    #:
    #: This exists because the two questions are different.  `obstacles`
    #: answers "is this a defect?"; ink answers "how much stuff is physically
    #: under this label?".  Scoring defects off bboxes is right (conservative,
    #: cheap); judging whether a MOVE improved matters needs the real thing.
    #: On 545M05 a three-balloon callout was relocated 37 mm onto a dimension
    #: grid the obstacle set could not see -- 5.83 mm2 of ink under it became
    #: 22.05 mm2 -- and every official score called that an improvement.  The
    #: user's eye did not.
    ink: list = field(default_factory=list)

    #: Occupancy of the sheet, for finding gaps the candidate ring cannot
    #: reach.  See isotidy/freespace.py.  None when unavailable.
    free: object | None = None

    _tree: STRtree | None = field(default=None, repr=False)
    _ink_tree: STRtree | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # An R-tree turns "what does this box hit?" from O(n) into O(log n).
        # At 30 labels it is irrelevant; at 2,000 on a real sheet it is the
        # difference between 40ms and 40s, so build it once, here.
        self._tree = STRtree(self.obstacles) if self.obstacles else None
        self._ink_tree = STRtree(self.ink) if self.ink else None

    def ink_under(self, box) -> float:
        """Area of drawn ink beneath `box`, in mm2.  0.0 when unavailable."""
        if self._ink_tree is None:
            return 0.0
        total = 0.0
        for i in self._ink_tree.query(box, predicate="intersects"):
            total += box.intersection(self.ink[int(i)]).area
        return total

    def obstacles_near(self, geom, exempt: set | None = None) -> list:
        """Obstacles intersecting `geom`, minus any owned by `exempt` handles."""
        if self._tree is None:
            return []
        idx = self._tree.query(geom, predicate="intersects")
        if not exempt or not self.owners:
            return [self.obstacles[i] for i in idx]
        return [self.obstacles[i] for i in idx
                if self.owners[i] not in exempt]

    def own_exempt(self, i: int) -> set:
        """Obstacle-owner handles that are PART OF label `i`'s own annotation.

        A callout's own leader and a dimension's own dimension line are drawn
        touching their label by convention; scoring those pairs would put the
        annotation permanently in collision with itself.  Covers the label's
        own handle, its leader, and its group partners' -- a welded
        balloon+text pair shares one arrow.
        """
        lab = self.labels[i]
        members = ([lab] if lab.group is None else
                   [l for l in self.labels if l.group == lab.group])
        handles = {l.handle for l in members}
        handles |= {l.leader_handle for l in members}
        handles.discard(None)
        return handles

    def reset(self) -> None:
        for lab in self.labels:
            lab.pos = lab.home

    def rigid_units(self) -> list[list[int]]:
        """Label indices bundled into things that move as one.

        An ungrouped label is a unit of one, so callers never need to special
        case it.  Order is by first member index, and members keep their scene
        order -- the solver must stay deterministic.
        """
        units: dict[int, list[int]] = {}
        singles: list[list[int]] = []
        for lab in self.labels:
            if lab.group is None:
                singles.append([lab.index])
            else:
                units.setdefault(lab.group, []).append(lab.index)
        out = singles + list(units.values())
        return sorted(out, key=lambda m: m[0])

    def same_group(self, i: int, j: int) -> bool:
        gi = self.labels[i].group
        return gi is not None and gi == self.labels[j].group
