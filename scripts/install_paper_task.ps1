param(
    [string]$Python = "",
    [string]$At = "17:30",
    [string]$TaskName = "QuantWorkbench-DailyPaper",
    [string]$Account = "lgbm_forward_20261023",
    [switch]$Replace
)
$ErrorActionPreference = "Stop"
if ($Account -notmatch '^[A-Za-z0-9_-]{1,40}$') { throw "Invalid account name" }
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $projectRoot ".venv\Scripts\python.exe" }
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$runner = Join-Path $projectRoot "scripts\run_daily_paper.py"
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument ('-X utf8 "' + $runner + '" --account "' + $Account + '"') -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 60)
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    if (-not $Replace) { throw "Task already exists: $TaskName. Use -Replace to back it up and update it." }
    $backupRoot = Join-Path $projectRoot "artifacts\scheduler-backups"
    [IO.Directory]::CreateDirectory($backupRoot) | Out-Null
    $backup = Join-Path $backupRoot ($TaskName + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.xml')
    [IO.File]::WriteAllText($backup, (Export-ScheduledTask -TaskName $TaskName))
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force:$Replace -Description "Update full-market daily data, require the completed session, then record the frozen paper account."
Write-Output "Registered $TaskName at $At. Runs while this user is logged in; missed runs start when available."
