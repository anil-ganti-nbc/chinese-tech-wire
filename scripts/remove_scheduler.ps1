# Remove Chinese Tech Wire scheduled task
#   powershell -ExecutionPolicy Bypass -File scripts/remove_scheduler.ps1

param(
    [string]$TaskName = "ChineseTechWire"
)

$ErrorActionPreference = "Stop"
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "Task '$TaskName' is not installed."
    exit 0
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed task '$TaskName'."
