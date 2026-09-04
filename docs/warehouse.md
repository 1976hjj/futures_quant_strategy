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
- `research.factor_values_raw`：已发布因子资产的研究入口，保留 RAW 值和 `is_present` 缺失标志；其物理数据位于 `data/factor_store/releases/`。
- `research.factor_values_processed`：去极值标准化与规模中性化因子入口；物理数据位于 `data/factor_store/processed_releases/`。
- `research.factor_values_all`：RAW 与 processed 发布的统一查询入口，使用 `release_id` 和 `variant` 区分语义。
- `research.forward_return_labels`：按信号日保存的固定入场/退出复权收益、有效性和失效原因；
- `research.factor_evidence_summary`：按 Evidence Bundle 发布的因子描述性摘要，当前不代表 OOS 或晋级结论。
- `research.factor_robustness_summary`：M4.3 的 HAC、块自助法、稳定性和 FDR 结果；当前仍是短窗口诊断。
- `raw.factor_stability_segments`：每个因子/变体按冻结边界切分的时间段统计。
- `research.multiple_testing_family_summary`：完整检验家族规模、最小 p/q 值与拒绝数量。
- `raw.factor_walk_forward_daily`：M4.4 逐 fold、因子和变体的日度 RankIC 序列。
- `research.factor_walk_forward_summary`：M4.4 的 Train/Validation/Test 均值、HAC、Bootstrap 与 BH-FDR 完整统计。
- `research.factor_walk_forward_decisions`：将 FDR 拒绝进一步拆成方向支持、方向证伪和未拒绝，避免把反向显著误称为发现。
- `raw.factor_regime_statistics`：按 PIT 趋势和波动状态形成的条件 RankIC 诊断。
- `research.walk_forward_family_summary`：117 个 Walk-Forward 假设的统一检验家族摘要。
- `raw.factor_value_correlation_daily`：M4.5 月末抽样日的逐对截面因子秩相关。
- `research.factor_correlation_summary`：因子值相关与全窗口日度 RankIC 相关的逐对摘要。
- `research.factor_variant_deduplication`：39 条因子×变体路径的近重复组、canonical 路径和折叠决定。
- `raw.factor_hierarchical_linkage`：使用冻结绝对相关距离的完整平均连接树。
- `research.factor_clusters`：canonical 路径所属信息簇、机械代表项、覆盖率和代表选择状态。
- `raw.factor_conditional_rank_ic_daily`：控制其他簇代表项后的日度半偏/偏 RankIC 与增量 R²。
- `research.factor_incremental_value_summary`：25 条 canonical 路径的 HAC、块自助法、BH-FDR 和方向判定。
- `research.factor_redundancy_family_summary`：M4.5 统一检验家族、重点证伪和禁止晋级状态摘要。

因子和标签代码应优先依赖 `research` 层。需要复核供应商字段或追溯证据时才直接查询 `raw` 层。

### `metadata`：血缘和质量

- `metadata.archive_manifest`：每个数据域的行数、源检查点哈希和构建时间；
- `metadata.column_dictionary`：关键字段单位和说明；
- `metadata.data_quality_summary`：已识别的数据质量问题及数量。
- `metadata.m2b_archive_manifest`：M2-B 各参考数据域的行数、checkpoint 哈希、PIT 等级和发布时间。
- `metadata.factor_registry`：不可变因子规范、实现哈希、来源、家族和生命周期；
- `metadata.factor_release_manifest`：每次因子发布的计算身份、数据范围、文件哈希、行数和质量状态；
- `metadata.factor_quality_summary`：每个发布、每个因子的覆盖率、非空数和数值范围。
- `metadata.factor_version_disposition`：旧因子版本的诊断、替代或退役决定及后继版本。
- `metadata.processed_factor_release_manifest`：processed 发布的父 RAW、预处理规范、文件哈希和计算身份。
- `metadata.processed_factor_quality_summary`：processed 因子的覆盖率与逐日标准化误差。
- `metadata.python_plugin_registry`：受限 Python 插件的实现哈希、入口、沙箱策略、源码 Artifact 和不可变版本登记。
- `metadata.label_release_manifest`：LabelSpec、执行约束级别、因子发布来源、文件哈希和有效/失效数量；
- `metadata.evidence_bundle_manifest`：因子发布、标签发布、评价器版本、限制条件和证据文件登记。
- `metadata.robustness_evidence_manifest`：M4.3 统计规格、输入 Evidence、检验家族和输出文件哈希。
- `metadata.walk_forward_evidence_manifest`：M4.4 Fold、输入发布、统计规格、文件哈希、限制和决策状态。
- `metadata.holdout_exposure_ledger`：Holdout/伪 OOS 区间读取前写入的不可逆暴露记录。
- `metadata.factor_redundancy_evidence_manifest`：M4.5 输入血缘、冻结阈值、账本快照、文件哈希和诊断决定。

M4.7 Explorer 不新增或回写 DuckDB 表。它以只读连接读取以上 Manifest、研究视图和 M4.5 Parquet，生成 `reports/factor_explorer/<report_id>/` 静态投影；报告 Manifest 明确标记为 `READ_ONLY_DERIVED_REPORT_NOT_EVIDENCE`。

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

这套数据库已完成 M2-A 核心日频数据、M2-B 历史证券状态/Universe、M2-C 公司行动/复权对账，以及 M2-D 财务报表 PIT 与修订版本。M2-E 扩展数据正在回填，完成审计前不会发布为正式研究输入。

M2-D 新增四个原始全字段视图：`raw.income_statement_versions`、`raw.balance_sheet_versions`、`raw.cashflow_statement_versions`、`raw.financial_indicator_versions`。研究入口为 `research.financial_versions_canonical`、`research.financial_revision_events`、`research.financial_pit_asof` 和 `research.financial_pit_exceptions`。
