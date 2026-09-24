# JQFactor 候选因子批量接入记录

## 本次范围

- 既有批次为 20 项；目录随后再新增 12 项代表性本地透明因子，当前候选批次共 32 项。
- 既有 20 项已物化区间：2016-01-04 至 2025-12-31；新增 12 项须执行下方批处理后才会生成同区间结果。
- 每项包含 2,430 个交易日、5,697 只历史证券、9,853,584 条股票日记录
  （允许因停牌、上市时长不足或原始字段缺失而出现空值）。
- 批次结果清单：`data/factor_store/jqdata_candidates_2016_2025.json`。
- 本批只生成因子值并发布到通用因子仓库，不执行完整 M4 检验。

## 数据时点与滚动窗口

- 财务因子使用公告日时点连接，公告当日收盘产生的信号从下一交易日才可使用，
  避免未来函数。
- TTM 项目采用“最近年报 + 当年累计值 - 上年同期累计值”构造。
- 20、21、60、120、252 日滚动因子必须满足完整有效窗口；窗口不足保留为空值，
  不用短窗口冒充完整窗口。
- `sharpe_ratio_60` 与 `beta` 对接近零的分母做保护；`Rank1M` 对 20 日
  收益缺失的证券不参与横截面排名。

## 口径说明

既有的 20 个名字和研究方向来自聚宽因子库。新增 12 项是按相同研究维度补充的本地透明
实现，因此不宣称与聚宽内部公式逐值相同。当前环境未配置 JQData 账号，因此本批数值
由本地治理后的行情、估值和 PIT 财务数据按公开口径复现。`growth`、`momentum`、
`beta`、`liquidity` 属于本地透明复现，不应宣称与聚宽供应商内部风格模型逐值相同。
正式研究时仍应使用系统已有的去极值、标准化和行业/市值中性化选项处理原始极值。

## 新增的代表性本地透明因子

| 维度 | 因子代码 | 本地公式 | 方向 |
| --- | --- | --- | --- |
| 估值 | `sales_to_price_ratio` | `1 / PS_TTM` | 高 |
| 估值 | `dividend_yield_ttm` | `dv_ttm / 100` | 高 |
| 估值 | `operating_cashflow_to_ev_ttm` | 经营现金流 TTM / 企业价值 | 高 |
| 质量 | `gross_margin_ttm` | `(收入 TTM - 营业成本 TTM) / 收入 TTM` | 高 |
| 质量 | `operating_margin_ttm` | 营业利润 TTM / 收入 TTM | 高 |
| 质量 | `asset_turnover_ttm` | 收入 TTM / 期末总资产 | 高 |
| 质量 | `operating_cashflow_to_debt` | 经营现金流 TTM / 总负债 | 高 |
| 质量 | `current_ratio` | 流动资产 / 流动负债 | 高 |
| 成长 | `revenue_growth_yoy` | `or_yoy / 100` | 高 |
| 风格 | `nonlinear_size` | `residual(ln(MV)^3 ~ ln(MV))` | 高 |
| 流动性 | `turnover_cv_20` | `std(turnover,20) / mean(turnover,20)` | 低 |
| 风险 | `return_skewness_120` | `skewness(日收益率,120)` | 高 |

新增因子同样按公告日期点连接财务数据；`turnover_cv_20` 和 `return_skewness_120` 分别要求
完整 20、120 日窗口。企业价值非正、分母为零或原始字段缺失时，保留为空值而不作伪填充。

## 可重复执行

```powershell
$env:PYTHONPATH = "src;."
python scripts/publish_jqdata_candidate_batch.py `
  --start 2016-01-04 `
  --end 2025-12-31 `
  --result data/factor_store/jqdata_candidates_2016_2025.json
```

批处理会命中已经发布的不可变版本；公式或计算引擎变更时必须提升因子版本，避免
静默覆盖旧结果。
