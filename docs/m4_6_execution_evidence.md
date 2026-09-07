# M4.6 可执行性证据

状态：已发布，独立审计 `PASS_WITH_FINDINGS`。

## 结论

M4.6 已在 2020-01-02～2025-12-31 的既有暴露研究窗口上，为 13 个 RAW 单因子 score 和 1 个等权排名组合生成可执行性证据。证据接收通用日频 score 输入，并统一执行 T+1 开盘买入、T+6 收盘卖出、停牌、涨跌停、退市、费用、滑点、冲击和容量规则。

本阶段是执行诊断，不是新的 OOS，也不产生 Core Pool 晋级结论。

## 数据与身份

- M2-E 优先核心投影：`sha256:cf1e1c77bdc172b1d897a9ee13fc9689c77bcbb9b3c3d56fe3d1a2b8f936172d`；
- Execution Evidence：`sha256:cbffb3e717b0ff4faa9aeac962c486d504080b9fb3b40f712d5367b00cf5ee9c`；
- 输入 RAW Factor Release：`sha256:3e3d4e69428ce879ee9b53ffc6c39bc8b17b8d49780d305ecff8c0e96ee94fe7`；
- 证据含 61,110 条日度记录、42 条 score×资金档摘要、481 条拒单原因汇总和 20,370 条换手记录。

M2-E 的核心身份只哈希 `stk_limit`、`index_classify` 和 `index_member_all` 的已完成分区，因此以后恢复 ownership 回填不会改变本次核心数据 vintage。

## 冻结执行语义

- score 在 T 日收盘后形成，最早于 T+1 开盘买入；
- 持有至 T+6 收盘；如果证券在持有期内退市，使用可获得的退市日收盘价，否则显式记为 `DELISTING_RETURN_UNAVAILABLE`；
- 日线无法观察涨跌停队列，采用保守规则：开盘触及涨停不买，退出价触及跌停不卖；
- 买卖佣金各 3 bps，卖出印花税 5 bps，基础滑点 2 bps；
- 冲击采用平方根参与率代理，参与率上限 10%，滑点上限 100 bps；
- 容量固定评估 100 万、1,000 万和 1 亿元三档资金；
- 单因子取每日 score 最高 20%，组合取各因子日内排名的简单等权平均。

这些参数全部进入 Execution Evidence 内容身份。未来 LightGBM 预测只需适配为同一 score 表结构，即可复用相同规则。

## 结果摘要

| 资金规模 | 平均成交率 | 平均成本 | 平均日度净收益 |
|---:|---:|---:|---:|
| 100 万元 | 98.7835% | 15.1683 bps | 0.2305% |
| 1,000 万元 | 98.7085% | 15.5238 bps | 0.2274% |
| 1 亿元 | 98.0353% | 16.5566 bps | 0.2192% |

资金规模增加时，成交率单调下降、成本单调上升。审计另记录 4,216 个“资金场景×订单”的退市终止收益不可用结果；这些记录被显式拒绝，没有按零收益填充。

## 产物

- `daily_execution.parquet`：逐 score、交易日和资金档的成交率、毛收益、净收益、成本与退市计数；
- `entity_summary.parquet`：逐 score 和资金档汇总；
- `rejection_summary.parquet`：完整拒单原因分布；
- `turnover_summary.parquet`：逐 score 日度单边换手；
- `reports/m4_6_execution_audit.json`：独立哈希、行数、数学和容量单调性审计；
- Factor Evidence Explorer 新快照：13 条 RAW 路径展示 Execution，非 RAW 变体不借用该证据。

## 限制

日线模型无法还原盘口队列、盘中成交路径和真实券商回报；平方根冲击是冻结代理，不是实盘成交预测。当前窗口已经暴露，所有收益数字仅用于诊断执行约束对结果的影响。
