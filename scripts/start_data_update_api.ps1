param([int]$Port = 8774)

$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskLogs = Join-Path $taskRoot 'artifacts\local_services'
New-Item -ItemType Directory -Path $taskLogs -Force | Out-Null
$taskListeners = @(netstat -ano -p tcp | Where-Object {
    $_ -match ('127\.0\.0\.1:' + $Port + '\s+.*LISTENING')
})
if ($taskListeners.Count -eq 0) {
    $taskPython = (Get-Command python.exe).Source
    $taskProcess = Start-Process -FilePath $taskPython `
        -ArgumentList @('scripts/serve_data_update_api.py', '--port', "$Port") `
        -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $taskLogs "$Port.stdout.log") `
        -RedirectStandardError (Join-Path $taskLogs "$Port.stderr.log")
    Write-Output "Started data-management API on $Port, PID $($taskProcess.Id)."
}
for ($taskAttempt = 0; $taskAttempt -lt 20; $taskAttempt++) {
    try {
        $taskHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/health" -TimeoutSec 2
        if ($taskHealth.service -ne 'data-management' -or $taskHealth.status -ne 'ok') {
            throw "Port $Port is occupied by a different service."
        }
        Write-Output 'Data-management API is ready.'
        exit 0
    } catch {
        if ($taskListeners.Count -gt 0) { throw }
        Start-Sleep -Milliseconds 250
    }
}
throw "Data-management API failed to start; inspect $taskLogs."
