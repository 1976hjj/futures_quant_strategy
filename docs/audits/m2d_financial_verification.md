# M2-D 财务 PIT 验收记录

验收时间：2026-09-03。

## 结论

M2-D 下载、发布和只读审计全部通过，可以作为后续财务因子与 M2-E 披露链数据的基础。

- 原始归档：`data/tushare_financial_archive`；
- DuckDB：`data/warehouse/alpha_research.duckdb`；
- checkpoint SHA-256：`sha256:af6f87979c48d70b7c8c34a2f2629715d4550f7b44e5d5720810af13679529ce`；
- 总行数：1,858,751；
- 自动化测试：83 passed；
- 审计报告：`reports/m2d_financial_audit.json`。

## 发布行数

| 数据集 | 行数 |
|---|---:|
| 利润表版本 | 427,550 |
| 资产负债表版本 | 483,266 |
| 现金流量表版本 | 403,255 |
| 财务指标版本 | 544,680 |

## PIT 与修订处理

`research.financial_versions_canonical` 保留全部原始版本；`research.financial_revision_events` 对同一证券、报告期和报表口径建立修订序号；`research.financial_pit_asof` 提供 `valid_from/valid_to` 区间。

审计隔离了 5 条时间异常：2 条缺少可用日期、3 条可用日期早于报告期结束日。异常保留在 `research.financial_pit_exceptions`，没有修改供应商原始记录。

## 门禁

- 四个接口的所有 143 个季末均存在终止分页；
- checkpoint、快照、压缩 payload 和解压 payload 哈希一致；
- 发布行数与 checkpoint 一致；
- 四个原始视图合计行数与 canonical 视图一致；
- PIT 有效区间不存在反向区间；
- 历史修订没有被 `update_flag` 覆盖。
