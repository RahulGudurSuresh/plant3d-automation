"""
Rasterise an isometric DXF to PNG for human inspection.

This is a DIAGNOSTIC view, not a CAD deliverable, so it deliberately ignores
the drawing's own ACI colours (ACI 2 yellow and ACI 4 cyan are near-invisible
on white) and repaints everything by semantic role.

Three rules drive the palette:

  1. GREY = IMMOVABLE, COLOUR = MOVABLE.
     Pipe centreline, component symbols, sheet frame and BOM are fixed
     obstacles and stay neutral.  Anything the solver is allowed to move
     carries a hue.  You can see the solver's search space at a glance.

  2. TEXT WEARS TEXT COLOUR, NEVER THE CLASS COLOUR.
     The glyphs are the content -- they render near-black so they are
     legible.  Class identity is carried by a tinted bounding box behind the
     label instead.  (Measured: the aqua slot is 2.74:1 on white, well under
     the 3:1 floor.  Coloured glyphs would repeat the bug we are fixing.)

  3. FULL STRENGTH ON THE OUTLINE, A TINT IN THE FILL.
     The box sits *under* the label text, so its fill must stay close to the
     surface or it eats the glyphs.  That leaves the outline as the only place
     a validated hue can appear at the strength it was validated at, so the
     outline is what carries identity.  Fills are blended toward the surface
     explicitly rather than with `alpha=`, so the composited colour stays a
     value we can measure -- see `_tint`.

The bounding box is not decoration: it is the exact rectangle the collision
detector consumes, so this render doubles as a debug view of the model.

Run:
    python tools/render.py fixtures/iso_congested.dxf out.png
    python tools/render.py fixtures/iso_congested.dxf zoom.png 40 30 140 100
"""

from __future__ import annotations

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

from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ezdxf
import matplotlib.pyplot as plt
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
from ezdxf.addons.drawing.properties import LayoutProperties
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle

from isotidy import DEFAULT_ROLES, JP1071_ROLES, Tuning, detect, extract

SITES = {"default": DEFAULT_ROLES, "jp1071": JP1071_ROLES}

# --------------------------------------------------------------------------
# Palette -- validated categorical slots, light surface #fcfcfb.
# The validated set is the four class hues PLUS the clash red; see the note on
# CLASS_COLOURS below for the run and why orange is not in it.
#   node scripts/validate_palette.js "#2a78d6,#4a3aa7,#1baf7a,#eda100,#d03b3b" \
#        --mode light --pairs all --surface "#fcfcfb"
#   -> ALL PASS (worst CVD dE 9.1, worst normal-vision dE 16.3)
# --------------------------------------------------------------------------
SURFACE = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"     # label glyphs
INK_SECONDARY = "#52514e"   # pipe centreline
INK_MUTED = "#898781"       # symbols
CHROME = "#c3c2b7"          # sheet frame, BOM -- most recessive

CLASS_ANNOTATION = "#2a78d6"    # slot 1, blue
CLASS_WELD = "#4a3aa7"          # slot 7, violet
CLASS_DIMENSION = "#1baf7a"     # slot 3, aqua
CLASS_CONTINUATION = "#eda100"  # slot 4, yellow

# Reserved status colour -- collisions only, never a class.
STATUS_CRITICAL = "#d03b3b"

