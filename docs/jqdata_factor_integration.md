# JQDATA 因子接入

当前接入第一版策略需要的 4 个原始候选因子，并增加 2 个不依赖聚宽账号的本地替代因子：

| 页面中文名 | JQData 因子代码 | 聚宽公式口径 | 研究方向 |
| --- | --- | --- | --- |
| 预期盈利收益率（聚宽原名：预期市盈率） | `predicted_earnings_to_price_ratio` | 分析师对未来一年预期盈利加权平均值 / 当前股票市值 | 高值优先 |
| 现金流量市值比 | `cash_earnings_to_price_ratio` | 过去一年的净经营现金流 / 当前股票市值 | 高值优先 |
| 利润市值比（TTM盈利收益率） | `earnings_to_price_ratio` | 过去一年的归母净利润 / 当前股票市值，即 `1 / PE_TTM` | 高值优先 |
| 月换手率 | `share_turnover_monthly` | `ln(sum(turn_over_ratio, 21个交易日))` | 排除高值 |
| 日收益率标准差（252日指数加权） | `daily_standard_deviation` | 过去 252 个交易日收益率的指数加权标准差，半衰期 42 个交易日 | 排除高值 |
| 残余波动率因子 | `resvol` | `0.50*daily_std + 0.42*historical_resid_sigma + 0.08*cum_range` | 排除高值 |

## 哪些使用本地数据

现金流量市值比、利润市值比、月换手率和日收益率标准差使用公开公式在本地治理数据上计算，
不需要 JQData 账号。财务因子只使用当日已经公告可见的数据，市值单位统一后再相除；
波动因子使用本地前收盘价和收盘价计算日收益率。

页面仍把这 6 个因子归在 `JQDATA` 分类，是为了说明公式来源，不代表全部数值来自聚宽。

## 哪些仍读取 JQData 因子值

预期盈利收益率依赖分析师一致预期；残余波动率还依赖聚宽定义的多个子因子。
为了不把本地近似值冒充为聚宽原值，这两个因子通过官方 `get_factor_values` 接口获取数值，
再按日期和证券代码与系统自己的 PIT 股票池连接。页面选择的日期范围仍然决定最终发布范围。

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

没有配置时，普通 Alpha158 和上述 4 个本地因子的计算不受影响；只有预期盈利收益率和
残余波动率原版会返回明确的配置提示。
