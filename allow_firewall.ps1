# PhoneCam - open the Windows firewall for ports 8080 (cert) and 8443 (camera).
# This is the only way the phone can reach the PC on the LAN. Needs admin rights -> the script restarts itself.
$ErrorActionPreference = "Stop"

$admin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $admin) {
    Write-Host "Elevated rights required - restarting as administrator (UAC)..." -ForegroundColor Yellow
    Start-Process powershell -Verb RunAs -ArgumentList "-ExecutionPolicy Bypass -File `"$PSCommandPath`""
    return
}

$name = "PhoneCam (8080,8443)"
# delete the old rule if it existed (idempotent)
Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule

New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort 8080,8443 -Profile Private,Domain | Out-Null

Write-Host "`nDone - ports 8080 and 8443 allowed (Private/Domain profile)." -ForegroundColor Green
Get-NetFirewallRule -DisplayName $name | Select-Object DisplayName,Enabled,Direction,Action,Profile | Format-Table -AutoSize
Read-Host "Press Enter to close"
