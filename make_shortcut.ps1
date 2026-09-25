# Create/refresh the Panopticon desktop shortcut.
#
# WHY NOT JUST POINT IT AT _launch.bat:
#   A .bat is a console program, so Windows allocates a console window for it,
#   and `uv run` then blocks for the entire life of the app -- so that console
#   sits on the taskbar for the whole session. Setting the shortcut to
#   "minimised" hides it but does not stop it existing.
#
# WHY NOT ALWAYS THE VENV'S pythonw.exe:
#   The shortcut needs a GUI-subsystem launcher, which Windows never gives a
#   console. A venv made by uv has a Scripts\pythonw.exe that is a copy of its
#   console trampoline: it is a console program, and it starts the console
#   python.exe. Windows then opens a console window before Panopticon's, and
#   the taskbar entry belongs to that console process. CPython's own GUI venv
#   launcher (Lib\venv\scripts\nt\pythonw.exe in the base install) reads the
#   venv's pyvenv.cfg and starts the base pythonw.exe inside the venv, with no
#   console. When the venv's pythonw.exe is a console program, this script
#   copies that launcher to Scripts\panopticonw.exe and points the shortcut at
#   it. A uv sync leaves the copy alone; a recreated venv needs this script
#   run again.
#
# TRADE-OFF: this bypasses `uv run`, so it does NOT sync dependencies first. If
# pyproject.toml changes, run `uv sync` once. _launch.bat is kept for exactly
# that case -- and for seeing console output while debugging: it runs
# python.exe, not pythonw.exe, and pauses on a non-zero exit so a failure
# before the log file opens stays on screen.
[CmdletBinding()]
param(
    [string] $ShortcutPath = "$([Environment]::GetFolderPath('Desktop'))\Panopticon.lnk",
    [string] $RepoDir      = ""
)

$ErrorActionPreference = "Stop"

# Resolved here, not as a param() default: in Windows PowerShell 5.1
# $PSScriptRoot is not yet populated while param() defaults are evaluated, so it
# arrives as an empty string and every Join-Path below fails.
if (-not $RepoDir) {
    $RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}

$venv    = Join-Path $RepoDir ".venv"
$pythonw = Join-Path $venv "Scripts\pythonw.exe"
$script  = Join-Path $RepoDir "gui.py"
$icon    = Join-Path $RepoDir "panopticon.ico"

if (-not (Test-Path $pythonw)) {
    Write-Host "No venv at $pythonw" -ForegroundColor Red
    Write-Host "Run 'uv sync' in $RepoDir first." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $script)) { Write-Host "Missing $script" -ForegroundColor Red; exit 1 }

# The PE header's Subsystem field: 2 is a GUI program, 3 a console program.
function Get-PeSubsystem([string] $Path) {
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $pe = [BitConverter]::ToInt32($bytes, 0x3C)
    return [BitConverter]::ToUInt16($bytes, $pe + 0x5C)
}

$launcher = $pythonw
if ((Get-PeSubsystem $pythonw) -ne 2) {
    $cfg = Join-Path $venv "pyvenv.cfg"
    $homeLine = Get-Content $cfg | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
    if (-not $homeLine) {
        Write-Host "$cfg names no home interpreter; cannot find a windowless launcher." -ForegroundColor Red
        exit 1
    }
    $baseHome = ($homeLine -split '=', 2)[1].Trim()
    $gui = Join-Path $baseHome "Lib\venv\scripts\nt\pythonw.exe"
    if (-not (Test-Path $gui) -or (Get-PeSubsystem $gui) -ne 2) {
        Write-Host "$pythonw is a console program and $gui is not a GUI launcher." -ForegroundColor Red
        Write-Host "The shortcut would open a console window. Recreate the venv from a" -ForegroundColor Red
        Write-Host "Python that ships its venv launchers, or use _launch.bat." -ForegroundColor Red
        exit 1
    }
    $launcher = Join-Path $venv "Scripts\panopticonw.exe"
    Copy-Item -LiteralPath $gui -Destination $launcher -Force
    Write-Host "The venv's pythonw.exe is a console program; the shortcut uses" -ForegroundColor Yellow
    Write-Host "CPython's GUI venv launcher, copied to $launcher" -ForegroundColor Yellow
}

$sh = New-Object -ComObject WScript.Shell
$sc = $sh.CreateShortcut($ShortcutPath)
$sc.TargetPath       = $launcher
$sc.Arguments        = '"' + $script + '"'
$sc.WorkingDirectory = $RepoDir
$sc.WindowStyle      = 1          # normal; irrelevant for a GUI binary, but explicit
$sc.Description      = "Panopticon multi-camera acquisition"
if (Test-Path $icon) { $sc.IconLocation = "$icon,0" }
$sc.Save()

Write-Host "Shortcut written: $ShortcutPath" -ForegroundColor Green
Write-Host "  Target : $($sc.TargetPath)"
Write-Host "  Args   : $($sc.Arguments)"
Write-Host "  WorkDir: $($sc.WorkingDirectory)"
Write-Host ""
Write-Host "No console window will appear. If the app fails to start and you"
Write-Host "need to see why, run _launch.bat instead -- it keeps the console"
Write-Host "open and pauses on failure so the traceback can be read."