#: Colour by LABEL CLASS, not by layer name -- layer names are site-specific,
#: classes are not.
#:
#: THE STATUS COLOUR IS PART OF THE PALETTE, SO IT IS VALIDATED WITH IT.
#: Validating the four classes alone passes and still ships a broken picture:
#: every box that matters is red, so red is the colour the eye must separate
#: from the other four.  Measured with the clash colour in the set:
#:
#:   node scripts/validate_palette.js "#2a78d6,#eb6834,#1baf7a,#4a3aa7,#d03b3b" \
#:        --mode light --pairs all --surface "#fcfcfb"
#:   -> FAIL  normal-vision #d03b3b <-> #eb6834  dE 10.8  (floor is 15)
#:
#: Orange sits 10.8 from the clash red -- under the floor, so a clashing label
#: and an ordinary weld balloon were near-indistinguishable.  That was the
#: actual bug in the first 209M05 render, not a matter of taste.  Dropping
#: orange for violet clears it:
#:
#:   node scripts/validate_palette.js "#2a78d6,#4a3aa7,#1baf7a,#eda100,#d03b3b" \
#:        --mode light --pairs all --surface "#fcfcfb"
#:   -> ALL PASS  (worst normal-vision dE 16.3, worst CVD dE 9.1)
#:
#: Yellow takes the continuation slot rather than the weld slot deliberately:
#: it is the weakest hue on white (1.79:1), and continuations are a handful of
#: large callout blocks where the tinted fill carries the identity, whereas
#: welds are ~18 small balloons that need the stronger outline.
CLASS_COLOURS = {
    "annotation": CLASS_ANNOTATION,
    "weld": CLASS_WELD,
    "balloon": CLASS_WELD,
    "dimension": CLASS_DIMENSION,
    "continuation": CLASS_CONTINUATION,
}

# layer -> (colour, lineweight_mm, movable?, legend label)
ROLES = {
    "ISO_FRAME":      (CHROME,        0.25, False, "Frame / BOM"),
    "ISO_PIPE":       (INK_SECONDARY, 0.60, False, "Pipe centreline"),
    "ISO_SYMBOL":     (INK_MUTED,     0.35, False, "Components"),
    "ISO_ANNOTATION": (CLASS_ANNOTATION, 0.25, True, "Component labels"),
    "ISO_WELD":       (CLASS_WELD,      0.25, True, "Weld numbers"),
    "ISO_DIMENSION":  (CLASS_DIMENSION, 0.25, True, "Dimension text"),
    "ISO_LEADER":     (INK_MUTED,       0.25, True, "Leaders"),
}

TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB"}


# --------------------------------------------------------------------------
# Themes
#
# "native" (default) renders the sheet the way AutoCAD does: black paper, and
# every layer in the colour ISOGEN gave it.  That is what the engineers who
# read these drawings already know by heart -- amber pipe, magenta dimensions,
# red welds, green valves -- and repainting it into greys, however principled
# the greys were, made the geometry harder to read, not easier.
#
# THE DRAWING'S OWN COLOURS ARE PART OF THE PALETTE, SO THE OVERLAY IS CHOSEN
# AGAINST THEM.  Surveyed off the delivered DWG:
#
#     Pipe #ff7f00 (ACI 30)      Dimension #ff00ff        Weld #ff0000
#     Valve/Symbol #00ff00       Supports #ffff00         Annotation #ffffff
#
# Measured against that set on #1a1a19, the reserved clash red FAILS -- it
# lands ΔE 9.0 from ISOGEN's own weld red, and welds are among the very labels
# being flagged, so a red box round a red balloon was the worst available
# choice.  Blue is the one hue ISOGEN leaves free, and it measures furthest
# from every colour on the sheet:
#
#     #d03b3b  vs natives -> dE  9.0 vs #ff0000   FAIL
#     #3987e5  vs natives -> dE 24.6 (worst, vs #c0c0c0)   PASS
#     #9085e9  vs natives -> dE 20.0
#     #00b7ff  vs natives -> dE 17.3
#
# Because blue does not *mean* "bad", the clash marker never leans on hue: it
# is also the only hatched box on the sheet, the only one with a heavy ring,
# and it is named in the legend.
#
# Class colours are dropped in this theme on purpose.  ISOGEN already encodes
# class in the geometry -- magenta IS the dimension, red IS the weld -- so a
# second class encoding on top would be eight competing hues to say what the
# drawing already says.  The overlay answers one question: is this label in
# trouble or not.
# --------------------------------------------------------------------------
DARK_SURFACE = "#1a1a19"
DARK_INK = "#f5f4f0"        # caption headline
DARK_INK_DIM = "#c3c2b7"    # caption body


