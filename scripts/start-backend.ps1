$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
$Pip = Join-Path $Backend ".venv\Scripts\pip.exe"
$LocalTemp = Join-Path $Backend ".tmp"

Set-Location $Backend
New-Item -ItemType Directory -Force -Path $LocalTemp | Out-Null
$env:TEMP = $LocalTemp
$env:TMP = $LocalTemp

$VenvPath = Join-Path $Backend ".venv"
if ((Test-Path $Python) -and !(Test-Path $Pip)) {
    try {
        Remove-Item -LiteralPath $VenvPath -Recurse -Force
    } catch {
        Write-Host "Could not repair .venv automatically. Remove backend\.venv and rerun this script."
        throw
    }
}

if (!(Test-Path $Python)) {
    py -3 -m venv .venv
}

if (!(Test-Path $Pip)) {
    & $Python -m ensurepip --upgrade
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements.txt
& $Python -m app.main
