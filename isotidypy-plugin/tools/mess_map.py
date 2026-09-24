"""
Map every reason a checker would call an isometric "messy", by category.

"It's messy" is a symptom.  This turns it into a list of located, named,
counted defects a piping lead can argue with.

WHY THE MARKERS ARE ALL ONE COLOUR.  An ISOGEN sheet has already spent the
spectrum: amber pipe, magenta dimensions, red welds, green valves, yellow
supports, white text.  That leaves a narrow blue band, and inside it the hues
are not separable -- measured, blue #3987e5 vs violet #9085e9 is dE 1.9 for
protanopia and 9.8 for normal vision, both far under the floor of 15.  So
colour cannot carry six categories here.  It carries ONE bit -- "defect" --
and the CODE LETTER carries the category.  That is the "never colour alone"
rule doing real work rather than being recited.

Categories, and what each costs the reader:

  A  OVERLAP           text on text or text on geometry.  Unreadable.
  B  LEADER CROSSING   a leader crosses another leader, a label, or drawn
                       component linework.  Reads as though the callouts are
                       swapped -- a traceability defect, not a cosmetic one.
                       (Component linework was a blind spot until 2026-08-18:
                       a leader cutting across a valve glyph scored B=0 and
                       the user caught it by eye.  The arrow TIP touching the
                       part it tags is pointing, not crossing -- exempt within
                       TIP_TOLERANCE.)
  C  WEAK ASSOCIATION  a label sits far from its part with NO leader.  The
                       reader cannot tell what it labels.
  D  DIMENSION DRIFT   dimension text sits off its own dimension line.
  E  SYMBOL CLUTTER    component symbols overlap in the 2D projection.  MOSTLY
                       CONVENTION (an operator belongs on its valve), flagged
                       for a human, never auto-"fixed".
  -  SHEET USE         global: how much of the sheet the drawing occupies.

Run:
    python tools/mess_map.py <file.dxf> <out.png> [--site=jp1071]
"""

from __future__ import annotations

import math
import sys

# The console must never kill the pipeline: JP1071 labels contain the
# centreline symbol (U+2104), which Windows' cp1252 stdout cannot encode --
# one print() of a label name crashed two production runs.  Degrade the
# glyph, not the run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ezdxf
import matplotlib.pyplot as plt
from ezdxf import bbox as ezb
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
from ezdxf.addons.drawing.properties import LayoutProperties
from matplotlib.patches import Circle
from shapely.geometry import LineString, Point
from shapely.geometry import box as sbox

from isotidy import DEFAULT_ROLES, JP1071_ROLES, Tuning, detect
from isotidy.extract import (component_symbols, extract, _dimension_parts,
                             _to_geometry)
from isotidy.leaders import symbol_hits

SITES = {"default": DEFAULT_ROLES, "jp1071": JP1071_ROLES}

SURFACE = "#1a1a19"
INK = "#f5f4f0"
INK_DIM = "#c3c2b7"
#: The one hue ISOGEN leaves free.  Measured worst-case dE 24.6 against every
#: colour the drawing itself uses -- see module docstring.
ALERT = "#3987e5"
#: Category E is usually convention, so it is deliberately recessive: a
#: reviewer should notice it last, not first.
ALERT_SOFT = "#6b7a8f"

CATEGORIES = {
    "A": ("OVERLAP", "text on text / text on geometry"),
    "B": ("LEADER CROSSING", "leader crosses leader, label, or component"),
    "C": ("WEAK ASSOCIATION", "label far from its part with no leader"),
    "D": ("DIMENSION DRIFT", "dimension text off its own dimension line"),
    "E": ("SYMBOL CLUTTER", "symbols overlap in projection - usually convention"),
}

