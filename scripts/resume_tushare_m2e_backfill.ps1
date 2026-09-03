$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$sourceRoot = Join-Path $projectRoot 'src'
$credentialPath = Join-Path $projectRoot 'secrets\tushare.env'
if (-not (Test-Path -LiteralPath $credentialPath)) {
    throw 'Stored Tushare credential file is missing.'
}
$credentialLine = (Get-Content -LiteralPath $credentialPath -Raw).Trim()
if ($credentialLine -notmatch '^TUSHARE_TOKEN=([0-9a-f]{40,128})$') {
    throw 'Stored Tushare credential file has an invalid format.'
}
$env:TUSHARE_TOKEN = $Matches[1]
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
    $sourceRoot
} else {
    "$sourceRoot;$previousPythonPath"
}

try {
    $arguments = @(
        'scripts/backfill_tushare_m2e.py',
        '--start', '1990-12-31',
        '--end', '2026-09-01',
        '--endpoint', 'https://t.xiaodefa.top/',
        '--output', 'data/tushare_m2e_archive',
        '--sleep-ms', '100',
        '--min-free-gb', '30'
    )
    $archiveRoot = Join-Path $projectRoot 'data\tushare_m2e_archive'
    New-Item -ItemType Directory -Path $archiveRoot -Force | Out-Null
    $process = Start-Process `
        -FilePath 'python' `
        -ArgumentList $arguments `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $archiveRoot 'backfill.stdout.log') `
        -RedirectStandardError (Join-Path $archiveRoot 'backfill.stderr.log') `
        -PassThru
    Write-Host "M2-E backfill started. PID: $($process.Id)"
} finally {
    Remove-Item Env:TUSHARE_TOKEN -ErrorAction SilentlyContinue
    if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONPATH = $previousPythonPath
    }
}
