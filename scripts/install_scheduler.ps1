# Chinese Tech Wire - install hourly Windows Task Scheduler job
# Idempotent: re-running updates the same task.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/install_scheduler.ps1
#
# Optional:
#   -IntervalMinutes 60
#   -TaskName "ChineseTechWire"
#
# Uses schtasks.exe (not Register-ScheduledTask duration) to avoid
# HRESULT 0x80041318 / Duration:P99999999DT23H59M59S on Windows.

param(
    [string]$TaskName = "ChineseTechWire",
    [int]$IntervalMinutes = 60
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$VenvPythonAlt = Join-Path $ProjectRoot "venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $PythonExe = $VenvPython
} elseif (Test-Path $VenvPythonAlt) {
    $PythonExe = $VenvPythonAlt
} else {
    $PythonExe = (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
    if (-not $PythonExe) {
        $PythonExe = (Get-Command py -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
    }
    if (-not $PythonExe) {
        Write-Error "No Python interpreter found. Create .venv or ensure python is on PATH."
    }
}

$PythonExe = (Resolve-Path $PythonExe).Path
$MainPy = Join-Path $ProjectRoot "main.py"
if (-not (Test-Path $MainPy)) {
    Write-Error "main.py not found at $MainPy"
}

# schtasks /TR is a single command line string
$Tr = '"' + $PythonExe + '" "' + $MainPy + '" --full-once --scheduled'

Write-Host "Project root : $ProjectRoot"
Write-Host "Python       : $PythonExe"
Write-Host "Command      : $Tr"
Write-Host "Interval     : every $IntervalMinutes minute(s)"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Updating existing task '$TaskName'..."
    schtasks.exe /Delete /TN $TaskName /F | Out-Null
} else {
    Write-Host "Creating task '$TaskName'..."
}

$StartTime = (Get-Date).AddMinutes(2).ToString("HH:mm")

if ($IntervalMinutes -eq 60) {
    $sc = "HOURLY"
    $mo = @()
} elseif (($IntervalMinutes -ge 1) -and ($IntervalMinutes -le 1439)) {
    $sc = "MINUTE"
    $mo = @("/MO", "$IntervalMinutes")
} else {
    Write-Error "IntervalMinutes must be between 1 and 1439 (got $IntervalMinutes)"
}

Write-Host "Running schtasks /Create /SC $sc ..."
$allArgs = @("/Create", "/TN", $TaskName, "/TR", $Tr, "/SC", $sc) + $mo + @("/ST", $StartTime, "/RL", "LIMITED", "/F")
& schtasks.exe @allArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "schtasks /Create failed with exit code $LASTEXITCODE"
}

# Patch WorkingDirectory + no-overlap (does not touch the schedule / duration)
try {
    $argLine = '"' + $MainPy + '" --full-once --scheduled'
    $newAction = New-ScheduledTaskAction -Execute $PythonExe -Argument $argLine -WorkingDirectory $ProjectRoot
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Set-ScheduledTask -TaskName $TaskName -Action $newAction -Settings $settings | Out-Null
    Write-Host "Patched WorkingDirectory + MultipleInstances=IgnoreNew"
} catch {
    Write-Host "Warning: could not patch WorkingDirectory/MultipleInstances: $($_.Exception.Message)"
    Write-Host "Task schedule was still created."
}

Write-Host ""
Write-Host "Installed task '$TaskName'."
Write-Host "  Schedule         = every $IntervalMinutes minute(s), indefinite (schtasks)"
Write-Host "  WorkingDirectory = $ProjectRoot"
Write-Host "  First start time = $StartTime"
Write-Host ""
Write-Host "Status:  powershell -ExecutionPolicy Bypass -File scripts\scheduler_status.ps1"
Write-Host "Remove:  powershell -ExecutionPolicy Bypass -File scripts\remove_scheduler.ps1"
Write-Host "Manual:  $PythonExe `"$MainPy`" --full-once"
