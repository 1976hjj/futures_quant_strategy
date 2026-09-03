# M2 真实 Provider 竖切片复核

复核日期：2026-09-01  
状态：PASS for bounded acquisition/archive slice; M2 not complete

## 1. 免费双源样本

- Provider：AKShare 1.18.64、BaoStock 00.9.20；
- 证券：`600000.SH`；
- 窗口：2024-05-06 至 2024-05-10；
- 结果：两个来源各5条日线，BaoStock另有5条每日状态；
- 三个独立 raw snapshot 均通过 lineage、hash、时间和 OHLC 审计；
- 规范化后的 OHLC、成交量和成交额交叉审计无 finding。

验收发现 AKShare 东方财富日线的成交量单位为“手”，BaoStock和 AKShare 新浪备用日线为“股”。内部单位现统一为 `shares`，上游、实际 endpoint 和原始单位进入 raw payload，100倍换算进入回归测试。

AKShare 东方财富 endpoint 曾连续连接失败，新浪 endpoint 正常。已实现记录上游的 fallback，但 fallback 不提升 PIT 等级。

## 2. Tushare 兼容网关权限样本

以下接口均返回 `code=0`：

- `trade_cal`、`daily`、`adj_factor`、`daily_basic`；
- `stock_basic(list_status=D)`；
- `suspend_d`、`stk_limit`、`namechange`；
- `income`、`balancesheet`、`cashflow`、`fina_indicator`；
- `dividend`。

Token 仅存在于进程内 HTTP request body。测试确认 Token 不进入 ProviderSpec、ProviderResponse、raw snapshot manifest 或 checkpoint。

## 3. 本地归档验收

首个正式归档窗口为2024-08-01至2024-08-10的前6个交易日：

| API | 分区 | 行数 | 原始字节 | gzip存储字节 |
|---|---:|---:|---:|---:|
| `daily` | 6 | 32,012 | 2,554,807 | 819,164 |
| `adj_factor` | 6 | 32,303 | 1,105,869 | 179,569 |
| `daily_basic` | 6 | 32,012 | 5,435,100 | 2,057,978 |

重复运行同一窗口时，已完成分区全部跳过。并发4路曾触发 HTTP 429，checkpoint 保存了成功分区，之后单并发恢复并补齐。由此将该网关默认并发固定为1，对429执行退避，不以积分宣传频率替代实测频率。

## 4. 全历史任务

- 范围：1990-12-19至2026-09-01；
- 交易日历：8,716个开市日；
- 目标：`daily`、`adj_factor`、`daily_basic` 共26,148个日期分区；
- 存储：确定性 gzip raw snapshot + 原始 payload hash + checkpoint；
- 安全水位：磁盘剩余少于30 GiB自动停止；
- 状态：已启动，可中断恢复。

## 5. 尚未证明

- 财务重述历史是否完整；
- 当前退市列表能否无未来信息地重建历史 Universe；
- 指数调样公告日/生效日是否完整；
- 公司行为与原始价格、复权因子能否逐笔对账；
- 自定义网关购买条款是否允许超出个人本地研究的用途。

因此该竖切片不能关闭整个 M2。
