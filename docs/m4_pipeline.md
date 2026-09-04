# M4 配置化研究流水线

状态：可用；M4.1～M4.5 与 M4.7 已接入，M4.6 将沿用同一编排契约。

## 1. 结论

M4 的生产入口是 `scripts/run_m4_pipeline.py`。因子集合来自不可变 RAW factor release，脚本不维护因子白名单；新增因子后先由 Factor Factory 物化一个新的 RAW release，再用一份 JSON 配置驱动预处理、基础证据、稳健统计、Walk-Forward、去重/聚类/增量证据和独立审计。

因子特例不得写进统计代码。预先声明方向、M4.4 后设反向诊断和重点证伪均放在配置的 `redundancy.direction_overrides` 中，并进入内容寻址身份。配置变化会产生不同的流水线 `config_id`；各阶段请求不变时复用已经校验过哈希的不可变资产。

当前历史配置的 `bind_configuration_to_asset_identity=false` 仅用于复用配置绑定功能出现之前发布的 M4.5 资产。新批次必须保留默认 `true`，使方向覆盖和候选策略的哈希进入资产 family 身份。

## 2. 日常命令

先做只读预检，不物化新证据：

```powershell
python scripts\run_m4_pipeline.py `
  --config config\m4_pipeline_current.json `
  --validate-only
```

执行配置中声明的全部阶段：

```powershell
python scripts\run_m4_pipeline.py `
  --config config\m4_pipeline_current.json
```

当前配置的机器报告写入 `reports/m4_pipeline_current.json`。报告包括配置快照、`config_id`、预检规模、警告、每个阶段的开始/结束时间、缓存命中、资产 ID、审计状态和失败 traceback。报告采用临时文件加原子替换；中途失败也保留已完成阶段。

## 3. 阶段

流水线按固定依赖顺序执行，配置中只需列出要运行的阶段：

1. `processed`：从 RAW release 发布所选 processed variants。
2. `basic_evidence`：为 RAW 和 processed releases 发布 M4.1 label 与基础证据。
3. `audit_basic_evidence`：逐个独立复算 M4.1 抽样截面。
4. `robustness`：把基础证据作为一个明确的 BH-FDR family 发布 M4.3。
5. `audit_robustness`：独立复算 HAC、循环块 bootstrap 和 BH-FDR。
6. `walk_forward`：按配置 folds、窗口和 family 发布 M4.4。
7. `redundancy`：从 M4.4 血缘自动读取全部因子×变体，发布 M4.5。
8. `audit_walk_forward`、`audit_redundancy`：独立验证对应资产。
9. `factor_explorer`：从已发布的 M4.1～M4.5 资产生成内容寻址的只读静态报告。
10. `audit_factor_explorer`：独立校验页面文件哈希、来源行数、canonical/duplicate 映射和缺失阶段占位。

依赖可以由同一次运行的上游输出满足，也可以显式固定已有 `source_*_id`。后者适合从中间资产恢复、只跑下游或只审计。

## 4. 新增一批因子

新增几百个因子时，研究人员不应修改 M4 脚本：

1. 在 Factor Registry 注册 FactorSpec/实现并发布一个新的 RAW release。
2. 复制 `config/m4_pipeline_current.json`，替换 `batch_id`、`raw_factor_release_id`、报告路径和研究窗口/folds。
3. 如需明确的 processed 变体，填写 `processed_variants` 并启用 `processed`；如已有 releases，则填写 `processed_factor_release_ids`。
4. 只把真正预先声明或明确标注为后设诊断的方向放入 `direction_overrides`。
5. 先执行 `--validate-only`，确认实体数、两两相关数和依赖，再执行完整命令。

`m4_pipeline_current.json` 为复现已发布资产而显式固定旧的 Walk-Forward engine `1.0.0`。新批次应删除该字段以采用当前默认 `duckdb-python-walk-forward-1.1.0`，或显式写入 `1.1.0`；该版本用 release 的去重 Universe keys 构造市场状态，不依赖任何特定因子存在。

M4.5 的两两相关在数学上是 O(N²)：`N` 是因子×变体路径数，不是原始因子数。流水线会预估 pair 数，并在超过 300 条路径时给出容量警告。`CLUSTER_REPRESENTATIVES_AND_FOCUS` 只缩减昂贵的条件 RankIC 候选集，不会偷删去重所需的完整 pair 证据。大批量运行因此已经不需要逐因子改代码或逐项对话，但仍应根据预检结果安排内存、临时磁盘和运行时间。

## 5. 不变性和研究边界

- 每个正式阶段仍发布内容寻址的 Manifest/Parquet，并注册 DuckDB 视图；流水线报告不是证据资产的替代品。
- 缓存命中前会校验请求和文件哈希，不会覆盖旧资产。
- 审计样本从资产内容中确定性选择，不再写死某个因子或日期。
- 方向支持、方向证伪和未拒绝零假设继续分开记录；代表因子不会自动晋级。
- 当前 2025 区间已经暴露。重复运行或换编排入口不会把它恢复成未见 Holdout。
- M4.6 的成交约束尚未完成，因此 Explorer 的 Execution 面板保持 `NOT_AVAILABLE`。Explorer 只展示研究证据，不产生 Core Pool、模型特征永久淘汰或模型晋级结论。

## 6. 当前兼容性验证

`config/m4_pipeline_current.json` 已针对现有 13 factors × 3 variants 运行。它缓存命中既有 M4.4、M4.5 和 Explorer 报告，M4.4、M4.5、M4.7 三项独立审计均为 `PASS_WITH_FINDINGS`，证明编排重构没有重新生成或改写大型资产。Explorer 报告 ID 为 `sha256:e86eab258a0e905f0e5e4296fe8ba943ef4dc8790a4898a4d1e514cb0ab9b17f`。

流水线完成后，可启动 React 前端查看最新报告：

```powershell
cd frontEnd
npm.cmd run dev
```

前端 `predev`/`prebuild` 会运行 `scripts/sync-factor-data.mjs`，只读同步 `reports/factor_explorer/latest.json` 指向的展示快照。前端不连接 DuckDB，也不是新的证据生产阶段。
