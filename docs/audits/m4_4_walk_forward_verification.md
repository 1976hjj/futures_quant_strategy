# M4.4 Walk-Forward 独立验收

验收状态：`PASS_WITH_FINDINGS`  
审计报告：`reports/m4_4_walk_forward_audit.json`

## 验收对象

- Walk-Forward release：`sha256:a32e6aa8bdfa962280b7cac5fdedfe0be4dd98b620a0295eec65b2956999a95e`；
- 56,745 条日度 RankIC；
- 117 条 fold×factor×variant 假设；
- 468 条 Regime 统计；
- 三个不可变因子输入和一个不可变 Label 输入。

## 独立检查

审计脚本没有调用生产发布器的统计实现。它独立完成并核对：

1. Manifest、输入因子 Manifest 与所有 Parquet 的内容哈希；
2. 固定日期 `2025-06-03` 的横截面 RankIC；
3. HAC 标准误、循环块 Bootstrap 和 BH q-value；
4. 方向只能来自声明或 Train，不能来自 Validation/Test；
5. Fold 边界、purge、embargo 和 session 数；
6. Regime 分组与总样本数对账；
7. Holdout 暴露账本记录；
8. 支持方向与显著反向的分类。

审计没有发现结构性失败。缓存重放验证返回 `cache_hit: true`，相同输入不会重新生成或覆盖资产。

## 必须保留的发现

- `price-momentum-20` 的 RAW、`WINSORIZED_ZSCORE`、`SIZE_NEUTRALIZED` 在三个测试 fold 中均显著违背预定正向；
- RAW 与 `WINSORIZED_ZSCORE` 的秩结果近似重复，拒绝数量不是独立发现数；
- WF-2025 已被读取并写入暴露账本，不得再次作为未见 Holdout；
- 当前标签没有完整纳入涨跌停、退市收益和交易成本；
- 因子在冗余、增量价值、成本和执行门禁前均不得晋级。

因此审计通过代表“资产、时间语义与统计计算可复现”，不代表“已经发现 57 或 60 个 Alpha”，也不代表策略可交易。
