# M2-E 交易约束、基准、披露与筹码数据

状态：全历史回填运行中。

M2-E 的目标不是一次性增加尽可能多的数据，而是补齐会改变研究结论可信度的数据域。所有接口继续使用不可变原始快照、内容哈希、单并发、断点续传和指数退避。

## 数据范围与顺序

第一队列（核心底座）：

- `index_basic`：指数档案；
- `index_classify`、`index_member_all`：申万 2021 分类及带进入/退出日期的历史行业成员；
- `index_weight`：沪深 300、中证 500、中证 1000、上证 50、科创 50 的月度权重；
- `stk_limit`：每日涨跌停价，用于成交可行性判断；
- `disclosure_date`：财报预计与实际披露日；
- `forecast_vip`、`express_vip`：业绩预告和业绩快报；
- `fina_mainbz_vip`：按产品和地区拆分的主营业务构成。

第二队列（重要筹码与杠杆数据）：

- `share_float`、`repurchase`、`stk_holdertrade`、`stk_holdernumber`；
- `pledge_stat`、`top10_holders`、`top10_floatholders`；
- `margin`、`margin_detail`、`margin_secs`；
- `hk_hold`（仅回填交易所仍按日披露的历史区间，截止 2024-08-19）。

资金流、龙虎榜和概念快照暂不进入核心底座。前两者供应商口径较强，后者没有可靠的历史进入/退出日期，直接用于历史因子容易产生未来函数。

## 分区原则

- 日频全市场接口按交易日分区；交易日只取 M2-B 的冻结交易日历；
- 财报扩展接口按合法季末与 offset 动态分页；
- 指数权重按指数和月份分区；
- 公司事件按月份和 offset 分区；
- 股东及质押数据按证券代码和 offset 分区；
- 任一满页响应都会自动增加预期页数，直到出现首个非满页才确认分区完整。

## 运行与监控

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\resume_tushare_m2e_backfill.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_m2e_dashboard.ps1
```

监控地址：`http://127.0.0.1:8769`。

正式任务覆盖 1990-12-31 至 2026-09-01，共 44,666 个基础分区。动态分页可能使最终页数增加。Token 只从本地忽略版本控制的 `secrets/tushare.env` 注入进程，不进入命令行、日志、checkpoint 或原始快照。

## PIT 使用约束

- 预告、快报、股东、回购、解禁等数据只能从 `ann_date` 或更保守的实际披露日之后使用；
- 指数和行业成员必须使用历史权重或 `in_date/out_date`，不得拿当前成分回填过去；
- 北向持股在 2024-08-20 后改为季度披露，不能伪造连续日频数据；
- 涨跌停价属于执行约束，不能把触及价格简单等同于一定无法成交，最终仍需结合成交量和订单模型。
