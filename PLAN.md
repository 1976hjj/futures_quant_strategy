# Alpha Research OS 实施计划

状态：Active implementation — M2-E extended-data backfill + M4.4 walk-forward/regime evidence completed
项目范围：A 股、日频、横截面因子研究  
最后更新：2026-09-04

## 1. 项目目标

本项目建设一个长期运行的因子研究系统，而不是单个策略或一次性研究报告。系统的核心产出是四类可追溯资产：

1. 具有 Point-In-Time 语义的数据版本；
2. 可执行、可测试、可版本化的因子包；
3. 不可变、可复现的实验证据；
4. 因子晋级、淘汰、衰减和复验的决策历史。

完整研究循环为：

```text
Data ingest -> Data audit -> Factor proposal -> Factor validation
-> Robustness -> Multiple testing -> Redundancy/incremental value
-> Cost/OOS -> Promotion or graveyard -> Portfolio research
-> Attribution -> Research memory -> Next hypothesis
```

研究可信度高于收益率。异常优秀结果默认进入怀疑性审计。

## 2. 第一版明确边界

### 2.1 包含

- A 股日频横截面因子；
- 股票级行情、交易状态、股票池、行业、公司行为和 PIT 财务数据；
- Long-only 可交易研究和 Long-short 诊断研究；
- 因子注册、计算、预处理、评价、去重、组合和 Walk-Forward；
- A 股 T+1、停牌、涨跌停、ST、新股、退市、费用、滑点、流动性和容量约束；
- 实验注册、报告、失败档案、审计和滚动锁箱；
- 后期自动研究 Agent。

### 2.2 暂不包含

- 分钟级和高频；
- 期货、期权、港美股和加密货币；
- 实盘券商下单；
- 前端；
- 强化学习交易 Agent；
- 第一阶段的自动公式挖掘和深度学习；
- 未经授权的开源代码复制。

## 3. 总体架构

系统分为六个逻辑边界：

1. **Research Kernel**：研究规则、身份、版本、权限、时间语义、审计和锁箱；
2. **Data Factory**：数据源、原始快照、PIT 发布、数据质量和数据版本；
3. **Factor Factory**：FactorSpec、表达式 DSL、Python 插件、计算、缓存、血缘和沙箱；
4. **Evidence Factory**：IC、稳健性、多重检验、冗余、增量价值、成本和 OOS；
5. **Portfolio Lab**：组合、风险约束、订单、成交模拟、容量和归因；
6. **Research Orchestrator**：实验 DAG、报告、研究记忆和未来 Agent 循环。

详细边界见 `docs/architecture.md`。

## 4. 暂定技术选择

这些选择属于可修改的架构决策，不属于不可修改的研究宪法。

| 领域 | 第一版选择 | 原因 |
|---|---|---|
| 语言 | Python 3.11+ | 量化、统计和开源生态成熟 |
| 包与环境 | `uv` + `pyproject.toml` | 锁定依赖，支持可复现环境 |
| 表格互操作 | pandas / NumPy | Qlib、statsmodels、jqfactor 兼容边界 |
| 存储 | Parquet + PyArrow | 列式、可分区、可哈希、便于不可变快照 |
| 本地查询 | DuckDB | 直接查询 Parquet，早期运维成本低 |
| 契约 | Pydantic + YAML | 结构化 Factor/Data/Experiment Spec |
| 统计 | SciPy + statsmodels + scikit-learn | 基础统计、HAC、回归和简单组合 |
| 测试 | pytest + Hypothesis | 单元、性质、合成数据和回归测试 |
| 工作流 | 先确定性 CLI/DAG，后接调度器 | 先证明语义，再引入编排复杂度 |
| 外部框架 | Qlib/jqfactor 仅通过 adapter | 防止核心语义绑定第三方假设 |
| 标准运行环境 | Linux 容器；Windows 可开发 | 降低环境漂移并兼容 RD-Agent 生态 |

第一版不引入 PostgreSQL、Kafka、Spark、微服务或复杂分布式调度。只有数据规模和多人协作证明需要时才升级。

## 5. 核心数据结构

### 5.1 标识

- `instrument_id`：系统内部稳定证券标识，不以可变证券代码充当永久主键；
- `dataset_version`：已发布数据快照身份；
- `universe_version`：股票池定义和历史成员快照；
- `factor_id` + `factor_version`：因子语义与实现版本；
- `experiment_id`：`EXP-YYYYMMDD-XXXX`；
- `artifact_id`：内容寻址哈希；
- `holdout_vintage`：滚动锁箱批次。

### 5.2 时间字段

