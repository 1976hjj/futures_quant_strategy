# Factor Evidence Explorer 与 Model Evidence 规划

状态：规划冻结候选；本文件只定义后续产品与证据边界，不代表功能已经实现。

## 1. 目标

系统需要让研究者看到“每个因子经历了什么、在什么上下文表现怎样、与其他因子如何相关、进入模型后是否有增量”，用于形成组合假设和调整策略。展示层不是淘汰器，也不能修改底层 Evidence Asset。

核心原则：

- 单因子结果是特征画像，不是永久生死判决。
- 模型整体结论来自严格隔离的模型级 Walk-Forward 和可执行性证据。
- 只有研究完整性失败可以全局禁止使用；统计弱、方向相反、线性冗余和某个模型下无贡献都是上下文状态。
- 所有图表和摘要必须能追溯到不可变 Manifest、Parquet、ExperimentRun 和暴露账本。

## 2. Factor Evidence Card

每个 `factor_id × factor_version × variant × context` 自动生成一张版本化证据卡。它至少展示：

### 身份与数据质量

- 公式/实现、来源、经济机制、方向假设、实现哈希和父级血缘；
- dataset、Universe、信号时钟、可用区间、warmup、覆盖率和缺失模式；
- 异常值、分布、截面离散度、时间漂移、行业/规模/流动性暴露；
- RAW 与 processed variants 的关系及 canonical/重复状态。

### 单因子预测证据

- Pearson IC、RankIC、ICIR、HAC 与 block-bootstrap 区间、FDR 状态；
- 分组收益、top-bottom spread、换手和多 horizon 衰减；
- Train/Validation/Test 各 fold、最差 fold、年度和 PIT Regime 结果；
- `DIRECTION_SUPPORTED`、`DIRECTION_CONTRADICTED`、`NOT_REJECTED` 分开显示；
- 明确标识描述性、回顾诊断、伪 OOS、已暴露样本或真正未见 OOS。

### 冗余与组合参考

- 因子值相关、日度 IC 相关、所属 cluster、机械代表项和近重复路径；
- 条件/正交 RankIC、增量 R²、控制集合和计算窗口；
- 与用户所选其他因子的并排相关、机制、覆盖、换手和 Regime 互补性；
- 不把 cluster representative、显著性计数或重要性排名称为独立 Alpha 数量。

### 可执行性

- gross/net 收益、费用、滑点、冲击、换手、容量和不同资金规模；
- 涨跌停、停牌、T+1、退市与订单未成交原因；
- 描述性 label 与 execution-aware 回放必须视觉上分区，避免把前者误读为可交易回测。

### 路由而非删除

一张卡可以同时具有多个标签：

- `STANDALONE_ELIGIBLE`：可以进入简单组合候选，但不等于 CORE。
- `MODEL_FEATURE_ELIGIBLE`：允许进入严格隔离的模型研究。
- `DIAGNOSTIC_ONLY`：只能用于理解或提出新假设。
- `CANONICALIZED_REDUNDANT`：默认折叠计算，但原资产和历史证据保留。
- `DIRECTION_CONTRADICTED`：原方向受到证伪；不禁止无方向模型使用。
- `REQUIRES_NEW_OOS`：结论已使用暴露样本，需要新 vintage 才能确认。
- `QUARANTINED_INTEGRITY_FAILURE`：存在泄漏、PIT、实现或数据完整性错误，禁止训练和交易。
- `RETIRED_IN_CONTEXT`：在明确上下文和多轮证据下退休，允许有理由、有预算地复验。

## 3. Evidence Explorer

Explorer 是 Evidence Card 的只读索引和比较层，第一版优先输出可重复生成的静态 HTML 加 DuckDB 查询视图，之后再决定是否需要交互式前端。

最低功能：

