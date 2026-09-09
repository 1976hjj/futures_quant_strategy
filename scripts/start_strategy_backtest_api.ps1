$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

python scripts/serve_strategy_backtest_api.py --host 127.0.0.1 --port 8773 `
  --allow-origin http://127.0.0.1:8872 --allow-origin http://localhost:8872
