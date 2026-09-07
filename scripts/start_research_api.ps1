$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
Set-Location $projectRoot
python scripts/serve_research_api.py --host 127.0.0.1 --port 8770 --allow-origin http://127.0.0.1:8871
