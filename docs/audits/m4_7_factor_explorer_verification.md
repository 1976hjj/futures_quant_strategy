# M4.7 Factor Evidence Explorer 验收

状态：`PASS_WITH_FINDINGS`

验收对象：

- Report ID：`sha256:e86eab258a0e905f0e5e4296fe8ba943ef4dc8790a4898a4d1e514cb0ab9b17f`
- Walk-Forward：`sha256:a32e6aa8bdfa962280b7cac5fdedfe0be4dd98b620a0295eec65b2956999a95e`
- Redundancy：`sha256:442cf98e81eb98eea2b41714ed1d9d5a6fb449b905e6805766f7f8f3c93a0626`
- Robustness：`sha256:a8368caf70682a1918fb3f2c7380e510b2b62a169db1e142a603d0d601337eaa`

## 验收结论

Explorer 已作为只读、离线、内容寻址的派生报告接入 M4 配置流水线。它没有数据库写入口、外部网络依赖或 secrets 读取路径，也不改变因子生命周期。报告 Manifest 的状态为 `READ_ONLY_DERIVED_REPORT_NOT_EVIDENCE`，页面不能代替正式 Evidence Asset 或晋级 Gate。

独立审计复核结果：

| 检查 | 结果 |
|---|---:|
| factor×variant 路径 | 39 |
| Walk-Forward fold 行 | 117 |
| Regime 行 | 468 |
| canonical 增量证据 | 25 |
| 两两相关关系 | 741 |
| duplicate 路径 | 14 |
| 文件/Manifest 哈希失败 | 0 |
| 来源行数不一致 | 0 |

14 条 duplicate 路径的本路径增量字段为 `NULL`，但可追溯到其 canonical 路径，避免把同一增量证据重复包装为独立 Alpha。所有路径仍保留 `MODEL_FEATURE_ELIGIBLE` 研究路由；方向证伪、短窗不显著或冗余均未触发永久全局淘汰。M4.6 与 M6 尚未发布，Execution 和 Model Contribution 均明确为 `NOT_AVAILABLE`。

## 复现命令

单独构建并审计：

```powershell
python scripts\build_factor_explorer.py --config config\factor_explorer_current.json
python scripts\audit_factor_explorer.py `
  --report-directory reports\factor_explorer\e86eab258a0e905f0e5e4296fe8ba943ef4dc8790a4898a4d1e514cb0ab9b17f
```

或执行含 M4.4/M4.5 缓存校验、独立审计与 Explorer 的整条流水线：

```powershell
python scripts\run_m4_pipeline.py --config config\m4_pipeline_current.json
```

本次整条流水线状态为 `PASS`；M4.4 与 M4.5 均命中已有不可变缓存，没有重算或覆盖大型资产。

## Findings

- 当前 2020-01-02～2025-12-31 区间已经暴露；页面明确显示其为研究诊断样本，不是新的 OOS。
- 静态解析、JavaScript 语法、来源行数和文件哈希检查已经完成。
- 后续 React 版本已使用本机 headless Chrome 在 1600×1000 视口完成首页真实渲染抽查，并验证页面与数据端点均返回 HTTP 200。多尺寸响应式布局与全部交互控件仍建议在正式使用时进行人工抽查；这不影响数据与 Manifest 完整性结论。
