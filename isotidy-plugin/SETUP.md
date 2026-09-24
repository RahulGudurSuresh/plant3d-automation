# IsoTidy — setup guide for the CAD engineer

Follow these steps in order on the machine (or VM) that has AutoCAD
Plant 3D installed.  Total time: about 15 minutes, done once.

What you end up with: three new commands inside Plant 3D —

| Command | What it does |
|---|---|
| `ISOTIDY` | tidies the open isometric in place; **Ctrl+Z once rejects everything** |
| `ISOTIDYSCORE` | dry run — prints the numbers, changes nothing |
| `ISOTIDYALL` | tidies a whole folder of DWGs; corrected copies go to `tidied\`, originals untouched |

plus an optional watcher that corrects every NEW iso automatically the
moment ISOGEN creates it.

Input is DWG, output is DWG — the plugin edits the drawing inside
AutoCAD itself.  There is no file conversion of any kind.

---

## Step 0 — copy the zip over and extract it

1. Copy `isotidy-plugin-handoff.zip` to this machine (USB stick, network
   share, VM shared folder — anything).
2. Right-click the zip → **Properties**.  IF you see an **Unblock**
   checkbox at the bottom, tick it → OK.  **If there is no such
   checkbox, the file is already trusted — nothing to do, carry on.**
   (Windows only adds it to files that arrived via browser download or
   email; USB and most share copies are never flagged.)
3. Extract the zip, for example to `C:\isotidy-plugin`.
4. Belt-and-braces — this one line works in every case (unblocks if
   needed, silently does nothing otherwise):

   ```powershell
   Get-ChildItem C:\isotidy-plugin -Recurse | Unblock-File
   ```

> Why this exists at all: a blocked plugin DLL silently refuses to load
> ("Unknown command ISOTIDY").  Running the line above once removes the
> possibility entirely.

## Step 1 — install the .NET 10 SDK (build tool, one time)

With internet:

```powershell
winget install Microsoft.DotNet.SDK.10
```

Without internet (air-gapped machine/VM): on any other PC, download the
**.NET 10 SDK — Windows x64 installer** from
`https://dotnet.microsoft.com/download/dotnet/10.0`, copy the installer
over, run it.

(Plant 3D 2026 and 2027 run plugins on .NET 10, so the .NET 8 SDK is
not enough — `dotnet --list-sdks` must show a `10.x` line.)

Nothing else is ever downloaded — the plugin has zero online
dependencies on purpose.

## Step 2 — build the plugin (one command)

Open PowerShell:

```powershell
cd C:\isotidy-plugin
dotnet build -c Release
```

Expected: `Build succeeded` and the file `bin\Release\IsoTidy.dll`.

The project finds AutoCAD 2026 automatically (then 2027, then 2025).  If
AutoCAD is installed somewhere unusual:

```powershell
dotnet build -c Release "-p:AcadDir=C:\Program Files\Autodesk\AutoCAD 2026"
```

> If the build prints ERRORS instead: copy the full error text and send
> it back to us — the AutoCAD programming interface shifts slightly
> between releases and a one-line fix on our side is normal.  Do not
> try to use a DLL from a failed build.

## Step 3 — install the plugin (one command)

```powershell
powershell -ExecutionPolicy Bypass -File deploy\Install-IsoTidy.ps1
```

This copies the plugin to `%APPDATA%\Autodesk\ApplicationPlugins` — a
folder AutoCAD trusts and auto-loads from — and unblocks the files.
From now on the commands exist in **every** Plant 3D session, no
NETLOAD needed.

Restart Plant 3D if it was open.

## Step 4 — validate before trusting it

Do this on **copies** of 3–5 isometrics you know well:

1. Open the copy, type `ISOTIDYSCORE`.  Read the BEFORE numbers —
   nothing has changed yet.
2. Type `ISOTIDY`.  Check with your own eyes:
   - every arrowhead is exactly where it was (this is a hard rule the
     tool enforces on itself — if you ever see a moved arrowhead,
     stop and report it);
   - labels are readable and off the dimension lines;
   - nothing sits on the DESIGN DATA table;
   - tag boxes hold their full text.
