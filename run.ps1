# PhoneCam - setup + start
# Usage:  right-click > Run with PowerShell,  or:  powershell -ExecutionPolicy Bypass -File run.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$venv = Join-Path $here ".venv"
$py = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "[1/3] Creating virtual environment..." -ForegroundColor Cyan
    python -m venv $venv
    & $py -m pip install --upgrade pip
    Write-Host "[2/3] Installing dependencies (this can take a minute)..." -ForegroundColor Cyan
    & $py -m pip install -r (Join-Path $here "requirements.txt")
} else {
    Write-Host "[1/3] Venv ready." -ForegroundColor Green
}

$cert = Join-Path $here "server\cert.pem"
if (-not (Test-Path $cert)) {
    Write-Host "[3/3] Generating HTTPS certificate..." -ForegroundColor Cyan
    & $py (Join-Path $here "server\make_cert.py")
} else {
    Write-Host "[3/3] Certificate ready." -ForegroundColor Green
}

Write-Host "`nStarting server...`n" -ForegroundColor Green
& $py (Join-Path $here "server\server.py")
