$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Frontend = Join-Path $Root "frontend"

Set-Location $Frontend

if (!(Test-Path "node_modules")) {
    npm.cmd install
}

npm.cmd run dev
