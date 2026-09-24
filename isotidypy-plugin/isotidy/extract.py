"""
DXF -> Scene.

This is the dirty layer.  Everything CAD-specific lives here so that model.py,
detect.py and solve.py stay pure geometry.  When you port to C#, this is the
file you rewrite against the AutoCAD Managed API; the rest transliterates.

Real ISOGEN output puts its annotations in three different kinds of entity,
and each needs its own adapter:

  MTEXT / TEXT   plain callouts ("DN 80", "CONNECTED WITH DWG NO. ...").
                 Move by translating the entity.

  DIMENSION      the measurements.  The rendered geometry lives in an
                 anonymous block (*D52, *D53 ...) holding 3 LINEs, an MTEXT
                 and 2 SOLID arrowheads.  The MTEXT gives the exact text box;
                 the line CARRYING THE ARROWHEADS is the dimension line, which
                 is both the anchor and the axis the text is allowed to slide
                 along (see _dimension_parts for why not simply the longest).
                 Move by setting dxf.text_midpoint (and the cached MTEXT).

  INSERT+ATTRIB  item balloons (AnnoCircle, AnnoGroupThreeCircle,
                 AnnoRectWide2 ...).  The bubble is a block; its ATTRIB holds
                 the item number.  Its leader is a SEPARATE MULTILEADER whose
                 last_leader_point lands on the bubble and whose first line
                 vertex is the arrow tip -- and that tip is the true anchor,
                 so balloons need no anchor inference at all.

ANCHOR RESOLUTION, in order of trust:
  1. the balloon's own leader arrow tip   (exact)
  2. the dimension line midpoint          (exact)
  3. XDATA written by the synthetic fixture (exact, test only)
  4. nearest point on fixed geometry      (a guess -- measured in the tests)
"""

from __future__ import annotations

from pathlib import Path

import ezdxf
from ezdxf import bbox
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry import box as shapely_box
from shapely.ops import nearest_points, unary_union

from .config import (DEFAULT_ROLES, DIM_DRIFT_FACTOR, DIM_DRIFT_FLOOR,
                     LayerRoles, Tuning)
from .model import Label, Scene, Vec2

TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB"}
FIXTURE_APPID = "ISOTIDY"

#: Entity types that carry annotation text but that extract() cannot yet turn
#: into Labels.  Each needs its own adapter -- see the roadmap.  Until then
#: they are COUNTED, so coverage is always reported honestly.
UNSUPPORTED_ANNOTATION_TYPES = {"LEADER"}


def _flatten_entity(entity, depth: int = 0) -> list[list[tuple]]:
    """An entity's DRAWN curves as 2D point chains, block references expanded."""
    out: list[list[tuple]] = []
    t = entity.dxftype()
    try:
        if t == "LINE":
            out.append([(entity.dxf.start.x, entity.dxf.start.y),
                        (entity.dxf.end.x, entity.dxf.end.y)])
        elif t == "LWPOLYLINE":
            out.append([(p[0], p[1]) for p in entity.get_points()])
        elif t in ("CIRCLE", "ARC", "ELLIPSE", "SPLINE"):
            pts = [(v.x, v.y) for v in entity.flattening(0.1)]
            if pts:
                out.append(pts)
        elif t == "SOLID":
            out.append([(entity.dxf.vtx0.x, entity.dxf.vtx0.y),
                        (entity.dxf.vtx1.x, entity.dxf.vtx1.y),
                        (entity.dxf.vtx2.x, entity.dxf.vtx2.y)])
        elif t == "HATCH":
            # A solid fill IS ink -- the support triangles and weld dots
            # ISOGEN draws as HATCH inside their blocks were invisible to
            # every check (2026-09-17: two leaders drawn straight through
            # the yellow support glyphs while B read 0).  Its boundary
            # ring stands for it.
            for path in entity.paths:
                if hasattr(path, "vertices"):
                    pts = [(v[0], v[1]) for v in path.vertices]
                else:
                    pts = []
                    for ed in path.edges:
                        if ed.type == "LineEdge":
                            pts.append((ed.start[0], ed.start[1]))
                        elif ed.type == "ArcEdge":
                            import math
                            cx, cy, r = ed.center[0], ed.center[1], ed.radius
                            a0, a1 = math.radians(ed.start_angle), math.radians(ed.end_angle)
                            if a1 < a0:
                                a1 += 2 * math.pi
                            n = max(4, int((a1 - a0) / math.radians(20)))
                            pts += [(cx + r * math.cos(a0 + (a1 - a0) * k / n),
                                     cy + r * math.sin(a0 + (a1 - a0) * k / n))
                                    for k in range(n + 1)]
                if len(pts) >= 3:
                    out.append(pts + [pts[0]])
        elif t == "INSERT" and depth < 4:
            for ve in entity.virtual_entities():
                out += _flatten_entity(ve, depth + 1)
    except Exception:
        pass
    return out


def component_symbols(doc) -> list[tuple[str, Polygon, list[LineString]]]:
    """(name, extent, drawn curves) per component symbol INSERT.

    BOUNDING BOXES ARE THE WRONG TOOL FOR LEADER ROUTING, which is why the
    curves come too.  A leader whose tip sits on a valve necessarily pierces
    that valve's bbox, so a bbox test scores every candidate route the same
    and cannot tell a clean gap through a cluster from a line drawn straight
    across the glyph.  The extent is kept only to answer "does this leader
    TERMINATE inside this symbol", which is a different question.
    """
    out = []
    for e in doc.modelspace().query("INSERT"):
        if e.dxf.name.startswith(("Anno", "title")):
            continue
        curves = [LineString(pts) for pts in _flatten_entity(e) if len(pts) >= 2]
        if not curves:
            continue
        xs0, ys0, xs1, ys1 = zip(*(c.bounds for c in curves))
        out.append((e.dxf.name,
                    shapely_box(min(xs0), min(ys0), max(xs1), max(ys1)),
                    curves))
    return out


