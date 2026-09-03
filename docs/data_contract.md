# Point-In-Time 数据契约

状态：M0 Draft

## 1. 基本原则

所有历史查询必须回答两个不同问题：

1. 该事实描述哪个经济时点？
2. 研究者在当时什么时候能够知道它？

`date + instrument` 只能作为分析索引，不能完整表达 PIT 语义。

## 2. 通用字段

所有正式数据集必须可追溯到以下字段或 manifest：

| 字段 | 含义 |
|---|---|
| `instrument_id` | 内部稳定证券标识 |
| `event_time` | 事实发生或对应时间 |
| `published_at` | 首次公开时间 |
| `available_at` | 最早允许进入研究信号的时间 |
| `ingested_at` | 系统实际获取时间 |
| `source` | 数据供应商和接口 |
| `source_record_id` | 供应商记录身份（若有） |
| `revision_id` | 更正/重述版本 |
| `record_hash` | 规范化记录内容哈希 |
| `dataset_version` | 发布数据版本 |

查询约束至少为：

```text
available_at <= signal_cutoff
```

如果研究模拟真实的数据接收延迟，还需要：

```text
ingested_at <= signal_cutoff
```

## 3. 核心表

### security_master

- `instrument_id`
- 证券代码及交易所；
- 证券类型；
- 上市、终止上市时间；
- 代码和简称历史；
- 板块历史；
- `valid_from` / `valid_to`。

### trading_calendar

- 市场；
- 交易日；
- 开闭市和集合竞价时间；
- 特殊交易安排。

### daily_bar_raw

- 原始 open/high/low/close；
- volume、amount；
- 前收盘；
- 数据源状态字段；
- 不在此表中永久覆盖复权价格。

### corporate_action

- 除权除息、送转、配股、拆并股等；
- 公告、登记、除权和支付时间；
- 修订历史；
- 由事件计算的 PIT adjustment artifact。

### daily_security_status

- ST/*ST 历史状态；
- 停牌；
- 涨跌停价格和规则版本；
- 是否一字板；
- 当日可买、可卖原因；
- 上市天数和退市整理期状态。

状态必须来自历史记录或可复算规则，禁止使用今天的状态回填过去。

### universe_membership

- `universe_id`；
- `instrument_id`；
- `valid_from` / `valid_to`；
- `announced_at` / `available_at`；
- 加入和移除原因；
- 来源版本。

指数研究必须区分“公告后可知”与“正式生效”。

### industry_membership

- 行业分类体系和版本；
- `valid_from` / `valid_to`；
- `available_at`；
- 历史行业变化。

### financial_fact

- 指标名和值；
- `report_period`；
- `statement_type`；
- `published_at`；
- `available_at`；
- `revision_id`；
- 是否为更正/重述；
- 单季度、累计值和单位语义。

## 4. 数据分层

```text
data/raw        原始响应和不可变文件
data/processed  结构标准化但未必 PIT-safe
data/pit        经过 available_at 规则发布的研究视图
data/universe   历史股票池和状态
data/metadata   schema、provider 和发布 manifest
```

`features` 和 `factor_cache` 属于派生 artifact，不属于原始数据真相。

## 5. Dataset Version

发布 manifest 至少记录：

- schema version；
- provider 和拉取区间；
- 原始快照哈希；
- 转换代码 commit；
- PIT rule version；
- adjustment rule version；
- 分区及行数；
- Data Audit 结果；
- 发布者和发布时间；
- 被替代版本（若有）。

已发布版本禁止原地覆盖。数据修复产生新版本，并记录与旧版本的差异。

## 6. 数据质量门禁

### BLOCKER

- 主键重复导致事实冲突；
- 未来 `available_at` 记录进入历史视图；
- Universe/退市/ST 状态出现幸存者偏差证据；
- 价格或复权无法对账；
- 标签或成交数据缺少关键时间字段；
- 数据版本无法追溯至原始快照。

依赖相关字段的正式研究必须停止。

### ERROR

- 大面积缺失或异常跳变；
- 财务单位/累计值语义不一致；
- 交易日历不完整；
- 公司行为未能对账。

### WARNING

- 局部缺失；
- 单一 Provider 短暂失败；
- 覆盖率下降但未跨预注册门槛。

门禁应按数据域阻断依赖实验，不应因不相关字段警告停止全部研究。

## 7. 复权与收益

- 保存原始交易价格；
- 复权因子作为有版本的派生数据；
- 因子研究价格、组合估值价格、真实成交价格分别声明；
- 公司行为收益处理必须与持仓现金流一致；
- 不允许把今天下载的整段前复权序列直接视为历史当时可见数据。

## 8. 第一 Provider 的能力评估

正式接入前必须填写：

- 是否提供历史退市证券；
- 是否提供历史 ST/停牌/涨跌停；
- 是否提供 PIT 指数成分；
- 财务首次披露和修订时间；
- 公司行为原始事件；
- 是否回填历史数据；
- 价格、成交量和成交额单位；
- API 许可和本地保存限制。

无法证明的能力必须标记 `UNVERIFIED`，相关结论不能升级为 Deployable。
