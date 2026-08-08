# Show Chinese Tech Wire scheduled task status
#   powershell -ExecutionPolicy Bypass -File scripts/scheduler_status.ps1

param(
    [string]$TaskName = "ChineseTechWire"
)

$ErrorActionPreference = "Continue"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "Scheduler: DISABLED (task '$TaskName' not found)"
    exit 0
}

$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Scheduler: ACTIVE"
Write-Host "  TaskName        : $($task.TaskName)"
Write-Host "  State           : $($task.State)"
Write-Host "  LastRunTime     : $($info.LastRunTime)"
Write-Host "  LastTaskResult  : $($info.LastTaskResult)"
Write-Host "  NextRunTime     : $($info.NextRunTime)"
Write-Host "  NumberOfMissed  : $($info.NumberOfMissedRuns)"

$action = $task.Actions | Select-Object -First 1
if ($action) {
    Write-Host "  Execute         : $($action.Execute)"
    Write-Host "  Arguments       : $($action.Arguments)"
    Write-Host "  WorkingDirectory: $($action.WorkingDirectory)"
}

$settings = $task.Settings
if ($settings) {
    Write-Host "  MultipleInstances: $($settings.MultipleInstances)"
}
