$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$secureToken = Read-Host 'Enter Tushare token for permission probe (input is hidden)' -AsSecureString
$tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)

try {
    $env:TUSHARE_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
    if ([string]::IsNullOrWhiteSpace($env:TUSHARE_TOKEN)) {
        throw 'Token must not be empty.'
    }
    & python 'scripts/probe_tushare_capabilities.py' `
        '--endpoint' 'https://t.xiaodefa.top/' `
        '--output' 'reports/tushare_capability_probe.json'
    if ($LASTEXITCODE -ne 0) {
        throw "Capability probe exited with code $LASTEXITCODE"
    }
} finally {
    Remove-Item Env:TUSHARE_TOKEN -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
}

Read-Host 'Probe finished. Press Enter to close this window'
