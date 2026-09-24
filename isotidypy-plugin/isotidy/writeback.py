"""
Scene -> DXF.

The mirror of extract.py, and the second half of the CAD-specific layer.

`Label.home` is never mutated by the solver, so write-back is a pure delta
applied to the original entity.  The tool never rebuilds an annotation, it
only moves it: style, height, layer and any XDATA the drawing carried survive
untouched.  That is what makes the output acceptable to a checker.

Each Label.kind moves differently:

  text        translate the TEXT/MTEXT entity.  Draw a leader if it travelled.
  dimension   set dxf.text_midpoint AND translate the cached MTEXT inside the
              *D geometry block, so the override and the rendered geometry
              agree in every viewer.  No leader -- a dimension that needs one
              has been moved too far, and the slide constraint prevents that.
  balloon     translate the INSERT (its ATTRIBs follow) and re-land its
              MULTILEADER so the arrow keeps pointing at the same component.
              Never draw a second leader; it already has one.
"""

from __future__ import annotations

from ezdxf.math import Vec3
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points

from .config import DEFAULT_ROLES, LayerRoles, Tuning
from .model import Scene

LEADER_COLOR = 8  # grey -- a leader is chrome, it must not compete with text


def sync_embedded_attribs(doc) -> int:
    """Make multi-line ATTRIBs draw where their balloon is.

    ISOGEN's item balloons (AnnoCircle etc.) use MULTI-LINE attributes
    (DXF 2018, attribute_type=2).  Such an ATTRIB carries an embedded MTEXT
    with its OWN insert point, and that -- not the ATTRIB's insert or
    align_point -- is where AutoCAD draws the number.  ezdxf's translate()
    moves the ATTRIB fields but never the embedded MTEXT, so every balloon
    the pipeline had ever moved shipped as an EMPTY circle with its number
    left at the old position (found 2026-09-17 in TrueView on the
    difficult_iso sheet; our renders hid it because they read the ATTRIB
    fields).  ISOGEN writes the embedded insert equal to align_point, so
    re-pinning it there after every edit restores what AutoCAD expects.

    Returns the number of attributes re-pinned.  Call before every save.
    """
    n = 0
    for insert in doc.modelspace().query("INSERT"):
        for a in insert.attribs:
            m = getattr(a, "_embedded_mtext", None)
            if m is None:
                continue
            if a.dxf.get("halign", 0) or a.dxf.get("valign", 0):
                target = a.dxf.get("align_point", a.dxf.insert)
            else:
                target = a.dxf.insert
            cur = m.dxf.get("insert", None)
            if cur is None or (cur - target).magnitude > 1e-9:
                m.dxf.insert = Vec3(target)
                n += 1
    return n


def save_dxf(doc, path) -> None:
    """The ONLY way the pipeline may write a DXF: sync, then save."""
    sync_embedded_attribs(doc)
    doc.saveas(path)


