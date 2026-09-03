$ErrorActionPreference = 'Stop'

$resumeScript = Join-Path $PSScriptRoot 'resume_tushare_reference_backfill.ps1'
$deadline = [DateTime]::UtcNow.AddMinutes(30)

while ([DateTime]::UtcNow -lt $deadline) {
    $candidate = ([string](Get-Clipboard -Raw -ErrorAction SilentlyContinue)).Trim()
    if ($candidate.Length -ge 40 -and $candidate.Length -le 128 -and $candidate -match '^[0-9a-f]+$') {
        $candidate = $null
        & $resumeScript -TokenFromClipboard
        exit $LASTEXITCODE
    }
    $candidate = $null
    Start-Sleep -Seconds 2
}

exit 2
