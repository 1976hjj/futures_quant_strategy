$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$arguments = @(
    'scripts/serve_backfill_dashboard.py',
    '--archive', 'data/tushare_reference_archive',
    '--expected-partitions', '15000',
    '--min-free-gb', '30',
    '--port', '8766',
    '--process-script', 'backfill_tushare_reference.py',
    '--title', 'M2-B-reference-backfill-monitor'
)

$process = Start-Process `
    -FilePath 'python' `
    -ArgumentList $arguments `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -PassThru

Write-Host "M2-B dashboard started. PID: $($process.Id)"
Write-Host 'Open: http://127.0.0.1:8766'