@dataclass(frozen=True)
class Theme:
    surface: str
    ink: str
    ink_dim: str
    #: Keep the drawing's own ACI colours instead of repainting by role.
    native: bool
    #: Outline for a label the solver tracks that is not in trouble.
    tracked: str
    #: Outline + hatch for a label in collision.
    clash: str
    #: How far the clash fill is blended toward the surface.
    clash_fill: float


THEMES = {
    "native": Theme(
        surface=DARK_SURFACE, ink=DARK_INK, ink_dim=DARK_INK_DIM,
        native=True, tracked=INK_MUTED, clash="#3987e5", clash_fill=0.30,
    ),
    # The original semantic view: white paper, geometry repainted to the
    # grey=immovable / colour=movable rule, class colour on every label box.
    "semantic": Theme(
        surface=SURFACE, ink=INK_PRIMARY, ink_dim=INK_SECONDARY,
        native=False, tracked=INK_MUTED, clash=STATUS_CRITICAL,
        clash_fill=0.34,
    ),
}


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _tint(colour: str, amount: float, surface: str = SURFACE) -> str:
    """Blend `colour` toward the surface and return the resulting hex.

    Deliberately NOT matplotlib's `alpha=`.  The palette is validated at full
    strength; drawing it at alpha 0.16 silently repaints every slot into a
    pastel nobody validated -- blue #2a78d6 lands on #dae7f5, 1.09:1 against
    the surface, which is how the first render managed to be simultaneously
    "validated" and unreadable.  Blending explicitly keeps the composited
    colour a real value we can measure and reason about, and keeps the full
    strength hue available for the outline, which is what carries identity.
    """
    c, s = _hex_to_rgb(colour), _hex_to_rgb(surface)
    mix = tuple(round(amount * a + (1 - amount) * b) for a, b in zip(c, s))
    return "#%02x%02x%02x" % mix


def _spaces(doc):
    """Modelspace plus every real block definition (not the layout blocks)."""
    yield doc.modelspace()
    for block in doc.blocks:
        if not block.name.lower().startswith(("*model_space", "*paper_space")):
            yield block


def _prepare_native(doc) -> None:
    """Keep every AutoCAD colour exactly as ISOGEN set it.

    Only the two things that *hide* content come off: the WIPEOUT masks behind
    the balloon groups and the opaque MTEXT background masks behind the
    dimension values.  Both are legitimate on a plotted sheet and both defeat
    this render, which exists to show what overlaps what.

    Nothing else is touched -- no layer repaint, no block normalisation.  On
    the black background ACI 7 resolves to white on its own (see
    `LayoutProperties.set_colors` in `render`), so the annotation text, the
    title block and the BOM all come out the way AutoCAD draws them.
    """
    _drop_wipeouts(doc)
    _restore_dimension_colours(doc)
    _detach_attribs(doc)
    for space in _spaces(doc):
        for e in space:
            if e.dxftype() in TEXT_TYPES:
                _drop_background_mask(e)


