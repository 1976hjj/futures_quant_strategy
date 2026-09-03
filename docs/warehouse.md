# 本地 A 股研究数据库

状态：已建成并通过全库审计（存在已识别、已隔离的供应商原始数据警告）  
数据库快照日期：2026-09-01  
数据库文件：`data/warehouse/alpha_research.duckdb`

## 1. 为什么使用 DuckDB，而不是 MySQL

这是个人研究机上的分析型数据库，主要工作是扫描几十年行情、做截面计算、滚动窗口和多表连接。DuckDB 直接查询 Parquet，适合这种列式分析；不需要启动服务、配置端口和维护账户，也不会为了查看少数列而读取整行。

MySQL 更适合高并发、频繁逐行增删改的业务系统。它可以在以后作为服务层或结果发布层，但不适合作为这 5,527 万行历史研究数据的第一底座。

当前设计仍然是一套真正可用 SQL 查询的数据库：客户端打开 `.duckdb` 文件后，会看到 schema、表、视图和元数据。大数据物理存放在压缩 Parquet 中，DuckDB 文件保存目录、视图和审计元数据。

## 2. 文件位置

```text
data/warehouse/
├── alpha_research.duckdb       # 数据库入口，连接工具打开这个文件
├── build_summary.json          # 本次构建摘要
└── parquet/
    ├── daily/year=YYYY/month=MM/data.parquet
    ├── adj_factor/year=YYYY/month=MM/data.parquet
    ├── daily_basic/year=YYYY/month=MM/data.parquet
    └── reference/
```

原始不可变归档仍保留在 `data/tushare_archive/`。仓库可以从归档重新生成；Parquet 和数据库不是唯一证据副本。

## 3. 用数据库工具连接

在 DBeaver、DataGrip 或其他支持 DuckDB 的客户端中新建 DuckDB 连接：

- Database/File：`D:\futures_quant_strategy\data\warehouse\alpha_research.duckdb`
- Host、Port、Username、Password：都不需要
- 连接模式：日常查看优先使用只读模式

如果客户端列表中没有 DuckDB，需要先安装该客户端的 DuckDB 驱动。仅支持 MySQL 协议的客户端不能直接打开 DuckDB；这种情况下后续可以增加 MySQL 发布镜像，但研究主库仍建议保留 DuckDB。

构建或刷新目录时，先关闭数据库客户端里未提交的事务，避免它长时间占用文件锁。

## 4. 数据库对象

### `raw`：供应商原始口径

- `raw.daily`：未复权日线行情，18,180,017 行；
- `raw.adj_factor`：复权因子，19,001,843 行；
- `raw.daily_basic`：每日估值、换手、股本与市值，18,089,460 行。

三张表覆盖 1990-12-19 至 2026-09-01，业务键均为 `(trade_date, ts_code)`。每行还保存 `source_snapshot_id` 和 `source_payload_artifact_id`，可以追溯到不可变原始响应。

原始单位务必注意：

- `raw.daily.vol`：手，1 手 = 100 股；
- `raw.daily.amount`：千元；
- `raw.daily_basic.total_share/float_share/free_share`：万股；
- `raw.daily_basic.total_mv/circ_mv`：万元。

### `research`：研究友好口径

- `research.market_daily`：成交量换成股、成交额换成人民币元，并带质量标志；
- `research.market_daily_tradable`：仅保留通过 OHLC 与基础可交易性检查的 18,179,826 行；
- `research.market_daily_anomalies`：191 行供应商 OHLC 异常，专供审计；
- `research.adj_factor`：复权因子研究视图；
- `research.daily_basic`：每日基础指标研究视图。
- `research.trading_calendar`：标准交易日历与上一交易日链条；
- `research.security_master`：包含已退市证券的 A 股主表；
- `research.security_name_history`：历史名称有效区间与公告时间；
- `research.security_session_state`：逐交易日的上市、ST、停牌、行情可用性和入池状态；
- `research.universe_daily`：通过默认状态门禁的 PIT 研究股票池。
- `research.corporate_action_announcements`：分红送转方案、修订阶段与可知日期；
- `research.corporate_action_events`：按除权日规范化的实施事件；
- `research.adjustment_factor_jumps`：复权因子跳变与行情参考比；
- `research.corporate_action_reconciliation_approved`：通过时间与数值对账门禁的公司行动白名单；
- `research.corporate_action_reconciliation_exceptions`：未解释、证据不全或误差超限的隔离记录。

因子和标签代码应优先依赖 `research` 层。需要复核供应商字段或追溯证据时才直接查询 `raw` 层。

### `metadata`：血缘和质量

- `metadata.archive_manifest`：每个数据域的行数、源检查点哈希和构建时间；
- `metadata.column_dictionary`：关键字段单位和说明；
- `metadata.data_quality_summary`：已识别的数据质量问题及数量。
- `metadata.m2b_archive_manifest`：M2-B 各参考数据域的行数、checkpoint 哈希、PIT 等级和发布时间。

## 5. 常用 SQL

查看三张原始表的行数和覆盖期：

```sql
SELECT 'daily' AS dataset, count(*) AS rows, min(trade_date), max(trade_date)
FROM raw.daily
UNION ALL
SELECT 'adj_factor', count(*), min(trade_date), max(trade_date)
FROM raw.adj_factor
UNION ALL
SELECT 'daily_basic', count(*), min(trade_date), max(trade_date)
FROM raw.daily_basic;
```

查看平安银行最近行情：

```sql
SELECT ts_code, trade_date, open, high, low, close, volume_shares, amount_cny
FROM research.market_daily_tradable
WHERE ts_code = '000001.SZ'
ORDER BY trade_date DESC
LIMIT 20;
```

把行情、复权因子和每日估值拼到一起：

```sql
SELECT
    p.ts_code,
    p.trade_date,
    p.close,
    a.adj_factor,
    b.turnover_rate,
    b.pe_ttm,
    b.pb,
    b.total_mv
FROM research.market_daily_tradable p
LEFT JOIN research.adj_factor a USING (trade_date, ts_code)
LEFT JOIN research.daily_basic b USING (trade_date, ts_code)
WHERE p.ts_code = '000001.SZ'
ORDER BY p.trade_date DESC
LIMIT 20;
```

查看已隔离异常：

```sql
SELECT * FROM metadata.data_quality_summary ORDER BY severity, issue_code;

SELECT ts_code, trade_date, open, high, low, close, volume_shares, amount_cny
FROM research.market_daily_anomalies
ORDER BY trade_date DESC;
```

## 6. 重建与审计

从不可变原始归档重建或断点续建：

```powershell
python scripts\build_market_warehouse.py `
  --archive data\tushare_archive `
  --warehouse data\warehouse
```

只读全库审计：

```powershell
python scripts\audit_market_warehouse.py `
  --output reports\warehouse_audit.json
```

审计包含源清单行数、覆盖期、空键、重复键、OHLC、负成交量、复权因子、研究层单位换算和异常隔离检查。供应商原始异常不会被修改，只会显式标记并从可交易视图隔离。

## 7. 当前边界

这套数据库已完成 M2-A 核心日频数据、M2-B 历史证券状态/Universe，以及 M2-C 公司行动/复权对账。完整 M2 现在只剩 M2-D：财务报表 PIT 与修订版本。
