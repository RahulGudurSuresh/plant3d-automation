# IsoTidyPy

**One command that cleans up ISOGEN isometric drawings: `isotidypy`.**

When Plant 3D generates an isometric, the labels, tags and dimensions
land on top of each other on busy sheets, on dimension lines, with
leaders crossing.  Someone fixes that by hand, 20–40 minutes per sheet.
This tool does it in about a minute per sheet, with the same logic
proven on 71 real project drawings, and it never makes a drawing worse:
every result is re-measured on the file that ships, and any step that
would regress is rolled back.

- **DWG in, DWG out** — no AutoCAD needed (the free ODA File Converter
  does DWG ↔ DXF; we measured that conversion lossless).
- **Arrowheads never move.**  Component symbols, the 3D model, PCF and
  ISOGEN are never touched.  Originals are never overwritten unless you
  ask (`--inplace`, with a backup).
- **Evidence with every run**: full-sheet BEFORE / AFTER / DIFF images.

Start with **[SETUP.md](SETUP.md)** — VS Code, one setup task, done.

```
isotidypy  iso.dwg              → tidied\iso.dwg
isotidypy  ProdIsos\            → every DWG in the folder
isotidypy  ProdIsos\  --watch   → auto-tidy each new iso as it appears
isotidypy  iso.dwg    --score   → numbers only, nothing written
isotidypy  --selftest           → regression suite
```

Relationship to the AutoCAD plugin (`isotidy-plugin`, C#): same rules,
same order.  This package is the reference implementation; the plugin
is the in-process port of it and is validated against this.
