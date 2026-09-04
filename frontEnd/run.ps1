[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 5173
)

$ErrorActionPreference = 'Stop'

function Get-PortOwnerIds {
    param([int]$LocalPort)

    @(Get-NetTCPConnection -State Listen -LocalPort $LocalPort -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Stop-PortOwners {
    param([int]$LocalPort)

    $ownerIds = Get-PortOwnerIds -LocalPort $LocalPort
    foreach ($ownerId in $ownerIds) {
        if ($ownerId -eq $PID) {
            throw "Port $LocalPort belongs to the current PowerShell process; refusing to stop itself."
        }

        $ownerProcess = Get-Process -Id $ownerId -ErrorAction SilentlyContinue
        if ($null -eq $ownerProcess) {
            continue
        }

        Write-Host "Port $LocalPort is occupied by $($ownerProcess.ProcessName) (PID $ownerId). Stopping it..." -ForegroundColor Yellow
        Stop-Process -Id $ownerId -Force
    }

    if ($ownerIds.Count -gt 0) {
        $deadline = (Get-Date).AddSeconds(10)
        while ((Get-PortOwnerIds -LocalPort $LocalPort).Count -gt 0 -and (Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 200
        }
    }

    $remainingOwnerIds = Get-PortOwnerIds -LocalPort $LocalPort
    if ($remainingOwnerIds.Count -gt 0) {
        throw "Port $LocalPort was not released. Remaining PIDs: $($remainingOwnerIds -join ', ')"
    }
}

Stop-PortOwners -LocalPort $Port
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'node_modules'))) {
    Write-Host 'Installing frontend dependencies for the first run...' -ForegroundColor Cyan
    & npm.cmd install --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) {
        throw "npm install failed with exit code $LASTEXITCODE"
    }
}

Write-Host "Starting Alpha Research OS frontend at http://127.0.0.1:$Port/" -ForegroundColor Green
& npm.cmd run dev -- --port $Port
exit $LASTEXITCODE