# --------------------------------------------------------------------------
# Entity -> shapely
# --------------------------------------------------------------------------
def _to_geometry(entity):
    """Best-effort conversion of a DXF entity to a shapely geometry.

    Returns None for anything we cannot meaningfully collide against.
    """
    t = entity.dxftype()
    try:
        if t == "LINE":
            s, e = entity.dxf.start, entity.dxf.end
            if (s.x, s.y) == (e.x, e.y):
                return None
            return LineString([(s.x, s.y), (e.x, e.y)])

        if t == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in entity.get_points("xy")]
            if len(pts) < 2:
                return None
            if entity.closed:
                return Polygon(pts).exterior
            return LineString(pts)

        if t == "POLYLINE":
            pts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
            return LineString(pts) if len(pts) >= 2 else None

        if t == "CIRCLE":
            c = entity.dxf.center
            return Point(c.x, c.y).buffer(entity.dxf.radius).exterior

        if t in ("ARC", "ELLIPSE", "SPLINE"):
            pts = [(p.x, p.y) for p in entity.flattening(0.2)]
            return LineString(pts) if len(pts) >= 2 else None

        if t in ("INSERT", "SOLID", "HATCH", "ACAD_TABLE"):
            # Symbols, arrowheads and tables: approximate by their bounding
            # box.  Exact outlines would mean exploding every block on every
            # collision test; the box is close enough to keep text off a
            # valve, and it is ~100x cheaper.
            return _bbox_polygon(entity)
    except Exception:
        return None
    return None


def _mtext_extents(e) -> tuple[Vec2, Vec2] | None:
    """True drawn extents of an MTEXT as (pos, size), or None to fall back.

    ezdxf's own extents WRAP the text even when the entity says not to.  With
    `width = 0` AutoCAD draws each paragraph on one unbroken line, but ezdxf
    lays it out in a narrow column: on 545M05 'GL5 GR5 HD5' at 3.75 mm text
    height came back 9.90 x 17.32 mm -- three stacked lines -- where the real
    glyphs run ~34 x 4 mm.  'DN 50' came back 7.57 x 11.05, i.e. two lines.

    Both dimensions are wrong, and in opposite and damaging directions: the
    box is too NARROW, so the model cannot see a balloon sitting on the end of
    its own size text (the user pointed at exactly this on 545M05 and the
    checker reported 0.00 mm2), and too TALL, so it reserves empty space
    above and below that other labels are then pushed out of.
    """
    if e.dxf.hasattr("width") and e.dxf.width:
        return None                      # genuinely wrapped: ezdxf is right
    try:
        text = e.plain_text()
        lines = text.split("\n") or [""]
        h = float(e.dxf.char_height)
        if h <= 0:
            return None
        name, factor = "txt.shx", 1.0
        try:
            style = e.doc.styles.get(e.dxf.style)
            name = style.dxf.get("font", name) or name
            factor = float(style.dxf.get("width", 1.0)) or 1.0
        except Exception:
            pass
        font = _font_cache(name, h)
        w = max((font.text_width(ln) for ln in lines), default=0.0) * factor
        # Line spacing: AutoCAD's default single spacing is 1.667 * char
        # height between baselines; the last line adds no leading.
        total_h = h + (len(lines) - 1) * h * 1.667 * float(
            e.dxf.get("line_spacing_factor", 1.0) or 1.0)
        if w <= 0.0 or total_h <= 0.0:
            return None
    except Exception:
        return None

    ip = e.dxf.insert
    # attachment_point: 1..3 top L/C/R, 4..6 middle, 7..9 bottom
    ap = int(e.dxf.get("attachment_point", 1))
    col = (ap - 1) % 3          # 0 left, 1 centre, 2 right
    row = (ap - 1) // 3         # 0 top,  1 middle, 2 bottom
    x = ip.x - (w / 2.0 if col == 1 else w if col == 2 else 0.0)
    y = ip.y - (total_h if row == 0 else
                total_h / 2.0 if row == 1 else 0.0)
    return (x, y), (w, total_h)


_FONTS: dict = {}


def _font_cache(name: str, height: float):
    key = (name, round(height, 3))
    if key not in _FONTS:
        from ezdxf.fonts import fonts
        _FONTS[key] = fonts.make_font(name, height)
    return _FONTS[key]


def _bbox_polygon(entity) -> Polygon | None:
    try:
        b = bbox.extents([entity], fast=False)
    except Exception:
        return None
    if not b.has_data or b.size.x <= 0 or b.size.y <= 0:
        return None
    return shapely_box(b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y)


def _closed_polygons(entities) -> list[Polygon]:
    out = []
    for e in entities:
        if e.dxftype() == "LWPOLYLINE" and e.closed:
            pts = [(p[0], p[1]) for p in e.get_points("xy")]
            if len(pts) >= 3:
                out.append(Polygon(pts))
    return out


# --------------------------------------------------------------------------
# Anchors
# --------------------------------------------------------------------------
def _xdata_anchor(entity) -> Vec2 | None:
    """Ground-truth anchor, present only in the synthetic fixture."""
    try:
        tags = entity.get_xdata(FIXTURE_APPID)
    except Exception:
        return None
    want = False
    for code, value in tags:
        if code == 1000 and value == "ANCHOR":
            want = True
        elif want and code == 1010:
            return (value[0], value[1])
    return None