- `event_time`：经济事实或行情发生时间；
- `published_at`：首次公开时间；
- `available_at`：按研究规则最早可用于信号的时间；
- `ingested_at`：本系统实际获得时间；
- `valid_from` / `valid_to`：状态有效区间；
- `revision_id`：供应商修订或财务重述版本。

完整契约见 `docs/data_contract.md` 和 `docs/experiment_contract.md`。

## 6. 模块依赖规则

```text
constitution/specs
        |
data contracts -> data ingest -> PIT publisher -> data auditor
        |                                      |
        +---------------- approved dataset ----+
                                               |
factor spec -> compiler/runtime -> factor artifact
                                               |
label spec + universe spec + split spec -> evaluator
                                               |
                              evidence bundle + audit
                                               |
                 promotion service / graveyard / core pool
                                               |
                         portfolio + execution + attribution
                                               |
                             report and research memory
```

硬性依赖约束：

- 因子计算不得依赖 evaluator、portfolio 或未来标签；
- Label 运行时与 Feature 运行时分离；
- 只有通过 Data Audit 的 `dataset_version` 可进入正式实验；
- 报告只能读取结构化证据，不得反向修改实验结果；
- Agent 只能通过受限接口提交假设和因子包；
- Holdout 不以普通文件路径暴露给因子、模型或 Agent 进程。

## 7. Milestone 路线

### M0：设计基线（已完成）

- 固定项目范围和原则；
- 审阅参考项目并固定 commit；
- 建立计划、架构、数据契约、实验契约和研究宪法；
- 建立最小目录骨架；
- 定义后续 Milestone 的独立审计门禁。

完成定义：所有文档相互一致，YAML 规则可解析，仓库没有因子或回测伪实现。

### M1：Research Integrity Kernel（已完成）

- 实现 DatasetSpec、UniverseSpec、LabelSpec、FactorSpec、ExperimentSpec；
- 实现实验 ID、manifest、内容哈希和不可变 artifact 接口；
- 建立合成的敌对数据集；
- P0 测试覆盖 lookahead、as-of、signal/execution、标签重叠和锁箱权限。

完成证据见 `docs/audits/m1_verification.md`。M1 的公式字段尚不可执行；公式 AST 与声明依赖的一致性由 M3 编译器负责。

### M2：Point-In-Time Data Factory（进行中：真实多源接入与历史归档）

- 第一数据 Provider adapter；
- 原始快照和发布流程；
- security master、历史状态、股票池、公司行为、财务事实；
- Data Auditor 和 severity gate；
- 发布第一个可研究数据版本。

当前已接入 AKShare、BaoStock 和 Tushare 兼容 HTTP 网关，支持压缩 raw snapshot、断点续传和 Token 隔离。M2-A 行情/复权因子/每日基础数据、M2-B 历史证券状态/ST/停牌/Universe、M2-C 公司行动/复权对账、M2-D 财务 PIT 与修订版本均已回填、发布并通过审计。M2-E 交易约束、历史行业/指数、披露链与重要筹码数据已启动全历史回填。

### M3：最小 Factor Factory

- Factor Registry；
- FactorSpec 和版本/血缘；
- Feature-only 表达式运行时；
- Python 插件逃生口和沙箱边界；
- 5 至 10 个哨兵因子及人工 golden cases；
- RAW 与 processed artifact 分离。

Phase 1 已实现白名单 Feature AST 编译器、编译依赖与声明依赖逐项对账、规范 AST 实现哈希、不可变 Registry、feature-only 表达式运行时，以及 5 个带人工 golden case 的哨兵因子。M3-A 又增加了外部来源与生命周期治理、13 个首批候选/复现因子、MAD 去极值/标准化/行业与规模中性化组件，并完成绑定 M2-A～M2-D checkpoint 的真实数据竖切。

M3.2 已完成表达式因子的按实验 RAW 物化与发布：计算键绑定因子规范/实现、M2 checkpoint 血缘、Universe、日期、变体、预处理和信号钟；Parquet 资产不可变存放，DuckDB 保存 Registry、Release Manifest 和质量摘要。首个正式 ALL-A-PIT 发布覆盖 2024 年一季度 58 个交易日、5,250 只证券、13 个因子、3,944,590 行，独立资产审计通过，重复请求已验证命中缓存。

