# Installs IsoTidy on THIS machine (the one with AutoCAD Plant 3D).
# Windows PowerShell 5.1 compatible -- run as the engineer, no admin needed.
#
#   powershell -ExecutionPolicy Bypass -File Install-IsoTidy.ps1
#
# What it does:
#   1. finds the built IsoTidy.dll (bin\Release next to this repo, or a
#      path you pass with -Dll)
#   2. copies the autoloader bundle to %APPDATA%\Autodesk\ApplicationPlugins
#      (that location is TRUSTED by AutoCAD -> no NETLOAD security prompt,
#      and the commands are available in every session automatically)
#   3. unblocks every copied file (files copied from a host/VM share are
#      zone-blocked and would fail to load -- DEPLOY-VM.md)
#
# After install: start Plant 3D, open an iso, type ISOTIDY.

param(
    [string]$Dll = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot     # ...\isotidy-plugin

if ($Dll -eq "") {
    $candidates = @(
        (Join-Path $root "bin\Release\IsoTidy.dll"),
        (Join-Path $root "bin\Release\net10.0-windows\IsoTidy.dll"),
        (Join-Path $root "bin\Release\net8.0-windows\IsoTidy.dll"),
        (Join-Path $root "IsoTidy.dll")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $Dll = $c; break }
    }
}
if ($Dll -eq "" -or -not (Test-Path $Dll)) {
    Write-Host "IsoTidy.dll not found. Build it first (README section 3):"
    Write-Host "    cd $root"
    Write-Host "    dotnet build -c Release"
    Write-Host "or pass the DLL path:  Install-IsoTidy.ps1 -Dll <path>"
    exit 1
}

# A loaded DLL cannot be overwritten: AutoCAD (or a watcher's
# accoreconsole) still running means the OLD plugin is locked in memory.
$running = Get-Process acad, accoreconsole -ErrorAction SilentlyContinue
if ($running) {
    Write-Host ""
    Write-Host "AutoCAD / Plant 3D is still running ($($running.Name -join ', ') - PID $($running.Id -join ', '))."
    Write-Host "It has the previous IsoTidy.dll loaded, so the new one cannot be"
    Write-Host "copied over it.  Closing the window is often not enough: acad.exe"
    Write-Host "can linger for a minute or hang invisibly.  With NO drawings open, run:"
    Write-Host "    Stop-Process -Name acad -Force"
    Write-Host "(and stop Watch-Isos.ps1 if it is running), then run this installer again."
    exit 1
}

$plugins = Join-Path $env:APPDATA "Autodesk\ApplicationPlugins"
$dest = Join-Path $plugins "IsoTidy.bundle"
New-Item -ItemType Directory -Force (Join-Path $dest "Contents") | Out-Null

Copy-Item (Join-Path $root "bundle\IsoTidy.bundle\PackageContents.xml") `
          (Join-Path $dest "PackageContents.xml") -Force
try {
    Copy-Item $Dll (Join-Path $dest "Contents\IsoTidy.dll") -Force -ErrorAction Stop
} catch {
    Write-Host ""
    Write-Host "Could not replace the installed IsoTidy.dll: $($_.Exception.Message)"
    Write-Host "Something still holds the old DLL open (AutoCAD, accoreconsole, or"
    Write-Host "an Explorer preview).  Close it and run the installer again."
    exit 1
}
# Never install a DLL older than the source it claims to come from.
$newest = (Get-ChildItem (Join-Path $root "src") -Filter *.cs |
           Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime
if ((Get-Item $Dll).LastWriteTime -lt $newest) {
    Write-Host ""
    Write-Host "WARNING: IsoTidy.dll is OLDER than the newest source file."
    Write-Host "The last build probably failed and left a stale DLL behind."
    Write-Host "Run 'dotnet build -c Release' again and read its errors before using this."
}

Get-ChildItem $dest -Recurse -File | ForEach-Object {
    try { Unblock-File $_.FullName -ErrorAction Stop } catch {}
}

Write-Host ""
Write-Host "Installed: $dest"
Write-Host ""
Write-Host "Next: start AutoCAD Plant 3D (restart it if it was open)."
Write-Host "Open any ISOGEN isometric and type:"
Write-Host "    ISOTIDY        tidy the open drawing (Ctrl+Z rejects)"
Write-Host "    ISOTIDYSCORE   dry run, numbers only"
Write-Host "    ISOTIDYALL     batch a folder -> .\tidied\ copies"
Write-Host ""
Write-Host "For hands-free tidying of every new iso, see Watch-Isos.ps1."
