# M2 Point-In-Time Data Factory 冻结规格

状态：Frozen for provider-neutral M2 build  
冻结日期：2026-09-01

## 1. 目标和完成边界

M2 把 Provider 原始响应变成可追溯、可审计、不可变的 PIT 数据版本。Provider 不能直接返回“可信研究 DataFrame”。

```text
Provider declaration + FetchRequest
-> ProviderResponse
-> immutable RawSnapshot
-> typed NormalizedRecord
-> PIT and domain audit
-> immutable Parquet partition
-> DatasetSpec + release manifest
```

Provider-neutral 竖切片先以合成 Provider 验证。M2 只有在首个真实 A 股 Provider 的能力、许可证和真实样本均通过门禁后才算全部完成。

## 2. Provider 声明

每个 Provider 版本必须冻结：

- Provider/adapter/API 版本；
- 官方来源和服务条款 URL；
- 获取、缓存、派生、再分发、商业使用和凭证约束；
- 每个数据域的字段、覆盖期、PIT 等级、修订行为和时间字段；
- 是否保留退市证券、历史 ST/停牌/涨跌停、历史指数成分、首次披露和修订；
- 单位、时区、频率限制和响应回填行为。

PIT 等级：

- `NATIVE_PIT`：Provider 提供可验证的历史可用时间与修订；
- `RECONSTRUCTED_PIT`：由原始事件和保守规则重建；
- `CURRENT_ONLY`：只有当前状态，禁止用于历史状态研究；
- `UNVERIFIED`：无法证明，依赖结论不得升级。

声明不是证据。`CURRENT_ONLY` 或 `UNVERIFIED` 数据不得伪装成 PIT。

## 3. RawSnapshot

RawSnapshot 必须包含请求规范、检索时间、响应媒体类型、原始 payload hash、Provider request ID、capability manifest hash 和许可证 manifest hash。

- payload 与 snapshot manifest 分别内容寻址；
- 相同响应允许幂等登记；
- 不同响应产生新 snapshot；
- 任何规范化记录必须引用其 raw snapshot；
- 原始响应禁止原地修复或覆盖。

## 4. NormalizedRecord 与 PIT 查询

通用字段：

- `logical_key`、`record_type`、`instrument_id`；
- `event_time`、`published_at`、`available_at`、`ingested_at`；
- `source`、`source_record_id`、`revision_id`；
- `raw_snapshot_id`、规范字段值和可复算 `record_hash`。

正式查询至少满足：

```text
available_at <= signal_cutoff
```

严格接收模拟还必须满足：

```text
ingested_at <= signal_cutoff
```

同一 logical key 的修订按 cutoff 前最后可用的版本选择；未来修订不得覆盖历史视图。

## 5. 发布门禁

以下情况为 BLOCKER：

- `available_at < published_at`；
- 主键与 revision 冲突；
- raw snapshot 血缘缺失；
- record hash 不可复算；
- 当前状态回填历史、退市证券从历史消失；
- Universe 使用未来公告或成员信息；
- 财务修订提前进入历史；
- OHLC/成交量硬约束错误；
- 公司行为参考价无法按冻结规则对账；
- Parquet partition 或 Dataset manifest 哈希不一致。

存在 BLOCKER 时不得发布 `audit_status=PASSED` 的 DatasetSpec。

## 6. M2 首批表

- `security_master`
- `trading_calendar`
- `daily_bar_raw`
- `daily_security_status`
- `universe_membership`
- `industry_membership`
- `financial_fact`
- `corporate_action`

合成竖切片可以只在一个小数据版本中覆盖所有表的关键反例，不代表真实 Provider 已覆盖这些能力。