3. Not happy? `Ctrl+Z` once undoes the entire tidy.
4. Happy? Save.  The corrected drawing is the same DWG, same name,
   same folder.

Only after this spot-check should the tool touch production drawings.

## Step 5 — daily use (pick whichever fits)

**Per drawing** — open the iso, `ISOTIDY`, glance, save.

**Per batch** — type `ISOTIDYALL`, point it at the project's ISO output
folder, e.g.:

```
C:\<Project>\Isometric\<Style>\ProdIsos
```

Corrected copies appear in `ProdIsos\tidied\`; your originals are never
overwritten.

**Fully automatic** — leave the watcher running (or add it as a Task
Scheduler job at logon):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\Watch-Isos.ps1 `
    -Folder "C:\<Project>\Isometric\<Style>\ProdIsos"
```

Every new iso ISOGEN drops into that folder is corrected in place
within seconds — same file name, still a DWG — and the untouched
original is kept in `ProdIsos\original\`.  If a sheet fails, the
original is restored automatically; nothing is ever left half-tidied.
`Ctrl+C` in that PowerShell window stops the watcher.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Unknown command "ISOTIDY"` | Plugin not loaded.  Did you restart Plant 3D after Step 3?  Still failing → the DLL is blocked: `Unblock-File "$env:APPDATA\Autodesk\ApplicationPlugins\IsoTidy.bundle\Contents\IsoTidy.dll"` and restart again. |
| Security prompt when loading | The DLL is outside the trusted folder.  Re-run Step 3 rather than NETLOADing from a random path. |
| `Build succeeded` but `IsoTidy.dll` missing | Look in `bin\Release\` (not `bin\Debug\`).  You built without `-c Release`. |
| Build error `acdbmgd could not be found` | AutoCAD not found — pass `-p:AcadDir=...` as in Step 2. |
| Build error `NETSDK1045: The current .NET SDK does not support targeting .NET 10.0` | Step 1 was skipped or installed the wrong SDK. `dotnet --list-sdks` must list a `10.x` entry; install the .NET 10 SDK and build again. |
| **Security - Unsigned Executable File** prompt in Plant 3D | Expected: the DLL is not code-signed. For a one-off test (NETLOAD of `bin\Release\IsoTidy.dll`) choose **Load Once**; if it ever appears for the installed bundle at startup, choose **Always Load** once. |
| `Unable to load ... IsoTidy.dll assembly` after NETLOAD in `accoreconsole` (watcher, or a script of your own) | The console enforces `SECURELOAD` like Plant 3D does but cannot show the prompt above, so with the default `SECURELOAD=1` it refuses the DLL silently - even from the ApplicationPlugins bundle (verified 2026-09-24). The shipped `Watch-Isos.ps1` already handles this with a throwaway `/isolate` profile in which `SECURELOAD` is 0; your real settings are untouched. In a script of your own, put `(setvar "SECURELOAD" 0)` before `_NETLOAD` and start the console with `/isolate <name> <folder>`. |
| `NO MANAGED ANNOTATIONS FOUND` when running | The drawing's layers don't match the configured project convention — send us the layer names it lists and we add them to the layer map (`src/Config.cs`). This is a safety refusal, not a crash: the tool will not "certify" a sheet it cannot read. |
| Watcher: `accoreconsole.exe not found` | Pass it explicitly: `-AcCoreConsole "C:\Program Files\Autodesk\AutoCAD 2026\accoreconsole.exe"` |
| A sheet looks WORSE after ISOTIDY | `Ctrl+Z`, save nothing, and send us the drawing — that outcome violates the tool's own rules and we treat it as a bug. |

## What this plugin never does

- never moves an arrowhead
- never touches component symbols, the 3D model, PCF files, or ISOGEN
- never overwrites an original in batch/watcher mode
- never phones home — no network access at build time or run time
