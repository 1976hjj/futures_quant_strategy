$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$arguments = @(
    'scripts/serve_backfill_dashboard.py',
    '--archive', 'data/tushare_m2e_archive',
    '--expected-partitions', '44666',
    '--min-free-gb', '30',
    '--port', '8769',
    '--process-script', 'backfill_tushare_m2e.py',
    '--title', 'M2-E-core-and-ownership-backfill-monitor'
)
$process = Start-Process `
    -FilePath 'python' `
    -ArgumentList $arguments `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -PassThru
Write-Host "M2-E dashboard started. PID: $($process.Id)"
Write-Host 'Open: http://127.0.0.1:8769'
