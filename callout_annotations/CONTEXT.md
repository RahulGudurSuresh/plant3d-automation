# callout_annotations — session context (handoff)

Written 2026-09-16. Read with `README.md`.

## What was asked

User does *right-click → Plant 3D → Piping → Full Line Number Callout* on an
ortho and gets `Size-Tag-Spec`; the P&ID has the full name. Given one DWG and
its P&ID PDF, show every line with its full P&ID name. Named the task
`callout_annotations`; same shared venv and folder shape as `isotidy`.

## What the inputs turned out to be (measured, not assumed)

* `Plan view.dwg` is an **ortho** (`PNP_PROJECTMGR/FileType = Ortho`), module
  GA M-02 of JP1071, **no annotations at all**, title
  block still "Doc No." — a work in progress. A1 layout, most viewports
  pushed outside the sheet border (not our concern).
* 11051 anonymous-block INSERTs in model space, all at origin, XDATA
  `PnPOrthoApp` = (int, source handle, int, "Piping", source-DWG GUID). No
  piping properties anywhere in the DWG: `PNP_CACHEBLOB2` is a gzip'd
  SQLite with only ortho bookkeeping (`PnPDwg2d_ViewDef` etc.).
* Layer = `<view>-<3D layer>`; the 3D layer is the **LineNumberTag**
  (`209M01`, `209L0401`, `412`, `227L53`). `M02` = module 02, `L####` =
  branches. ISO names in `../real inputs` (`209M05`, `353M05`) confirm.
* Each view's 2D linework lies in a WCS plane: plan Z=0, Right/Left X=0,
  Back/Front2/Front3 Y=0. Viewport `view_direction_vector` gives the side;
  `arbitrary_axes(n)` gives the DCS the viewport displays — verified against
  every viewport's `view_center_point`. Scales 1:30 (main) and 1:20
  (sections).
* P&ID binder: 22 vector pages, 500 names, grammar
  `area(3)-seq(3)-DN(3)-service-spec[-suffix]`. Areas 214 and 217 share
  sequence numbers (453–457, 505, 540, 541 …), so the join on `seq` alone is
  ambiguous for 5 seqs in this module; OD from linework resolves those where
  the sizes differ.

## Decisions

* **Model-space MTEXT in the view plane**, not paper-space text: it is where
  Plant 3D puts its own ortho annotations, it pans with the viewport, and the
  freeze bookkeeping makes it view-specific.
* **Never guess**: ambiguous / missing → red `-CALLOUT-REVIEW` layer, short
  label, full detail in `report.csv`.
* **OD estimate = mode of parallel-line spacing, snapped to a standard OD**
  (per-pair voting over the whole table was wrong 2/3 of the time). Used
  only as a tie-breaker; size disagreements are reported as prompts.
* One callout per substantial run; secondary runs only if ≥ 25 % of the
  longest (`Tuning.secondary_run_ratio`), max 3 per tag per view.

## Verified

* DXF → DWG → DXF through ODA keeps all MTEXT/LEADER, `Autodesk_PNP`,
  `DwgView*` xrecords, all 11051 `PnPOrthoApp` XDATA and the per-viewport
  frozen lists (`tests/test_pipeline.py::test_writeback_and_freeze`).
* NOT yet verified: opening the output in Plant 3D itself (no CAD on this
  host; the air-gapped VM is the place — see memory `cad-vm-air-gapped`).
  Things to look at there: text reads correctly in the Left view (mirrored
  plane), leaders' arrowheads, and that *Update View* leaves the callouts.

## isotidy trial (2026-09-16, `tools/tidy_with_isotidy.py`)

User found the result too crowded and asked to run isotidy on it. Bridge
= per view, a paper-mm sheet (linework fixed, callouts movable, exact
anchors via isotidy's `ISOTIDY/ANCHOR` XDATA, vertical labels as
multi-line proxies), viewport window as a closed `Frame` polyline.

* **Without the frame** isotidy "solved" Back View 17 498 → 4 630 mm² by
  parking ~40 % of labels outside the window — invisible in the viewport.
  Never run it frameless.
* **With the frame** (the honest result): Back 18 204 → 13 550 (−26 %),
  Plan 3 855 → 2 596, Left 7 156 → 4 494, Right 4 188 → 2 656, sections
  −50…−70 %. 21 minutes for 8 views (isotidy's extract on 18–29k curves
  per view is the cost).
* Conclusion: the big views are over-populated, not badly placed. 62
  full-length names in a 170×121 mm elevation at 1:30 exceed the white
  space. The fix is fewer/shorter callouts per view (label each line once
  in its clearest view; short form on sections), not a better solver.

## 2026-09-17: "it's shit" -- and it was. What changed

User screenshot: the Back View viewport was a wall of text. `tools/
render_paper.py` (real entities, real fonts, view rotated onto XY) showed
what my model-PNGs hid: labels 10-30 % wider than estimated (txt.shx),
stacked on each other in the bottom band, on top of pipes.

Fixes, in order of effect:
1. **Quality gate** (`place.py`): pass 1 places only clean callouts, plan
   first; pass 2 forces one per unnamed P&ID line. Label-on-label is a
   hard ban. Coverage is per P&ID NAME (branch tags share the line's
   callout). Back View went 62 -> 12 labels, all readable.
2. **Weighted linework**: an absolute "nothing under the text" gate can
   never pass on a module GA (best plan spot had 20-60 h of curves under
   it). Curves are weighted by their 3D layer: pipe 1.0, equipment 0.8,
   steel (numeric layers) 0.3, CENTER/HIDDEN/HATCH 0.1.
3. **Real widths + Arial**: `char_aspect` 0.72 -> 0.85, style `arial`
   (exists in the drawing) instead of Standard/txt.shx.
4. **MTEXT background mask** (`bg_fill=3`, scale 1.15).
5. `max_per_view` 3 -> 2.

isotidy bridge is superseded: the problem was what to label, not where.

## Next steps if the user wants "real" Plant 3D annotations

The proper fix is data, not text: write area/service/suffix into the pipe
rows so Plant 3D's own callout shows them. Needs the project (`Project.xml`
+ `Piping.dcf`, path recorded in `PNP_PROJECTMGR`) — either a C# plugin like
`../isotidy-plugin` (safe, uses the Plant 3D API) or SQLite writes (fast,
bypasses locking). `match.py` already produces the per-tag values.
