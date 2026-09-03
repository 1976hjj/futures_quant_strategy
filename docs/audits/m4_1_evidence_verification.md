# M4.1 Label 与基础证据独立验收

验收结论：PASS。

## 验收对象

- Label Release：`sha256:778b6ba94398514dd123180cf69c2d446a3628c4394ff19ad3ddbe485e05bcae`；
- Evidence Bundle：`sha256:4c2bc48115894c0618149004c84bc1c820b1f1ec7c799b1f07160c370ef6faf3`；
- 标签约束：`BAR_AND_SUSPENSION_ONLY`；
- 数据窗口：2024-01-02 至 2024-03-29 的信号，退出数据延伸至 T+6。

## 机器语义验收

| 检查 | 结果 |
|---|---:|
| T+1 开盘入场、T+6 收盘退出 | PASS |
| 复权收益手工样例 | PASS |
| 缺失入场日不顺延 | PASS |
| 停牌、买入受限、卖出受限、未知限制分类 | PASS |
| 正向因子 Pearson IC / RankIC = 1 | PASS |
| 反向控制 RankIC = -1 | PASS |
| 正反抵消控制平均 RankIC = 0 | PASS |
| Label 可用时间不晚于因子时阻断 | PASS |
| 缺失因子仍保留在覆盖率分母 | PASS |

## 正式资产验收

| 检查 | 结果 |
|---|---:|
| Label / Evidence 文件与 Manifest 哈希 | PASS |
| DuckDB 登记与 Manifest | PASS |
| Label 与因子信号键完全一致 | PASS |
| Label 重复键 / 非有限值 | 0 / 0 |
| 有效标签固定 session 对齐错误 | 0 |
| 有效标签复权收益对账错误 | 0 |
| 标签时间钟错误 | 0 |
| 日度证据 / 分组收益 / 因子摘要 | 754 / 3,770 / 13 |
| Python 与 DuckDB IC/RankIC/分组交叉验证 | PASS |
| 重复请求命中不可变缓存 | PASS |

机器可读证据：`reports/m4_1_evidence_audit.json`。

## 审计边界

本验收只证明标签和描述性统计的工程、时间与数学语义。以下项目未完成，因此任何因子都不得晋级：涨跌停、退市收益、交易成本、HAC/Bootstrap、多重检验、稳定性、Walk-Forward 和 OOS。当前真实结果不得用于选择窗口、方向或因子。
