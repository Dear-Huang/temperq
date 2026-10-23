param(
    [string]$Python = "",
    [string]$At = "18:00",
    [string]$TaskName = "QuantWorkbench-DailyPaper"
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $projectRoot ".venv\Scripts\python.exe" }
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument "-X utf8 -m qwb.cli paper tick --all" -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "Task already exists: $TaskName. Choose another TaskName or edit it in Task Scheduler."
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Daily Quant Workbench paper-account ledger; requires updated daily data and calendar."
Write-Output "Registered $TaskName at $At. Runs while this user is logged in; missed runs start when available."
