param(
    [string]$TaskName = "HataBot Telegram Listener"
)

$ErrorActionPreference = "Stop"

$Runner = Join-Path $PSScriptRoot "run_bot_listener.ps1"
if (-not (Test-Path $Runner)) {
    throw "Runner script not found: $Runner"
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Scheduled task '$TaskName' installed. The Telegram control bot will start at logon."
