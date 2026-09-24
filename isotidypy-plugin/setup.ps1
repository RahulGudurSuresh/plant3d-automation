# One-shot setup for isotidypy on a Windows machine.  Windows PowerShell 5.1.
#
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#
# 1. Python 3.10+   (installs 3.11 via winget if missing)
# 2. ODA File Converter (installs via winget if missing -- free)
# 3. a private .venv in this folder + all packages (offline from wheels\
#    when possible, online otherwise)
# 4. the `isotidypy` command inside that venv, verified
# 5. the AutoCAD bridge (acad-bridge\isotidypy.lsp) with this machine's
#    paths filled in, for those who want an ISOTIDYPY command in AutoCAD
# Nothing is written outside this folder except what winget installs.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

function Say($m) { Write-Host ""; Write-Host "== $m" }

# --- 1. Python -------------------------------------------------------------
Say "Python"
$py = $null
foreach ($cand in @("py -3.12", "py -3.11", "py -3.10", "python")) {
    try {
        $v = & cmd /c "$cand -c `"import sys;print(sys.version_info[:2])`"" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -match "\((\d+), (\d+)\)") {
            if ([int]$matches[1] -eq 3 -and [int]$matches[2] -ge 10) { $py = $cand; break }
        }
    } catch {}
}
if (-not $py) {
    Write-Host "Python 3.10+ not found -- installing Python 3.11 (winget) ..."
    winget install --id Python.Python.3.11 -e --accept-package-agreements --accept-source-agreements
    $py = "py -3.11"
}
Write-Host "using: $py"

# --- 2. ODA File Converter -------------------------------------------------
Say "ODA File Converter"
$oda = Get-ChildItem "C:\Program Files\ODA\*\ODAFileConverter.exe" -ErrorAction SilentlyContinue
if (-not $oda) { $oda = Get-ChildItem "C:\Program Files (x86)\ODA\*\ODAFileConverter.exe" -ErrorAction SilentlyContinue }
if (-not $oda) {
    Write-Host "not found -- installing (winget) ..."
    winget install --id ODA.ODAFileConverter -e --accept-package-agreements --accept-source-agreements
    $oda = Get-ChildItem "C:\Program Files\ODA\*\ODAFileConverter.exe" -ErrorAction SilentlyContinue
}
if ($oda) { Write-Host "found: $($oda[0].FullName)" }
else { Write-Host "!! still not found. Install it from https://www.opendesign.com/guestfiles/oda_file_converter and re-run."; exit 1 }

# --- 3. venv + packages ----------------------------------------------------
Say "virtual environment"
if (-not (Test-Path ".venv")) { & cmd /c "$py -m venv .venv" }
$pip = ".\.venv\Scripts\python.exe -m pip"
& cmd /c "$pip install --upgrade pip --quiet"
$installed = $false
if (Test-Path "wheels") {
    Write-Host "installing from wheels\ (offline) ..."
    & cmd /c "$pip install --no-index --find-links wheels -e . --quiet"
    if ($LASTEXITCODE -eq 0) { $installed = $true }
    else { Write-Host "offline install did not cover this Python version -- trying online ..." }
}
if (-not $installed) {
    & cmd /c "$pip install -e . --quiet"
    if ($LASTEXITCODE -ne 0) { Write-Host "!! pip install failed (no internet and no matching wheels?)"; exit 1 }
}

# --- 4. verify -------------------------------------------------------------
Say "verify"
$exe = Join-Path $root ".venv\Scripts\isotidypy.exe"
& $exe --version
if ($LASTEXITCODE -ne 0) { Write-Host "!! isotidypy did not start"; exit 1 }

# --- 5. AutoCAD bridge with real paths -------------------------------------
Say "AutoCAD bridge"
$tpl = Join-Path $root "acad-bridge\isotidypy.lsp.template"
$lsp = Join-Path $root "acad-bridge\isotidypy.lsp"
if (Test-Path $tpl) {
    (Get-Content $tpl -Raw).Replace("__ISOTIDYPY_EXE__", $exe.Replace("\", "\\")) |
        Set-Content $lsp -Encoding ascii
    Write-Host "wrote $lsp  (APPLOAD it in AutoCAD for an ISOTIDYPY command -- optional)"
}

Get-ChildItem $root -Recurse -File | ForEach-Object { try { Unblock-File $_.FullName -ErrorAction Stop } catch {} }

Say "done"
Write-Host "  .\.venv\Scripts\isotidypy.exe  ""C:\path\to\iso.dwg""      tidy one drawing"
Write-Host "  .\.venv\Scripts\isotidypy.exe  ""C:\path\to\ProdIsos""      tidy a folder"
Write-Host "  .\.venv\Scripts\isotidypy.exe  ""C:\path\to\ProdIsos"" --watch   auto-tidy new isos"
Write-Host "  .\.venv\Scripts\isotidypy.exe  --selftest                     regression suite"
Write-Host "In VS Code: Terminal > Run Task > pick one of the IsoTidyPy tasks."
