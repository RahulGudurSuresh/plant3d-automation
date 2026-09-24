# IsoTidy — the in-Plant 3D plugin (Phase C port)

> **2026-09-24 — retargeted to .NET 10.**  Plant 3D 2026 and 2027 both host
> plugins on **.NET 10** (their `acdbmgd.dll` is stamped
> `.NETCoreApp,Version=v10.0`), not .NET 8 as this port assumed.  A net8.0
> project cannot reference those DLLs at all, so `IsoTidy.csproj` now targets
> `net10.0-windows` (it drops to `net8.0-windows` by itself only when
> `AcadDir` is a 2025 install) and auto-detects AutoCAD **2026 first, then
> 2027**.  The build machine needs the **.NET 10 SDK** — SETUP.md Step 1.
> **Verified 2026-09-24 on the dev host (Plant 3D 2026, R25.1):**
> `dotnet build -c Release` -> 0 warnings, 0 errors; the DLL NETLOADs in the
> 2026 core console (`accoreconsole.exe`) and `ISOTIDYSCORE` dry-runs a real
> ISOGEN sheet (JP1071-000-PP-ISO-24-209M05: 4 label-over-geometry overlaps,
> 40.22 mm2 before, 0.00 achievable).  `deploy\Watch-Isos.ps1 -Once` then ran
> the live `ISOTIDY` on a copy of the same sheet in the 2026 console: 40.22 ->
> 0.00 mm2 measured on the written drawing, 37 labels moved, saved, original
> kept — the first successful write since the 2026-09-08 failure.  The same
> DLL also loaded and tidied in the **2027** console (R26.0), so the 25.1
> build is forward-compatible; the bundle manifest still pins the autoloader
> to 2026.  Still NOT done: a run inside the Plant 3D window itself, and a
> visual check of the written sheet (numbers only so far).
>
> The console test also exposed two watcher defects, both fixed in
> `deploy\Watch-Isos.ps1`: (1) with the default `SECURELOAD=1` the console
> silently refuses an unsigned DLL from any path outside the AutoCAD install
> folder — the ApplicationPlugins bundle included — and does not run the
> bundle autoloader, so a refused NETLOAD + "Unknown command" + QSAVE still
> exited 0 and the sheet was marked done untouched; the watcher now uses a
> throwaway `/isolate` profile with `SECURELOAD 0` and reads its verdict from
> what ISOTIDY printed.  (2) `-Once` could never tidy anything because the
> stability gate needs two polls.  It also gained `-Dll <path>` for testing a
> fresh build without installing, and prefers the 2026 console over newer ones.

