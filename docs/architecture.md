# 系统架构基线

状态：M0 Draft  
范围：A 股日频 Alpha Research OS

## 1. 架构目标

架构首先服务于可复现、可审计和防止信息泄漏，其次才服务于计算速度。系统必须支持长期增加数据源、因子、实验和 Agent，而不会丢失历史语义。

## 2. 三个平面

### Evidence Plane

保存系统认定的事实：

- 原始数据快照；
- PIT 数据版本；
- 因子值 artifact；
- 实验 manifest；
- 统计输出；
- 订单和成交记录；
- 数据暴露和审计记录。

Evidence Plane 只追加。修复通过新版本表达，不覆盖旧版本。

### Decision Plane

保存系统如何根据证据做决定：

- 因子状态事件；
- Promotion gate 结果；
- Graveyard 原因；
- Core Pool 上下文；
- 参数和模型选择；
- 人工审批。

Decision Plane 不得修改 Evidence Plane。

### Compute Plane

执行可重放任务：

- 数据转换；
- 因子计算；
- 预处理；
- 统计评价；
- 组合拟合；
- 回测和报告生成。

Compute Plane 是无状态工作进程。正式状态只能通过 artifact 与 registry 提交。

## 3. 六个子系统

### 3.1 Research Kernel

职责：

- 加载不可自动修改的研究规则；
- 创建 ID、manifest 和 lineage；
- 校验 Dataset/Universe/Label/Factor/Experiment/Execution Spec；
- 管理权限与 Holdout capability；
- 记录数据暴露、规则版本和审计事件。

Kernel 不计算因子，也不负责策略收益。

### 3.2 Data Factory

数据生命周期：

```text
provider response
-> raw immutable snapshot
-> schema validation
-> normalization
-> PIT availability derivation
-> integrity audit
-> dataset publication
```

正式实验只能引用已发布且未被标记为 `BLOCKED` 的 `dataset_version`。

### 3.3 Factor Factory

因子是一份 `FactorSpec` 加可执行实现，不是一个匿名 DataFrame 列。

支持两类实现：

1. **Expression factor**：受限、可解析、可规范化的表达式树；
2. **Python factor plugin**：用于无法由 DSL 表达的复杂逻辑，必须在沙箱内运行。

Feature 表达式类型禁止未来引用。Label 表达式在独立运行时执行，不能被 Factor runtime 导入。

Factor artifact 的逻辑键至少包括：

```text
factor_id
factor_version
dataset_version
universe_version
clock_spec_version
variant          # raw/winsorized/zscore/neutralized/...
date range
implementation_hash
```

### 3.4 Evidence Factory

输入必须是冻结的 factor、label、universe、split 和 evaluation specs。

输出为 Evidence Bundle：

- 输入 manifest；
- 日度 IC 序列；
- 汇总统计与不确定性；
- Robustness Matrix；
- Multiple Testing family 和校正结果；
- 冗余与边际增量；
- 成本/流动性/容量结果；
- OOS fold 结果；
- 审计 findings；
- 完整失败原因。

### 3.5 Portfolio Lab

严格区分：

```text
prediction -> target portfolio -> orders -> eligible fills -> holdings -> PnL
```

不得直接从因子排名跳到收益序列。所有收益必须能解释为订单、成交和持仓变化。

回放与 Paper Trading 共享以下纯逻辑：

- 信号冻结；
- 目标权重；
- 风险限制；
- 订单生成；
- T+1 可卖数量；
- 订单状态机；
- 成本和归因。

两者只在 Market Data Feed 和 Fill Provider 上不同。

### 3.6 Research Orchestrator

第一阶段是确定性 DAG；Agent 后置。

允许的 Agent 操作：

- 读取已授权研究证据；
- 查询 Graveyard/Core/Cluster；
- 提交 HypothesisSpec；
- 提交受限 FactorSpec 和实现；
- 请求运行非 Holdout 实验；
- 生成解释性报告。

禁止的 Agent 操作：

- 修改 constitution；
- 直接写原始/PIT 数据；
- 覆盖实验和失败记录；
- 读取或推断未释放 Holdout；
- 改变已经冻结的 gate；
- 自行把因子晋级为 CORE。

## 4. 关键接口边界

### DataProvider

Provider 只负责获取并描述来源，不直接提供“已经可信”的研究数据。每个 Provider 必须声明：

- 可提供字段；
- 历史覆盖；
- PIT 能力；
- 修订行为；
- 调用时间和响应哈希；
- 许可证和再分发限制。

### DatasetPublisher

将通过审计的数据分区发布为不可变 `dataset_version`。同一个版本永不原地修复。

### FactorRuntime

仅接收 Feature-safe 数据视图。运行时没有 Label 和 Holdout 读取能力。

### Evaluator

只接收已冻结 artifact 引用。统计函数不得自行改变 Universe、填充规则或因子方向。

### PromotionService

硬性门禁优先于总分。任何 `LOOKAHEAD`、`DATA_ERROR`、未做 OOS、无成本测试或无流动性测试，都不能被高 IC 抵消。

## 5. Contextual Factor Status

状态不是因子的全局永久属性。状态键为：

```text
(factor_version,
 universe_version,
 label_spec,
 preprocessing_variant,
 execution_spec,
 dataset_vintage)
```

同一因子可以在一个上下文为 `CORE`，在另一个上下文为 `REJECTED`。全局摘要只是这些上下文状态的派生视图。

## 6. 时间与成交状态机

```text
event occurs
-> information published
-> information available under research clock
-> signal cutoff
-> signal frozen
-> order created
-> next eligible session
-> blocked / partially filled / filled / expired
-> holding and T+1 sellable quantity updated
```

“T+1”不是一个简单 `shift(1)`。停牌、价格限制、成交量和订单有效期都会改变首次可成交事件。

## 7. Artifact 与 Registry

Registry 保存小型结构化元数据；大对象保存在 artifact store。

第一版：

- Registry：DuckDB；
- Market warehouse：DuckDB 目录与研究视图 + 按年月分区的 Parquet 数据；
- Artifact：本地 Parquet/JSON/HTML，按内容哈希和 manifest 管理；
- Raw snapshot：只追加；
- 报告：从 Evidence Bundle 可重复生成。

当前市场仓库入口为 `data/warehouse/alpha_research.duckdb`。`raw` schema 保留供应商原始字段和单位，`research` schema 提供单位标准化、质量标志和可交易行情视图，`metadata` schema 保存归档血缘、字段字典和质量摘要。具体连接与查询方式见 `docs/warehouse.md`。

后期迁移 PostgreSQL 或对象存储时，逻辑身份和 manifest 不改变。

## 8. 失败处理

异常分三类：

- `INVALID_RUN`：实现、数据或基础设施错误，不能解释为因子失败；
- `REJECTED_HYPOTHESIS`：实验有效执行，但未通过预注册门槛；
- `CONTEXT_LIMITED`：只在特定规模、时期或 Regime 下成立。

Graveyard 必须区分这三类，防止把代码 bug 当作经济结论，也防止修复 bug 后删除旧失败记录。

## 9. 外部框架集成

- Qlib：Dataset/Model/Workflow/Benchmark adapter；
- jqfactor_analyzer：统计交叉验证 adapter；
- 外部因子库：通过来源和公式版本导入；
- RD-Agent/AlphaGen：后期作为 hypothesis/factor proposal provider；
- 任何外部框架都不能绕过 Kernel、Data Audit 或 Promotion gate。
