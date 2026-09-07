# Research read API

本地只读研究接口监听 `127.0.0.1:8770`，与前端 `127.0.0.1:8871` 分离运行。

启动：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_research_api.ps1
```

接口：

- `GET /api/v1/health`：服务健康状态。
- `GET /api/v1/quality/overview`：每次请求重新读取最新发布的质量审计产物，返回检查项、真实计数、覆盖区间、审计批次、低覆盖因子和源文件版本。

接口只读取 `reports/` 及财务归档的 `run_status.json`，不打开或修改 DuckDB，不触发数据、因子、模型或回测任务。响应设置 `Cache-Control: no-store`。默认只允许 `http://127.0.0.1:8871` 跨域读取，可通过 `--allow-origin` 显式修改。

修改审计文件后无需重启接口。刷新前端质量页会发起新请求，并在响应头和页面中显示新的 `generated_at`。若刷新失败，页面保留上次成功响应并显示错误和旧响应时间。
