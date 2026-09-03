# M2-C 公司行动与复权对账验收记录

验收时间：2026-09-03  
结论：`PASSED_WITH_WARNINGS`  
机器可读报告：`reports/m2c_corporate_action_audit.json`

## 1. 发布身份

- 原始分区：5,894 个 A 股证券分区；
- 原始方案/公告：321,942 行；
- checkpoint SHA-256：`a5af65dd3894c0cf5e1c7929bf24caa0f6adeccba09e028fab95c2fffdb2fec6`；
- PIT 等级：`RECONSTRUCTED_PIT`。

所有 snapshot manifest、压缩 payload、解压后 payload、行数和分区证券代码均已验证。

## 2. 对账结果

| 类别 | 行数 | 处理 |
|---|---:|---|
| 分红事件与因子跳变同日匹配 | 57,330 | 继续执行知情时间和数值门禁 |
| 供应商微小因子漂移、行情无除权变化 | 24,914 | 显式标记 `TECHNICAL_FACTOR_DRIFT` |
| 缺少对应行情上下文 | 3,208 | 隔离 |
| 行情确有调整但 dividend 无事件 | 1,234 | 可能是配股/换股等，隔离 |
| 因子与行情参考比不一致 | 124 | 隔离 |
| 其他未解释因子变化 | 14 | 隔离 |
| dividend 有实施事件但因子未跳变 | 507 | 隔离 |

同日匹配样本中，因子/行情比值绝对误差中位数为 `3.84e-05`，95% 分位数为 `4.63e-04`；事件理论参考价与行情 `pre_close` 误差中位数接近 0，95% 分位数为 0.005 元。

## 3. 白名单门禁

`approved_for_dividend_adjustment=true` 必须同时满足：

- 事件与复权因子在同一生效日匹配；
- 实施信息最晚在前一交易日已可知；
- 因子跳变与行情除权参考比误差不超过 0.005；
- 事件理论参考价与 `pre_close` 误差不超过 0.05 元或价格的 0.5%。

最终 55,469 个事件通过；1,861 个同日匹配事件未通过全部门禁。正式研究只允许读取 `research.corporate_action_reconciliation_approved`，其余进入 `research.corporate_action_reconciliation_exceptions`。

## 4. 已知警告

- 9 个实施记录的可知日晚于除权日，未通过白名单门禁；
- 52 个除权日有多个不同经济方案候选，保留全部候选，事件视图使用最新实施候选；
- `dividend` 不是完整的配股、合并换股和代码迁移事件库，未解释调整不会被伪造成分红事件。

上述警告均有明确隔离路径，不会流入批准视图，因此不阻断 M2-C 关闭。
