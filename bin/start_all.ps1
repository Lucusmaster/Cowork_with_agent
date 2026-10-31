# ASCII-only wrapper (no CJK) so PowerShell 5.1 never mis-decodes it.
# Usage: powershell -ExecutionPolicy Bypass -File bin\start_all.ps1 [--stop|--status]
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "bin\start_all.py"

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    & $python.Source $script @args
} else {
    & py -3 $script @args
}