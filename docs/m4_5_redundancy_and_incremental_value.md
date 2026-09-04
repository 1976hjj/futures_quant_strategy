# M4.5 因子去重、聚类与增量价值

状态：已发布，独立审计 `PASS_WITH_FINDINGS`  
结论：`NO_PROMOTION_REDUNDANCY_DIAGNOSTIC`

## 1. 研究边界

M4.5 只读取 M4.4 已冻结的三个长窗口因子发布、同一标签发布和日度 RankIC 资产。2020-01-02～2025-12-31 已经是暴露研究样本，资产请求将其固定标记为 `EXPOSED_RESEARCH_SAMPLE_NOT_OOS`。本阶段没有创建新的 Holdout 事件，也没有把 2025 重新命名为未见样本。

本阶段解决“不同统计显著是否只是同一信息”的问题，不解决涨跌停成交、退市收益、费用、冲击成本或容量。任何簇代表、显著偏相关或增量 R² 都不得自动触发因子晋级。

## 2. 冻结方法

- 因子值相关：每个自然月最后一个可用交易日，逐日横截面 Spearman 相关，共 72 个抽样日；
- IC 相关：2020～2025 全窗口日度 RankIC 序列的 Pearson 相关；
- 变体去重：仅在同一因子版本内，平均因子值秩相关和日度 IC 相关同时不低于 `0.995` 才折叠；
- 聚类距离：`1 - max(abs(因子值相关), abs(IC 相关))`；平均连接，切割阈值 `0.35`；
- 簇代表：先取簇内平均距离最小的 medoid，再按覆盖率、固定变体优先级和 ID 破平；不使用收益均值选代表；
- 条件统计：候选与标签先在各自完整当日 Universe 上转秩，再在候选与控制集共同非空证券上计算；控制集为其他簇的机械代表；
- `conditional_rank_ic` 为候选残差与原标签秩的半偏相关，`orthogonal_rank_ic` 为候选和标签同时控制后的偏相关；
- 推断：Newey–West/Bartlett HAC(5)、循环 moving-block bootstrap（块长 5、10,000 次）和覆盖全部 25 个 canonical 路径的统一 BH-FDR。

## 3. 不可变资产

- Redundancy ID：`sha256:442cf98e81eb98eea2b41714ed1d9d5a6fb449b905e6805766f7f8f3c93a0626`；
- 物理目录：`data/evidence_store/redundancy/442cf98e81eb98eea2b41714ed1d9d5a6fb449b905e6805766f7f8f3c93a0626/`；
- 39 条原始因子×变体路径，741 个无序路径对；
- 53,352 条月末逐日因子值相关（72 × 741）；
- 25 条 canonical 路径、10 个层次聚类簇；
- 36,375 条日度条件统计（25 × 1,455）；
- 25 条增量价值假设和 1 条统一检验家族摘要。

数据库入口见 `docs/warehouse.md`。Manifest 同时绑定 M4.4 Manifest 哈希、三个因子 Parquet 哈希、标签 Manifest、统计规格和运行前 Holdout 暴露账本快照。

## 4. 去重与聚类结果

13 个因子的 RAW 与 `WINSORIZED_ZSCORE` 都通过双阈值去重门禁；其中最低的平均因子值秩相关为 `0.999979`，最低日度 IC 相关为 `0.999998`。因此两类路径不能分别计算为两个 Alpha。

`book-to-price` 的 `SIZE_NEUTRALIZED` 也与 RAW 同时达到去重阈值：平均因子值秩相关 `0.996610`，日度 IC 相关 `0.995715`。39 条路径最终折叠为 25 条 canonical 路径。

平均连接在冻结阈值下形成 10 个信息簇。每个簇只有一个 `REPRESENTATIVE_NOT_PROMOTED`，代表项只用于后续控制集和查询；簇内的其他成员仍保留完整证据，不从历史中删除。

## 5. 重点证伪结果

| 重点路径 | canonical 变体数 | 平均原始 RankIC | 平均正交 RankIC | 平均增量 R² | 严格方向结果 |
|---|---:|---:|---:|---:|---|
| `volume-shock-20` | 2 | -0.0214～-0.0221 | -0.0118～-0.0119 | 0.00319～0.00320 | `INCREMENTAL_DIRECTION_SUPPORTED` |
| `return-volatility-20` | 2 | -0.0676～-0.0693 | -0.0612～-0.0628 | 0.01948～0.01999 | `INCREMENTAL_DIRECTION_SUPPORTED` |
| 反向 `price-momentum-20` | 2 | -0.0426～-0.0436 | -0.0296～-0.0307 | 0.01111～0.01137 | `INCREMENTAL_DIRECTION_SUPPORTED` |

六条重点路径的 HAC BH q 值均不高于 `1.74e-05`，bootstrap BH q 值均为 `0.0002083`。但三类结果的证据含义不同：

- `return-volatility-20` 的负方向来自原 FactorSpec，可视为对既有方向假设的暴露样本诊断；
- `volume-shock-20` 的负方向来自 M4.4 已观察结果，是选择后的延伸诊断；
- 反向 `price-momentum-20` 是 M4.4 证伪原正向动量后定义的后设方向，只支持“原动量方向继续被证伪”，不能称为新的确认性反转 Alpha。

25 个假设中 HAC 拒绝 17 个、bootstrap 拒绝 18 个；要求两者都拒绝且方向一致后，12 个为方向支持、1 个为方向证伪。这个计数是相关路径的诊断数量，不是独立 Alpha 数量。

## 6. 独立审计

审计脚本 `scripts/audit_m4_5_redundancy.py` 不调用生产聚类或偏相关函数，独立检查：

1. 所有上游和输出哈希、Manifest 身份与 DuckDB 注册；
2. 暴露账本快照未变化，且 M4.5 没有创建新 Holdout 记录；
3. 路径对、去重组、canonical 数、树的 `n-1` 合并、簇代表唯一性和数值边界；
4. 固定截面的 RAW/WINSORIZED 因子值 Spearman 相关；
5. 全日度 IC 序列相关；
6. 固定日期、固定候选与控制集的半偏/偏 RankIC 和增量 R²。

审计报告位于 `reports/m4_5_redundancy_audit.json`，结论为 `PASS_WITH_FINDINGS`。通过表示计算和血缘可复现，不表示因子已经可交易或可晋级。

## 7. 下一步

M4.6 等待 M2-E 正式发布后，重新构造纳入涨跌停、退市状态/收益、成本、换手和容量约束的可执行证据。M4.7 建立 Factor Evidence Card、只读 Evidence Explorer、跨实现复算和多通道路由；单因子弱、方向证伪或线性冗余不会因此永久失去模型特征资格。M4.5 不向 Core Pool 写入任何成员。
