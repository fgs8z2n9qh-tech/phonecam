# Focal - start the standalone GUI app (no console window).
# On first run: venv + dependencies + certificate.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$venv = Join-Path $here ".venv"
$py = Join-Path $venv "Scripts\python.exe"
$pyw = Join-Path $venv "Scripts\pythonw.exe"

if (-not (Test-Path $py)) {
    Write-Host "Installing virtual environment + dependencies (this can take a few minutes)..." -ForegroundColor Cyan
    python -m venv $venv
    & $py -m pip install --upgrade pip
    & $py -m pip install -r (Join-Path $here "requirements.txt")
} else {
    # install any missing new packages (PySide6, etc.)
    & $py -m pip install -r (Join-Path $here "requirements.txt") -q
}

if (-not (Test-Path (Join-Path $here "server\cert.pem"))) {
    & $py (Join-Path $here "server\make_cert.py")
}

Write-Host "Starting Focal app..." -ForegroundColor Green
Start-Process -FilePath $pyw -ArgumentList (Join-Path $here "app.py") -WorkingDirectory $here
