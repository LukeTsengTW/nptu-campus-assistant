[CmdletBinding()]
param(
    [string]$ProjectDirectory,
    [string]$DockerCommand = "docker",
    [string]$HealthUrl = "http://127.0.0.1:8000/health",
    [ValidateRange(1, 3600)]
    [int]$DockerWaitTimeoutSeconds = 180,
    [ValidateRange(1, 3600)]
    [int]$HealthWaitTimeoutSeconds = 180,
    [ValidateRange(1, 60000)]
    [int]$PollIntervalMilliseconds = 2000,
    [string]$LogPath = (Join-Path $env:LOCALAPPDATA "NptuCampusAssistant\startup.log")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $ProjectDirectory) {
    $ProjectDirectory = Split-Path -Parent $PSScriptRoot
}

function Write-StartupLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    $logDirectory = Split-Path -Parent $LogPath
    if ($logDirectory) {
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    }
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $LogPath -Value "[$timestamp] $Message" -Encoding UTF8
}

function Wait-Until {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Condition,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][string]$TimeoutMessage
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (& $Condition) {
            return
        }
        Start-Sleep -Milliseconds $PollIntervalMilliseconds
    } while ((Get-Date) -lt $deadline)

    throw $TimeoutMessage
}

try {
    $resolvedProjectDirectory = (Resolve-Path -LiteralPath $ProjectDirectory).Path
    Write-StartupLog "等待 Docker Desktop 引擎。"
    Wait-Until -TimeoutSeconds $DockerWaitTimeoutSeconds -TimeoutMessage "Docker Desktop 引擎未在期限內就緒。" -Condition {
        try {
            $dockerInfoOutput = & $DockerCommand info --format "{{.ServerVersion}}" 2>$null
            $dockerExitCode = $LASTEXITCODE
            $dockerInfoOutput | Out-Null
            return $dockerExitCode -eq 0
        }
        catch {
            return $false
        }
    }

    Write-StartupLog "Docker Desktop 已就緒；啟動 Compose 服務。"
    $composeOutput = & $DockerCommand compose --project-directory $resolvedProjectDirectory up -d 2>&1
    $composeExitCode = $LASTEXITCODE
    foreach ($line in $composeOutput) {
        Write-StartupLog "Docker: $line"
    }
    if ($composeExitCode -ne 0) {
        throw "docker compose up -d 失敗，結束碼：$composeExitCode。"
    }

    Write-StartupLog "等待 API health check。"
    Wait-Until -TimeoutSeconds $HealthWaitTimeoutSeconds -TimeoutMessage "API 未在期限內就緒：$HealthUrl" -Condition {
        try {
            $response = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5
            return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
        }
        catch {
            return $false
        }
    }

    Write-StartupLog "API 已就緒：$HealthUrl"
    exit 0
}
catch {
    Write-StartupLog "啟動失敗：$($_.Exception.Message)"
    Write-Error $_
    exit 1
}