def apply(doc, scene: Scene, roles: LayerRoles = DEFAULT_ROLES,
          cfg: Tuning = Tuning()) -> tuple[int, int]:
    """Move every relocated label.  Returns (labels_moved, leaders_drawn)."""
    msp = doc.modelspace()
    _clear_leaders(msp, roles.leader)
    _ensure_layer(doc, roles.leader)

    # A welded callout owns ONE arrow.  If any member balloon carries a
    # MULTILEADER, that leader speaks for the whole group -- drawing a second
    # straight leader for the text member gave the delivered drawing two
    # leaders pointing at targets 24.6 mm apart, which reads as two different
    # claims about where the callout belongs.
    group_has_ml = {
        lab.group for lab in scene.labels
        if lab.group is not None and lab.leader_handle
    }

    # ONE leader per welded callout, whoever draws it.  Without this, two
    # moved-far members of the same stack each got their own line.
    drawn_groups: set[int] = set()

    def _needs_line(lab) -> bool:
        if lab.leader_handle:
            return False
        if lab.group is not None and (lab.group in group_has_ml
                                      or lab.group in drawn_groups):
            return False
        return True

    def _line_for(lab) -> bool:
        nonlocal leaders
        if _draw_leader(msp, lab, roles.leader, cfg, scene):
            leaders += 1
            lab.drawn_leader = True
            if lab.group is not None:
                drawn_groups.add(lab.group)
            return True
        return False

    moved = leaders = 0
    for lab in scene.labels:
        if not lab.moved():
            # A leader WE drew in an earlier round was just deleted by
            # _clear_leaders; the label did not move this round (its home is
            # already the far position), so the old "draw only for movers"
            # rule lost the leader for good and the sheet regressed to a
            # weak association.  Redraw it where the label now stands.
            if lab.drawn_leader and _needs_line(lab):
                _line_for(lab)
            continue
        dx, dy = lab.delta()
        entity = doc.entitydb.get(lab.handle)
        if entity is None:
            continue

        if lab.kind == "dimension":
            ok = _move_dimension(doc, entity, dx, dy)
        elif lab.kind == "balloon":
            ok = _move_balloon(doc, entity, lab, dx, dy, scene)
            # A LEADERLESS balloon (or tag stack) moved away from its part
            # is exactly as untraceable as moved text -- on 202M01_r0-1 the
            # solver relocated the 'A13217PI6501'/'8' stack 29 mm out and
            # the audit rightly called it a new weak association.  Text
            # already gets a line at this threshold; balloons now do too.
            if (ok and _needs_line(lab)
                    and (lab.displacement() >= cfg.leader_threshold
                         or lab.drawn_leader)):
                _line_for(lab)
        else:
            entity.translate(dx, dy, 0)
            ok = True
            if lab.leader_handle:
                # It already has an ISOGEN leader: move the landing with it.
                ok = _reland_leader(doc, lab, scene)
                if not ok:
                    entity.translate(-dx, -dy, 0)
            # Plain text that has drifted far enough to lose its visual
            # association needs a leader, or we have traded an overlap for an
            # ambiguity -- on a piping iso that is a QA defect, not a cosmetic
            # one.  A leader drawn in an earlier round also follows its label.
            elif (_needs_line(lab)
                    and (lab.displacement() >= cfg.leader_threshold
                         or lab.drawn_leader)):
                _line_for(lab)
        if ok:
            moved += 1

    return moved, leaders


# --------------------------------------------------------------------------
# Per-kind movers
# --------------------------------------------------------------------------
def _move_dimension(doc, dim, dx: float, dy: float) -> bool:
    """Relocate dimension text.

    Two things must change together.  `text_midpoint` is what AutoCAD uses
    when it regenerates the dimension; the MTEXT inside the *D geometry block
    is what every viewer draws until it does.  Update one and not the other
    and the drawing looks different depending on who opens it.
    """
    try:
        tm = dim.dxf.text_midpoint
        dim.dxf.text_midpoint = Vec3(tm.x + dx, tm.y + dy, tm.z)
    except Exception:
        return False

    # Bit 128 of group code 70: "text position defined by user".  ISOGEN
    # already sets it, but a dimension without it would snap back on regen.
    try:
        dim.dxf.dimtype = dim.dxf.dimtype | 128
    except Exception:
        pass

    # ISOGEN's dimension style has DIMTMOVE=1 ("add a leader when the text
    # is moved").  We never regenerate the dimension here, so nothing shows
    # -- but the moment AutoCAD regenerates it (any edit, or the in-process
    # plugin's RecomputeDimensionBlock) it draws a stub from the dimension
    # line to the moved number: the "small legend lines" seen on 2026-09-11.
    # Override per dimension: 2 = move the text freely, no leader.
    try:
        ov = dim.override()
        ov.update({"dimtmove": 2})
        ov.commit()
    except Exception:
        pass

    name = dim.dxf.get("geometry", None)
    blk = doc.blocks.get(name) if name else None
    if blk is not None:
        for x in blk:
            if x.dxftype() in ("MTEXT", "TEXT"):
                x.translate(dx, dy, 0)
                break
    return True


def _move_balloon(doc, insert, lab, dx: float, dy: float,
                  scene: Scene) -> bool:
    """Move an item balloon and bring its leader with it.

    The bubble and its leader are separate entities in ISOGEN output, so
    moving the bubble alone leaves an arrow pointing at empty space.  We move
    the landing end only -- the arrow tip stays on the component it
    identifies, which is the whole point of the leader.
    """
    try:
        insert.translate(dx, dy, 0)   # ATTRIBs travel with the INSERT
    except Exception:
        return False

    if _reland_leader(doc, lab, scene):
        return True
    # A leader we failed to re-land is worse than none: it points nowhere.
    # Undo the move rather than ship a misleading drawing.
    insert.translate(-dx, -dy, 0)
    return False