FAR_FROM_PART = 25.0     # mm; beyond this a label needs a leader to be traceable
# Drift floor/factor are SHARED with the extractor (Label.slide_limit) so the
# solver can never place text where this audit will flag it.
from isotidy.config import DIM_DRIFT_FLOOR as DIM_DRIFT           # noqa: E402
from isotidy.config import DIM_DRIFT_FACTOR                        # noqa: E402
TIP_TOLERANCE = 3.0      # mm; a leader ending at its component is not a crossing


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------
def flatten_attribs(msp) -> int:
    """Turn block ATTRIBs into standalone TEXT before rendering.

    ezdxf's matplotlib backend draws an INSERT's attached ATTRIBs at their
    PRE-TRANSLATION position: translate the INSERT and the balloon circle
    moves while its number stays behind.  Reproduced in isolation -- one
    translate() on one AnnoCircle and the '5' renders 18 mm from its ring.
    The DXF is correct (dxf.insert AND dxf.align_point both move, so AutoCAD
    draws it properly); only this renderer is wrong.

    That matters more than a cosmetic glitch: every diagnostic PNG of a
    SOLVED drawing showed balloons apparently torn apart, which is exactly
    the defect the tool is supposed to be removing.  Re-emitting the attribs
    as plain TEXT at their own coordinates bypasses the broken path.

    Mutates the in-memory doc only -- never the file being audited.
    """
    from ezdxf.enums import TextEntityAlignment
    n = 0
    for ins in list(msp.query("INSERT")):
        if not ins.attribs:
            continue
        for a in list(ins.attribs):
            try:
                # Honour the attribute's OWN alignment.  Forcing every one to
                # MIDDLE_CENTER re-centred the left-aligned title-block values
                # on their insert point, so "1000 / 450" rendered doubled over
                # itself -- a fix for one render bug introducing another.
                centred = a.dxf.halign == 1 and a.dxf.valign == 2
                p = (a.dxf.align_point
                     if centred and a.dxf.hasattr("align_point")
                     else a.dxf.insert)
                t = msp.add_text(a.dxf.text, dxfattribs={
                    "layer": a.dxf.layer,
                    "height": a.dxf.height,
                    "rotation": a.dxf.rotation,
                    "style": a.dxf.style,
                })
                t.set_placement(
                    (p.x, p.y),
                    align=(TextEntityAlignment.MIDDLE_CENTER if centred
                           else TextEntityAlignment.LEFT))
                if a.dxf.hasattr("color"):
                    t.dxf.color = a.dxf.color
                n += 1
            except Exception:
                continue
        try:
            ins.delete_all_attribs()
        except Exception:
            pass
    return n


def _leaders(msp):
    """(handle, tip, full path) per leader.

    The path is the WHOLE vertex chain plus the landing -- not tip-to-landing.
    The endpoint shortcut silently erased elbows, so a leader correctly
    re-routed AROUND an obstacle still 'crossed' it in the audit: the checker
    would have kept reporting a defect the file no longer has.
    """
    out = []
    for ml in msp.query("MULTILEADER"):
        try:
            ctx = ml.context
            for ld in ctx.leaders:
                lp = ld.last_leader_point
                for ln in ld.lines:
                    if ln.vertices:
                        pts = [(v.x, v.y) for v in ln.vertices]
                        pts.append((lp.x, lp.y))
                        out.append((ml.dxf.handle,
                                    (pts[0][0], pts[0][1]),
                                    LineString(pts)))
                        break
        except Exception:
            continue
    return out


# Component-symbol geometry and the leader-vs-symbol rule live in isotidy/ --
# one definition, and the portable core is where they belong for the C# port.


