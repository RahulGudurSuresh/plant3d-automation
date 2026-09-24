# Phase D: tidy every NEW iso the moment ISOGEN drops it -- no command,
# the engineer only ever opens corrected drawings.
# Windows PowerShell 5.1 compatible.
#
#   powershell -ExecutionPolicy Bypass -File Watch-Isos.ps1 `
#       -Folder "C:\Plant3D Projects\JP1071\Isometric\Check_A1\ProdIsos"
#
# How it works (deliberately boring and robust):
#   - polls the folder (default every 15 s; a FileSystemWatcher misses
#     events on network shares, polling never does)
#   - waits until a new DWG has been STABLE for two polls (ISOGEN may
#     still be writing it)
#   - copies the untouched original to .\original\  (never-overwrite rule)
#   - runs AutoCAD's console engine on it:
#         accoreconsole.exe /i <dwg> /s tidy-one.scr
#     which NETLOADs IsoTidy.dll, runs ISOTIDY, and QSAVEs -- so the DWG
#     in place IS the corrected one, still a DWG, same name, same folder
#   - records it in .isotidy-done.txt so nothing is tidied twice
#
# Requirements: Install-IsoTidy.ps1 has been run (the .scr NETLOADs the DLL
# from the ApplicationPlugins bundle), or pass -Dll <path> to use any build.
#
# SECURELOAD (verified 2026-09-24 on Plant 3D 2026): accoreconsole enforces
# it exactly like acad.exe but cannot show the "unsigned executable" prompt,
# so with the default SECURELOAD=1 it silently REFUSES an unsigned DLL from
# any folder outside the AutoCAD install directory -- the ApplicationPlugins
# bundle included -- and it does not run the bundle autoloader either.  The
# generated .scr therefore sets SECURELOAD 0, but only inside a throwaway
# accoreconsole profile (/isolate) under %LOCALAPPDATA%\IsoTidy; the
# engineer's real AutoCAD profile is never touched.

param(
    [Parameter(Mandatory = $true)][string]$Folder,
    [int]$IntervalSeconds = 15,
    [string]$AcCoreConsole = "",
    [string]$Dll = "",
    [switch]$Once
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $Folder)) { Write-Host "No such folder: $Folder"; exit 1 }

