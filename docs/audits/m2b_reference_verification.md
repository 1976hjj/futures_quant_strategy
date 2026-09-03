# M2-B 历史状态与 Universe 验收记录

验收时间：2026-09-03  
结论：`PASSED_WITH_WARNINGS`  
机器可读报告：`reports/m2b_reference_audit.json`

## 1. 发布身份

- DuckDB：`data/warehouse/alpha_research.duckdb`
- checkpoint SHA-256：`74b69a1cd8672d0815a062d5aa82d6253e411188e0cccd8dafee2e0a3e93aea6`
- 覆盖期：1990-12-19 至 2026-09-01
- 开市日：8,716

## 2. 原始发布行数

| 数据域 | 行数 | PIT 等级 |
|---|---:|---|
| trading_calendar | 13,041 | `RECONSTRUCTED_PIT` |
| security_master | 5,897 | `CURRENT_SNAPSHOT_WITH_EVENT_DATES` |
| name_history | 8,414 | `RECONSTRUCTED_PIT` |
| stock_st | 721,738 | `RECONSTRUCTED_PIT` |
| suspensions | 641,798 | `RECONSTRUCTED_PIT` |

`namechange` 的无日期请求恰好返回 9,000 行，具有截断嫌疑。发布器不使用该响应，而是使用 1990–2026 年逐年区间快照的 8,414 行；两类原始响应均保留。

## 3. 独立门禁结果

- 所有 checkpoint 、payload 哈希与发布行数一致；
- 交易日历 `pretrade_date` 错链 0 条；
- A 股 5,894 只，缺失上市日 0 只；
- 保留历史退市 A 股 338 只，退市日后状态 0 条；
- `security_session_state` 18,716,685 行，业务键重复 0 条；
- 停牌状态 568,623 行；
- 默认 Universe 16,598,544 行；
- Universe 中的 ST、停牌、未知 ST、无合格行情记录均为 0 条；
- 名称字段中 Unicode 替换字符 0 条。

2026-09-01 的默认 Universe 为 5,307 只。`000001.SZ` 样本在该日为非 ST、非停牌、有合格行情且可入池。

## 4. 已知警告

- 2000 年以前有 835,661 个证券交易日缺少可证明的 ST 状态。系统将其保留为 `NULL` 并排除出 Universe，不推断为非 ST。
- 15,733,904 行无当日可用的历史名称，因此使用主表当前名称回退；`name_is_point_in_time=false` 显式暴露此限制。名称仅用于显示和辅助 ST 证据，未被伪装为原生 PIT 数据。

这两项警告都没有破坏 Universe 的保守门禁，因此不阻断 M2-B 关闭。

## 5. 结论与后续

M2-B 通过功能、血缘、PIT 和保守 Universe 门禁，可作为后续因子计算的股票池底座。M2 剩余两个数据主任务是 M2-C 公司行动/复权对账与 M2-D 财务 PIT/修订版本。
