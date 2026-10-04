<#
  Sets up OpenGrokBot's computer on a Windows VM so Bots, routines and background turns keep running when your laptop is closed.

  Run in an elevated PowerShell on the VM, from the project folder:
      powershell -ExecutionPolicy Bypass -File deploy\setup-remote-windows.ps1 -Port 8765 -OpenFirewall

  It: installs Python 3.11 if missing (winget), creates .venv, installs the headless service dependencies and Chromium,
  generates an access token, and registers a scheduled task that starts the service at logon and keeps it running.
  Secrets (provider keys, connector tokens) are stored in this VM's Windows Credential Manager when you enter them from the desktop app.
#>
param(
    [int]$Port = 8765,
    [switch]$OpenFirewall,
    [string]$BindAddress = "0.0.0.0"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Python 3.11..."
    winget install -e --id Python.Python.3.11 --accept-source-agreements --accept-package-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
}

if (-not (Test-Path ".venv")) { python -m venv .venv }
$py = Join-Path $root ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r requirements-service.txt
& $py -m playwright install chromium

# access token: stored in the Credential Manager by the service on first run; print it once for you to copy
$token = & $py -c "from service.runner import service_token; print(service_token())"
Write-Host ""
Write-Host "ACCESS TOKEN (paste into the desktop app: Settings > Computer > Remote):" -ForegroundColor Yellow
Write-Host $token -ForegroundColor Yellow
Write-Host ""

$action = New-ScheduledTaskAction -Execute $py -Argument "main.py --service --host $BindAddress --port $Port" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "OpenGrokBotService" -Action $action -Trigger $trigger -Settings $settings -Description "OpenGrokBot background service" -Force | Out-Null
Start-ScheduledTask -TaskName "OpenGrokBotService"

if ($OpenFirewall) {
    New-NetFirewallRule -DisplayName "OpenGrokBot service" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
    Write-Host "Firewall rule added for TCP $Port. Prefer a VPN (e.g. Tailscale) or an SSH tunnel over exposing this port to the internet."
}

Write-Host "Service started on port $Port. In the desktop app: Settings > Computer > 'Remote' > http://<this-vm>:$Port and the token above."
Write-Host "Enable auto-logon (or run the task as a service account) so the service starts after VM reboots."
