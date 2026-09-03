# 实验与因子契约

状态：M0 Draft

## 1. FactorSpec

每个因子版本至少声明：

- `factor_id`、名称、版本、作者和来源；
- 经济假设和预期机制；
- 公式或实现入口；
- `implementation_type`：expression/python；
- 所需字段和数据域；
- lookback 和 warmup；
- signal cutoff；
- 缺失值、无穷值和异常值行为；
- 允许的 Universe；
- 原始方向假设；
- 代码/AST 哈希；
- 父因子和生成过程；
- 最小单元测试与人工样例。

因子方向如果通过数据决定，只能在 Train 内决定并冻结到 Validation/Test。

## 2. 表达式类型隔离

系统至少区分：

- `FeatureExpression`：只允许当前已知和历史引用；
- `LabelExpression`：允许未来引用，只能在 Evaluator 内运行；
- `ExecutionExpression`：定义信号后可成交价格和事件。

三者不能互相隐式转换。Feature runtime 无权读取 Label 数据。

## 3. Factor Artifact

标准长表字段：

- `date`
- `instrument_id`
- `factor_id`
- `factor_version`
- `variant`
- `value`
- `available_at`
- `dataset_version`
- `universe_version`
- `artifact_id`

RAW、Winsorized、Standardized、Industry Neutralized、Industry+Size Neutralized 是不同 variant，禁止覆盖。

## 4. LabelSpec

必须显式声明：

- signal cutoff；
- entry event/price；
- exit event/price；
- horizon；
- 是否重叠；
- 停牌和不可成交如何处理；
- 公司行为和退市收益；
- benchmark/excess return 规则；
- 最后一个可计算样本的截断方式。

“1D forward return”不是充分定义。

## 5. ExperimentSpec

正式实验在运行前冻结：

- `experiment_id` 与 parent；
- hypothesis；
- Git commit 和 dirty-tree 状态；
- dataset/universe/factor/label/preprocessing 版本；
- train/validation/test/embargo；
- evaluator 配置；
- multiple-testing family id；
- 成本、执行和容量模型；
- 参数搜索空间及预算；
- random seed；
- promotion gates；
- 是否允许读取某个 Holdout vintage。

运行后不得原地修改 ExperimentSpec。修改产生新的 experiment id。

## 6. ExperimentRun

运行记录至少包含：

- 开始、结束、运行环境；
- 输入 manifest 哈希；
- stdout/stderr 和异常；
- 输出 artifact 哈希；
- 每个 stage 状态；
- 资源消耗；
- 数据覆盖范围；
- warnings 和 audit findings；
- 结果状态：`SUCCEEDED`、`FAILED_INFRA`、`INVALIDATED`、`REJECTED`、`PROMOTED`。

失败运行仍需永久保存。

## 7. Multiple Testing Family

检验家族必须在看结果前定义，至少说明是否包含：

- 所有候选因子；
- 同一因子的不同参数；
- 不同 horizon；
- 不同预处理 variant；
- 不同 Universe；
- 不同组合方法；
- 同一研究方向的连续 Agent 轮次。

只对最终挑中的 horizon 做 FDR 属于选择性汇报。

## 8. Walk-Forward Contract

每个 Fold 必须包含：

- Train：因子方向、特征选择、参数、预处理拟合；
- Validation：模型和候选方案选择；
- Test：只评价冻结方案；
- Purge：删除会通过标签 horizon 跨越边界的样本；
- Embargo：按配置隔离边界附近数据；
- Fold 结果及被选择因子；
- Test 是否曾被先前研究暴露。

重叠 Test 窗口产生相关 Fold，汇总统计必须明确处理，不能把它们当独立样本。

## 9. 因子晋级

晋级使用硬门禁和 Scorecard 两层：

### 硬门禁

- 无数据/实现 blocker；
- 无未来数据和非法 Test 使用；
- 覆盖率和样本量达标；
- 已完成预注册的 multiple-testing correction；
- 已完成 OOS；
- Tradable 必须完成成本测试；
- Deployable 必须完成流动性和容量测试。

### Scorecard

- Predictive power；
- Stability；
- Independence/incremental value；
- Tradability；
- Regime robustness；
- Decay；
- Interpretability。

Scorecard 权重配置化，但不能越过硬门禁。

## 10. 状态事件

建议事件类型：

- `FACTOR_REGISTERED`
- `IMPLEMENTATION_VERIFIED`
- `EVALUATION_COMPLETED`
- `AUDIT_FAILED`
- `REJECTED`
- `MARKED_REDUNDANT`
- `WATCHED`
- `PROMOTED_TO_CORE`
- `DECAY_DETECTED`
- `RETIRED`
- `INVALIDATED_BY_DATA_REVISION`
- `RETEST_REQUESTED`

当前状态由事件投影生成，事件本身不可删除。
