# M4.1 Label 与基础证据工厂

状态：第一版已完成并通过黄金样本、正式数据和独立交叉实现审计。

## 1. 时间语义

当前标准标签为 `next-open-to-5d-close-total-return@1.0.0-provisional`：

```text
T 日 15:00 后因子可用
-> T+1 固定交易日开盘入场
-> T+6 固定交易日收盘退出
-> exit_adjusted_price / entry_adjusted_price - 1
```

入场与退出相差 5 个 session。若证券在固定边界缺失、停牌或没有可交易行情，标签失效并保存原因；系统不会偷偷顺延到下一次有价格的日期，因为顺延会改变持有期和样本选择。

复权价格为原始价格乘当日 `adj_factor`，因此标签包含持有期间已发生公司行动的财富变化。标签的 `available_at` 是退出日收盘，必须严格晚于因子可用时间。

## 2. 当前执行约束等级

当前发布等级为 `BAR_AND_SUSPENSION_ONLY`，已处理：

- PIT 股票池和信号资格；
- 入场/退出行情是否存在；
- 停牌与行情可交易标志；
- 复权因子和固定 session 对齐。

尚未处理：

- 涨停无法买入、跌停无法卖出；
- 退市收益替代模型；
- 费用、滑点和冲击成本。

原因是 M2-E `stk_limit` 尚未发布。完成后会生成新的 `LIMIT_AWARE` Label Release；不会修改本次 provisional 资产。

## 3. 基础证据

每个因子、每个交易日保存：

- Universe 数、因子非空数、有效标签数和配对样本数；
- 因子覆盖率；
- Pearson IC；
- 使用平均并列秩的 RankIC；
- 五分组平均未来收益。

因子摘要保存日度 IC 均值、有效 IC 日数、高组减低组描述性收益、顶部和底部分组换手。分组使用平均秩；并列值不会通过证券代码被人为拆成虚假的精确次序。

## 4. 正式 Q1 验收资产

- 来源因子发布：`sha256:2faab50ecfd39544d40c7c451b9f9325cdab57f075d12b6a52a50593fe18778e`；
- Label Release：`sha256:778b6ba94398514dd123180cf69c2d446a3628c4394ff19ad3ddbe485e05bcae`；
- Evidence Bundle：`sha256:4c2bc48115894c0618149004c84bc1c820b1f1ec7c799b1f07160c370ef6faf3`；
- 标签：303,430，其中有效 303,354、失效 76；
- 日度证据：754 行，即 13 因子 × 58 日；
- 分组收益：3,770 行，即 13 × 58 × 5；
- 因子摘要：13 行；
- 重复运行：Label 与 Evidence 均命中内容缓存。

物理资产位于 `data/evidence_store/`，DuckDB 查询入口为：

- `research.forward_return_labels`；
- `raw.factor_daily_evidence`；
- `raw.factor_quantile_returns`；
- `research.factor_evidence_summary`。

## 5. 如何理解当前结果

当前证据只用于证明系统可以正确对齐和计算。时间窗口只有 2024 年一季度，而且这批因子曾参与系统开发；因此即便某个 IC 数值较高，也不能称为发现 Alpha，更不能据此选择方向或晋级。

下一阶段必须增加涨跌停与退市处理、HAC/Bootstrap、不稳定性诊断、多重检验和严格 Walk-Forward/OOS。M3-A 中使用未复权历史价格比值的价格类候选也需要公司行动敏感性审计后才能进入科学解释。
