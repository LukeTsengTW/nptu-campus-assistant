[CmdletBinding()]
param(
    [string]$TaskName = "NPTU Campus Assistant Backend",
    [string]$StartupScript,
    [switch]$OutputDefinition
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $StartupScript) {
    $StartupScript = Join-Path $PSScriptRoot "start-nptu-assistant.ps1"
}

$resolvedStartupScript = (Resolve-Path -LiteralPath $StartupScript).Path
$projectDirectory = Split-Path -Parent (Split-Path -Parent $resolvedStartupScript)
$powerShellExecutable = (Get-Command powershell.exe -ErrorAction Stop).Source
$taskArguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$resolvedStartupScript`""

$definition = [ordered]@{
    TaskName = $TaskName
    Execute = $powerShellExecutable
    Arguments = $taskArguments
    WorkingDirectory = $projectDirectory
    Trigger = "AtLogOn"
    RestartCount = 3
    RestartIntervalMinutes = 1
}

if ($OutputDefinition) {
    $definition | ConvertTo-Json -Compress
    exit 0
}

$action = New-ScheduledTaskAction `
    -Execute $powerShellExecutable `
    -Argument $taskArguments `
    -WorkingDirectory $projectDirectory
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "登入 Windows 後啟動 NPTU 校務資訊助理本機後端。" `
    -Force | Out-Null

Write-Output "已建立排程工作：$TaskName"