# --- locate accoreconsole.exe (2026 preferred, else newest) ----------------
# 2026 (R25.1) is the release this build targets; a newer AutoCAD is only
# used when 2026 is absent.
if ($AcCoreConsole -eq "") {
    $hits = Get-ChildItem "C:\Program Files\Autodesk\AutoCAD *\accoreconsole.exe" `
            -ErrorAction SilentlyContinue | Sort-Object FullName -Descending
    $pref = @($hits | Where-Object { $_.FullName -like "*AutoCAD 2026*" })
    if ($pref.Count -gt 0) { $AcCoreConsole = $pref[0].FullName }
    elseif ($hits)         { $AcCoreConsole = $hits[0].FullName }
}
if ($AcCoreConsole -eq "" -or -not (Test-Path $AcCoreConsole)) {
    Write-Host "accoreconsole.exe not found -- pass -AcCoreConsole <path>"
    exit 1
}

$dll = $Dll
if ($dll -eq "") {
    $dll = Join-Path $env:APPDATA "Autodesk\ApplicationPlugins\IsoTidy.bundle\Contents\IsoTidy.dll"
}
if (-not (Test-Path $dll)) {
    Write-Host "IsoTidy.dll not found ($dll) -- run Install-IsoTidy.ps1 first, or pass -Dll <path>."
    exit 1
}
$dll = (Resolve-Path $dll).Path
if ($dll -match ' ') {
    # A space ends a line in an AutoCAD script, so NETLOAD cannot be given
    # such a path from a .scr (a user name with a space would trigger this).
    Write-Host "The DLL path contains a space, which an AutoCAD script cannot pass to NETLOAD:"
    Write-Host "    $dll"
    Write-Host "Copy IsoTidy.dll to a folder without spaces and pass it with -Dll."
    exit 1
}

# --- the per-drawing script (generated so the DLL path is absolute) --------
# SECURELOAD 0 is what lets the console NETLOAD an unsigned DLL (see the
# header); it only ever applies to the isolated profile created below.
$scr = Join-Path $env:TEMP "isotidy-one.scr"
@"
(setvar "SECURELOAD" 0)
_NETLOAD
$dll
_ISOTIDY
_QSAVE
"@ | Out-File $scr -Encoding ascii
$profileDir = Join-Path $env:LOCALAPPDATA "IsoTidy\accoreconsole-profile"
New-Item -ItemType Directory -Force $profileDir | Out-Null
$log = Join-Path $env:TEMP "isotidy-one.log"

$doneFile = Join-Path $Folder ".isotidy-done.txt"
if (-not (Test-Path $doneFile)) {
    New-Item -ItemType File $doneFile | Out-Null
}
$origDir = Join-Path $Folder "original"
New-Item -ItemType Directory -Force $origDir | Out-Null

Write-Host "Watching $Folder  (engine: $AcCoreConsole)"
Write-Host "Originals preserved in $origDir; Ctrl+C stops the watcher."

$sizes = @{}
$pass = 0
while ($true) {
    $pass++
    $done = @(Get-Content $doneFile -ErrorAction SilentlyContinue)
    $dwgs = Get-ChildItem (Join-Path $Folder "*.dwg") -ErrorAction SilentlyContinue
    foreach ($f in $dwgs) {
        if ($done -contains $f.Name) { continue }

        # stability gate: same size on two consecutive polls
        $prev = $sizes[$f.Name]
        $sizes[$f.Name] = $f.Length
        if ($null -eq $prev -or $prev -ne $f.Length) { continue }

        Write-Host ("[{0}] tidying {1} ..." -f (Get-Date -Format HH:mm:ss), $f.Name)
        Copy-Item $f.FullName (Join-Path $origDir $f.Name) -Force

        $p = Start-Process -FilePath $AcCoreConsole `
             -ArgumentList @("/i", "`"$($f.FullName)`"", "/s", "`"$scr`"", "/l", "en-US",
                             "/isolate", "isotidy", "`"$profileDir`"") `
             -RedirectStandardOutput $log -Wait -PassThru -NoNewWindow

        # Exit code 0 only means "the script ran to its end".  A refused
        # NETLOAD + "Unknown command" + QSAVE of the untouched drawing ALSO
        # exits 0 (seen 2026-09-24), so the verdict comes from what ISOTIDY
        # printed.  The console's stdout is NUL-padded; strip that first.
        $out = ""
        try {
            $bytes = [System.IO.File]::ReadAllBytes($log) | Where-Object { $_ -ne 0 }
            $out = [System.Text.Encoding]::ASCII.GetString([byte[]]$bytes)
        } catch {}
        $tidied  = $out.Contains("AFTER (measured on the written drawing)")
        $problem = $null
        foreach ($sig in @("Unable to load", "Unknown command",
                           "!! ISOTIDY REFUSED", "!! NO MANAGED ANNOTATIONS", "!! stage")) {
            if ($out.Contains($sig)) { $problem = $sig; break }
        }
        if ($p.ExitCode -eq 0 -and $tidied -and $null -eq $problem) {
            Add-Content $doneFile $f.Name
            Write-Host ("    done -- {0} corrected in place (original kept)" -f $f.Name)
        } else {
            # Put the original back and keep the console log next to it.
            Copy-Item (Join-Path $origDir $f.Name) $f.FullName -Force
            Copy-Item $log (Join-Path $origDir ($f.BaseName + ".console.log")) -Force -ErrorAction SilentlyContinue
            if ($null -eq $problem) { $problem = "no AFTER report, exit code $($p.ExitCode)" }
            if ($problem -like "!!*") {
                # ISOTIDY itself declined this sheet and would decline it again:
                # record it as handled (untouched) instead of retrying forever.
                Add-Content $doneFile $f.Name
                Write-Host ("    !! {0} on {1} -- left untouched, see original\{2}.console.log" `
                            -f $problem, $f.Name, $f.BaseName)
            } else {
                Write-Host ("    !! {0} on {1} -- original restored, will retry next poll" `
                            -f $problem, $f.Name)
            }
        }
    }
    # -Once still needs two polls: the first only records sizes for the
    # stability gate, the second does the work.
    if ($Once -and $pass -ge 2) { break }
    Start-Sleep -Seconds $(if ($Once) { 2 } else { $IntervalSeconds })
}