def _detach_attribs(doc) -> None:
    """Redraw balloon numbers as standalone TEXT at their own coordinates.

    THIS IS A RENDER FIX ONLY -- the DWG is not the problem.  Checked across
    all 18 balloons, matched by DXF handle: both `insert` AND `align_point`
    follow their INSERT with 0.000 drift, so AutoCAD centres every number in
    its circle correctly.

    ezdxf draws a block reference's ATTRIBs from inside `draw_insert`, while
    the block transform is live (frontend.py: "Draw ATTRIB entities at last...
    Block reference attributes are located __outside__ the block reference!").
    An ATTRIB already carries absolute WCS coordinates, so it comes out
    displaced -- which is why a relocated balloon renders as an empty circle
    with its number floating off to one side, and why that made the solver
    look like it was tearing balloons apart when it was not.

    Lifting each ATTRIB out to modelspace as a TEXT sidesteps the block
    transform entirely and keeps ezdxf's own text placement.
    """
    msp = doc.modelspace()
    for insert in msp.query("INSERT"):
        attribs = list(insert.attribs)
        if not attribs:
            continue
        for a in attribs:
            text = msp.add_text(
                a.dxf.text,
                dxfattribs={
                    "layer": a.dxf.layer,
                    "height": a.dxf.get("height", 2.5),
                    "rotation": a.dxf.get("rotation", 0.0),
                    "style": a.dxf.get("style", "Standard"),
                    "color": a.dxf.get("color", 256),
                },
            )
            # halign/valign are meaningless without the align_point they
            # measure from, so carry the pair or neither.
            ap = a.dxf.get("align_point", None)
            # A multi-line ATTRIB is drawn by AutoCAD at its EMBEDDED MTEXT
            # insert, not at insert/align_point.  Render from the same point
            # AutoCAD uses, or the picture lies (2026-09-17: empty circles
            # in TrueView while every render showed centred numbers).
            m = getattr(a, "_embedded_mtext", None)
            if m is not None and m.dxf.get("insert", None) is not None:
                ap = m.dxf.insert
                if not (a.dxf.get("halign", 0) or a.dxf.get("valign", 0)):
                    a.dxf.insert = m.dxf.insert
                    ap = None
            if ap is not None:
                text.dxf.align_point = ap
                text.dxf.halign = a.dxf.get("halign", 0)
                text.dxf.valign = a.dxf.get("valign", 0)
            text.dxf.insert = a.dxf.insert
            if a.rgb is not None:
                text.rgb = a.rgb
        insert.attribs.clear()


def _restore_dimension_colours(doc) -> None:
    """Repaint dimension geometry the colour AutoCAD actually gives it.

    Worked around here because ezdxf gets it wrong.  A DIMENSION keeps its
    geometry in an anonymous *D block whose lines are BYBLOCK, and the
    AdskIsoMetric style leaves dimclrd/dimclre BYBLOCK too, so in AutoCAD they
    inherit the DIMENSION's own colour -- magenta, off the Dimension layer.
    ezdxf resolves BYBLOCK correctly for an ordinary INSERT (which is why the
    valves come out green) but not for a dimension's geometry block: it drops
    to the foreground colour and every dimension on the sheet drew white.

    Verified rather than assumed -- rendering the untouched DXF through
    ezdxf's own `qsave`, filtered to DIMENSION only, yields 0 magenta pixels.

    dimclrt is left alone: the style sets it to ACI 7 explicitly, so white
    dimension *text* on magenta lines is correct and already renders that way.
    """
    ctx = RenderContext(doc)
    for dim in doc.modelspace().query("DIMENSION"):
        block_name = dim.dxf.get("geometry", None)
        if not block_name:
            continue
        try:
            block = doc.blocks.get(block_name)
        except ezdxf.DXFKeyError:
            continue
        colour = ctx.resolve_all(dim).color[:7]  # may carry an alpha suffix
        try:
            rgb = _hex_to_rgb(colour)
        except ValueError:
            continue
        for e in block:
            if e.dxf.get("color", 256) == 0:  # BYBLOCK, and nothing else
                e.rgb = rgb


