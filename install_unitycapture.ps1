# Install/register Unity Capture (standalone DirectShow virtual webcam).
# NO OBS, NO compiling. Needs admin rights (regsvr32 -> HKLM). The script restarts
# itself elevated if needed. If the package is already downloaded, it will NOT download again.
$ErrorActionPreference = "Stop"

$admin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $admin) {
    Write-Host "Elevated rights required - restarting as administrator (UAC)..." -ForegroundColor Yellow
    Start-Process powershell -Verb RunAs -ArgumentList "-ExecutionPolicy Bypass -File `"$PSCommandPath`""
    return
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dest = Join-Path $here "UnityCapture"

# Look for an already-downloaded Install folder; if none, download it
$installDir = $null
if (Test-Path $dest) {
    $installDir = Get-ChildItem $dest -Recurse -Directory -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -eq "Install" } | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $installDir) {
    $zip = Join-Path $env:TEMP "UnityCapture.zip"
    $url = "https://github.com/schellingb/UnityCapture/archive/refs/heads/master.zip"
    Write-Host "Downloading Unity Capture..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $dest -Force
    $installDir = Get-ChildItem $dest -Recurse -Directory |
                  Where-Object { $_.Name -eq "Install" } | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $installDir) { throw "Could not find the Install folder." }
Write-Host "Install folder: $installDir" -ForegroundColor DarkGray

# The correct DLL names: UnityCaptureFilter64.dll / UnityCaptureFilter32.dll
$dll64 = Join-Path $installDir "UnityCaptureFilter64.dll"
$dll32 = Join-Path $installDir "UnityCaptureFilter32.dll"

# Matching-bitness regsvr32: 64-bit DLL -> System32, 32-bit DLL -> SysWOW64
$reg64 = Join-Path $env:windir "System32\regsvr32.exe"
$reg32 = Join-Path $env:windir "SysWOW64\regsvr32.exe"

Write-Host "Registering (regsvr32)..." -ForegroundColor Cyan
if (Test-Path $dll64) {
    & $reg64 /s $dll64
    if ($LASTEXITCODE -eq 0) { Write-Host "  64-bit filter registered ✓" -ForegroundColor Green }
    else { Write-Host "  64-bit registration FAILED (exit $LASTEXITCODE)" -ForegroundColor Red }
} else { Write-Host "  missing $dll64" -ForegroundColor Red }
if (Test-Path $dll32) {
    & $reg32 /s $dll32
    if ($LASTEXITCODE -eq 0) { Write-Host "  32-bit filter registered ✓" -ForegroundColor Green }
    else { Write-Host "  32-bit registration FAILED (exit $LASTEXITCODE)" -ForegroundColor Red }
} else { Write-Host "  missing $dll32" -ForegroundColor Red }

# Check whether the camera now appears among the DirectShow devices
$cat = "HKLM:\SOFTWARE\Classes\CLSID\{860BB310-5D01-11d0-BD3B-00A0C911CE86}\Instance"
$ok = $false
if (Test-Path $cat) {
    Get-ChildItem $cat | ForEach-Object {
        $fn = (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).FriendlyName
        if ($fn -like "*Unity*") { $script:ok = $true; Write-Host "`nCAMERA REGISTERED: $fn" -ForegroundColor Green }
    }
}
if (-not $ok) { Write-Host "`nWARNING: the Unity camera did not appear in the list." -ForegroundColor Yellow }
Write-Host "`nIMPORTANT: do NOT move/delete the UnityCapture folder - the registration points to this file." -ForegroundColor DarkYellow
Read-Host "`nPress Enter to close"
