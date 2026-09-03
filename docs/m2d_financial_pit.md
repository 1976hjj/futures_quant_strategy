# M2-D 财务 PIT 与修订版本

状态：接口权限、分页和历史修订字段已验证；数据契约与回填器已实现，全历史回填进行中。

## 1. 目标

M2-D 回答的不是“这家公司现在看起来过去赚了多少”，而是：

> 在历史某个交易日收盘时，当时已公开的某个报告期最新财务版本是什么？

数据链路：

```text
income_vip / balancesheet_vip / cashflow_vip / fina_indicator_vip
-> 按报告期和 offset 的不可变 RawSnapshot
-> 原始公告版本表
-> 修订链与可知时间
-> as-of PIT 选择器
-> 单季/累计/TTM 明确口径
-> 财务因子输入白名单
```

## 2. 已验证的接口能力

当前网关已实测返回 `code=0`：

- `income_vip`；
- `balancesheet_vip`；
- `cashflow_vip`；
- `fina_indicator_vip`。

VIP 版可按 `period` 取全市场，并支持 `limit=5000` 和 `offset`。2023 年报期实测：

| 接口 | offset 0 | offset 5000 | offset 10000 |
|---|---:|---:|---:|
| income_vip | 5,000 | 1,724 | - |
| balancesheet_vip | 5,000 | 5,000 | 1,774 |
| cashflow_vip | 5,000 | 2,790 | - |
| fina_indicator_vip | 5,000 | 2,869 | - |

因此每个报告期必须动态翻页，直到首个少于 5,000 行的页面。任何恰好 5,000 行的响应都不能当作完整分区。

## 3. 时间与版本契约

- `end_date`：经济事实所属报告期，不是可用时间；
- `ann_date`：报表公告日；
- `f_ann_date`：实际公告日，有值时作为更保守的公开时间依据；
- `available_date`：`coalesce(f_ann_date, ann_date)`，日频研究默认下一交易日才可用；
- `report_type`：合并、单季、调整后、调整前和母公司口径，不得混合；
- `update_flag`：供应商的最新版本标记，不能用它删除历史修订版本。

同一 `(ts_code, end_date, report_type)` 如有多个公告日或不同值，必须全部保留。PIT 选择只能在 `available_date <= as_of_date` 的候选中取当时最新版本。

## 4. 回填分区

不取“每只股票一次”，因为老公司历史版本可超过普通接口上限。正式分区键为：

```text
(api_name, period, offset)
```

`period` 只使用合法季末：`0331`、`0630`、`0930`、`1231`。每页是独立快照，checkpoint 记录行数、payload 哈希和页终止标记。

监控与恢复：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts\start_financial_dashboard.ps1

powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts\resume_tushare_financial_backfill.ps1
```

监控页：`http://127.0.0.1:8768`。分页会根据满 5,000 行响应动态扩展，因此总分区数和 ETA 在任务进行中可能增加。

## 5. 发布层

计划对象：

- `raw.income_statement_versions`；
- `raw.balance_sheet_versions`；
- `raw.cashflow_statement_versions`；
- `raw.financial_indicator_versions`；
- `research.financial_versions_canonical`；
- `research.financial_pit_asof`；
- `research.financial_revision_events`；
- `research.financial_pit_exceptions`。

原始层保存接口返回的全部字段；研究层首先发布明确单位和口径的核心字段，但不丢弃其他供应商字段。

## 6. 验收门禁

- 分页连续，每个报告期必须出现终止页；
- checkpoint、snapshot、payload 和解压内容哈希一致；
- 关键时间和证券代码不得为空；
- 报告期必须与请求 `period` 一致；
- 原始修订版本不得被 `update_flag=1` 覆盖；
- as-of 选择不得读取未来公告；
- 年初至当期累计值、单季值和 TTM 值必须分开命名；
- 同期报表的会计恒等式、现金勾稽和跨表关系必须报告。