def _xdata_home(entity) -> Vec2 | None:
    """ISOGEN-original position, stamped by writeback on the FIRST move.

    `Label.home` re-baselines on every re-extract, so a veto judged against
    it lets a label walk stage over stage (82 mm on difficult_iso).  This
    tag is the memory that stops the ratchet; see Label.original_home.
    """
    try:
        tags = entity.get_xdata(FIXTURE_APPID)
    except Exception:
        return None
    want = False
    for code, value in tags:
        if code == 1000 and value == "HOME":
            want = True
        elif want and code == 1010:
            return (value[0], value[1])
    return None


def _infer_anchor(center: Vec2, geoms) -> Vec2:
    """Fallback for plain text: the nearest point on fixed geometry.

    Crude, and it will be wrong sometimes -- a label sitting between two
    close components can bind to the wrong one.  Balloons and dimensions
    never reach here; they carry exact anchors.
    """
    p = Point(center)
    best, best_d = center, float("inf")
    for g in geoms:
        d = g.distance(p)
        if d < best_d:
            best_d = d
            # nearest_points, not project/interpolate: fixed geometry now
            # includes polygons (block bounding boxes), and project() only
            # accepts lineal geometry.
            q = nearest_points(p, g)[1]
            best = (q.x, q.y)
    return best


def _slide_direction(center: Vec2, dim_lines: list[LineString],
                     search: float = 20.0) -> Vec2 | None:
    """Unit vector a constrained label may slide along."""
    p = Point(center)
    near = [ln for ln in dim_lines if ln.distance(p) <= search]
    if not near:
        return None
    line = max(near, key=lambda ln: ln.length)
    return _unit(line.coords[0], line.coords[-1])


def _unit(a, b) -> Vec2 | None:
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = (dx * dx + dy * dy) ** 0.5
    return (dx / n, dy / n) if n else None


# --------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------
#: An arrowhead SOLID sits ON the dimension line's end.  Measured on 209M05:
#: centroid-to-endpoint distance 1.63 mm for the true line, 23.5 mm for the
#: nearest witness line -- any threshold between those works; 2.5 gives slack
#: for bigger arrowheads without reaching the witness lines.
_ARROWHEAD_TOL = 2.5


def _solid_centroid(solid) -> Vec2 | None:
    try:
        pts = {(solid.dxf.vtx0.x, solid.dxf.vtx0.y),
               (solid.dxf.vtx1.x, solid.dxf.vtx1.y),
               (solid.dxf.vtx2.x, solid.dxf.vtx2.y),
               (solid.dxf.vtx3.x, solid.dxf.vtx3.y)}
    except Exception:
        return None
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts),
            sum(p[1] for p in pts) / len(pts))


def _dimension_parts(dim, doc):
    """(text_mtext, dimension_line) from a DIMENSION's geometry block.

    Returns (None, None) when the block is missing -- some exporters drop the
    anonymous blocks, and a dimension we cannot measure is one we must not
    pretend to have handled.

    THE DIMENSION LINE IS THE ONE CARRYING THE ARROWHEADS, NOT THE LONGEST.
    The block holds three LINEs -- the dimension line and two witness lines
    running back to the measured points -- plus two SOLID arrowheads sitting
    on the dimension line's ends.  "Longest wins" survived until a real sheet
    produced a witness line longer than its dimension line: on 209M05 the
    '254' dim has witness lines of 23.99/24.02 mm against a 22.06 mm dimension
    line, so the text was constrained to slide 60 degrees off its true axis
    and the solver dutifully moved it up a witness line.  Counting arrowheads
    within _ARROWHEAD_TOL of the endpoints is structural, not statistical:
    2 arrowheads beats 1 beats 0, and only a tie falls back to length.
    """
    name = dim.dxf.get("geometry", None)
    if not name:
        return None, None
    blk = doc.blocks.get(name)
    if blk is None:
        return None, None
    texts = [x for x in blk if x.dxftype() in ("MTEXT", "TEXT")]
    lines = [x for x in blk if x.dxftype() == "LINE"]
    if not texts:
        return None, None
    dim_line = None
    if lines:
        arrows = [c for x in blk if x.dxftype() in ("SOLID", "TRACE")
                  for c in [_solid_centroid(x)] if c is not None]

        def support(l) -> int:
            ends = ((l.dxf.start.x, l.dxf.start.y), (l.dxf.end.x, l.dxf.end.y))
            n = 0
            for c in arrows:
                if any((c[0] - e[0]) ** 2 + (c[1] - e[1]) ** 2
                       <= _ARROWHEAD_TOL ** 2 for e in ends):
                    n += 1
            return n

        best = max(lines, key=lambda l: (
            support(l), (l.dxf.end - l.dxf.start).magnitude))
        dim_line = LineString([(best.dxf.start.x, best.dxf.start.y),
                               (best.dxf.end.x, best.dxf.end.y)])
    return texts[0], dim_line


def _leader_index(mleaders) -> list[tuple[Vec2, Vec2, str]]:
    """[(landing, arrow_tip, handle)] for every MULTILEADER.

    landing   -- where the leader meets its balloon
    arrow_tip -- what the balloon is pointing AT: the true anchor
    """
    out = []
    for ml in mleaders:
        try:
            ctx = ml.context
            for ld in ctx.leaders:
                landing = ld.last_leader_point
                tip = None
                for ln in ld.lines:
                    if ln.vertices:
                        tip = ln.vertices[0]
                        break
                if landing is None or tip is None:
                    continue
                out.append(((landing.x, landing.y), (tip.x, tip.y),
                            ml.dxf.handle))
        except Exception:
            continue
    return out


