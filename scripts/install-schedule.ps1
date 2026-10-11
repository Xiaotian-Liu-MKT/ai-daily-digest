[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$taskName = "AIDailyDigest"

if (-not (Test-Path -LiteralPath $pythonw)) {
    throw "Missing $pythonw. Create the venv first: python -m venv .venv; .venv\Scripts\python -m pip install -e ."
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument '-m ai_daily_digest run' -WorkingDirectory $root

# The laptop is usually asleep at 08:00 and rarely rebooted, so the daily trigger alone
# is not enough: logon covers cold boots, StartWhenAvailable covers wake-ups after 08:00.
# The run itself is idempotent and backfills any missing day of the past week.
$triggerDaily = New-ScheduledTaskTrigger -Daily -At 08:00
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 120) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

# Interactive: the claude login, user PATH and vault are only available in the user session.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger @($triggerDaily, $triggerLogon) `
    -Settings $settings -Principal $principal `
    -Description "AI daily digest: summarise yesterday's Claude Code, Codex and agy sessions into the Obsidian vault, plus a daily investment-opportunity scan (investment-research + Codex)." `
    -Force | Out-Null

Write-Output "Registered scheduled task '$taskName' (daily 08:00 + at logon, catch-up enabled)."
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State | Format-List
