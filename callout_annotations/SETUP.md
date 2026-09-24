# callout_annotations — setup

Puts the **full P&ID line name** on every pipe run of a Plant 3D ortho
DWG. Input: the ortho DWG and the P&ID binder PDF. Output: a copy of the
DWG with one MTEXT callout + leader per run, plus a report of what still
needs a human. No AutoCAD is needed to run it. See `README.md` for how it
works and `CONTEXT.md` for the decisions behind it.

## 1. Prerequisites

| Need | Why | Get it |
|---|---|---|
| Python 3.11 (64-bit) | the tool | `winget install Python.Python.3.11` |
| ODA File Converter (free) | DWG ↔ DXF; the code looks in `C:\Program Files\ODA\*\ODAFileConverter.exe` | `winget install ODA.ODAFileConverter` |
| A P&ID PDF with a **text layer** | the line names are read from the text, not OCR'd | export from the P&ID tool; a scanned binder will be refused |

## 2. Python packages

On the development host use the shared virtualenv one level above this
repository (`d:\AI_Rahul\Automation\.venv`); do **not** create a `.venv`
inside this folder. It already has everything.

On any other machine, create an environment anywhere outside the repo
and install the packages once:

```powershell
py -3.11 -m venv C:\venvs\callout
C:\venvs\callout\Scripts\python.exe -m pip install "ezdxf>=1.4.4" "shapely>=2.1" "numpy>=2.2" "matplotlib>=3.11" "pypdfium2>=5.12" "pytest>=9"
```

Below, `python` means that interpreter.

## 3. Inputs

The JP1071 sample inputs are **not in this repository** (client data).
Put your own in `inputs\` or anywhere else; the file names are free:

```
inputs\
  Plan view.dwg      the Plant 3D ortho (must be an ortho, not a model)
  PID-Binder.pdf     the P&ID binder, vector pages
```

The line-name grammar is per site (`callout_annotations/config.py`,
class `Site`). Only `jp1071` exists today and `--site default` is an
alias for it. Another project with a different naming rule needs its own
`Site` entry there.

## 4. Run

From this folder:

```powershell
python -m callout_annotations.cli "inputs\Plan view.dwg" inputs\PID-Binder.pdf --out out --png
```

Options: `--mode inline` (default, beside the pipe where clean) or
`--mode margin` (all labels outside the drawing); `--label 01` appends a
version tag to the output names; a `.dxf` may be given instead of the
`.dwg` to skip the ODA round trip.

Results land in `out\`:

| File | What |
|---|---|
| `<name>_CALLOUTS.dwg` | the ortho with callouts; open in Plant 3D / AutoCAD |
| `report.md` | read first: what needs a human, what was resolved by size |
| `report.csv` | one row per line tag: status, label, candidates, views |
| `<view>.png` | one picture per view (with `--png`) |

Red callouts on layer `*-CALLOUT-REVIEW` are the ones the tool would not
guess (ambiguous or missing in the P&ID). Re-running deletes the previous
callouts first, so it is safe to iterate on the same drawing.

## 5. Verify the installation

```powershell
python -m pytest tests -q
```

`tests\test_units.py` always runs (10 tests, under a second).
`tests\test_pipeline.py` is the regression over the real inputs and
skips itself when `inputs\` is absent.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ODA File Converter not found` | install it (see §1); it must be under `C:\Program Files\ODA\` |
| the CLI stops saying the PDF has no text layer | the binder is scanned; export a vector PDF from the P&ID tool |
| a line comes out red `NOT IN P&ID` | the tag's sequence number is in no P&ID name; check the binder is the right revision |
| a line comes out red with two candidates | two areas share the sequence number and the sizes match; pick by hand, `report.csv` lists both |
| everything unresolved | the drawing's layer naming does not follow `<view>-<LineNumberTag>`; see `Site` in `config.py` |
