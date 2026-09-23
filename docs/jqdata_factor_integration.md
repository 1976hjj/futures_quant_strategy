# JQDATA 因子接入

当前保留可以由本地治理数据复现的候选因子。依赖分析师一致预期、且本地没有对应数据的
`predicted_earnings_to_price_ratio` 已从可选目录移除。

| 页面中文名 | JQData 因子代码 | 聚宽公式口径 | 研究方向 |
| --- | --- | --- | --- |
| 现金流量市值比 | `cash_earnings_to_price_ratio` | 过去一年的净经营现金流 / 当前股票市值 | 高值优先 |
| 利润市值比（TTM盈利收益率） | `earnings_to_price_ratio` | 过去一年的归母净利润 / 当前股票市值，即 `1 / PE_TTM` | 高值优先 |
| 月换手率 | `share_turnover_monthly` | `ln(sum(turn_over_ratio, 21个交易日))` | 排除高值 |
| 日收益率标准差（252日指数加权） | `daily_standard_deviation` | 过去 252 个交易日收益率的指数加权标准差，半衰期 42 个交易日 | 排除高值 |
| 残余波动率因子 | `resvol` | `0.50*daily_std + 0.42*historical_resid_sigma + 0.08*cum_range` | 排除高值 |

## 哪些使用本地数据

现金流量市值比、利润市值比、月换手率、日收益率标准差和残余波动率使用公开公式在本地治理数据上计算，
不需要 JQData 账号。财务因子只使用当日已经公告可见的数据，市值单位统一后再相除；
波动因子使用本地复权价格和日收益率。残余波动率由 252 日指数加权波动、相对等权市场收益的
历史回归残差波动，以及 252 日累计收益区间按 `0.50 / 0.42 / 0.08` 合成。

页面仍把这些因子归在 `JQDATA` 分类，是为了说明公式来源，不代表数值来自聚宽。

## 为什么移除预期盈利收益率

本地 `forecast_vip` 是上市公司的业绩预告，并不是分析师对未来一年的一致盈利预测，不能替代
聚宽原因子的输入。为避免用错误数据生成名称相同的因子，该因子不再出现在页面目录中。

## 本机配置

安装可选依赖：

```powershell
python -m pip install -e ".[data-jqdata]"
```

启动 API 前，在同一个 PowerShell 会话中设置账号。密码只放环境变量，不写进仓库：

```powershell
$env:JQDATA_USERNAME = "你的聚宽账号"
$env:JQDATA_PASSWORD = "你的聚宽密码"
python scripts/serve_m4_control_api.py
```

当前目录中的 JQData 来源因子均可使用本地数据计算，不要求配置聚宽账号。