def _pair_leader(box: Polygon, leaders, tol: float = 3.0):
    """Find the leader that lands on this balloon."""
    best, best_d = None, tol
    for landing, tip, handle in leaders:
        d = box.distance(Point(landing))
        if d <= best_d:
            best_d, best = d, (landing, tip, handle)
    return best


def _leader_segments(mleaders) -> list[tuple[LineString, str]]:
    """[(polyline, owner handle)] -- the actual drawn path of every leader.

    Read from ml.context, NOT ml.virtual_entities().  virtual_entities()
    replays the entity's cached PROXY GRAPHIC -- AutoCAD's last rendering --
    which is trimmed at the arrowhead, emits HATCH/POLYLINE rather than
    LINE/LWPOLYLINE, and goes stale the moment anyone edits the leader without
    refreshing the cache (writeback re-lands leaders and does exactly that).
    The context data is what AutoCAD itself regenerates from, so it is the
    authoritative geometry: line vertices, then the landing point, then the
    dogleg if the style has one.
    """
    out = []
    for ml in mleaders:
        try:
            ctx = ml.context
            for ld in ctx.leaders:
                pts: list[Vec2] = []
                for ln in ld.lines:
                    pts.extend((v.x, v.y) for v in ln.vertices)
                if ld.has_last_leader_line and ld.last_leader_point is not None:
                    p = ld.last_leader_point
                    pts.append((p.x, p.y))
                    if ld.has_dogleg_vector and ld.dogleg_length > 0:
                        d = ld.dogleg_vector
                        pts.append((p.x + d.x * ld.dogleg_length,
                                    p.y + d.y * ld.dogleg_length))
                pts = [p for i, p in enumerate(pts)
                       if i == 0 or p != pts[i - 1]]
                if len(pts) >= 2:
                    out.append((LineString(pts), ml.dxf.handle))
        except Exception:
            continue
    return out