M3.3 已完成受限 Python 插件沙箱：插件源码由白名单 AST 约束，禁止导入、文件/网络/环境变量、属性反射、下标和无限循环等逃逸面；执行使用独立 `python -I -S` 进程、空工作目录、净化环境、单子进程 Job/资源上限、超时杀死和严格输入输出协议。插件源码进入不可变 Artifact Store，并登记至 `metadata.python_plugin_registry` 与 Factor Registry。分支型哨兵插件已在 M2 正式数据的 3 只证券、5 个交易日上完成 15 行重放，7 类攻击探针全部拒绝，独立审计通过。M3.4 对 M2-A～M2-D 的正式数据接入已验收；依赖 M2-E 历史行业数据的中性化变体待 M2-E 发布后补验。M4 已开始生成描述性证据，但任何因子仍未被认定为有效或进入 Core Pool。

### M4：最小 Evidence Factory

- Pearson IC、RankIC、ICIR、覆盖率、分层收益、换手和衰减；
- 标签对齐黄金测试；
- 与 jqfactor_analyzer/Qlib 在明确相同假设下交叉验证；
- Newey-West、block bootstrap 和 BH-FDR 的第一版；
- 生成 Evidence Bundle 和 Suspicious Result Audit。

M4.1 已完成第一版 Label 与描述性证据链：冻结 T 日收盘后信号、T+1 固定开盘入场、T+6 固定收盘退出的 5-session 复权总收益标签；缺失、停牌或不可交易边界不顺延而是显式失效。已实现日度 Pearson IC、平均秩 RankIC、覆盖率、五分组收益和上下分组换手，并生成不可变 Label Release 与 Evidence Bundle。正式 Q1 验收包含 303,430 个标签、754 行日度因子证据、3,770 行分组收益和 13 行因子摘要；独立 Python 与 DuckDB 交叉计算一致。当前标签明确为 `BAR_AND_SUSPENSION_ONLY`，在 M2-E 涨跌停价、退市收益、成本、HAC、多重检验和 OOS 完成前，证据状态固定为 `DESCRIPTIVE_ONLY_NOT_OOS`，不得据此晋级因子。

M4.2 已完成公司行动口径修正和第一版 processed 资产链。20 日动量、5 日反转、隔夜跳空发布为使用 `price × adj_factor` 的 2.0.0，旧 1.0.0 永久保留并登记为 `SUPERSEDED_DIAGNOSTIC`。修正 RAW、`WINSORIZED_ZSCORE` 和 `SIZE_NEUTRALIZED` 各自拥有独立计算键、Parquet、Manifest、质量摘要和 Evidence Bundle；逐日标准化误差与规模暴露残余均通过独立复算门禁。行业中性化仍等待 M2-E 历史申万成员正式发布，不使用当前行业倒填历史。详见 `docs/m4_2_corrected_and_processed_factors.md`。

M4.3 已完成短窗口稳健统计资产：冻结日度 RankIC 为主指标、Newey–West/Bartlett 5 阶滞后、循环 moving-block bootstrap（块长 5、10,000 次、固定种子）、三段时间稳定性和双侧 BH-FDR。13 个因子 × 3 个变体共 39 个假设进入同一检验家族；HAC 与 bootstrap 经 FDR 后均为 0 个拒绝，系统没有据此晋级任何因子。该结果只说明 Q1 证据不足，不等价于证明因子永久无效。详见 `docs/m4_3_robust_statistical_evidence.md`。

M4.4 已完成 2020-01-02～2025-12-31 长窗口、扩展训练窗的 Walk-Forward 与 PIT Regime 证据链。系统冻结 2023/2024/2025 三个测试 fold、5-session purge/embargo、训练期方向选择、HAC(5)、循环块自助法和覆盖全部 117 个 fold×因子×变体假设的统一 BH-FDR 家族，并在读取前写入不可逆 Holdout 暴露账本。2025 首次研究读取后已永久标记为暴露，不能再次声称为未见样本。独立审计通过但有重要发现：20 日动量三个变体在全部测试 fold 中均显著反向；RAW 与 `WINSORIZED_ZSCORE` 的秩检验高度重复；当前 Label 尚未纳入涨跌停、退市收益和交易成本。因此决策固定为 `NO_PROMOTION_DIAGNOSTIC_AND_PSEUDO_OOS`，任何因子仍不得进入 Core Pool。详见 `docs/m4_4_walk_forward_and_regime.md`。

### M5：Alpha158 接入

- 逐公式映射、版本和来源；
- 单元测试、随机人工样本与 Qlib 对照；
- 不允许整库一次性无验证导入；
- 输出公式差异和无法等价复现清单。

### M6：稳健性与去重

- 年度、规模、行业、流动性、波动和事后 Regime 矩阵；
- 因子值/IC/收益相关、层次聚类、VIF；
- 条件 IC、正交 IC 和 Core Pool 边际增量；
- Factor Graveyard 和研究饱和预警。

### M7：扩展因子库