> **STATUS 2026-09-08 — read before using on real drawings.**  The first
> live run (JP1070-028LL) wrecked a sheet while reporting success: four
> port bugs, all fixed (attribute double-move, leaders on dimension text,
> unchecked leader landings, and AFTER numbers taken from the plan instead
> of the written drawing — `ISOTIDY` now re-measures the written result and
> **refuses to commit if it is worse**).  The same day the Python side
> gained three stages (orphan-leader claims, relocate-and-widen for boxed
> tags that cannot grow in place, thread/graze repair); they are ported here
> in `src/Polish.cs` so the two stay in parity.  **This build has not been
> compiled or validated yet** — do the SETUP.md Step 4 check on copies, and
> compare `ISOTIDYSCORE` numbers with the Python pipeline on the same sheets
> before any production use.
>
> **Compile check without AutoCAD** (any machine with the .NET 10 SDK):
> `cd compile-check && dotnet build -c Release`.  It compiles the real
> `src\*.cs` against `Stubs.cs`, a stand-in for every AutoCAD type the
> plugin touches.  A clean build proves the C# is sound; it proves NOTHING
> about the real API (only the three AutoCAD DLLs in `acad-ref\` do).  Run
> it before every hand-over.  Full behaviour parity with the Python
> reference is tracked in [PARITY.md](PARITY.md).
>
> **Setting this up on the CAD machine? Start with [SETUP.md](SETUP.md)**
> — the complete step-by-step for the engineer (copy, unblock, build,
> install, validate, daily use, troubleshooting).  This README is the
> technical background behind it.

> **2026-09-07**: `src/Polish.cs` adds the four defect classes proven on
> the Python side this month (CONTEXT.md §12b–12d): title-block table
> pricing, vestigial-dogleg hygiene, own-text landings, and true-width
> frame sizing (in-process the REAL fonts measure the text — the render
> substitution gap does not exist here).  The managed MLeader API also
> keeps entity- and context-level dogleg state in sync natively, so the
> half-updated-record anomaly of the DXF path cannot occur.
>
> **Plant 3D inside a VM?**  Same plugin, unchanged — see
> [DEPLOY-VM.md](DEPLOY-VM.md) for the two build routes and the one real
> gotcha (`Unblock-File`).
>
> **Turnkey deployment** (machine that has Plant 3D): build once, then
> `deploy\Install-IsoTidy.ps1` puts the autoloader bundle in
> `%APPDATA%\Autodesk\ApplicationPlugins` (AutoCAD's autoloader location, so
> the commands are there at startup with no NETLOAD; should Plant 3D ever
> show its unsigned-executable prompt for the bundle, choose *Always Load*
> once) and unblocks everything; the commands are available in every
> session from then on.  **Phase D is real now**: `deploy\Watch-Isos.ps1`
> watches the project's `ProdIsos` folder and tidies every NEW iso the
> moment ISOGEN finishes writing it, headlessly via `accoreconsole.exe`
> — corrected in place, same name, same folder, still a DWG, with the
> untouched original kept in `.\original\`.  The engineer only ever
> opens corrected drawings.  (Failure on a sheet restores the original
> and retries; nothing is ever left half-tidied.  The verdict comes from
> what ISOTIDY printed, never from the console's exit code — see the
> 2026-09-24 note at the top.)

The C# .NET port of the proven Python pipeline one folder up.  Once loaded,
the engineer types **`ISOTIDY`** inside AutoCAD Plant 3D and the overlapping
annotations on the open isometric are de-conflicted in place — no DXF, no ODA
converter, one Ctrl+Z to reject everything.

> **Status: first port, not yet validated.**  The Python pipeline
> (`..\tools\pipeline.py`) remains the reference implementation, proven on
> 71 real drawings.  This plugin must be validated against it (see
> "Validation" below) before its output is trusted for deliverables.

---

## 1. Where this fits in the CAD engineer's workflow

Today (no plugin):

```
route pipe in 3D → place split lines → Create Iso
      → Plant 3D exports PCF → ISOGEN draws the DWG
      → DWG lands in  <Project>\Isometric\<Style>\ProdIsos\
      → engineer opens it → ★ 20-40 min dragging labels apart by hand ★
      → check → issue for fabrication
```

With the plugin, exactly ONE step changes — the starred one:

```
      → engineer opens it → types ISOTIDY (seconds)
      → reviews the sheet → happy: save | unhappy: Ctrl+Z once, all undone
      → check → issue for fabrication
```

Nothing upstream changes: same modelling, same split lines, same Create Iso,
same ISOGEN, same folders.  The plugin only automates the manual
drag-text-around step.  (`ISOTIDYALL` batches a whole folder at once, writing
tidied copies to a `tidied\` subfolder and never touching the originals.
Phase D — hooking Create Iso itself so drawings are tidied before anyone
opens them — builds on this same DLL later, via the Plant 3D SDK.)

## 2. Prerequisites

| What | Why |
|---|---|
| AutoCAD Plant 3D **2026 or 2027** (licensed, on the target machine) | 2026 and 2027 run plugins on **.NET 10** (verified on both installs, 2026-09-24). Plant 3D 2025 is .NET 8: the csproj switches itself to `net8.0-windows` when `AcadDir` points at a 2025 install. For Plant 3D 2024 or older, the project must target .NET Framework 4.8 instead — change `TargetFramework` to `net48` and it should compile unmodified. |
| **.NET 10 SDK** on whichever machine does the *build* | `winget install Microsoft.DotNet.SDK.10` — needed once, to compile. The target machine needs nothing extra (AutoCAD 2026+ ships its own .NET 10 runtime). |
| The three AutoCAD managed DLLs at build time | `acdbmgd.dll`, `acmgd.dll`, `accoremgd.dll` — referenced, never copied. |

No NuGet packages, no internet needed at build or run time — deliberate,
because the CAD machine here is an air-gapped VM.

## 3. Building

### Case A — building ON the machine that has Plant 3D

```powershell
cd isotidy-plugin
dotnet build -c Release
# output: bin\Release\IsoTidy.dll
```

The csproj finds `C:\Program Files\Autodesk\AutoCAD 2026` first (then 2027, then 2025)
automatically.  Different path? Pass it:
`dotnet build -c Release "-p:AcadDir=D:\Autodesk\AutoCAD 2026"`

### Case B — building on the host, running in the air-gapped VM  ← our setup

1. **In the VM**: copy these three files from
   `C:\Program Files\Autodesk\AutoCAD 2026\` into the shared
   `VM-Transfer` folder: `acdbmgd.dll`, `acmgd.dll`, `accoremgd.dll`.
2. **On the host**: copy them into `isotidy-plugin\acad-ref\`
   (create the folder). The csproj falls back to it automatically when no
   AutoCAD install is found.
3. **On the host**: `dotnet build -c Release`
4. Copy `bin\Release\IsoTidy.dll` into `VM-Transfer`, then in the VM to
   somewhere permanent, e.g. `C:\IsoTidy\IsoTidy.dll`.

The reference DLLs are compile-time only (`Private=false`); at run time the
plugin binds to the real ones inside AutoCAD.

## 4. Loading it in Plant 3D

### Quick way (try it out): NETLOAD

1. Start Plant 3D, open any isometric DWG.
2. Type `NETLOAD`, browse to `IsoTidy.dll`, OK.
3. If Windows blocked the copied file: right-click the DLL → Properties →
   check **Unblock** first (or run `Unblock-File C:\IsoTidy\IsoTidy.dll`).
4. Type `ISOTIDYSCORE` — you should see a BEFORE score in the command line.

NETLOAD lasts for the session only.

### Permanent way: the autoloader bundle

1. Copy the `bundle\IsoTidy.bundle\` folder to
   `%APPDATA%\Autodesk\ApplicationPlugins\IsoTidy.bundle\`
2. Create a `Contents\` subfolder inside it and put `IsoTidy.dll` there.
3. Restart Plant 3D — the commands are always available, on every machine
   you repeat these two steps on.

## 5. Using it

| Command | What it does |
|---|---|
| `ISOTIDYSCORE` | **Dry run.** Prints the BEFORE score and what the solver could achieve. Changes nothing — run this first on any new sheet. |
| `ISOTIDY` | Tidies the current drawing. Prints before/after (label↔label, label↔geometry, label↔frame, mm²). **One Ctrl+Z undoes everything.** |
| `ISOTIDYALL` | Prompts for a folder; tidies every DWG in it headlessly and writes copies to `<folder>\tidied\`. Originals untouched. |

Built-in guarantees (same as the Python reference):
- **Never moves a leader arrowhead** — the tip stays on the component.
- **Dimension text never leaves its own line** (slides only, capped at the
  drift limit derived from the sheet's own median offset).
- **Ink veto** — no label ends up sitting on more drawn linework than
  ISOGEN left it on; **scorekeeper** — every move is re-measured against
  the whole sheet and reverted if total overlap grew.
- Welded callouts (balloon+text tucks, stacked reducer/offset callouts)
  move as one; a label moved far away gets a grey leader on layer
  `ISOTIDY_LEADER` so it stays traceable.
- Deterministic: same drawing in, same result out, every run.
- Refuses to report "clean" on a drawing whose layers it cannot read.

**New site / different ISOGEN style?** Edit `LayerRoles.Jp1071()` in
`src/Config.cs` (layer names + balloon block prefix) — that's the entire
per-client configuration, mirroring `isotidy/config.py`.

## 6. Scope — what this port does and doesn't do yet

Ported and active: extraction (text, dimensions, balloons, MULTILEADERs,
callout-stack + tuck grouping, prior-leader claiming), the free-space grid,
the full solver with both vetoes and the zero-residual sweep, write-back
with leader re-landing and drawn leaders, all three commands.

Still owned by the Python reference (run `..\tools\pipeline.py` when you
need them, port later):
- **Category-B leader re-routing** (`tools/reroute.py`) — bending leader
  tails around obstacles and the relieve() label-shuffle. The solver here
  still *prices* leader crossings when placing labels; it just doesn't
  re-bend existing leaders afterwards.
- **Tuck shaving** (`_shave_tucks`) — the cosmetic easing of a balloon off
  its own size text.
- The PNG evidence set (renders live in the Python tools).
- Category E (symbol clutter) is counted nowhere here and touched nowhere —
  per standing instruction.

Geometry note: rect-vs-curve overlap areas use a clipped-length×width
approximation instead of exact polygon booleans (no external geometry
library on an air-gapped box). Scores may differ from Python in the second
decimal; the validation step below is how we confirm that never changes a
*decision*.

## 7. Validation before trusting it (the Phase C exit gate)

For a sample of sheets (start with 209M05, 545M05, 164M05_r0-3):

1. `ISOTIDYSCORE` on the original in Plant 3D → note the BEFORE numbers.
2. Run the Python pipeline on the same DWG → compare BEFORE numbers.
   They should agree closely (small deltas from the area approximation are
   fine; different *counts* are not — investigate any).
3. `ISOTIDY`, save under a new name, run the Python **audit** on it
   (`tools\mess_map.py`) → A/B/C/D counts should match what the plugin
   claimed, and no category may be worse than the original.
4. Eyeball the sheet. The eye is the final acceptance test.

## 8. API notes (if the build complains)

Autodesk occasionally reshapes the managed MLeader surface between releases.
Everything else in this project uses long-stable APIs; the one spot to
adapt is `Extract.TryLeaderEnds` / `Writeback.RelandLeader`, which call
`MLeader.GetFirstVertex(0)`, `GetLastVertex(0)`, `SetLastVertex(0, pt)`.
If your release names them differently (e.g. indexed via
`GetLeaderLineIndexes`), the ObjectARX Managed Reference (installed with
the ObjectARX SDK, or the online .NET API docs for your release) has the
exact signatures — the *logic* stays identical: read both ends, move only
the landing, never the tip.

## 9. File map

```
isotidy-plugin\
  IsoTidy.csproj          build file (no NuGet; AutoCAD refs by path)
  src\
    Geometry.cs           rects, segments, ribbons -- self-contained 2D geometry
    Config.cs             LayerRoles + Tuning (numbers mirror ..\isotidy\config.py)
    Model.cs              Label / Scene           (mirrors model.py)
    FreeSpace.cs          occupancy grid          (mirrors freespace.py)
    Detect.cs             the referee             (mirrors detect.py)
    Extract.cs            Database -> Scene       (replaces extract.py)
    Solve.cs              candidates+costs+vetoes (mirrors solve.py)
    Writeback.cs          Scene -> Database       (replaces writeback.py)
    Commands.cs           ISOTIDY / ISOTIDYSCORE / ISOTIDYALL
  bundle\IsoTidy.bundle\
    PackageContents.xml   autoloader manifest (put built DLL in Contents\)
  acad-ref\               (you create; the 3 AutoCAD DLLs when building off-box)
```

Rule of the two codebases: **algorithm changes are proven in Python first**
(fleet run + tests), then mirrored here.  The Python side carries the sweep
tables and the war stories in its comments; this side carries the delivery.
