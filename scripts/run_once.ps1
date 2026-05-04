param(
    [string]$ConfigPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

if (-not $ConfigPath) {
    $ConfigPath = Join-Path $ProjectRoot "config\config.yaml"
}

$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    $PythonExe = "python"
}

$env:PYTHONPATH = Join-Path $ProjectRoot "src"
Set-Location $ProjectRoot

& $PythonExe -m hata_bot --config $ConfigPath run
exit $LASTEXITCODE

