$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$sourceRoot = Join-Path $projectRoot 'src'
$previousPythonPath = $env:PYTHONPATH
$secureToken = Read-Host 'Enter Tushare token (input is hidden)' -AsSecureString
$tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)

try {
    $env:TUSHARE_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
    $env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
        $sourceRoot
    } else {
        "$sourceRoot;$previousPythonPath"
    }
    if ([string]::IsNullOrWhiteSpace($env:TUSHARE_TOKEN)) {
        throw 'Token must not be empty.'
    }

    $arguments = @(
        'scripts/backfill_tushare_daily.py',
        '--start', '1990-12-19',
        '--end', '2026-09-01',
        '--endpoint', 'https://t.xiaodefa.top/',
        '--output', 'data/tushare_archive',
        '--workers', '1',
        '--sleep-ms', '100',
        '--min-free-gb', '30'
    )
    $process = Start-Process `
        -FilePath 'python' `
        -ArgumentList $arguments `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -PassThru
    Write-Host "Backfill resumed in a hidden process. PID: $($process.Id)"
    Write-Host 'The token was not written to disk or included in the process command line.'
} finally {
    Remove-Item Env:TUSHARE_TOKEN -ErrorAction SilentlyContinue
    if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONPATH = $previousPythonPath
    }
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
}

Read-Host 'Press Enter to close this window'
