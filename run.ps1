[CmdletBinding()]
param(
    [string]$FrontendPath = 'E:\codex\Agent\quant_stock_strategy_system\frontEnd'
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$factorApiPort = 8771
$backendPort = 8773
$frontendPort = 8872

function Get-PortOwnerIds {
    param([int]$LocalPort)

    @(
        Get-NetTCPConnection -State Listen -LocalPort $LocalPort -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
}

function Stop-PortOwners {
    param([int]$LocalPort)

    $ownerIds = Get-PortOwnerIds -LocalPort $LocalPort
    foreach ($ownerId in $ownerIds) {
        if ($ownerId -eq $PID) {
            throw "Port $LocalPort belongs to this PowerShell process; refusing to stop itself."
        }
        if ($ownerId -le 4) {
            throw "Port $LocalPort is owned by a Windows system process (PID $ownerId); refusing to stop it."
        }

        $ownerProcess = Get-Process -Id $ownerId -ErrorAction SilentlyContinue
        if ($null -eq $ownerProcess) {
            continue
        }

        Write-Host "Stopping the existing service on port $LocalPort (PID $ownerId, $($ownerProcess.ProcessName))..." -ForegroundColor Yellow
        Stop-Process -Id $ownerId -Force
    }

    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-PortOwnerIds -LocalPort $LocalPort).Count -gt 0 -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 200
    }

    $remainingOwnerIds = Get-PortOwnerIds -LocalPort $LocalPort
    if ($remainingOwnerIds.Count -gt 0) {
        throw "Port $LocalPort was not released. Remaining PIDs: $($remainingOwnerIds -join ', ')"
    }
}

function Wait-ForHttp {
    param(
        [string]$Uri,
        [string]$ServiceName,
        [int]$TimeoutSeconds = 40
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $null = Invoke-WebRequest -Uri $Uri -TimeoutSec 2 -UseBasicParsing
            return
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }

    throw "$ServiceName did not become ready at $Uri within $TimeoutSeconds seconds. Check the latest logs under reports."
}

if (-not (Test-Path -LiteralPath $FrontendPath -PathType Container)) {
    throw "Frontend directory was not found: $FrontendPath. Pass its location with -FrontendPath."
}
if (-not (Test-Path -LiteralPath (Join-Path $FrontendPath 'package.json') -PathType Leaf)) {
    throw "No package.json was found in the frontend directory: $FrontendPath"
}

$pythonCommand = Get-Command python.exe -ErrorAction Stop
$npmCommand = Get-Command npm.cmd -ErrorAction Stop
$cmdCommand = Get-Command cmd.exe -ErrorAction Stop
$reportsPath = Join-Path $projectRoot 'reports'
New-Item -ItemType Directory -Path $reportsPath -Force | Out-Null
$serviceStartStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backendOutLog = Join-Path $reportsPath "strategy-backtest-api-$serviceStartStamp.stdout.log"
$backendErrLog = Join-Path $reportsPath "strategy-backtest-api-$serviceStartStamp.stderr.log"
$factorApiOutLog = Join-Path $reportsPath "m4-control-api-$serviceStartStamp.stdout.log"
$factorApiErrLog = Join-Path $reportsPath "m4-control-api-$serviceStartStamp.stderr.log"
$frontendOutLog = Join-Path $reportsPath "strategy-frontend-$serviceStartStamp.stdout.log"
$frontendErrLog = Join-Path $reportsPath "strategy-frontend-$serviceStartStamp.stderr.log"

Stop-PortOwners -LocalPort $factorApiPort
Stop-PortOwners -LocalPort $backendPort
Stop-PortOwners -LocalPort $frontendPort

$frontendModules = Join-Path $FrontendPath 'node_modules'
if (-not (Test-Path -LiteralPath $frontendModules -PathType Container)) {
    Write-Host 'Installing frontend dependencies...' -ForegroundColor Cyan
    Push-Location -LiteralPath $FrontendPath
    try {
        & $npmCommand.Source install --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) {
            throw "npm install failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

Write-Host 'Starting factor-library API...' -ForegroundColor Cyan
$factorApiProcess = Start-Process `
    -FilePath $pythonCommand.Source `
    -ArgumentList @(
        'scripts/serve_m4_control_api.py',
        '--host', '127.0.0.1',
        '--port', "$factorApiPort",
        '--allow-origin', "http://127.0.0.1:$frontendPort",
        '--allow-origin', "http://localhost:$frontendPort"
    ) `
    -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $factorApiOutLog `
    -RedirectStandardError $factorApiErrLog `
    -WindowStyle Hidden `
    -PassThru

Write-Host 'Starting strategy backtest API...' -ForegroundColor Cyan
$backendProcess = Start-Process `
    -FilePath $pythonCommand.Source `
    -ArgumentList @(
        'scripts/serve_strategy_backtest_api.py',
        '--host', '127.0.0.1',
        '--port', "$backendPort",
        '--allow-origin', "http://127.0.0.1:$frontendPort",
        '--allow-origin', "http://localhost:$frontendPort"
    ) `
    -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $backendOutLog `
    -RedirectStandardError $backendErrLog `
    -WindowStyle Hidden `
    -PassThru

Write-Host 'Starting strategy frontend...' -ForegroundColor Cyan
$frontendProcess = Start-Process `
    -FilePath $cmdCommand.Source `
    -ArgumentList @('/d', '/c', 'npm.cmd run dev') `
    -WorkingDirectory $FrontendPath `
    -RedirectStandardOutput $frontendOutLog `
    -RedirectStandardError $frontendErrLog `
    -WindowStyle Hidden `
    -PassThru

try {
    Wait-ForHttp -Uri "http://127.0.0.1:$factorApiPort/api/v1/health" -ServiceName 'Factor-library API'
    Wait-ForHttp -Uri "http://127.0.0.1:$backendPort/api/v1/health" -ServiceName 'Backtest API'
    Wait-ForHttp -Uri "http://127.0.0.1:$frontendPort/" -ServiceName 'Frontend'
}
catch {
    Write-Host "Factor-library API logs: $factorApiOutLog ; $factorApiErrLog" -ForegroundColor Yellow
    Write-Host "Backtest API logs: $backendOutLog ; $backendErrLog" -ForegroundColor Yellow
    Write-Host "Frontend logs: $frontendOutLog ; $frontendErrLog" -ForegroundColor Yellow
    throw
}

Write-Host ''
Write-Host 'Strategy backtest services are ready:' -ForegroundColor Green
Write-Host "  Factor library: http://127.0.0.1:$factorApiPort/api/v1/health"
Write-Host "  Frontend: http://127.0.0.1:$frontendPort/"
Write-Host "  API:      http://127.0.0.1:$backendPort/api/v1/health"
Write-Host "  Logs:     $reportsPath"
Write-Host "  Process IDs: factor library $($factorApiProcess.Id), backend $($backendProcess.Id), frontend launcher $($frontendProcess.Id)"
