"""
Collision detection and scoring.

This module is the project's SOURCE OF TRUTH.  It is also the test oracle the
solver is graded against, so it deliberately knows nothing about the solver.
If the two ever share state, the score stops being evidence.

Three kinds of collision, and they are not equally bad:

  label <-> label     two annotations on top of each other.  The headline bug.
  label <-> geometry  text sitting on a pipe, a symbol or a dimension line.
                      Less visible than the first, but it is what makes a
                      drawing look unprofessional.
  label <-> frame     text outside the drawable region or inside the BOM.
                      Never acceptable -- this is a hard constraint, not a
                      preference.

Overlap is reported as AREA, not a count.  A count says "47 collisions" whether
each is a hairline touch or a fully buried label; area distinguishes them, and
being continuous it also gives the solver a gradient to descend.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shapely.strtree import STRtree

from .config import Tuning
from .model import Scene


# --------------------------------------------------------------------------
# Shared geometric primitive (policy-free -- the solver may use it too)
# --------------------------------------------------------------------------
def overlap_area(geom, others) -> float:
    """Total area of `geom` covered by any of `others`."""
    total = 0.0
    for o in others:
        if geom.intersects(o):
            total += geom.intersection(o).area
    return total


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Collision:
    a: int                 # label index
    b: int | None          # other label index, or None for geometry/frame
    kind: str              # "label" | "geometry" | "frame"
    area: float            # mm^2 of overlap


@dataclass
class Report:
    n_labels: int
    collisions: list[Collision] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[Collision]:
        return [c for c in self.collisions if c.kind == kind]

    @property
    def overlap_area(self) -> float:
        return sum(c.area for c in self.collisions)

    @property
    def labels_involved(self) -> set[int]:
        out: set[int] = set()
        for c in self.collisions:
            out.add(c.a)
            if c.b is not None:
                out.add(c.b)
        return out

    @property
    def clean(self) -> bool:
        return not self.collisions

    def summary(self, title: str = "") -> str:
        lab = self.of_kind("label")
        geo = self.of_kind("geometry")
        frm = self.of_kind("frame")
        involved = len(self.labels_involved)
        pct = 100.0 * involved / self.n_labels if self.n_labels else 0.0
        head = f"  {title}" if title else ""
        return (
            f"{head}\n"
            f"    label <-> label     {len(lab):4d} pairs   "
            f"{sum(c.area for c in lab):8.2f} mm2\n"
            f"    label <-> geometry  {len(geo):4d} hits    "
            f"{sum(c.area for c in geo):8.2f} mm2\n"
            f"    label <-> frame     {len(frm):4d} hits    "
            f"{sum(c.area for c in frm):8.2f} mm2\n"
            f"    ----------------------------------------------\n"
            f"    labels affected     {involved:4d} / {self.n_labels}"
            f"  ({pct:.0f}%)\n"
            f"    total overlap       {self.overlap_area:8.2f} mm2"
        )


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------
def detect(scene: Scene, cfg: Tuning = Tuning()) -> Report:
    """Find every collision in the scene's CURRENT placement."""
    report = Report(n_labels=len(scene.labels))
    boxes = [lab.box(cfg.clearance) for lab in scene.labels]

    # --- label <-> label -------------------------------------------------
    # Spatial index over the labels themselves: on a real sheet with ~2,000
    # annotations the naive double loop is 2 million comparisons per
    # evaluation, and the solver calls this thousands of times.
    if boxes:
        tree = STRtree(boxes)
        seen: set[tuple[int, int]] = set()
        for i, b in enumerate(boxes):
            for j in tree.query(b, predicate="intersects"):
                j = int(j)
                if i == j:
                    continue
                key = (i, j) if i < j else (j, i)
                if key in seen:
                    continue
                seen.add(key)
                # A balloon tucked onto its own size text is ONE annotation
                # drawn the way ISOGEN draws it, not two things colliding.
                # Counting it made every such pair look like a defect and
                # sent the solver off to break the callout apart.
                if scene.same_group(i, j):
                    continue
                area = b.intersection(boxes[j]).area
                if area > 0.0:
                    report.collisions.append(
                        Collision(a=key[0], b=key[1], kind="label", area=area))

    # --- label <-> fixed geometry ---------------------------------------
    # A label is never scored against geometry that IS its own annotation: a
    # balloon's leader lands on the bubble's edge and a short dimension's text
    # sits on its own dimension line -- both are the drawing convention, not
    # defects.  Everyone else's leaders and lines stay obstacles.
    for i, b in enumerate(boxes):
        area = overlap_area(b, scene.obstacles_near(b, scene.own_exempt(i)))
        if area > 0.0:
            report.collisions.append(
                Collision(a=i, b=None, kind="geometry", area=area))

    # --- label <-> frame -------------------------------------------------
    if scene.allowed is not None:
        for i, b in enumerate(boxes):
            outside = b.difference(scene.allowed).area
            if outside > 1e-9:
                report.collisions.append(
                    Collision(a=i, b=None, kind="frame", area=outside))

    return report


def colliding_labels(scene: Scene, cfg: Tuning = Tuning()) -> set[int]:
    """Convenience for the renderer: which label indices are in trouble."""
    return detect(scene, cfg).labels_involved