def _reland_leader(doc, lab, scene: Scene) -> bool:
    """Bring a label's existing MULTILEADER landing along with the label.

    Used by EVERY kind of label that owns one, not just balloons.  It used to
    live inside the balloon mover, so a plain text callout with an ISOGEN
    leader was translated while its arrow stayed behind: on 353M05 'OFFSET 13'
    finished 41 mm from its part with no leader attached to it, and the sheet
    kept a leader pointing at nothing.  The audit caught it as a category-C
    weak association, which is exactly what it was.
    """
    if not lab.leader_handle:
        return True
    ml = doc.entitydb.get(lab.leader_handle)
    if ml is None:
        return True
    try:
        ctx = ml.context
        landing = _pick_landing(scene, lab)
        new = Vec3(landing[0], landing[1], 0)
        for ld in ctx.leaders:
            ld.last_leader_point = new
        ctx.base_point = new
        # The entity carries AutoCAD's cached rendering (proxy graphic), and
        # ezdxf edits do not refresh it -- every proxy-honouring viewer,
        # including our own render tool, would keep drawing the OLD leader
        # path.  Dropping the cache forces regeneration from the context data
        # we just updated, so what ships is what viewers show.
        ml.proxy_graphic = None
    except Exception:
        return False
    return True


def _pick_landing(scene: Scene, lab) -> tuple[float, float]:
    """Choose where a leader from the anchor meets its label.

    Nearest-point-to-tip is the obvious landing and it is what this used to
    do unconditionally -- and on a real sheet the swung leader promptly cut an
    8 mm chord through a stationary dimension text that the old path had only
    grazed.  The swing is a NEW line segment nobody priced: the solver checks
    the leader to the label's own box, not what the re-landed path crosses on
    the way there.

    So check it here, where the path is finally known.  Candidates are the
    nearest point plus the four edge midpoints of the box; each is scored by
    (labels crossed, geometry crossed, length), in that order, candidate
    order breaking the final tie for determinism.  Group partners are never
    counted -- a leader is allowed to graze the text welded to its own
    balloon, that is ISOGEN's own geometry -- and neither is anything
    touching the arrow tip: the component the leader points AT is not an
    obstacle to reaching it.
    """
    tip = Point(lab.anchor)
    box = lab.box()
    x1, y1, x2, y2 = box.bounds
    near = nearest_points(tip, box)[1]
    candidates = [
        (near.x, near.y),
        ((x1 + x2) / 2.0, y1), ((x1 + x2) / 2.0, y2),
        (x1, (y1 + y2) / 2.0), (x2, (y1 + y2) / 2.0),
    ]

    own_group = set()
    if lab.group is not None:
        own_group = {l.index for l in scene.labels if l.group == lab.group}
    own_group.add(lab.index)
    others = [l.box() for l in scene.labels if l.index not in own_group]
    exempt = scene.own_exempt(lab.index)
    geoms = [o for o, own in zip(scene.obstacles, scene.owners)
             if own not in exempt and o.distance(tip) > 0.05]

    def score(cand):
        path = LineString([(tip.x, tip.y), cand])
        label_x = sum(1 for b in others if path.intersects(b))
        geo_x = sum(1 for g in geoms if path.intersects(g))
        return (label_x, geo_x, path.length)

    best = min(range(len(candidates)), key=lambda i: (*score(candidates[i]), i))
    return candidates[best]


# --------------------------------------------------------------------------
# Leaders for plain text
# --------------------------------------------------------------------------
def _ensure_layer(doc, name: str) -> None:
    if name not in doc.layers:
        doc.layers.add(name=name, color=LEADER_COLOR)


def _clear_leaders(msp, layer: str) -> None:
    """Delete leaders from a previous run so re-running is idempotent."""
    for e in list(msp.query(f'*[layer=="{layer}"]')):
        msp.delete_entity(e)


def _draw_leader(msp, lab, layer: str, cfg: Tuning, scene: Scene) -> bool:
    """Straight leader from the anchor to a crossing-checked edge landing.

    Same landing policy as a re-landed balloon leader (_pick_landing): fewest
    labels crossed, then fewest geometry crossings, then shortest.  Elbowed
    routing with a horizontal landing is still a separate problem.
    """
    p = Point(lab.anchor)
    if lab.box(cfg.clearance * 0.5).contains(p):
        return False
    landing = _pick_landing(scene, lab)
    msp.add_line((p.x, p.y), landing, dxfattribs={"layer": layer})
    return True