def _repaint(doc, roles=None) -> None:
    """Override the drawing's ACI colours with the semantic palette.

    Driven by ROLES for the fixture, and by the site's LayerRoles for real
    drawings -- so the grey=immovable / colour=movable rule holds regardless
    of what the client calls their layers.
    """
    for name, (colour, lw_mm, _movable, _label) in ROLES.items():
        try:
            layer = doc.layers.get(name)
        except ezdxf.DXFTableEntryError:
            continue
        layer.rgb = _hex_to_rgb(colour)
        layer.dxf.lineweight = int(lw_mm * 100)  # DXF lineweight is 1/100 mm

    if roles is not None:
        # The pipe run is the backbone of the drawing and has to read as one.
        # This used to paint it INK_MUTED -- the palest ink in the palette --
        # so the geometry came out fainter than the annotation sitting on top
        # of it, and fainter than the legend swatch advertising it.  A real
        # site config draws no line between "pipe" and "components" (JP1071
        # lumps Pipe/Valve/Fitting/Flange into `fixed`), so they share one
        # grey here and `_legend` names them together rather than claiming a
        # three-way split the drawing does not have.
        for name in roles.fixed:
            _try_paint(doc, name, INK_SECONDARY, 0.35)
        for name in roles.frame:
            _try_paint(doc, name, CHROME, 0.25)
        # Non-text on a movable layer is dimension lines and leaders: support
        # marks, so they recede.  The glyphs are forced to INK_PRIMARY below
        # whatever layer they sit on.
        for name in roles.movable:
            _try_paint(doc, name, INK_MUTED, 0.25)

    _drop_wipeouts(doc)
    _normalise_block_colours(doc)

    ink = _hex_to_rgb(INK_PRIMARY)
    for e in doc.modelspace():
        if e.dxftype() in TEXT_TYPES:
            e.rgb = ink  # true colour beats BYLAYER
            _drop_background_mask(e)
        elif e.dxftype() == "INSERT":
            # Balloon numbers are ATTRIBs INSIDE the block reference, often on
            # layer 0 -- which at this site is the frame layer.  Left BYLAYER
            # they get painted chrome and vanish.  They are annotation text
            # and must read as such, whatever layer they happen to sit on.
            for a in e.attribs:
                a.rgb = ink


def _drop_wipeouts(doc) -> None:
    """Delete ISOGEN's WIPEOUT masks before rendering.

    The balloon group blocks each carry a WIPEOUT: a mask that blanks the
    geometry behind the balloons so they stay readable on the plotted sheet.
    Two reasons it cannot stay.  AutoCAD paints a wipeout in the *background*
    colour; ezdxf's matplotlib backend has no such notion and fills it as a
    solid dark polygon -- which is why every balloon group rendered as a black
    slab with its item numbers knocked out of it.  And even drawn correctly, a
    mask is wrong for this particular view: it hides precisely the overlap
    this render exists to photograph.

    Same reasoning as `_drop_background_mask`, one entity type up.
    """
    for space in _spaces(doc):
        for e in list(space.query("WIPEOUT")):
            space.delete_entity(e)


def _normalise_block_colours(doc) -> None:
    """Strip ISOGEN's hardcoded ACI colours from inside block definitions.

    The BOM is not modelspace geometry: it lives in the anonymous table blocks
    *T10 / *T11 with ACI baked onto the entities -- aci=1 (red) for the item
    rows, aci=2 (yellow) for the "FABRICATION ITEMS" / "ERECTION ITEMS"
    headers.  `_repaint` walks `doc.modelspace()` only, so none of that was
    ever reached and the BOM rendered in raw ISOGEN red-on-white with a yellow
    header -- the single least readable thing on the sheet.

    Only entities carrying an *explicit* index (1..255) are touched.  256 is
    BYLAYER (already handled by the layer repaint) and 0 is BYBLOCK, which is
    how the balloon blocks inherit their colour from the INSERT -- repainting
    either would break the parts that currently work.

    This is also where the dimension values get rescued.  Each DIMENSION keeps
    its text in an anonymous *D block as MTEXT with an opaque background mask
    (bg_fill=3 over ACI 9, a dark grey).  `_drop_background_mask` only ever
    ran over modelspace, so it never reached them: every dimension value on
    the sheet rendered as a solid dark rectangle with its digits buried in it.
    They are annotation content, so they take label ink, not chrome.
    """
    muted, chrome = _hex_to_rgb(INK_MUTED), _hex_to_rgb(CHROME)
    ink = _hex_to_rgb(INK_PRIMARY)
    for block in doc.blocks:
        if block.name.lower().startswith(("*model_space", "*paper_space")):
            continue
        is_dimension = block.name.startswith("*D")
        for e in block:
            if e.dxftype() in TEXT_TYPES:
                _drop_background_mask(e)
                if is_dimension:
                    e.rgb = ink
                    continue
            if not 1 <= e.dxf.get("color", 256) <= 255:
                continue
            # Text stays legible but recessive; rules and hatching recede
            # further -- the BOM is a fixed obstacle, not the subject.
            e.rgb = muted if e.dxftype() in TEXT_TYPES else chrome


