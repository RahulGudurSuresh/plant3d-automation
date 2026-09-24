"""isotidypy -- the proven Python isotidy pipeline as one command.

    isotidypy <drawing.dwg>          tidy one drawing  -> tidied\<name>.dwg
    isotidypy <folder>               tidy every DWG in a folder
    isotidypy <folder> --watch       keep watching; tidy each new iso as it lands
    isotidypy <path> --score         measure only, write nothing
    isotidypy <path> --inplace       replace the original (backup in original\)
    isotidypy --selftest             run the fixture regression suite

DWG in, DWG out.  Internally: ODA File Converter (DWG -> DXF), the isotidy
solver + rework stages, ODA back to DWG, and every result is re-measured on
the file that ships.  No AutoCAD needed.
"""
__version__ = "1.0.0"