1. 因子总览：按 family、source、variant、coverage、cluster、route、fold/regime 状态筛选。
2. 单因子详情：展示一张完整 Evidence Card 和全部上游资产链接。
3. 多因子比较：选择若干因子并排查看相关、方向、稳定性、换手、成本、容量和互补性。
4. Cluster 视图：显示树、canonical 路径、代表项、折叠原因和簇间关系。
5. FeatureSet 草案：把选择保存为新的 `FeatureSetSpec`，记录选择者、时间、理由、来源报告和当时已暴露区间。
6. 历史对比：查看同一因子、模型或 FeatureSet 在不同 dataset vintage、Universe、horizon 和执行假设下的变化。

Explorer 只能读取结构化证据。在已经生成的暴露报告内排序和过滤保持纯只读；把手工组合保存、导出或用于新实验时，才登记为研究事件并绑定来源报告。如果据此调整策略，后续实验不得把用于选择的区间称为未见 OOS。

M4.7 MVP 的页面、静态构建、数据快照、颜色语言和验收细则见 `docs/factor_evidence_explorer_ui.md`。

## 4. Model Evidence Card

每个冻结 `ModelSpec × model_version × fold × execution_context` 生成模型证据卡，至少包括：

- FeatureSet、标签、目标、模型代码/依赖、参数空间、搜索预算和随机种子；
- 每个 fold 的 Train/Validation/Test 时间、purge、embargo、样本量和暴露状态；
- 与等权、线性、历史生产模型等简单基线的成对比较；
- RankIC、损失、校准、分组收益、gross/net PnL、最大回撤、换手、成本、容量和最差 fold；
- 每个 fold 实际入模特征、选择频率及因缺失、冗余或训练内筛选被排除的原因；
- gain/split importance、permutation importance、SHAP 分布与跨 fold 稳定性；
- 单特征、cluster、机制组消融后的增量变化及不确定性；
- 预测和特征漂移、Regime 表现、失败案例和审计 findings。

重要性、SHAP 或某次消融都不能独立决定因子生命周期。只有预注册、折内执行、相对冻结基线且在 OOS 与执行约束下重复的模型级增量，才能支持模型晋级。

## 5. LightGBM 研究契约

LightGBM 第一版遵循以下边界：

```text
outer Train
  -> fit preprocessing/missing policy
  -> fold-local dedup and feature selection
  -> inner Train/Validation parameter search
  -> freeze FeatureSet + model + portfolio mapping
outer Test
  -> one evaluation read
  -> execution-aware orders/fills/PnL
  -> immutable Model Evidence Asset
```

- 全窗口单因子报告可用于提出下一轮假设，但不能用于同一个未见 Test 的折外筛选。
- 树模型不要求每个输入特征具有稳定的单调方向；方向证伪不等于特征无用。
- 单因子边际信号接近零的特征仍可能通过交互有效，因此除完整性/质量硬失败外保留在模型候选池。
- 不能无限保留所有特征并反复调参；FeatureSet 数量、模型次数、搜索预算和查看 Test 次数都进入研究预算与暴露账本。
- “组合有用”属于具体模型、FeatureSet、Universe、horizon、执行假设和 dataset vintage，不回写成因子的全局永久属性。

## 6. 报告发布与验收

正式产物分三层：

1. 证据层：不可变 Parquet/JSON Manifest 和 DuckDB 视图，是唯一事实来源。
2. 展示层：可重建的 Factor/Model HTML 报告和比较页面，不承载新计算结论。
3. 决策层：只追加的 route/lifecycle 事件，记录依据、上下文和批准者。

首版完成定义：

- 任意发布因子无需改报告代码即可生成 Evidence Card；
- 报告中的每个数值能够定位到 asset ID、字段和上下文；
- 单因子弱或方向证伪不会自动产生全局 `REJECTED`；
- 能导出不可变 FeatureSetSpec 并在模型 fold 中重放；
- LightGBM 与简单基线在相同 folds、标签和执行假设下比较；
- 报告明确区分描述性、已暴露诊断、真正 OOS 和 execution-aware 结果；
- 独立审计验证展示汇总与底层证据一致，且展示层无法修改证据或生命周期。