def _drop_background_mask(e) -> None:
    """Turn off MTEXT opaque background fill for the diagnostic view.

    ISOGEN masks annotation text so it stays readable over geometry.  That is
    right for the drawing and wrong for this render: the mask paints as a
    solid block and hides the very text we are here to inspect.
    """
    try:
        if e.dxftype() == "MTEXT" and e.dxf.hasattr("bg_fill"):
            e.dxf.bg_fill = 0
    except Exception:
        pass


def _try_paint(doc, layer_name: str, colour: str, lw_mm: float) -> None:
    try:
        layer = doc.layers.get(layer_name)
    except ezdxf.DXFTableEntryError:
        return
    layer.rgb = _hex_to_rgb(colour)
    layer.dxf.lineweight = int(lw_mm * 100)


def render(dxf_path: Path, png_path: Path, dpi: int = 180,
           window=None, show_boxes: bool = True, site: str = "default",
           theme: str = "native") -> None:
    """window = (x0, y0, x1, y1) in drawing units, to zoom on a region.

    The boxes come from isotidy's own Scene, not from a second reading of the
    DXF, so the picture and the score cannot drift apart.  A renderer with its
    own idea of where the labels are is a renderer that will eventually lie to
    you about whether the solver worked.
    """
    th = THEMES[theme]
    roles = SITES[site]
    doc, scene = extract(dxf_path, roles)
    # Score against the PRINT target, exactly as the CLI does.  A renderer
    # holding a different idea of "too close" than the scorer will eventually
    # show green boxes on a drawing the CLI is calling a defect.
    cfg = Tuning()
    try:
        from ezdxf import bbox as _bb
        _b = _bb.extents(doc.modelspace(), fast=True)
        cfg = cfg.for_sheet(float(_b.size.x) if _b.has_data else 0.0)
    except Exception:
        pass
    report = detect(scene, cfg)
    in_trouble = report.labels_involved
    msp = doc.modelspace()

    boxes = []
    if show_boxes:
        for lab in scene.labels:
            if lab.index in in_trouble:
                colour = th.clash
            elif th.native:
                # No class hue here: ISOGEN's own layer colours already say
                # what kind of label this is.  The box only marks the extent
                # the collision detector actually used.
                colour = th.tracked
            else:
                colour = CLASS_COLOURS.get(lab.cls, INK_MUTED)
            boxes.append((lab.pos, lab.size, colour))

    if th.native:
        _prepare_native(doc)
    else:
        _repaint(doc, roles)

    plt.rcParams["hatch.linewidth"] = 0.7

    fig = plt.figure(figsize=(16.5, 11.7), facecolor=th.surface)  # A3 in inches
    # Reserve a strip top AND bottom -- caption above, legend below -- so
    # neither can ever sit on the drawing.  Overlapping chrome is the same bug
    # we are here to fix, and a caption stamped across the sheet border is us
    # committing it in the act of reporting it.
    ax = fig.add_axes([0, 0.072, 1, 0.855])
    ax.set_facecolor(th.surface)
    ax.set_axis_off()

    # Label boxes go down first so the geometry and glyphs sit on top.
    #
    # Identity rides on a FULL-STRENGTH OUTLINE and the fill is only a hint,
    # rather than the reverse.  The box sits under the label text, so the fill
    # has to stay near the surface to keep the glyphs readable -- which leaves
    # the outline as the only place a validated hue can actually appear at the
    # strength it was validated at.
    for (x, y), (w, h), colour in boxes:
        clash = colour == th.clash
        ax.add_patch(Rectangle(
            (x, y), w, h,
            facecolor=_tint(colour, th.clash_fill if clash else 0.16,
                            th.surface),
            edgecolor=colour,
            # A clash is never colour alone: it also wears a hatch, and it is
            # the only hatched thing on the sheet.  Texture is what survives a
            # greyscale print, a red-green reader, and a drawing that already
            # spends six hues on its own geometry.
            hatch="///" if clash else None,
            linewidth=2.2 if clash else 0.7, zorder=0,
        ))

    # adjust_figure=False is load-bearing.  MatplotlibBackend.finalize() calls
    # set_size_inches() from the drawing's aspect ratio, which THREW AWAY the
    # A3 canvas above and replaced it with matplotlib's default 6.83x4.8in.
    # Every earlier render was therefore ~74 dpi, not the 180 requested: an A3
    # sheet of 2 mm annotation text crushed into 1229 px, and a legend laid
    # out for 16.5 in squeezed into 6.83 in until it clipped at both ends.
    backend = MatplotlibBackend(ax, adjust_figure=False)
    # Telling the render context what colour the paper is does more than set a
    # background: ACI 7 means "whatever contrasts with the paper", so this is
    # what flips the annotation text, title block and BOM to white instead of
    # leaving them black-on-black.
    layout_properties = LayoutProperties.from_layout(msp)
    layout_properties.set_colors(th.surface)
    Frontend(RenderContext(doc), backend).draw_layout(
        msp, finalize=True, layout_properties=layout_properties)

    if window:
        x0, y0, x1, y1 = window
        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)
    ax.set_aspect("equal", adjustable="datalim")

    _legend(fig, roles, {lab.cls for lab in scene.labels}, th)
    _caption(fig, dxf_path, scene, report, th)

    fig.savefig(png_path, dpi=dpi, facecolor=th.surface)
    plt.close(fig)
    print(f"rendered -> {png_path}  "
          f"({len(scene.labels)} labels, {len(report.collisions)} collisions)")