def _dim_text_offset(e, doc):
    """(distance, x, y, text) of a dimension's text from its own dim line.

    ONE DEFINITION OF "ITS LINE", shared with the extractor: the block LINE
    carrying the arrowheads (`extract._dimension_parts`).  This used to build
    the line from the DEFPOINTS (direction defpoint2 -> defpoint3), which is
    right for an ALIGNED dimension but wrong for the ROTATED ones ISOGEN
    emits (dimtype 160): there defpoint2/3 are the measured points, diagonal
    to the drawn line.  Measured on 164M05_r0-3, every dim sits exactly
    2.78 mm from its drawn line -- ISOGEN's house offset -- while the
    defpoint line scattered 0.2..7.1 mm and reported 2 phantom drifts.
    """
    texts_line = _dimension_parts(e, doc)
    mt, line = texts_line
    if mt is None or line is None:
        return None
    b = ezb.extents([mt], fast=False)
    mid = Point((b.extmin.x + b.extmax.x) / 2, (b.extmin.y + b.extmax.y) / 2)
    # Extend the (finite) drawn line so text slid past its end still measures
    # perpendicular offset, not distance to the endpoint.
    (x1, y1), (x2, y2) = line.coords[0], line.coords[-1]
    dx, dy = x2 - x1, y2 - y1
    L = (dx * dx + dy * dy) ** 0.5
    if L < 1e-6:
        return None
    ux, uy = dx / L, dy / L
    ext = LineString([(x1 - ux * 500, y1 - uy * 500),
                      (x2 + ux * 500, y2 + uy * 500)])
    return ext.distance(mid), mid.x, mid.y, e.dxf.text


def own_line_contact(path: Path, roles, cfg: Tuning) -> tuple[int, float]:
    """(labels touching their OWN dimension line / leader, total mm2).

    Not a defect -- it is ISOGEN's convention -- but it is contact the reader
    looks through, so it is measured separately and reported so any reduction
    is visible rather than asserted.
    """
    _d, scene = extract(path, roles, cfg)
    n, area = 0, 0.0
    for i, lab in enumerate(scene.labels):
        b = lab.box(cfg.clearance)
        exempt = scene.own_exempt(i)
        allo = sum(b.intersection(o).area
                   for o in scene.obstacles_near(b) if b.intersects(o))
        oth = sum(b.intersection(o).area
                  for o in scene.obstacles_near(b, exempt) if b.intersects(o))
        own = max(0.0, allo - oth)
        if own > 1e-6:
            n += 1
            area += own
    return n, area


