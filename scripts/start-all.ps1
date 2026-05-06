$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$BackendScript = Join-Path $Root "scripts\start-backend.ps1"
$FrontendScript = Join-Path $Root "scripts\start-frontend.ps1"

Start-Process powershell.exe -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$BackendScript`""
Start-Process powershell.exe -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$FrontendScript`""

Write-Host "POLYORACLE backend:  http://localhost:8000"
Write-Host "POLYORACLE frontend: http://localhost:3000"