def _claim_orphan_leaders(labels: list[Label], leaders) -> None:
    """Give every unclaimed MULTILEADER to the label it visibly serves.

    Balloon pairing (tol 3.0, landing ON the bubble) leaves the leaders that
    belong to plain text unowned -- ISOGEN lands those short of the text box
    (8.37 mm measured on 209M05).  Unowned is not neutral once leaders are
    obstacles: the leader of a continuation callout ends touching its own
    text, and without an owner that text would sit permanently "in collision"
    with its own arrow.  Nearest-label-within-tolerance, strongest claim
    first, one leader per label -- same discipline as the balloon pairing.
    """
    claimed = {lab.leader_handle for lab in labels if lab.leader_handle}
    orphans = [(landing, handle) for landing, _tip, handle in leaders
               if handle not in claimed]
    if not orphans:
        return
    claims = []
    for landing, handle in orphans:
        p = Point(landing)
        for lab in labels:
            if lab.leader_handle is not None:
                continue
            d = lab.box().distance(p)
            if d <= 10.0:   # covers the measured 8.37 mm ISOGEN landing gap
                claims.append((d, handle, lab.index))
    claims.sort(key=lambda c: (c[0], c[1], c[2]))
    taken: set[str] = set()
    for _d, handle, idx in claims:
        if handle in taken or labels[idx].leader_handle is not None:
            continue
        taken.add(handle)
        labels[idx].leader_handle = handle


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------
def extract(path: Path | str, roles: LayerRoles = DEFAULT_ROLES,
            cfg: Tuning = Tuning()) -> tuple[object, Scene]:
    """Read a DXF and return (doc, Scene)."""
    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()

    text_entities, dim_entities, balloon_entities = [], [], []
    mleaders, frame_entities = [], []
    fixed_geoms, dim_lines = [], []
    #: (geometry, owner handle | None).  Ownership marks geometry that IS part
    #: of some annotation -- a dimension's own line, a callout's own leader --
    #: so scoring can skip exactly the self-pairs and nothing else.
    obstacle_geoms: list[tuple[object, str | None]] = []
    unmapped: dict[str, int] = {}
    unsupported: dict[str, int] = {}

    #: LINE leaders a previous isotidy run drew (on our own layer).  They are
    #: not labels -- but they are not nothing either: each is claimed by the
    #: label it serves (endpoint on the box) so re-runs know the leader
    #: exists, and it becomes an OWNED obstacle so other labels stay off it.
    prior_leaders: list = []

    for e in msp:
        layer, t = e.dxf.layer, e.dxftype()
        if layer == roles.leader:
            if t == "LINE":
                g = _to_geometry(e)
                if g is not None:
                    prior_leaders.append(g)
            continue  # our own previous output -- keeps re-runs idempotent

        # --- movable annotations ----------------------------------------
        if t in TEXT_TYPES:
            if roles.is_movable(layer):
                text_entities.append(e)
            elif layer not in roles.frame:
                # Text we were not told to manage.  Silence here is how a
                # mis-configured run reports "clean".
                unmapped[layer] = unmapped.get(layer, 0) + 1
            continue

        if t == "DIMENSION":
            if roles.read_dimensions and roles.is_movable(layer):
                dim_entities.append(e)
            elif layer not in roles.frame:
                unsupported["DIMENSION"] = unsupported.get("DIMENSION", 0) + 1
            continue

        if t == "MULTILEADER":
            mleaders.append(e)
            continue

        if t == "INSERT" and e.attribs and layer not in roles.frame:
            if roles.is_balloon(e.dxf.name):
                balloon_entities.append(e)
                continue
            unsupported["INSERT+ATTRIB"] = \
                unsupported.get("INSERT+ATTRIB", 0) + len(e.attribs)

        if t in UNSUPPORTED_ANNOTATION_TYPES and layer not in roles.frame:
            unsupported[t] = unsupported.get(t, 0) + 1

        # --- everything else is an obstacle ------------------------------
        if layer in roles.frame:
            frame_entities.append(e)
            continue

        # COMPONENT SYMBOLS ARE THEIR DRAWN CURVES, NOT THEIR BOX.  The
        # bbox approximation walled off visually empty paper: on 209M05 the
        # pocket left of balloon 7 -- clear to the eye, and clear of every
        # drawn line -- scored 15.35 mm2 of 'overlap' against the flange and
        # reducer boxes, so no label could ever be placed where the user
        # pointed.  Real outlines cost more geometry but tell the truth; the
        # solid BOM tables and hatches keep their boxes, which for them ARE
        # the truth.
        if t == "INSERT":
            curves = [LineString(pts) for pts in _flatten_entity(e)
                      if len(pts) >= 2]
            if curves:
                for c in curves:
                    obstacle_geoms.append((c, None))
                if layer in roles.fixed:
                    fixed_geoms.extend(curves)
                continue
        geom = _to_geometry(e)
        if geom is None:
            continue
        obstacle_geoms.append((geom, None))
        if layer in roles.fixed:
            fixed_geoms.append(geom)
        if layer in roles.constrained and geom.geom_type == "LineString":
            dim_lines.append(geom)

    leaders = _leader_index(mleaders)
    labels: list[Label] = []

    # --- plain text ------------------------------------------------------
    for e in text_entities:
        measured = _mtext_extents(e) if e.dxftype() == "MTEXT" else None
        if measured is not None:
            pos, size = measured
        else:
            try:
                b = bbox.extents([e], fast=True)
            except Exception:
                continue
            if not b.has_data:
                continue
            pos: Vec2 = (b.extmin.x, b.extmin.y)
            size: Vec2 = (b.size.x, b.size.y)
        center: Vec2 = (pos[0] + size[0] / 2.0, pos[1] + size[1] / 2.0)
        layer = e.dxf.layer
        labels.append(Label(
            index=len(labels), handle=e.dxf.handle,
            text=(e.dxf.text if e.dxftype() == "TEXT" else e.text),
            layer=layer, cls=roles.label_class(layer) or "other",
            anchor=_xdata_anchor(e) or _infer_anchor(center, fixed_geoms),
            size=size, pos=pos, home=pos,
            slide=(_slide_direction(center, dim_lines)
                   if layer in roles.constrained else None),
            kind="text",
        ))

    # --- dimensions ------------------------------------------------------
    for e in dim_entities:
        mt, dim_line = _dimension_parts(e, doc)
        if mt is None:
            unsupported["DIMENSION"] = unsupported.get("DIMENSION", 0) + 1
            continue
        try:
            b = bbox.extents([mt], fast=False)
        except Exception:
            unsupported["DIMENSION"] = unsupported.get("DIMENSION", 0) + 1
            continue
        pos = (b.extmin.x, b.extmin.y)
        size = (b.size.x, b.size.y)
        if dim_line is not None:
            # The dimension OWNS its line.  ISOGEN deliberately parks short
            # dims' text against (or on) their own dimension line -- '45' on
            # 209M05 sits square on its 2.4 mm line -- and scoring that as a
            # collision is the balloon-on-own-text mistake all over again.
            # Other labels are still scored against this line.
            obstacle_geoms.append((dim_line, e.dxf.handle))
            mid = dim_line.interpolate(0.5, normalized=True)
            anchor = (mid.x, mid.y)
            slide = _unit(dim_line.coords[0], dim_line.coords[-1])
        else:
            anchor = (pos[0] + size[0] / 2.0, pos[1] + size[1] / 2.0)
            slide = None

        # EVERY line the dimension draws is an obstacle, not just its centre
        # line: the witness lines, and the ticks, all live in the same
        # anonymous *D block and were invisible to the solver, so a callout
        # could be parked squarely on a witness line and nothing scored it.
        # On 353M05 that left 'DN 100X80' and 'OFFSET 13' sitting on
        # dimension lines through every pass.  Owned by this dimension, so
        # its own text is still free to sit against its own geometry.
        #
        # This block once sat BETWEEN the if-arm above and its else -- which
        # turned the else into a for/else that fired after every loop, so
        # every dimension had its anchor and slide silently overwritten:
        # slide=None (free to wander off its line) and anchor=its own centre.
        for _pts in _dim_block_curves(doc, e):
            obstacle_geoms.append((LineString(_pts), e.dxf.handle))
        labels.append(Label(
            index=len(labels), handle=e.dxf.handle,
            text=e.dxf.text or f"{e.get_measurement():.0f}",
            layer=e.dxf.layer, cls="dimension",
            anchor=anchor, size=size, pos=pos, home=pos,
            slide=slide, kind="dimension",
        ))

    # THE SOLVER MAY NOT PLACE DIMENSION TEXT WHERE THE AUDIT CALLS IT
    # DRIFT.  The slide menu offers perpendicular offsets (fine nudges, the
    # flip, the double flip) and some of them end past the category-D limit
    # -- on the first honest fleet run that manufactured a new D defect on
    # 21 of 71 sheets.  The limit is the auditor's own: max(floor, factor x
    # the sheet's median offset), derived HERE because only the extractor
    # sees every dimension, minus a margin so boundary candidates cannot
    # land exactly on the line between fixer-legal and checker-illegal.
    dim_perps = []
    for lab in labels:
        if lab.kind == "dimension" and lab.slide is not None:
            n = (-lab.slide[1], lab.slide[0])
            cx, cy = lab.home[0] + lab.size[0] / 2, lab.home[1] + lab.size[1] / 2
            dim_perps.append((lab, abs((cx - lab.anchor[0]) * n[0]
                                       + (cy - lab.anchor[1]) * n[1])))
    if dim_perps:
        med = sorted(p for _l, p in dim_perps)[len(dim_perps) // 2]
        limit = max(DIM_DRIFT_FLOOR, DIM_DRIFT_FACTOR * med) - 0.3
        for lab, _p in dim_perps:
            lab.slide_limit = limit

    # --- balloons --------------------------------------------------------
    for e in balloon_entities:
        b = _bbox_polygon(e)
        if b is None:
            unsupported["INSERT+ATTRIB"] = \
                unsupported.get("INSERT+ATTRIB", 0) + len(e.attribs)
            continue
        minx, miny, maxx, maxy = b.bounds
        pos, size = (minx, miny), (maxx - minx, maxy - miny)
        pair = _pair_leader(b, leaders)
        if pair is not None:
            _landing, tip, handle = pair
            anchor, leader_handle = tip, handle
        else:
            anchor = _infer_anchor((minx + size[0] / 2, miny + size[1] / 2),
                                   fixed_geoms)
            leader_handle = None
        labels.append(Label(
            index=len(labels), handle=e.dxf.handle,
            text="/".join(a.dxf.text for a in e.attribs),
            layer=e.dxf.layer, cls="balloon",
            anchor=anchor, size=size, pos=pos, home=pos,
            slide=None, kind="balloon", leader_handle=leader_handle,
        ))

    _group_balloons_with_their_text(labels, cfg)
    _group_callout_stacks(labels, cfg)
    _claim_orphan_leaders(labels, leaders)
    _split_multi_owner_groups(labels)

    # Prior-run LINE leaders: claim by the endpoint sitting on a label's box
    # (the landing end -- the other end is on the component).  The claimer
    # keeps its leader across rounds -- writeback redraws it instead of
    # losing it, and the audit counts the association as served.  They are
    # deliberately NOT obstacles: making them obstacles reshuffled 16 labels
    # on the second pass of the fixture (idempotence is a regression test),
    # because pass one cannot see where pass one's leaders will be drawn.
    for g in prior_leaders:
        ends = (Point(g.coords[0]), Point(g.coords[-1]))
        best_d, owner = 1.5, None
        for lab in labels:
            d = min(lab.box().distance(p) for p in ends)
            if d < best_d:
                best_d, owner = d, lab
        if owner is not None:
            owner.drawn_leader = True

    # Leaders are obstacles too -- a label parked on someone else's arrow is
    # exactly the ambiguity this tool exists to remove -- but each one carries
    # its owner's handle so a callout is never scored against its own.
    obstacle_geoms.extend(_leader_segments(mleaders))

    # Obstacles get area: lines have none, so "how much do these overlap?"
    # would always be 0.  Buffering by half the apparent line width makes the
    # metric meaningful and continuous -- which the solver hill-climbs on.
    buffered = [g.buffer(cfg.geom_buffer) for g, _owner in obstacle_geoms]
    owners = [owner for _g, owner in obstacle_geoms]

    # ISOGEN-original positions, surviving across stages (see _xdata_home).
    for lab in labels:
        ent = doc.entitydb.get(lab.handle)
        oh = _xdata_home(ent) if ent is not None else None
        lab.original_home = oh if oh is not None else lab.home

    curves = _drawn_curves(doc, msp, roles)
    allowed = _allowed_region(frame_entities, msp, cfg)
    return doc, Scene(
        labels=labels,
        obstacles=buffered,
        owners=owners,
        ink=[c.buffer(cfg.ink_halfwidth) for c in curves],
        free=_free_space(curves, obstacle_geoms, frame_entities, allowed, cfg),
        allowed=allowed,
        unmapped_text=unmapped,
        unsupported=unsupported,
    )


def _free_space(curves, obstacle_geoms, frame_entities, allowed, cfg: Tuning):
    """Rasterise the drawing so the solver can ask where a box actually fits.

    Only STATIC geometry goes in -- drawn ink and the fixed obstacles.  Labels
    are left out on purpose: they move while the solver runs, so baking them
    into the grid would make it describe a sheet that no longer exists.

    LINES ARE STAMPED ALONG THEIR PATH, NEVER BY THEIR BOUNDING BOX.  An
    isometric is nothing but long diagonals, and the bounding box of one
    diagonal is a large rectangle of mostly empty paper: filling those would
    black out the sheet and the map would report no free space anywhere.
    """
    try:
        if allowed is None:
            return None
        from .freespace import FreeSpace
        grid = FreeSpace(allowed.bounds, cfg.free_cell)
        grid.add_segments((list(c.coords) for c in curves), cfg.ink_halfwidth)
        for geom, _owner in obstacle_geoms:
            x0, y0, x1, y1 = geom.bounds
            if geom.geom_type in ("LineString", "LinearRing"):
                grid.add_segments([list(geom.coords)], cfg.geom_buffer)
            elif (x1 - x0) < 400.0 and (y1 - y0) < 400.0:
                # Solid things -- symbols, BOM tables -- fill their box.  The
                # size guard drops the title-block INSERT, whose bounding box
                # is the entire sheet and would mark every cell occupied.
                grid.add_rect(x0, y0, x1, y1)

        # THE TITLE BLOCK IS NOT FREE SPACE, even though the permitted region
        # includes it: on a real ISOGEN sheet the border and the title strip
        # live inside ONE whole-sheet INSERT, so `_allowed_region` cannot
        # subtract the strip without erasing the drawing.  The ring's 15 mm
        # reach hid that; a 30 mm free-space search would have parked labels
        # in the revision table.  Stamp what the frame actually draws --
        # every rule line, and every field of text -- so those cells are
        # occupied on their own merits.
        for e in frame_entities:
            for pts in _flatten_entity(e):
                if len(pts) >= 2:
                    grid.add_segments([pts], cfg.geom_buffer)
            try:
                subs = (e.virtual_entities() if e.dxftype() == "INSERT"
                        else [e])
            except Exception:
                subs = [e]
            for s in subs:
                if s.dxftype() not in TEXT_TYPES:
                    continue
                try:
                    b = bbox.extents([s], fast=True)
                except Exception:
                    continue
                if b.has_data:
                    grid.add_rect(b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y)
        return grid.finish()
    except Exception:
        return None                # a missing map must never break a solve


def _dim_block_curves(doc, dim) -> list:
    """Point chains for every non-text entity in a DIMENSION's geometry block."""
    name = dim.dxf.get("geometry", None)
    if not name:
        return []
    try:
        block = doc.blocks.get(name)
    except Exception:
        return []
    out = []
    for e in block:
        if e.dxftype() in TEXT_TYPES:
            continue
        for pts in _flatten_entity(e):
            if len(pts) >= 2:
                out.append(pts)
    return out


def _drawn_curves(doc, msp, roles: LayerRoles) -> list:
    """Every curve actually drawn on the sheet, unbuffered.

    Deliberately NOT the obstacle set.  Two differences matter:

      - DIMENSION WITNESS LINES ARE IN HERE.  A dimension keeps its geometry
        in an anonymous *D block, and only the dimension line itself was ever
        lifted out as an obstacle, so the witness lines -- which on a dense
        sheet like 545M05 form most of the visible grid -- were invisible to
        the solver.  That is how a callout came to be parked in the middle of
        one.
      - Component symbols contribute their real outlines, not their bounding
        boxes, so a gap inside a glyph's box reads as the gap it is.

    Annotation text is excluded: labels are scored against each other
    directly, and counting them here would double-charge.

    Buffered by the caller -- `Scene.ink` wants ribbons with area, the
    free-space grid wants the bare path to stamp.
    """
    curves = []
    for _name, _extent, cs in component_symbols(doc):
        curves.extend(cs)
    for e in msp:
        t = e.dxftype()
        if t in TEXT_TYPES or e.dxf.layer in roles.frame:
            continue
        if t in ("LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "SPLINE"):
            for pts in _flatten_entity(e):
                if len(pts) >= 2:
                    curves.append(LineString(pts))
        elif t == "DIMENSION":
            block_name = e.dxf.get("geometry", None)
            if not block_name:
                continue
            try:
                block = doc.blocks.get(block_name)
            except Exception:
                continue
            for be in block:
                if be.dxftype() in TEXT_TYPES:
                    continue
                for pts in _flatten_entity(be):
                    if len(pts) >= 2:
                        curves.append(LineString(pts))
    return curves


def _split_multi_owner_groups(labels: list[Label]) -> None:
    """A weld may contain at most ONE leader.

    The tuck and stack rules weld by geometry alone.  On 206M05_r0 a '3/9'
    balloon sat ON a CONNECTED note -- two independent callouts, each with
    its own MULTILEADER -- and the tuck rule welded them, so the overlap
    between them vanished from every score (group members are never scored
    against each other).  On 325M01_r0-3 a balloon '10' with its own arrow
    was welded to a tag with its own arrow the same way.  Two arrows means
    two callouts.  Split: each distinct leader seeds its own group; the
    leaderless members (an OPERATOR text, a tucked balloon) follow the
    nearest owner, so a genuine stack keeps its single arrow.
    """
    by_group: dict[int, list[Label]] = {}
    for l in labels:
        if l.group is not None:
            by_group.setdefault(l.group, []).append(l)
    if not by_group:
        return
    next_group = max(by_group) + 1
    for g, mem in list(by_group.items()):
        owners: dict[str, list[Label]] = {}
        for l in mem:
            if l.leader_handle:
                owners.setdefault(l.leader_handle, []).append(l)
        if len(owners) < 2:
            continue
        seeds: list[Label] = []
        for k, (_h, ls) in enumerate(owners.items()):
            gid = g if k == 0 else next_group
            if k > 0:
                next_group += 1
            for l in ls:
                l.group = gid
            seeds.append(ls[0])
        for l in mem:
            if l.leader_handle:
                continue
            near = min(seeds, key=lambda s: l.box().distance(s.box()))
            l.group = near.group


def _group_balloons_with_their_text(labels: list[Label], cfg: Tuning) -> None:
    """Weld each item balloon to the size text it belongs to.

    ISOGEN sets a balloon down on the corner of its own text.  Measured across
    209M05 the raw box overlap is 35.43 mm^2 on all six tucked pairs -- the
    same number every time, because it is a fixed layout rule -- plus one pair
    that misses by 1.20 mm and only registers through the clearance padding.
    Every one of the 7 "label<->label collisions" on that sheet was a balloon
    on its own text.  There were no genuine annotation-vs-annotation clashes
    at all.

    Pairing is by strongest claim, and it is mutual: a balloon takes the text
    it overlaps most, and a text can be claimed only once.  Near-misses within
    `cfg.pair_gap` count too, so the tuck is recognised even when ISOGEN
    leaves a hair of daylight.  Anything unclaimed stays a unit of one --
    group balloons like "11/13/7" own no size text and must keep colliding
    with their neighbours like everybody else.
    """
    balloons = [l for l in labels if l.cls == "balloon"]
    texts = [l for l in labels if l.cls == "annotation"]
    if not balloons or not texts:
        return

    claims: list[tuple[float, int, int]] = []
    for b in balloons:
        bb = b.box()
        for t in texts:
            tb = t.box()
            inter = bb.intersection(tb).area
            # Score: real overlap outranks any near-miss; among near-misses,
            # closer wins.  One scale so a single sort settles it.
            if inter > 0.0:
                score = inter
            else:
                gap = bb.distance(tb)
                if gap > cfg.pair_gap:
                    continue
                score = -gap
            claims.append((score, b.index, t.index))

    # Strongest claim first, so a contested text goes to the balloon actually
    # sitting on it rather than to whichever was extracted first.
    claims.sort(key=lambda c: (-c[0], c[1], c[2]))
    taken_b: set[int] = set()
    taken_t: set[int] = set()
    next_group = 0
    for _score, bi, ti in claims:
        if bi in taken_b or ti in taken_t:
            continue
        taken_b.add(bi)
        taken_t.add(ti)
        labels[bi].group = next_group
        labels[ti].group = next_group
        next_group += 1


def _group_callout_stacks(labels: list[Label], cfg: Tuning) -> None:
    """Weld ISOGEN's stacked callouts into one group.

    A reducer or offset callout is emitted as a COLUMN of entities -- e.g.
    'OFFSET 6' / balloon '6' / 'FOB' / 'DN 50X40' on 164M05_r0-3 -- all
    LEFT-ALIGNED to the same x (exactly, it is a layout rule) with a uniform
    ~2.5 mm gap, and ONE leader landing on the top member.  Treating the
    members as independent labels produced two failures at once: the checker
    flagged every leaderless member as a category-C defect (three phantoms
    per stack), and the solver was free to move one member without the
    others, tearing apart what a fabricator reads as one sentence.

    Membership is deliberately strict -- alignment within `stack_align`
    (coincidence at 0.3 mm is unlikely; the layout rule makes it exact),
    vertical gap within `stack_gap`, annotations and balloons only
    (dimensions slide on their own line; continuation notes are their own
    callouts).  Merging respects groups the tuck-pairing already made:
    adjacency is unioned, so a stack containing a welded balloon+text pair
    stays one unit.
    """
    idx = [l.index for l in labels
           if l.cls in ("annotation", "balloon")]
    if len(idx) < 2:
        return

    parent = {i: i for i in range(len(labels))}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    # existing groups (balloon tucked on its text) survive the merge
    by_group: dict[int, list[int]] = {}
    for l in labels:
        if l.group is not None:
            by_group.setdefault(l.group, []).append(l.index)
    for members in by_group.values():
        for m in members[1:]:
            union(members[0], m)

    for a in idx:
        la = labels[a]
        for b in idx:
            if b <= a:
                continue
            lb = labels[b]
            if abs(la.pos[0] - lb.pos[0]) > cfg.stack_align:
                continue
            # vertical gap between the two boxes (negative = overlap)
            gap = max(lb.pos[1] - (la.pos[1] + la.size[1]),
                      la.pos[1] - (lb.pos[1] + lb.size[1]))
            if -0.5 <= gap <= cfg.stack_gap:
                union(a, b)

    # re-number: every component of 2+ becomes a group
    comps: dict[int, list[int]] = {}
    for i in range(len(labels)):
        comps.setdefault(find(i), []).append(i)
    next_group = 0
    for members in comps.values():
        if len(members) < 2:
            continue
        for m in members:
            labels[m].group = next_group
        next_group += 1


def _allowed_region(frame_entities, msp, cfg: Tuning) -> Polygon | None:
    """The region labels are permitted to occupy.

    Derived from the drawing, never configured.  Two shapes of drawing:

    FIXTURE-STYLE -- the border and reserved blocks are closed polylines.
        Largest = sheet border, anything inside it = reserved, subtract it.

    REAL ISOGEN -- the border is inside a 'title block' INSERT whose bounding
        box IS the whole sheet, and the BOM is an ACAD_TABLE.  Subtracting the
        title block would erase the drawing, so we take the sheet extents and
        subtract only the frame entities that are SMALL relative to the sheet
        (the tables and title strip), keeping the ones that are the sheet.
    """
    polys = _closed_polygons(frame_entities)
    if polys:
        border = max(polys, key=lambda p: p.area)
        region = border.buffer(-cfg.frame_margin)
        holes = [p for p in polys if p is not border
                 and border.contains(p.representative_point())]
        if holes:
            region = region.difference(unary_union(holes))
        return region if not region.is_empty else None

    if not frame_entities:
        return None
    try:
        b = bbox.extents(msp, fast=True)
    except Exception:
        return None
    if not b.has_data:
        return None
    sheet = shapely_box(b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y)
    region = sheet.buffer(-cfg.frame_margin)
    reserved = []
    for e in frame_entities:
        p = _bbox_polygon(e)
        if p is not None and p.area < 0.4 * sheet.area:
            reserved.append(p)
    if reserved:
        region = region.difference(unary_union(reserved))
    return region if not region.is_empty else None