- GTJA191；
- WorldQuant Alpha101；
- 经典基本面和技术因子；
- Alpha360 作为模型特征集单独管理；
- 合法可获得的外部公开因子。

### M8：Core Pool 与简单组合

- Contextual Core Pool；
- Equal/IC/ICIR/Ridge/LASSO/ElasticNet；
- 简单基线与复杂组合的 OOS 增量检验；
- 动态选择只作为待证伪候选。

### M9：Portfolio、Execution 与 Walk-Forward

- Nested Walk-Forward、purge 和 embargo；
- Long-only 与研究型 Long-short；
- T+1、订单生命周期、不可成交、费用、滑点和容量；
- Fold 稳定性、最差 Fold 和相关 Fold 处理。

### M10：归因与滚动锁箱

- 风格、行业、市场和特异收益归因；
- 滚动 Holdout vintage；
- 暴露账本；
- 失败后禁止把已暴露区间重新命名为 Holdout。

### M11：Research Agent

- Observe/Hypothesis/Design/Implement/Test/Audit/Decision/Memory；
- 沙箱运行、预算、权限和停止规则；
- 首先搜索 Graveyard 和已有 Cluster；
- Agent 不拥有晋级、规则修改或锁箱读取权限。

### M12：Paper Trading

- 与历史回放共享 Factor、Portfolio 和 Order 语义；
- 每日数据快照、信号、订单、未成交原因和持仓审计；
- 实盘 API 仍不在本阶段范围内。

## 8. 开发与独立审计流程

每个 Milestone 执行四段式流程：

1. **Specification freeze**：先冻结接口、时钟、测试和验收阈值；
2. **Build**：实现者完成代码和自测；
3. **Verification**：在冻结 artifact 上检查功能、回归和可复现性；
4. **Statistical audit**：假设结果为假，寻找泄漏、选择偏差和错误统计。

审计阶段先只提交 findings，不直接修改被审计结果。修复作为新的变更和实验运行。

详细门禁见 `docs/milestone_gates.md`。

## 9. 主要风险

| 风险 | 后果 | 第一控制措施 |
|---|---|---|
| 数据源没有历史 PIT 状态 | 幸存者偏差和财务前视 | Provider 能力清单；无法证明则禁止相关研究结论 |
| 复权数据包含未来公司行为 | 因子和收益泄漏 | 保存原始价、公司行为和 PIT 复权版本 |
| 信号与成交价语义混淆 | 回测虚高 | 显式 ClockSpec/LabelSpec/ExecutionSpec |
| 反复查看历史 Test | 伪 OOS | 数据暴露账本和滚动锁箱 |
| 多窗口、多预处理选择性汇报 | 数据挖掘偏差 | 预注册检验家族和全量实验日志 |
| 因子库数量虚高 | 重复 Alpha | AST 规范化 + 多维相关 + 条件增量 |
| Agent 自动扩大错误 | 批量产生伪研究 | Agent 最后接入；沙箱、门禁和只追加记录 |
| 过早抽象或分布式化 | 工程进度停滞 | 先完成一条可信竖切片 |
| 免费数据不满足研究级质量 | 无法达到目标可信度 | 数据源能力与预算尽早确定 |
| 开源项目许可证不清 | 法律和维护风险 | 无明确许可证项目仅学习思想，不复制代码 |

## 10. 待项目所有者确认

以下决定不阻塞 M0，但在 M2 前必须确认：

1. 首个数据源及预算：免费源、Tushare Pro、聚宽、Wind、Choice 或其他；
2. 第一 Universe：建议中证全指可交易域，报告同时给出 CSI300/500/1000 子域；
3. 可接受的可靠历史起点；
4. 第一研究 horizon：建议 1D、5D、10D、20D，首个组合以 5D 为主；
5. 第一版目标资金规模和最大成交额参与率；
6. Long-short 是否只作为统计诊断，不宣称现实可交易；
7. 滚动锁箱释放周期及谁有解锁权限；
8. Linux 容器是否作为 CI 和正式运行标准。

## 11. M0 结束后的第一开发任务

M1 不从真实数据下载开始，而从敌对合成数据和契约开始。我们会故意构造：

- 提前一天可见的财报；
- 后来退市但历史仍存在的股票；
- 历史 ST 状态变化；
- 涨停无法买入、跌停无法卖出；
- T 日收盘信号误用 T 日收盘成交；
- 重叠 5D 标签跨越切分边界；
- 全样本确定因子方向；
- 被 Agent 或研究进程尝试读取的锁箱数据。

系统必须首先证明它能够拒绝这些错误，然后才允许接入真实因子库。
