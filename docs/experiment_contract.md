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

## 5.1 FeatureSetSpec

用于模型研究的特征集合必须作为不可变对象发布，至少声明：

- factor release 与每个 `factor_id/factor_version/variant`；
- inclusion/exclusion reason、canonical/cluster 关系和来源 Evidence ID；
- 缺失处理、预处理、去重与筛选规则；
- 规则是在 outer Train、inner Train/Validation 还是研究者已暴露样本上形成；
- 创建者、创建时间、父 FeatureSet 和研究预算事件；
- 当时已查看的 Test/Holdout vintage。

Evidence Explorer 导出的手工组合只是下一轮实验输入。用来选择组合的数据已经暴露，不得继续充当该组合的未见 Test。

## 5.2 ModelSpec 与 ModelRun

ModelSpec 除 ExperimentSpec 外还必须冻结模型实现/依赖哈希、目标函数、参数空间、搜索算法与预算、随机种子、FeatureSet、预测到组合的映射和模型级基线。ModelRun 必须逐 fold 保存：

- 实际拟合的预处理和 FeatureSet；
- Train/Validation 选择轨迹与最终参数；
- Test 预测及其首次读取事件；
- gain/split、permutation、SHAP 和消融证据；
- 相对简单基线的预测及 execution-aware 增量；
- 模型文件、环境、日志、资源消耗和失败原因。

特征重要性和 SHAP 是模型依赖的解释证据，不是因子的全局 Alpha 资格。

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

## 9. 证据路由与晋级

系统分别路由独立因子资格、模型特征资格和模型/策略资格。单因子证据卡不执行永久淘汰；晋级使用硬门禁和 Scorecard 两层。

### 硬门禁

- 无数据/实现 blocker；完整性 blocker 同时禁止 standalone、模型训练和交易；
- 无未来数据和非法 Test 使用；
- 覆盖率和样本量达标；
- 已完成预注册的 multiple-testing correction；
- 已完成 OOS；
- Tradable 必须完成成本测试；
- Deployable 必须完成流动性和容量测试。

单因子不显著、方向证伪、线性冗余或在某个模型中 importance 较低，不属于跨模型硬门禁。它们分别产生 `DIAGNOSTIC_ONLY`、`DIRECTION_CONTRADICTED`、`CANONICALIZED_REDUNDANT` 或 `RETIRED_IN_CONTEXT` 等上下文标签。

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
- `MARKED_MODEL_FEATURE_ELIGIBLE`
- `QUARANTINED_INTEGRITY_FAILURE`
- `FEATURESET_CREATED`
- `MODEL_EVALUATION_COMPLETED`
- `RETIRED_IN_CONTEXT`

当前状态由事件投影生成，事件本身不可删除。
