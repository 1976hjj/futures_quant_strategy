$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$arguments = @(
    'scripts/serve_backfill_dashboard.py',
    '--archive', 'data/tushare_financial_archive',
    '--expected-partitions', '572',
    '--min-free-gb', '30',
    '--port', '8768',
    '--process-script', 'backfill_tushare_financials.py',
    '--title', 'M2-D-financial-PIT-backfill-monitor'
)
$process = Start-Process `
    -FilePath 'python' `
    -ArgumentList $arguments `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -PassThru
Write-Host "M2-D dashboard started. PID: $($process.Id)"
Write-Host 'Open: http://127.0.0.1:8768'
