# M2-B 历史证券状态与可交易 Universe

状态：真实数据回填、仓库发布与独立审计均已完成；审计结论为 `PASSED_WITH_WARNINGS`。

发布快照：2026-09-03；数据覆盖至 2026-09-01。审计证据见
`reports/m2b_reference_audit.json` 和 `docs/audits/m2b_reference_verification.md`。

## 1. 目标

M2-B 回答的是“在历史上的某个交易日，这只证券当时是什么状态，是否有资格进入研究股票池”，而不是提前规定某个策略必须选哪些股票。

数据链路：

```text
Tushare-compatible API
-> 不可变 RawSnapshot
-> 带哈希的断点 checkpoint
-> 类型化 Parquet
-> DuckDB raw/research/metadata
-> security_session_state
-> universe_daily
```

## 2. 真实数据域

- `trade_cal`：上交所基准交易日历，覆盖自然日和开闭市状态；
- `stock_basic`：分别保存 L/D/P/G/UN 五种上市状态，避免只保留当前上市公司；
- `namechange`：历史简称、生效区间和公告日期；
- `stock_st`：2000-01-01 以来逐交易日 ST 列表；
- `suspend_d`：逐交易日停复牌事件。

`stock_basic` 是当前抓取时点的证券主表快照。系统只使用其上市/退市事件日期重建区间，不允许在退市前利用未来退市信息排除证券。该数据域标记为 `CURRENT_SNAPSHOT_WITH_EVENT_DATES`，不冒充原生 PIT。

## 3. 三态 ST 规则

`is_st` 不是简单的真假两值：

- `true`：当日 `stock_st` 明确列出，或当日历史名称带 ST 风险标识；
- `false`：历史名称明确不是 ST，或 2000 年以后完整的当日 `stock_st` 列表中不存在；
- `NULL`：2000 年以前又缺少有效历史名称，状态无法证明。

未知状态默认不能进入 `universe_daily`。系统不会为了增加回测样本而把未知强行填成非 ST。

## 4. 数据库对象

Raw 层：

- `raw.trading_calendar`
- `raw.security_master`
- `raw.name_history`
- `raw.stock_st`
- `raw.suspensions`

Research 层：

- `research.trading_calendar`
- `research.security_master`
- `research.security_name_history`
- `research.security_session_state`
- `research.universe_daily`

`research.security_session_state` 的业务键是 `(trade_date, ts_code)`，主要字段包括：

- `listed_session_number`：上市后的第几个开市日；
- `security_name`、`name_is_point_in_time`；
- `is_st`、`st_source`；
- `is_suspended`、`suspend_events`；
- `has_market_bar`、`is_tradeable_bar`；
- `eligible_for_signal`。

当前默认 `eligible_for_signal` 要求：ST 状态明确为否、当日未停牌、存在通过行情质量检查的交易记录。涨跌停后的订单是否成交属于执行模型，不在此处伪装成 Universe 规则。

## 5. 回填与监控

先启动监控页：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts\start_reference_dashboard.ps1
```

页面地址：`http://127.0.0.1:8766`

再启动安全回填器：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts\resume_tushare_reference_backfill.ps1
```

启动脚本会从本机忽略版本控制的 `secrets/tushare.env` 读取 Token。Token 不写入命令行参数、checkpoint、日志、RawSnapshot 或数据库，也不会进入 Git。

回填默认单并发。任务先获取最新交易日的 `stock_st` 和 `suspend_d` 做权限预检；之后按历史日期顺序执行。HTTP 429、网关 5xx、超时、不完整响应和可重试 JSON 解码错误使用有上限的指数退避并持续重试。

本次归档共完成 15,222 个分区。`namechange` 的无日期全量请求恰好返回 9,000 行，存在截断嫌疑，因此没有直接用于发布；系统另按年度请求 37 个区间，并以这 8,414 行可核验区间快照构建名称历史。原始全量响应仍保留作审计证据。

## 6. 发布与审计

数据回填完成后执行：

```powershell
python scripts\build_reference_warehouse.py

python scripts\audit_reference_warehouse.py `
  --output reports\m2b_reference_audit.json
```

审计门槛包括：

- checkpoint 与所有原始 payload 哈希一致；
- 证券主表和逐日状态业务键唯一；
- 交易日历的上一交易日链条连续；
- 退市证券保留历史、退市后不再生成状态；
- Universe 不得包含 ST、停牌、未知 ST 或坏行情；
- 当前名称回填和 2000 年前未知 ST 必须显式报告；
- 每行状态可以追溯到源 RawSnapshot。

## 7. 当前边界

M2-B 不负责财务报表 PIT、公司行动复权核对，也不负责模拟涨跌停板上的实际成交。这些分别属于 M2-D、M2-C 和执行/组合实验层。

已知但受控的不确定性：2000 年以前有 835,661 个证券交易日缺少可证明的 ST 状态，默认排除；15,733,904 行名称使用当前主表名称回退并带显式标志。两者均未被静默填充成可靠 PIT 事实。
