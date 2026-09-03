param(
    [switch]$TokenFromClipboard,
    [switch]$UseStoredToken
)

$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$sourceRoot = Join-Path $projectRoot 'src'
$previousPythonPath = $env:PYTHONPATH
$tokenPointer = [IntPtr]::Zero

try {
    $credentialPath = 'HKCU:\Software\AlphaResearchOS'
    $credentialFile = Join-Path $projectRoot 'secrets\tushare.env'
    $storedCredential = Get-ItemPropertyValue `
        -Path $credentialPath `
        -Name 'TushareTokenDpapi' `
        -ErrorAction SilentlyContinue

    if ($UseStoredToken -or (
        -not $TokenFromClipboard -and (
            -not [string]::IsNullOrWhiteSpace($storedCredential) -or (Test-Path -LiteralPath $credentialFile)
        )
    )) {
        if (-not [string]::IsNullOrWhiteSpace($storedCredential)) {
            $secureToken = ConvertTo-SecureString $storedCredential
            $tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
            $env:TUSHARE_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
        } elseif (Test-Path -LiteralPath $credentialFile) {
            $credentialLine = (Get-Content -LiteralPath $credentialFile -Raw).Trim()
            if ($credentialLine -notmatch '^TUSHARE_TOKEN=([0-9a-f]{40,128})$') {
                throw 'Stored Tushare credential file has an invalid format.'
            }
            $env:TUSHARE_TOKEN = $Matches[1]
            $credentialLine = $null
        } else {
            throw 'No stored Tushare credential is available.'
        }
    } elseif ($TokenFromClipboard) {
        $env:TUSHARE_TOKEN = (Get-Clipboard -Raw).Trim()
    } else {
        $secureToken = Read-Host 'Enter Tushare token for M2-B (input is hidden)' -AsSecureString
        $tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
        $env:TUSHARE_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
    }
    $env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
        $sourceRoot
    } else {
        "$sourceRoot;$previousPythonPath"
    }
    if ([string]::IsNullOrWhiteSpace($env:TUSHARE_TOKEN)) {
        throw 'Token must not be empty.'
    }

    $arguments = @(
        'scripts/backfill_tushare_reference.py',
        '--start', '1990-12-19',
        '--end', '2026-09-01',
        '--endpoint', 'https://t.xiaodefa.top/',
        '--output', 'data/tushare_reference_archive',
        '--sleep-ms', '100',
        '--min-free-gb', '30'
    )
    $archiveRoot = Join-Path $projectRoot 'data\tushare_reference_archive'
    New-Item -ItemType Directory -Path $archiveRoot -Force | Out-Null
    $process = Start-Process `
        -FilePath 'python' `
        -ArgumentList $arguments `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $archiveRoot 'backfill.stdout.log') `
        -RedirectStandardError (Join-Path $archiveRoot 'backfill.stderr.log') `
        -PassThru
    Write-Host "M2-B backfill started in a hidden process. PID: $($process.Id)"
    Write-Host 'The token was not included in the child process command line or logs.'
} finally {
    Remove-Item Env:TUSHARE_TOKEN -ErrorAction SilentlyContinue
    if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONPATH = $previousPythonPath
    }
    if ($tokenPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
    }
}

if (-not $TokenFromClipboard -and -not $UseStoredToken) {
    Read-Host 'Press Enter to close this window'
}