def _caption(fig, dxf_path: Path, scene, report, th: Theme) -> None:
    """Stamp the score onto the image.

    A before/after pair of pictures is an argument; a before/after pair of
    pictures carrying their own numbers is evidence.
    """
    involved = len(report.labels_involved)
    lab = len(report.of_kind("label"))
    geo = len(report.of_kind("geometry"))
    frm = len(report.of_kind("frame"))
    # The caption is text, so it wears text ink -- painting the whole block in
    # the status colour (as this used to) both drowns the sheet in salmon and
    # spends the reserved clash colour on something that is not a clash.  Only
    # the score line carries status, and only when there is something to
    # report, so red on this sheet means exactly one thing.
    fig.text(
        0.012, 0.992, dxf_path.name,
        va="top", ha="left", fontsize=13, family="monospace",
        color=th.ink, weight="bold",
    )
    fig.text(
        0.012, 0.969,
        f"{len(scene.labels)} labels   {involved} in collision\n"
        f"label↔label {lab}   label↔geometry {geo}   label↔frame {frm}\n"
        f"overlap {report.overlap_area:.2f} mm²",
        va="top", ha="left", fontsize=11, family="monospace",
        color=th.ink_dim if report.clean else th.clash,
        linespacing=1.6,
    )


def _legend(fig, roles, present: set[str], th: Theme) -> None:
    """Identity is never carried by colour alone -- name every class.

    Lives in its own reserved strip below the drawing, laid out in one row.

    Built from what was actually painted, not from a fixed list.  Hardcoding
    the fixture's three-way grey split made the legend lie on every real
    drawing -- it advertised a dark "Pipe centreline" that the site config
    never paints -- and listing all four label classes advertised a
    Continuation colour that appears nowhere on a sheet whose continuation
    callouts happen to sit on the Annotation layer.  A swatch for a colour
    that is not in the picture is worse than no swatch: it sends the reader
    hunting for it.
    """
    if th.native:
        # Two states, because the overlay only encodes two.  Naming ISOGEN's
        # layer colours here would be a second, competing legend for a scheme
        # the reader already knows and the tool does not control.
        handles = [
            Patch(facecolor="none", edgecolor=th.tracked, linewidth=1.1,
                  label="Label tracked (no clash)"),
            Patch(facecolor=_tint(th.clash, th.clash_fill, th.surface),
                  edgecolor=th.clash, linewidth=2.2, hatch="///",
                  label="IN COLLISION"),
        ]
        leg = fig.legend(
            handles=handles, loc="lower center", ncol=2, frameon=False,
            fontsize=11, labelcolor=th.ink_dim, borderaxespad=0.4,
            columnspacing=3.0, handlelength=2.4, handletextpad=0.8,
        )
        leg.set_zorder(100)
        # Above the legend, not on it: the reserved strip is only 7% of the
        # sheet and `loc="lower center"` fills it from the bottom up.
        fig.text(
            0.5, 0.050,
            "geometry, annotation and BOM keep the drawing's own AutoCAD "
            "layer colours",
            ha="center", va="bottom", fontsize=9, color=th.ink_dim, alpha=0.75,
        )
        return

    if roles is DEFAULT_ROLES:
        fixed = [
            Line2D([], [], color=ROLES[k][0], lw=2.5,
                   label=f"{ROLES[k][3]} (fixed)")
            for k in ("ISO_PIPE", "ISO_SYMBOL", "ISO_FRAME")
        ]
    else:
        fixed = [
            Line2D([], [], color=INK_SECONDARY, lw=2.5,
                   label="Pipe & components (fixed)"),
            Line2D([], [], color=CHROME, lw=2.5, label="Frame / BOM (fixed)"),
        ]
    # The swatch is built exactly like the box on the drawing -- tinted fill,
    # full-strength outline, hatch on the clash.  A legend drawn at a
    # different alpha to the thing it explains is a legend that lies.
    movable = [
        Patch(facecolor=_tint(c, 0.16), edgecolor=c, linewidth=1.1,
              label=f"{n} (movable)")
        # 'weld' and 'balloon' share a slot in CLASS_COLOURS, so they share one
        # legend entry -- match on either or the entry vanishes from a sheet
        # that is full of balloons.
        for cls, n, c in ((("annotation",), "Text / callouts", CLASS_ANNOTATION),
                          (("weld", "balloon"), "Balloons / welds", CLASS_WELD),
                          (("dimension",), "Dimension text", CLASS_DIMENSION),
                          (("continuation",), "Continuation", CLASS_CONTINUATION))
        if present.intersection(cls)
    ]
    # Status colour, reserved -- never a class, so a clash can never be
    # mistaken for a category.
    movable.append(Patch(facecolor=_tint(STATUS_CRITICAL, 0.34),
                         edgecolor=STATUS_CRITICAL, linewidth=2.0,
                         hatch="///", label="IN COLLISION"))
    leg = fig.legend(
        handles=fixed + movable, loc="lower center", ncol=4,
        frameon=False, fontsize=10, labelcolor=INK_SECONDARY,
        borderaxespad=0.4, columnspacing=2.0, handlelength=2.2,
        handletextpad=0.7, labelspacing=0.5,
    )
    leg.set_zorder(100)


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    site = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--site=")),
                "default")
    theme = next((a.split("=", 1)[1] for a in sys.argv
                  if a.startswith("--theme=")), "native")
    src, dst = Path(argv[0]), Path(argv[1]) if len(argv) > 1 else None
    dst = dst or src.with_suffix(".png")
    win = tuple(float(v) for v in argv[2:6]) if len(argv) >= 6 else None
    render(src, dst, window=win, site=site, theme=theme)
