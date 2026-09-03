$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$arguments = @(
    'scripts/serve_backfill_dashboard.py',
    '--archive', 'data/tushare_corporate_action_archive',
    '--expected-partitions', '5894',
    '--min-free-gb', '30',
    '--port', '8767',
    '--process-script', 'backfill_tushare_corporate_actions.py',
    '--title', 'M2-C-corporate-action-backfill-monitor'
)
$process = Start-Process `
    -FilePath 'python' `
    -ArgumentList $arguments `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -PassThru
Write-Host "M2-C dashboard started. PID: $($process.Id)"
Write-Host 'Open: http://127.0.0.1:8767'
