# IsoTidyPy — setup with VS Code (10 minutes, once)

**What this is.** The proven Python isotidy pipeline packaged as one
command, `isotidypy`: DWG in, corrected DWG out. Internally it converts
DWG → DXF with the free ODA File Converter, runs the solver and every
rework stage proven on 71 real drawings, converts back, and re-measures
the file that ships. **No AutoCAD is needed to run it.** Optional: a
6-line bridge gives AutoCAD an `ISOTIDYPY` command that calls it.

Nothing here moves an arrowhead, touches the 3D model, PCF or ISOGEN,
or overwrites an original unless you say `--inplace` (and then a backup
is kept in `original\`).

---

## 1. Copy and unblock

Copy `isotidypy-plugin.zip` to the machine. Right-click → Properties →
tick **Unblock** if the box is there (it often isn't — then nothing to
do). Extract, for example to `D:\isotidypy-plugin`. Any drive works; a
local folder (not OneDrive) is smoother.

## 2. Open the folder in VS Code

File → Open Folder → the extracted folder. VS Code will pick up the
workspace settings and tasks automatically. If it offers to install the
**Python extension**, accept (it is optional for running, useful for
debugging).

## 3. Run the setup task

**Terminal → Run Task → `IsoTidyPy: 1. Setup (one time)`**

That runs `setup.ps1`, which:
- finds Python 3.10+ (installs 3.11 via winget if missing),
- finds the ODA File Converter (installs it via winget if missing — it
  is free),
- creates a private `.venv` in the folder and installs every package —
  **offline from `wheels\`** when the Python version matches (3.11 or
  3.12), online otherwise,
- verifies `isotidypy --version`,
- writes the AutoCAD bridge with this machine's paths filled in,
- unblocks every file.

No admin rights needed. If the machine has no internet and no matching
wheels, it says so — install Python 3.11 or 3.12 and re-run.

Prefer the terminal? Same thing:

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

## 4. Prove it works

**Terminal → Run Task → `IsoTidyPy: 2. Self-test`** — runs the fixture
regression suite (11 tests, ~40 s). All green = the installation matches
the reference machine.

Then tidy a real drawing — **on a copy the first time**:
**Run Task → `IsoTidyPy: Tidy a file or folder`** → paste the path.
The output ends with a VERDICT block: defect counts before → after,
arrowheads unmoved, remaining findings, and where the DWG went.

## 5. Daily use — three ways

| Want | Do |
|---|---|
| tidy one drawing | Task **Tidy a file or folder** → the `.dwg` path → result in `tidied\` next to it |
| tidy a whole folder | same task → the folder path (e.g. `…\Isometric\<Style>\ProdIsos`) |
| auto-tidy every new iso | Task **Watch a folder** → the `ProdIsos` path; leave the terminal open |
| just measure, change nothing | Task **Score** |

Every run also writes an evidence folder `isotidypy_out\<drawing>\` with
full-sheet BEFORE / AFTER / DIFF images and the DXFs — that is what to
look at when judging a result.

From a plain terminal it is the same commands:

```powershell
.\.venv\Scripts\isotidypy.exe "C:\path\to\iso.dwg"
.\.venv\Scripts\isotidypy.exe "C:\path\to\ProdIsos"
.\.venv\Scripts\isotidypy.exe "C:\path\to\ProdIsos" --watch
.\.venv\Scripts\isotidypy.exe "C:\path\to\iso.dwg" --inplace     # replaces; backup in original\
.\.venv\Scripts\isotidypy.exe "C:\path\to\iso.dwg" --score       # measure only
```

## 6. Optional — an ISOTIDYPY command inside AutoCAD

After setup, `acad-bridge\isotidypy.lsp` exists. In Plant 3D: `APPLOAD`
→ pick it (add to *Startup Suite* to keep it). Then, with an iso open,
type `ISOTIDYPY`: the drawing is saved, the tool runs in a console
window, and the corrected copy appears in `tidied\`. The open drawing
itself is never written to (AutoCAD holds it locked) — open the copy.
This bridge is deliberately tiny and has not been run on a live AutoCAD
by its author; the command-line tool is the same thing without it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ODA File Converter is not installed` | `winget install ODA.ODAFileConverter`, or the download link it prints |
| `winget` not recognised | install Python and ODA by hand (both free), re-run setup |
| setup: `pip install failed` | no internet and no wheels for this Python — install Python 3.11 or 3.12 and re-run |
| `REFUSING VERDICT: 0 labels mapped` | the drawing's layers do not match the configured project convention — send the drawing; it is a safety refusal, not a crash |
| a run says a rework stage was **rolled back** | it would have made the sheet worse; the tool kept the better version — working as designed |
| result looks wrong | open `isotidypy_out\<drawing>\*_DIFF.png` — it shows exactly what moved; send it with the drawing |

## What is in the folder

```
isotidypy\      the command (cli.py)          tools\      pipeline, rework, checkers, renders
isotidy\        the solver library            tests\ fixtures\   regression suite
wheels\         offline packages              acad-bridge\       optional AutoCAD command
.vscode\        tasks / settings / debug      setup.ps1          one-shot installer
```
