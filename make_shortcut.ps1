# Create/refresh the Panopticon desktop shortcut.
#
# WHY NOT JUST POINT IT AT _launch.bat:
#   A .bat is a console program, so Windows allocates a console window for it,
#   and `uv run` then blocks for the entire life of the app -- so that console
#   sits on the taskbar for the whole session. Setting the shortcut to
#   "minimised" hides it but does not stop it existing.
#
#   pythonw.exe is a GUI-subsystem binary: Windows never gives it a console at
#   all. Pointing the shortcut straight at the venv's pythonw.exe launches the
#   app with exactly one window, which is what you want.
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

$pythonw = Join-Path $RepoDir ".venv\Scripts\pythonw.exe"
$script  = Join-Path $RepoDir "gui.py"
$icon    = Join-Path $RepoDir "panopticon.ico"

if (-not (Test-Path $pythonw)) {
    Write-Host "No venv at $pythonw" -ForegroundColor Red
    Write-Host "Run 'uv sync' in $RepoDir first." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $script)) { Write-Host "Missing $script" -ForegroundColor Red; exit 1 }

$sh = New-Object -ComObject WScript.Shell
$sc = $sh.CreateShortcut($ShortcutPath)
$sc.TargetPath       = $pythonw
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
