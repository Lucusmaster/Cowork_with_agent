# install-shortcut.ps1
#
# Create a desktop shortcut that runs bin\agent-team-launcher.bat.
#
# ASCII-only on purpose: Windows PowerShell 5.1 parses .ps1 files as ANSI
# unless they carry a BOM, so non-ASCII text here fails with a parse error
# ("The string is missing the terminator"). Keep messages English; the
# Python side prints Chinese.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File bin\install-shortcut.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File bin\install-shortcut.ps1 -Force
#   # testing: write the shortcut somewhere else, leaving the real desktop alone
#   powershell -NoProfile -ExecutionPolicy Bypass -File bin\install-shortcut.ps1 -Desktop C:\Temp\dt
#
param(
    [string]$ProjectPath = "",
    [string]$Desktop = "",
    [string]$PythonW = "",
    [string]$Icon = "",
    [switch]$Force,
    [switch]$App
)

$ErrorActionPreference = 'Stop'

# Repo root: one level above this script (bin\..), so it works from anywhere
if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
    $ProjectPath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}
$ProjectPath = (Resolve-Path $ProjectPath).Path

$IsApp = [bool]$App
$Launcher = Join-Path $ProjectPath 'bin\agent-team-launcher.bat'
if (-not $IsApp) {
    if (-not (Test-Path $Launcher)) {
        Write-Host "[ERROR] launcher not found: $Launcher" -ForegroundColor Red
        exit 1
    }
} else {
    # App mode: no console window at all, so target pythonw.exe (python.exe flashes a
    # black box). The app window itself is opened by `launcher --app` (Edge/Chrome).
    if ([string]::IsNullOrWhiteSpace($PythonW) -or -not (Test-Path $PythonW)) {
        Write-Host "[ERROR] pythonw.exe not found - pass -PythonW <path>" -ForegroundColor Red
        exit 1
    }
}

if ([string]::IsNullOrWhiteSpace($Desktop)) {
    $Desktop = [Environment]::GetFolderPath('Desktop')
}
if (-not (Test-Path $Desktop)) {
    Write-Host "[ERROR] desktop directory not found: $Desktop" -ForegroundColor Red
    exit 1
}

$ShortcutName = if ($IsApp) { 'agent-team-app.lnk' } else { 'agent-team.lnk' }
$Shortcut = Join-Path $Desktop $ShortcutName
if ((Test-Path $Shortcut) -and (-not $Force)) {
    Write-Host "[INFO] shortcut already exists - pass -Force to overwrite: $Shortcut" -ForegroundColor Yellow
    exit 2
}

# Target the .bat directly: double-clicking then behaves exactly like running
# the script by hand, with no cmd.exe /c quoting layer to get wrong.
$Shell = New-Object -ComObject WScript.Shell
$Link = $Shell.CreateShortcut($Shortcut)
if ($IsApp) {
    $Link.TargetPath = $PythonW
    $Link.Arguments = '-m agent_team.launcher --app'
    $Link.Description = 'agent-team workbench (app window, no address bar)'
} else {
    $Link.TargetPath = $Launcher
    $Link.Description = 'agent-team workbench'
}
$Link.WorkingDirectory = $ProjectPath
# Use our own icon when it exists; fall back to the system one so the shortcut
# never renders as a blank white square.
if ([string]::IsNullOrWhiteSpace($Icon) -or -not (Test-Path $Icon)) {
    $Link.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
} else {
    $Link.IconLocation = (Resolve-Path $Icon).Path
}
$Link.WindowStyle = 1
$Link.Save()

if (-not (Test-Path $Shortcut)) {
    Write-Host "[ERROR] shortcut was not written: $Shortcut" -ForegroundColor Red
    exit 1
}
Write-Host "[OK] shortcut: $Shortcut" -ForegroundColor Green
if ($IsApp) {
    Write-Host "     target  : $PythonW -m agent_team.launcher --app"
    Write-Host "     double-click to start the workbench in its own window"
} else {
    Write-Host "     target  : $Launcher"
    Write-Host "     double-click to start the workbench (opens a one-time link)"
}
exit 0