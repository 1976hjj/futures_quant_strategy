# Alpha Research OS Frontend

React + TypeScript + Vite 实现的 Factor Evidence Explorer。页面只读取 M4 流水线生成的展示快照，不连接或修改 DuckDB。

## 启动

推荐直接运行启动脚本。它会终止占用指定端口的旧进程、等待端口释放、安装缺失依赖并启动 Vite：

```powershell
cd D:\futures_quant_strategy\frontEnd
.\run.ps1
```

默认端口为 5173，也可以指定其他端口：

```powershell
.\run.ps1 -Port 5180
```

手工启动方式：

```powershell
cd D:\futures_quant_strategy\frontEnd
npm.cmd install
npm.cmd run dev
```

访问 `http://127.0.0.1:5173`。

`predev` 和 `prebuild` 会自动读取 `../reports/factor_explorer/latest.json`，把对应的 `evidence-summary.json` 同步到本地 public 目录。先运行研究流水线即可刷新数据：

```powershell
cd D:\futures_quant_strategy
python scripts\run_m4_pipeline.py --config config\m4_pipeline_current.json
```

## 构建

```powershell
cd D:\futures_quant_strategy\frontEnd
npm.cmd run build
npm.cmd run preview
```
