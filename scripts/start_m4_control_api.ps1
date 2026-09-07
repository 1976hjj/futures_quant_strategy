$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = "$(Join-Path $projectRoot 'src');$projectRoot"
Set-Location $projectRoot
python scripts/serve_m4_control_api.py --host 127.0.0.1 --port 8771 `
  --allow-origin http://127.0.0.1:8872 --allow-origin http://localhost:8872
