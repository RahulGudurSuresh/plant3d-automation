"""
Callouts -> DXF.

Each callout becomes an MTEXT and, when it stands off the pipe, a LEADER,
both in MODEL space in the plane of their view:

  * MTEXT.insert is a WCS point (the label's bottom-left), text_direction
    the WCS vector of +u rotated by the label angle, extrusion the view
    normal.  With the normal pointing at the viewer and the direction along
    the viewport's DCS X, the text reads correctly in that viewport and is
    mirrored in none we care about -- the other viewports freeze its layer.

  * LEADER vertices are WCS, from the label edge to the anchor on the pipe;
    its normal is the view normal.

Layers: "<view>-CALLOUT" and "<view>-CALLOUT-REVIEW", created on demand
and FROZEN in every viewport that freezes that view's other layers.  Skip
that and the plan's callouts show up in the side views at the wrong angle.

Re-runs are idempotent: everything on those layers is deleted first.
"""

from __future__ import annotations

from ezdxf.math import Vec3

from .config import DEFAULT_TUNING, JP1071, Site, Tuning
from .ortho import Ortho
from .place import Callout

#: MTEXT attachment point: bottom-left, so `insert` is the box corner
#: place.py computed.
BOTTOM_LEFT = 7

#: XDATA namespace on every callout: tag, anchor (view coords) and width,
#: so a later pass (tools/tidy_with_isotidy.py) can rebuild the Callout
#: from the DXF alone instead of re-running placement.
APPID = "CALLOUT_ANN"


def _layer_names(ortho: Ortho, site: Site) -> dict[str, tuple[str, str]]:
    return {v: (f"{v}-{site.callout_layer}", f"{v}-{site.review_layer}")
            for v in ortho.frames}


def clear(ortho: Ortho, site: Site = JP1071) -> int:
    """Delete every entity this tool wrote earlier.  Returns the count."""
    names = {n for pair in _layer_names(ortho, site).values() for n in pair}
    n = 0
    for e in list(ortho.doc.modelspace()):
        if e.dxf.layer in names:
            ortho.doc.modelspace().delete_entity(e)
            n += 1
    return n


def _ensure_layers(ortho: Ortho, site: Site) -> None:
    doc = ortho.doc
    for view, (normal, review) in _layer_names(ortho, site).items():
        for name, color in ((normal, site.callout_color), (review, site.review_color)):
            if not doc.layers.has_entry(name):
                doc.layers.add(name, color=color)
    _freeze_in_other_viewports(ortho, site)


def _freeze_in_other_viewports(ortho: Ortho, site: Site) -> None:
    """A viewport that hides a view's linework must hide its callouts too."""
    doc = ortho.doc
    probe: dict[str, str] = {}      # view -> one of its original layers
    for layer in doc.layers:
        name = layer.dxf.name
        m = site.view_layer_re.match(name)
        if m is None or name.endswith((site.callout_layer, site.review_layer)):
            continue
        view = m.group("view")
        if view in ortho.frames and view not in probe:
            probe[view] = name
    ours = _layer_names(ortho, site)
    for layout in doc.layouts:
        if layout.name == "Model":
            continue
        for vp in layout.query("VIEWPORT"):
            if vp.dxf.id == 1:
                continue
            frozen = list(vp.frozen_layers)
            fset = set(frozen)
            changed = False
            for view, sample in probe.items():
                if sample in fset:
                    for name in ours[view]:
                        if name not in fset:
                            frozen.append(name)
                            fset.add(name)
                            changed = True
            if changed:
                vp.frozen_layers = frozen


def set_windows(ortho: Ortho, windows: dict[str, tuple[float, float, float, float]]) -> int:
    """Enlarge each view's VIEWPORT to a new (u, v) window, same scale.

    The paper-space viewport keeps its centre and scale; its paper size
    and view centre change so the margin the layout needs is actually
    visible.  Returns the number of viewports changed.
    """
    doc = ortho.doc
    n = 0
    for view, (x0, y0, x1, y1) in windows.items():
        fr = ortho.frames.get(view)
        if fr is None:
            continue
        fr.window = (x0, y0, x1, y1)
        for handle in fr.viewport_handles:
            vp = doc.entitydb.get(handle)
            if vp is None:
                continue
            vp.dxf.view_center_point = ((x0 + x1) / 2, (y0 + y1) / 2, 0.0)
            vp.dxf.view_height = y1 - y0
            vp.dxf.height = (y1 - y0) / fr.scale
            vp.dxf.width = (x1 - x0) / fr.scale
            n += 1
    return n


def apply(ortho: Ortho, callouts: list[Callout], site: Site = JP1071,
          cfg: Tuning = DEFAULT_TUNING) -> tuple[int, int]:
    """Write the callouts.  Returns (texts, leaders)."""
    doc = ortho.doc
    msp = doc.modelspace()
    clear(ortho, site)
    _ensure_layers(ortho, site)
    style = site.text_style if doc.styles.has_entry(site.text_style) else "Standard"
    dimstyle = "Standard" if doc.dimstyles.has_entry("Standard") else None
    layers = _layer_names(ortho, site)

    texts = leaders = 0
    for c in callouts:
        fr = ortho.frames[c.view]
        layer = layers[c.view][1 if c.review else 0]
        insert = fr.to_wcs(*c.pos)
        mt = msp.add_mtext(c.text, dxfattribs={
            "layer": layer, "style": style,
            "char_height": c.height, "attachment_point": BOTTOM_LEFT,
            "insert": insert, "width": 0.0,
        })
        mt.dxf.text_direction = fr.direction_wcs(c.angle)
        mt.dxf.extrusion = fr.n
        if cfg.mask:
            mt.set_bg_color("canvas", scale=cfg.mask_scale)
        if not doc.appids.has_entry(APPID):
            doc.appids.add(APPID)
        mt.set_xdata(APPID, [(1000, c.tag), (1040, c.anchor[0]), (1040, c.anchor[1]),
                             (1040, c.width), (1040, c.angle)])
        texts += 1

        pts = c.leader_points()
        if pts is None:
            continue
        start, end = pts
        vertices = [fr.to_wcs(*end), fr.to_wcs(*start)]     # arrow at the pipe
        ld = msp.add_leader(vertices, dimstyle=dimstyle or "Standard",
                            dxfattribs={"layer": layer},
                            override={"dimasz": c.height * 0.8,
                                      "dimgap": 0.0})
        ld.dxf.annotation_type = 3          # no attached annotation
        ld.dxf.has_hookline = 0
        ld.dxf.normal_vector = fr.n
        ld.dxf.horizontal_direction = fr.u
        leaders += 1
    return texts, leaders