def audit(path: Path, roles, cfg: Tuning) -> tuple[list, dict]:
    """Return (findings, notes).  A finding is (code, x, y, description)."""
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    _d, scene = extract(path, roles, cfg)
    found: list[tuple[str, float, float, str]] = []

    # --- A: true overlaps ------------------------------------------------
    for c in detect(scene, cfg).collisions:
        lab = scene.labels[c.a]
        x, y = lab.center()
        other = (scene.labels[c.b].text[:14] if c.b is not None else "geometry")
        found.append(("A", x, y,
                      f"{lab.text[:16]!r} on {other} - {c.area:.2f} mm2"))

    # --- B: leader crossings ---------------------------------------------
    lds = _leaders(msp)
    for i in range(len(lds)):
        for j in range(i + 1, len(lds)):
            if lds[i][2].crosses(lds[j][2]):
                p = lds[i][2].intersection(lds[j][2])
                if p.geom_type == "Point":
                    # Two leaders fanning out from ONE shared arrowhead
                    # differ in their stored tips only by float noise
                    # (1e-12 mm on 202M01), which turns their common
                    # origin into a phantom micro-crossing.  An
                    # intersection AT both tips is shared-tip contact,
                    # not a crossing.
                    ta, tb = lds[i][2].coords[0], lds[j][2].coords[0]
                    if (abs(p.x - ta[0]) < 0.5 and abs(p.y - ta[1]) < 0.5
                            and abs(p.x - tb[0]) < 0.5
                            and abs(p.y - tb[1]) < 0.5):
                        continue
                    found.append(("B", p.x, p.y, "leader crosses leader"))
    # GROUP-AWARE, for the same reason C is: a callout is a balloon tucked on
    # its size text sharing ONE leader, so that leader necessarily runs to --
    # and often through -- its own text.  Charging it reported a defect every
    # time the solver correctly re-landed a grouped balloon.
    owner_group = {}
    for lab in scene.labels:
        if lab.leader_handle:
            owner_group[lab.leader_handle] = (lab.group, lab.index)
    for h, tip, line in lds:
        grp, oidx = owner_group.get(h, (None, None))
        for lab in scene.labels:
            if lab.index == oidx:
                continue
            if grp is not None and lab.group == grp:
                continue
            if line.crosses(lab.box()):
                x, y = lab.center()
                found.append(("B", x, y,
                              f"leader crosses label {lab.text[:16]!r}"))
    # Leader over a drawn COMPONENT SYMBOL.  Found by the user's eye on
    # 209M05, not by any checker: an elbowed leader cleared both leader
    # crossings and cut across the valve glyph instead, and B=0 stayed true.
    # `symbol_hits` owns the two exemptions (arrow tip, and a leader that
    # terminates inside the symbol it attaches to).
    symbols = component_symbols(doc)
    for h, tip, line in lds:
        landing = line.coords[-1]
        for name, where in symbol_hits(line, tip, landing, symbols,
                                       TIP_TOLERANCE):
            found.append(("B", where.x, where.y,
                          f"leader crosses component {name.split('-')[0]}"))

    # --- C: far from part with no leader ---------------------------------
    # GROUP-AWARE.  ISOGEN tucks a balloon onto its own size text and gives
    # the PAIR one leader, usually attached to the text.  Checking per label
    # reported balloon '4' as untraceable when its group's leader made it
    # perfectly traceable -- a false defect produced by the checker, not by
    # the drawing.
    for lab in scene.labels:
        d = lab.anchor_distance()
        if d <= FAR_FROM_PART:
            continue
        mates = ([lab] if lab.group is None else
                 [o for o in scene.labels if o.group == lab.group])
        if any(m.leader_handle or m.drawn_leader for m in mates):
            continue
        # A welded stack is ONE callout: if any member sits close to the
        # part, the reader traces the whole column from there.  Charging the
        # tail members of a valve-tag stack whose head is adjacent to its
        # valve reported defects no drafter would fix (balloon '5' on
        # 164M05_r0-2, 26 mm out, two group mates nearer).
        if any(m.anchor_distance() <= FAR_FROM_PART for m in mates):
            continue
        x, y = lab.center()
        found.append(("C", x, y,
                      f"{lab.text[:16]!r} sits {d:.0f} mm from its part, "
                      f"no leader"))

    # --- D: dimension text off its own line ------------------------------
    # The dimension line is derived from the DEFPOINTS, not from "the longest
    # LINE in the geometry block" -- on short dimensions the witness lines are
    # longer than the dimension line, and that heuristic reported 11 false
    # defects out of 18 before this was fixed.
    # THE THRESHOLD COMES FROM THE DRAWING, NOT FROM ME.  ISOGEN offsets every
    # dimension text from its line by a house standard -- median 2.75 mm on
    # this sheet.  A fixed 3.0 mm rule sat just above that norm and flagged the
    # one dimension that was 1.7 mm past it as "drift", which is measuring the
    # convention rather than a defect.  Drift is now judged against the sheet's
    # own median, so only a genuine outlier is reported.
    offsets = []
    for e in msp.query("DIMENSION"):
        r = _dim_text_offset(e, doc)
        if r is not None:
            offsets.append(r)
    if offsets:
        med = sorted(d for d, _x, _y, _t in offsets)[len(offsets) // 2]
        limit = max(DIM_DRIFT, med * DIM_DRIFT_FACTOR)
        for d, x, y, txt in offsets:
            if d > limit:
                found.append(("D", x, y,
                              f"dim {txt!r} sits {d:.1f} mm off its line "
                              f"(sheet norm {med:.1f} mm)"))

    # --- E: symbol clutter (projection artifact) -------------------------
    sym = []
    for e in msp.query("INSERT"):
        if e.dxf.name.startswith(("Anno", "title")):
            continue
        b = ezb.extents([e], fast=False)
        if b.has_data and b.size.x < 50 and b.size.y < 50:
            sym.append((e.dxf.name,
                        sbox(b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y)))
    clutter = []
    for i in range(len(sym)):
        for j in range(i + 1, len(sym)):
            a = sym[i][1].intersection(sym[j][1]).area
            if a > 1.0:
                c = sym[i][1].intersection(sym[j][1]).centroid
                clutter.append((a, c.x, c.y, f"{sym[i][0]} / {sym[j][0]}"))
    clutter.sort(reverse=True)
    for a, x, y, what in clutter[:8]:
        found.append(("E", x, y, f"{what} - {a:.0f} mm2"))

    # --- global notes -----------------------------------------------------
    pipe = [e for e in msp if e.dxf.layer in roles.fixed]
    n_own, a_own = own_line_contact(path, roles, cfg)
    notes = {"symbol_clutter_total": len(clutter),
             "labels": len(scene.labels),
             "own_line_n": n_own, "own_line_area": a_own}
    if pipe:
        pb = ezb.extents(pipe, fast=True)
        sheet = ezb.extents(msp, fast=True)
        if pb.has_data and sheet.has_data:
            notes["sheet_use"] = 100.0 * (pb.size.x * pb.size.y) / \
                (sheet.size.x * sheet.size.y)
    return found, notes


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------
def render_map(path: Path, out: Path, site: str = "jp1071") -> None:
    roles = SITES[site]
    cfg = Tuning()                      # NATIVE drawing scale, no print derivation
    found, notes = audit(path, roles, cfg)

    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    flatten_attribs(msp)      # see that function's docstring -- renderer bug

    # Three bands that never share space: header / drawing / legend.  The
    # first version of this let the header float over the drawing axes and
    # the legend columns run into each other -- a messiness report that was
    # itself unreadable.
    fig = plt.figure(figsize=(22, 17), facecolor=SURFACE)
    ax = fig.add_axes([0.005, 0.285, 0.99, 0.645])
    ax.set_facecolor(SURFACE)
    ax.set_axis_off()

    lp = LayoutProperties.from_layout(msp)
    lp.set_colors(SURFACE)
    # adjust_figure=False is NOT optional here.  Left at its default, the
    # backend resizes the whole figure to the drawing's aspect ratio and
    # discards figsize -- a 22x17 canvas silently became 6.8x4.8, so every
    # font sized for the big canvas rendered ~3x too large and the panels
    # collided.  Two layout "fixes" chased that symptom before measuring the
    # saved PNG (956x672, not 3080x2380) found the cause.
    Frontend(RenderContext(doc),
             MatplotlibBackend(ax, adjust_figure=False)).draw_layout(
        msp, finalize=True, layout_properties=lp)

    ax.set_aspect("equal", adjustable="datalim")
    seq = _mark(fig, ax, found)
    _panel(fig, path, found, notes, seq)
    fig.savefig(out, dpi=140, facecolor=SURFACE)
    plt.close(fig)
    print(f"mess map -> {out}")
    for code in sorted(seq):
        print(f"  {code} {CATEGORIES[code][0]:<18} {seq[code]}")


def _mark(fig, ax, found) -> dict[str, int]:
    """Ring every defect at its true location, then place its code badge so
    that NO TWO BADGES OVERLAP.

    This is the same problem the tool exists to solve, so it is solved the
    same way: a ring of candidate offsets round the anchor, greedy placement,
    most-crowded first, and a leader back to the ring when the badge had to
    travel.  A defect map whose own labels pile up in the congested corner
    would be unable to show you the congested corner.

    Placement runs in DISPLAY PIXELS, not drawing units.  Badge size comes
    from the font size, which is in points, and the data-to-points ratio
    depends on the axes aspect fit -- computing it in drawing units means
    deriving a number the renderer already knows.
    """
    fig.canvas.draw()                       # transforms are stale until drawn
    px_per_pt = fig.dpi / 72.0

    order = sorted(range(len(found)), key=lambda i: found[i][0])
    seq: dict[str, int] = {}
    tags = []
    for i in order:
        code, x, y, _d = found[i]
        seq[code] = seq.get(code, 0) + 1
        soft = code == "E"
        fs = 10.0 if soft else 13.0
        label = f"{code}{seq[code]}"
        w = (0.62 * fs * len(label) + 7) * px_per_pt   # +padding
        h = (fs * 1.5) * px_per_pt
        anchor = ax.transData.transform((x, y))
        tags.append({"label": label, "soft": soft, "fs": fs,
                     "anchor": anchor, "w": w, "h": h,
                     "r": (5.0 if soft else 7.0)})

    # Rings mark the truth and never move, so they are obstacles for every
    # badge -- including their own, which is why a badge never sits on top of
    # the very thing it is pointing at.
    rings = [sbox(t["anchor"][0] - 9, t["anchor"][1] - 9,
                  t["anchor"][0] + 9, t["anchor"][1] + 9) for t in tags]

    radii = (16.0, 26.0, 38.0, 52.0, 70.0, 92.0, 118.0, 150.0)

    def _best_slot(t, others):
        """Cheapest legal slot for one badge.

        OVERLAP IS PRICED 1000x ABOVE DISTANCE, and the early exit fires only
        on a genuinely clean slot.  The first version weighted them
        comparably and broke out at "good enough", so two badges settled with
        a few px2 of overlap each -- invisible to me, caught by the check
        below.  Separation is the point of this routine; it does not get to
        be traded against tidiness.
        """
        ax_, ay_ = t["anchor"]
        best, best_ov, best_cost = None, None, None
        for r in radii:
            for k in range(16):
                th = 2.0 * math.pi * k / 16.0
                cx = ax_ + math.cos(th) * (r + t["w"] / 2.0)
                cy = ay_ + math.sin(th) * (r + t["h"] / 2.0)
                box = sbox(cx - t["w"] / 2, cy - t["h"] / 2,
                           cx + t["w"] / 2, cy + t["h"] / 2)
                ov = sum(box.intersection(p).area for p in others
                         if box.intersects(p))
                ov += sum(box.intersection(g).area for g in rings
                          if box.intersects(g))
                cost = ov * 1000.0 + r
                if best_cost is None or cost < best_cost:
                    best_cost, best_ov, best = cost, ov, (cx, cy, box)
            if best_ov is not None and best_ov <= 0.0:
                break                       # truly clean slot -- stop widening
        return best

    placed: list = [None] * len(tags)
    for idx, t in enumerate(tags):
        cx, cy, box = _best_slot(t, [p for p in placed if p is not None])
        placed[idx] = box
        t["pos"] = (cx, cy)

    # Greedy is order-dependent: a badge placed early can box in a later one.
    # Re-place any badge that still overlaps, now that every other position is
    # known.  Converges in one or two sweeps at this scale.
    for _sweep in range(4):
        bad = [i for i in range(len(placed))
               if any(placed[i].intersects(placed[j])
                      and placed[i].intersection(placed[j]).area > 1.0
                      for j in range(len(placed)) if j != i)]
        if not bad:
            break
        for i in bad:
            others = [placed[j] for j in range(len(placed)) if j != i]
            cx, cy, box = _best_slot(tags[i], others)
            placed[i] = box
            tags[i]["pos"] = (cx, cy)

    # Verify, do not assume.  This map's whole claim is "here is every defect,
    # legibly" -- badges that quietly landed on each other would break exactly
    # the promise the picture is making, and it is one line to check.
    clashes = [(tags[i]["label"], tags[j]["label"],
                placed[i].intersection(placed[j]).area)
               for i in range(len(placed)) for j in range(i + 1, len(placed))
               if placed[i].intersects(placed[j])
               and placed[i].intersection(placed[j]).area > 1.0]
    if clashes:
        print(f"  !! {len(clashes)} badge pairs still overlap: "
              + ", ".join(f"{a}/{b}" for a, b, _ in clashes[:6]))
    else:
        print(f"  badge placement: {len(placed)} markers, none overlapping")

    inv = ax.transData.inverted()
    for t in tags:
        colour = ALERT_SOFT if t["soft"] else ALERT
        ax_, ay_ = t["anchor"]
        cx, cy = t["pos"]
        p_anchor = inv.transform((ax_, ay_))
        p_badge = inv.transform((cx, cy))
        ax.add_patch(Circle(p_anchor, t["r"], fill=False, edgecolor=colour,
                            linewidth=2.2 if not t["soft"] else 1.3,
                            zorder=60))
        # Leader only when the badge actually travelled -- a connector on a
        # badge already touching its ring is noise.
        if math.hypot(cx - ax_, cy - ay_) > 26:
            ax.plot([p_anchor[0], p_badge[0]], [p_anchor[1], p_badge[1]],
                    color=colour, linewidth=0.9, zorder=59, alpha=0.75)
        ax.text(p_badge[0], p_badge[1], t["label"], color=colour,
                fontsize=t["fs"], fontweight="bold", family="monospace",
                ha="center", va="center", zorder=61,
                bbox=dict(boxstyle="square,pad=0.22", fc=SURFACE,
                          ec=colour, lw=1.1))
    return seq


def _panel(fig, path: Path, found, notes, seq) -> None:
    """Header and legend as SINGLE pre-formatted monospace blocks.

    Hand-placing each line at a guessed figure fraction produced a report
    about messiness that was itself unreadable -- twice.  One text object with
    newlines and space-padded columns cannot overlap itself: alignment becomes
    structural rather than a calculation that has to come out right.
    """
    total = sum(v for k, v in seq.items() if k != "E")

    head = [path.name, ""]
    head.append(f"{notes.get('labels', 0)} annotations     "
                f"{total} defects to fix     "
                f"{seq.get('E', 0)} projection items to review")
    if "own_line_n" in notes:
        head.append(f"own-line contact (not a defect, ISOGEN convention): "
                    f"{notes['own_line_n']} labels, "
                    f"{notes['own_line_area']:.1f} mm2")
    if "sheet_use" in notes:
        head.append(f"drawing occupies {notes['sheet_use']:.0f}% of the sheet "
                    f"- the rest is empty space the annotations could use")
    fig.text(0.010, 0.995, "\n".join(head), color=INK, fontsize=13,
             va="top", ha="left", family="monospace", linespacing=1.7)

    # Three fixed-width columns, two rows.  W is the column width in
    # CHARACTERS -- the only unit that guarantees monospace columns line up.
    W = 42
    order = ["A", "B", "C", "D", "E"]
    cells: dict[str, list[str]] = {}
    for code in order:
        name, meaning = CATEGORIES[code]
        n = seq.get(code, 0)
        lines = [f"{code}  {name}  ({n})", f"   {meaning}"]
        items = [d for c, _x, _y, d in found if c == code]
        for k, d in enumerate(items[:6]):
            lines.append(f"   {code}{k+1} {d[:W-8]}")
        if len(items) > 6:
            lines.append(f"   ... +{len(items) - 6} more")
        cells[code] = lines

    block = ["WHERE IT IS MESSY, AND WHAT KIND", ""]
    for row in (order[:3], order[3:]):
        depth = max(len(cells[c]) for c in row)
        for i in range(depth):
            line = ""
            for c in row:
                seg = cells[c][i] if i < len(cells[c]) else ""
                line += seg[:W].ljust(W)
            block.append(line.rstrip())
        block.append("")
    fig.text(0.010, 0.262, "\n".join(block), color=INK_DIM, fontsize=9.5,
             va="top", ha="left", family="monospace", linespacing=1.5)


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    site = next((a.split("=", 1)[1] for a in sys.argv
                 if a.startswith("--site=")), "jp1071")
    render_map(Path(argv[0]), Path(argv[1]), site)
