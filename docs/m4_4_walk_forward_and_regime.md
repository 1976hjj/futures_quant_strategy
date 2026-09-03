# M4.4 长窗口 Walk-Forward 与 Regime 证据

状态：已完成，独立审计 `PASS_WITH_FINDINGS`  
结论：`NO_PROMOTION_DIAGNOSTIC_AND_PSEUDO_OOS`

## 1. 本阶段解决什么问题

M4.3 只有 2024 年一季度的短窗口，统计功效不足。M4.4 把同一批 13 个因子及三个变体扩展到 2020-01-02～2025-12-31，并将“拟合方向”和“测试结论”按时间隔离。它回答的是：已有候选的横截面预测方向能否在后续年度重复出现，以及效果是否依赖简单市场状态。

这仍不是可交易策略回测。当前标签只处理行情与停牌，尚未完整处理涨跌停无法成交、退市收益和交易成本。

## 2. 冻结输入与不可变资产

| 资产 | 内容寻址 ID | 规模 |
|---|---|---:|
| 长窗口 RAW 因子 | `sha256:3e3d4e69428ce879ee9b53ffc6c39bc8b17b8d49780d305ecff8c0e96ee94fe7` | 88,859,342 行 |
| `WINSORIZED_ZSCORE` | `sha256:b18a177256ad433e20cf97d543a5e68d324c6c3c4d9859be8981a41ee8761009` | 88,859,342 行 |
| `SIZE_NEUTRALIZED` | `sha256:0e3b87d5c2fbdae6b46569d633e2a370add03eae27c49d4bee8a31266fc6a91a` | 88,859,342 行 |
| 5-session Label | `sha256:27c18a35d88ad93c9aff307f5dfac204fcb05d622e7ae992b450e02ed7f9227f` | 6,835,334 行 |
| Walk-Forward 证据 | `sha256:a32e6aa8bdfa962280b7cac5fdedfe0be4dd98b620a0295eec65b2956999a95e` | 56,745 日度 IC；117 个 fold 假设；468 个 regime 统计 |

长区间因子发布使用按年有界物化和最终流式合并，避免一次性把全量中间结果装入内存。分区暂存支持安全恢复；最终发布仍是一个经过哈希校验的不可变 Parquet 资产。

## 3. Walk-Forward 契约

| Fold | Train | Validation | Test | 测试性质 |
|---|---|---|---|---|
| WF-2023 | 2020-01-02～2021-12-24 | 2022-01-11～2022-12-23 | 2023-01-10～2023-12-29 | 事后诊断 |
| WF-2024 | 2020-01-02～2022-12-23 | 2023-01-10～2023-12-22 | 2024-01-09～2024-12-31 | 事后诊断 |
| WF-2025 | 2020-01-02～2023-12-22 | 2024-01-09～2024-12-24 | 2025-01-09～2025-12-31 | 首次研究读取，现已暴露 |

每个边界使用 5 个交易日 purge 和 5 个交易日 embargo。因子方向来自预先声明；没有声明时只允许使用训练期平均 RankIC 的符号，测试期绝不参与方向选择。

WF-2025 在读取任何结果前写入 `metadata.holdout_exposure_ledger`。读取发生后，该区间已永久失去“未见 Holdout”资格。它比普通回看更接近 OOS，但不是实时前瞻部署结果，因此准确名称是 pseudo-OOS。

## 4. 统计口径

- 主指标：逐日横截面 Spearman RankIC；
- 推断：Newey–West/Bartlett，最大滞后 5；
- 重采样：循环 moving-block bootstrap，块长 5，10,000 次，固定随机种子；
- 多重检验：3 folds × 3 variants × 13 factors = 117 个假设作为同一个 BH-FDR 家族，失败结果不允许删除；
- Regime：只使用当时可见的等权市场日收益，60 日趋势方向与 20 日波动；高低波动阈值只由该 fold 的训练期中位数确定；
- 每个 Regime 至少 20 个测试日才允许解释，样本不足显式标记。

FDR 拒绝零假设只代表“显著不为零”。必须再看有向测试 IC 的符号：正值是预定方向得到支持，负值是预定方向被证伪。

## 5. 主要结果

| Fold | HAC 支持 | HAC 反向 | Bootstrap 支持 | Bootstrap 反向 |
|---|---:|---:|---:|---:|
| WF-2023 | 18 | 3 | 18 | 3 |
| WF-2024 | 12 | 3 | 13 | 3 |
| WF-2025 | 18 | 3 | 20 | 3 |

以上数量是“因子变体假设”，不是独立 Alpha 数量。RAW 和 `WINSORIZED_ZSCORE` 在秩统计上接近重复，不能把它们分别计为两个发现。

目前最值得进入下一轮证伪的现象是：

- `volume-shock-20` 的三个变体在三个测试 fold 中都得到方向支持；
- `return-volatility-20` 的 `SIZE_NEUTRALIZED` 变体在三个测试 fold 中都得到方向支持；
- `price-momentum-20` 的三个变体在三个测试 fold 中都显著反向，说明当前“正向动量”假设被持续证伪，不能包装成有效发现。

这些只是候选证据。变体相关、因子簇重复、交易约束和成本尚未消除，不能据此晋级。

## 6. 数据库入口

- `raw.factor_walk_forward_daily`：逐日、逐 fold、逐因子变体的 RankIC；
- `research.factor_walk_forward_summary`：117 个冻结假设的完整统计量；
- `research.factor_walk_forward_decisions`：在摘要上增加 HAC/Bootstrap 的 `DIRECTION_SUPPORTED`、`DIRECTION_CONTRADICTED` 或 `NOT_REJECTED` 判定；
- `raw.factor_regime_statistics`：468 条 PIT Regime 条件统计；
- `research.walk_forward_family_summary`：统一多重检验家族摘要；
- `metadata.walk_forward_evidence_manifest`：证据资产血缘、哈希、限制与结论；
- `metadata.holdout_exposure_ledger`：不可逆样本暴露记录。

## 7. 当前边界与下一步

M4.4 不晋级因子，原因不是“结果不好”，而是尚缺四个关键门禁：

1. M2-E 发布后的涨跌停、退市与历史状态约束标签；
2. 真实费率、冲击成本、换手和容量；
3. 因子相关聚类、重复信息和条件增量价值；
4. 新的未暴露时间窗口或持续 paper research 结果。

下一步应做 M4.5 因子冗余、聚类与增量价值。它可以使用已经暴露的 2020～2025 研究样本，但所有模型选择必须明确记为研究内选择，不能再把 2025 包装为 Holdout。

