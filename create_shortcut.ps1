# Create a desktop shortcut for the Focal app (starts with no console window).
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw = Join-Path $here ".venv\Scripts\pythonw.exe"
$appPy = Join-Path $here "app.py"
$icon = Join-Path $here "assets\icon.ico"
$runPs = Join-Path $here "run_app.ps1"

$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "Focal.lnk"

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
if (Test-Path $pyw) {
    # venv ready -> launch directly, no console window
    $sc.TargetPath = $pyw
    $sc.Arguments = "`"$appPy`""
} else {
    # no venv yet -> run_app.ps1 handles it (install + start)
    $sc.TargetPath = "powershell.exe"
    $sc.Arguments = "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runPs`""
}
$sc.WorkingDirectory = $here
if (Test-Path $icon) { $sc.IconLocation = $icon }
$sc.Description = "Focal - phone as a wireless webcam"
$sc.Save()
Write-Host "Shortcut created:" -ForegroundColor Green
Write-Host "  $lnk"
