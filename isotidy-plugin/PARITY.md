# Parity map — Python reference ↔ IsoTidy plugin

The Python pipeline (one folder up) is where every rule is proven on real
sheets.  The plugin must do the SAME thing, in the SAME order, or the two
drift apart — which is how the 2026-09-08 live-run failure happened.
This table is the checkable statement of "the plugin does what Python
does".  Update it in the same commit as any behaviour change.

Legend: **=** ported and equivalent · **~** ported with a documented
difference · **✗** not ported (Python only).

## Pipeline order

| Step | Python | Plugin | Status |
|---|---|---|---|
| Read drawing | `extract.extract()` (DXF via ODA) | `Extract.FromDatabase()` (in-process) | = (in-process reads the real fonts — text boxes are exact, not estimated) |
| Reserve title-block table | `rework.table_rect()` + solver injection | `Polish.ReserveTitleBlockTables()` → `Scene.Reserved` | = (measured from the block's own rule lines, never hardcoded) |
| Dogleg hygiene | `rework.stage_doglegs()` | `Polish.LeaderHygiene()` (dogleg branch) + orphan branch in `Polish.Claims()` | = (incl. strip-when-pointing-at-nothing >5 mm, strip-away-when-no-clean-reland, 3 mm attach-by-dogleg) |
| Stack re-align | `rework.stage_realign()` | `Polish.Realign()` | = (frame + attrib + landing shift onto the column, ≤12 mm, clearance-checked) |
| One leader per weld | `extract._split_multi_owner_groups()` | `Extract.SplitMultiOwnerGroups()` | = |
| Own-text landings | `rework.stage_landings()` | `Polish.LeaderHygiene()` (own-run branch) | = |
| Orphan-leader claims | `rework.stage_claims()` (union-box test, 8 mm) | `Polish.Claims()` | = |
| Solve (place labels) | `solve.solve()` | `Solver.Solve()` | = (same candidates, ink veto, scorekeeper, zero-residual sweep) |
| Write back moves | `writeback.apply()` | `Writeback.Apply()` | = after 2026-09-08 fixes (attributes moved once, measured; no leaders for dimension text; shared crossing-checked `PickLanding`) |
| Frames (render width) | `rework.stage_frames()` | `Polish.FixFrames()` | ~ Python sizes for the PNG font (metric ÷ width factor); in-process the true font width is measured directly. Same 2 mm margins, same left-pin rule for stacks |
| Frames that cannot grow | `rework.relocate_and_widen()` | `Polish.RelocateStack(frame, need)` | = |
| Threads / grazes | `rework.stage_threads()` | `Polish.Threads()` | = |
| Residual hairlines | `rework.stage_hairlines()` | `Polish.Hairlines()` | = |
| Table clearing by solver | `rework.stage_table()` (inject + re-solve rounds) | covered by `Scene.Reserved` being priced inside `Solver.Solve()` | ~ one pass instead of up to 6 rounds |
| Audit what ships | `pipeline.py` round-trips the DWG and re-detects | `Commands.Run` re-extracts after writeback, **aborts if worse** | = |
| Never-worse gate granularity | `rework._worse()`: per STAGE, lexicographic — B/C/D count first, then overlap; a stage that removes structural defects may cost ≤ 0.5 mm² overlap | whole-run gate on measured overlap only (no B/C/D audit in-process yet) | ~ plugin is stricter and coarser: a run that clears defects at a hairline's cost is refused. Porting the audit categories closes this |
| Category-B leader re-routing (elbows) | `tools/reroute.py` (`relieve()`, clean-only bends) | — | ✗ not ported |
| Tuck relief | `solve._shave_tucks()` | — | ✗ not ported |
| Renders / diff images | `tools/sheet_png.py` etc. | — | ✗ (not needed in-process; validation renders come from the Python side) |

## Shared rule set for any leader landing

| Rule | Python (`rework.Ctx.line_clean`) | Plugin (`Polish.LeaderClean`) |
|---|---|---|
| never cross another leader | shapely `crosses` | `SegCross` orientation test |
| ≥ 1.5 mm from other leaders, judged outside every arrow hub (3 mm) | `hub` = union of tip discs | `SegSep` skips samples inside any hub |
| twins from one shared arrowhead exempt from separation | tips within 0.5 mm | same |
| never inside another text's hard pad (clearance + geom buffer) | `box(clearance+geom_buffer)` | `Box(pad)` |
| threading: ≤ 8 mm inside a 2.5 mm comfort zone | `COMFORT/THREAD_MAX` | `Comfort/ThreadMax` |
| may touch own boxes, ≤ 2 mm run through them | `OWN_TOL` | `OwnRunTol` |
| no component symbol crossed | `symbol_hits()` | ownerless obstacles via `ObstaclesNear` (~ symbols are obstacles in-process) |
| landing candidates | 4 edge midpoints + 4 corners, nearest tip first | `EdgeCands`, same |

## Dimension text and DIMTMOVE (2026-09-11)

ISOGEN's dimension style carries DIMTMOVE=1 ("add a leader when the text
is moved").  The plugin moves text and then lets AutoCAD regenerate the
dimension (`RecomputeDimensionBlock`), which obeyed that and drew a short
stub from the dimension line to the relocated number on every moved
dimension — the "small legend lines" the engineer saw.  Python never
regenerates (it edits the cached `*D` block), so it showed nothing, but
AutoCAD would draw the same stub on its next regen.  Both now set a
per-dimension override DIMTMOVE=2 (move text freely, no leader):
`dim.Dimtmove = 2` in C#, `dim.override().update({"dimtmove": 2})` in
Python — the latter verified to be written by ezdxf and to survive the
ODA DXF→DWG→DXF round trip.

## Diagnostics (plugin only)

`ISOTIDY` writes `<drawing>.isotidy.log` next to the DWG as it runs — one
timestamped line per stage, plus any stage exception with its stack.
AutoCAD's command line does not repaint during a modal command, so this
file is the only way to see where a "not responding" run is.  Send it
with the drawing.

## Invariants both sides enforce

- Arrow tips are never moved (`vertices[0]` / `GetFirstVertex(0)` untouched).
- Every MULTILEADER edit updates every copy of the state (context AND
  entity in DXF; the managed API does this itself in-process).
- Dimension text never receives a drawn leader; it slides on its own line.
- Nothing ships without being re-measured from the written result.

## Known gaps (plugin behind Python)

1. `reroute.py` category-B elbow routing — the largest remaining gap; the
   plugin re-lands straight, never bends.
2. `_shave_tucks` cosmetic balloon relief.
3. Table clearing is single-pass (priced reservation) rather than the
   Python's iterated inject-and-resolve; dense title-block corners may
   keep a residual the Python clears.
4. **Stale leader breaks (bug, 2026-09-18).**  ISOGEN stores a break in a
   leader wherever it passes over another line.  Re-landing kept the old
   breaks, so viewers draw the leader BENT through a point on its old path
   (25/45 leaders on difficult_iso v13, 339 across the fleet).  Python
   fixed on the one save path (`isotidy.dimlines.drop_stale_breaks`, called
   by `writeback.save_dxf`).  `Writeback.RelandLeader` still only calls
   `SetLastVertex` and keeps the breaks -- must clear the leader line's
   breaks (or re-create the leader line) whenever the landing moves.
   Verify in AutoCAD first: re-land one ISOGEN leader that has a break and
   look for the kink.
5. **Leaders over dimension linework (user rule, 2026-09-18).**  Python:
   `isotidy.dimlines` (checker + fixer share `DimIndex`), audit category B,
   `Ctx.line_clean` / `relocate_stack` refuse any leader crossing a
   dimension line, witness line or arrowhead, new `rework.stage_dimcross`
   (re-land, else move the stack, searching round the arrow tip, straight
   leaders only).  Plugin: `Polish.LeaderClean` does not know the rule and
   there is no DimCross stage.

## Validation protocol (before any production use)

Same sheet through both: Python `tools/pipeline.py` + `tools/rework.py`
(DXF path) and `ISOTIDY` (in-process).  Convert the plugin's DWG back with
ODA, run `tools/sheet_health.py` on both.  Acceptance: both clean by every
rule, arrowheads 100% unmoved on both, and no class of finding present on
one that is absent on the other.  First reference sheet:
`real inputs\plugin-fail\JP1070-000-PP-ISO-01-028LL_r0-1.dwg` — Python
result 0.00 mm², health clean, 31/31 tips (2026-09-08).
