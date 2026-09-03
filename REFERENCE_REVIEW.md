# 参考项目代码评审

评审日期：2026-08-31  
评审性质：M0 架构与方法评审，不代表完整安全审计或许可证法律意见

## 1. 评审方法

本轮没有只依据 README。八个仓库均以 shallow clone 固定到具体 commit，并抽查了：

- 目录和模块边界；
- 因子/标签表达方式；
- 数据预处理；
- IC、FDR、Newey-West 等统计实现；
- Walk-Forward 切分；
- 成交价和交易约束；
- 实验记录和失败记录；
- 测试覆盖与许可证文件。

后续真正复用某个接口或算法前，仍需对目标 commit 做针对性测试和许可证确认。

## 2. 固定版本

| 项目 | Commit | Commit 日期 | 仓库许可证文件 | 定位 |
|---|---|---:|---|---|
| [microsoft/qlib](https://github.com/microsoft/qlib) | `79633dd9506ea689e5400dea0197717b5b3d74b7` | 2026-07-23 | MIT | 综合量化研究基础设施 |
| [microsoft/RD-Agent](https://github.com/microsoft/RD-Agent) | `6762f84f9bc0f5c6486c50a00e128a57ac6c3683` | 2026-08-04 | MIT | 自动研究与开发循环 |
| [JoinQuant/jqfactor_analyzer](https://github.com/JoinQuant/jqfactor_analyzer) | `69e677dc0dd9bed9fece02a70b9c81ce3d0afc53` | 2025-02-20 | MIT | 单因子分析和归因 |
| [fkchaos/a-share-quant-sim](https://github.com/fkchaos/a-share-quant-sim) | `f4cacf31dcfe0c7af3ec5fd1b697250e081441f8` | 2026-08-31 | MIT 文本 | A 股研究、回测和模拟盘 |
| [cauchy481/AlphaForge](https://github.com/cauchy481/AlphaForge) | `ee3939b447741525175e3b4ba760052d816b44b8` | 2026-06-30 | MIT | 个人 A 股因子研究框架 |
| [ICT-FinD-Lab/alphagen](https://github.com/ICT-FinD-Lab/alphagen) | `259687e8f316994426416c530a94842a2fe6405e` | 2026-06-04 | 根目录未发现许可证文件 | 公式 Alpha 生成和因子池 |
| [DulyHao/AlphaForge](https://github.com/DulyHao/AlphaForge) | `d0cfc27df23c60f271bc885fd43027b86b787746` | 2024-09-01 | 根目录未发现许可证文件 | AAAI 2025 公式因子生成与动态组合原型 |
| [warren618/AlphaForge](https://github.com/warren618/AlphaForge) | `9852b083aa841e49d1f3f5c8b632eae6b2a9e69a` | 2026-04-02 | MIT | Crypto 因子和回测小型框架 |

对于未发现明确许可证文件的仓库，本项目只学习公开论文和设计思想，不复制其实现代码，直到许可证得到确认。

## 3. microsoft/qlib

### 实际观察

- `qlib/contrib/data/handler.py` 将 Alpha158/Alpha360 包装为 `DataHandlerLP`，并区分 infer/learn processors；需要拟合的 processor 会接收 `fit_start_time` 和 `fit_end_time`。
- Alpha158/Alpha360 默认标签是 `Ref($close, -2) / Ref($close, -1) - 1`，说明 Qlib 的负 `Ref` 表示未来引用。该标签语义必须结合 Qlib 的信号和成交约定理解，不能按字符串直觉复制。
- `qlib/contrib/data/loader.py` 用表达式配置生成 Alpha158/Alpha360 特征，表达式引擎和可配置 operator 集合具有较高复用价值。
- `qlib/workflow/recorder.py` 提供接近 MLflow 的参数、指标和 artifact 记录抽象。
- `qlib/backtest/exchange.py` 支持 deal price、涨跌停表达式、停牌和 volume limit，但常见 CN benchmark 配置仍使用 `deal_price: close` 与固定 `limit_threshold: 0.095`。
- Exchange 以 `$close` 缺失推断停牌，并允许使用表达式定义买卖限制。这是通用框架能力，不等于完整 A 股历史交易规则。

### 准备借鉴

- Data Handler、Processor、Dataset 的职责分离；
- 表达式式特征定义和 operator 注册；
- 配置驱动 workflow；
- Recorder/Artifact 的实验接口；
- 模型、策略、Executor 和 Exchange 的抽象边界；
- Alpha158/Alpha360 作为公式和结果交叉检查来源。

### 不直接复用

- Qlib 数据目录不作为本项目数据真相层；
- 默认股票池、默认 label、默认复权和默认成交配置不直接采用；
- 不用固定 9.5% 阈值代替历史板块/ST/规则版本；
- 不让 Qlib 的负 `Ref` 约定同时承担 Feature 与 Label 语义；
- Qlib backtest 只能作为 adapter/benchmark，正式 A 股执行由本项目定义。

### 采用结论

**高价值基础设施参考，通过 adapter 集成；不作为系统内核。**

## 4. microsoft/RD-Agent

### 实际观察

- `RDLoop` 的真实步骤为 propose、experiment generation、coding、running、feedback、record，并使用 Trace 保存历史实验和反馈。
- Hypothesis、Experiment、Developer、Runner、Summarizer 均通过配置化类路径组合，扩展性较好。
- Qlib Factor Runner 会把新因子输出为 Parquet，注入实验 workspace，再由 Qlib 配置运行基线或组合实验。
- 当前因子去重主要计算新因子与已有因子的日度横截面相关，再对时间求平均，以 `corr < 0.99` 保留；没有使用绝对相关，且不是完整的条件增量或多维冗余检验。
- 因子提案提示在循环达到一定次数后鼓励尝试高 IC 的机器学习因子。这种目标导向不符合本项目“可信度优先”的默认目标。

### 准备借鉴

- Hypothesis -> Experiment -> Code -> Run -> Feedback -> Trace 的闭环；
- 独立 experiment workspace；
- 失败也写入 trace；
- 可替换 proposal/coder/runner/summarizer；
- 人工在 hypothesis 和 feedback 节点介入的能力；
- 容器化执行生成代码。

### 不直接复用

- 第一阶段不接入 Agent 循环；
- 不以模型表现提升作为默认反馈目标；
- 不采用仅靠 0.99 因子值相关的去重；
- 不允许 Agent 直接继承 Test/Holdout 结果进入下一轮上下文；
- 不允许 Agent 修改门槛、基础因子或研究宪法；
- 不把 RD-Agent 的 Qlib 场景当作 PIT 或 A 股交易审计实现。

### 采用结论

**后期 Control Plane 重要参考。先复用循环思想，不在基础阶段引入其运行时。**

## 5. JoinQuant/jqfactor_analyzer

### 实际观察

- `compute_forward_returns` 的核心是 `prices.pct_change(period).shift(-period)`。它并不知道传入的是收盘、开盘还是可成交价格，执行语义完全由调用者负责。
- 默认 IC 使用日度横截面 Spearman 相关；支持按行业分组、去均值、权重、分位数组合、换手和 rank autocorrelation。
- 测试覆盖若干手工小样本的 IC、因子收益、换手和自相关，适合做基础数值对照。
- 项目没有覆盖 PIT、Newey-West、FDR、Walk-Forward 或真实订单成交。

### 准备借鉴

- `date, asset` MultiIndex 的标准输入形态；
- IC、分层收益、换手和自相关的 API/结果形式；
- 行业调整和权重分析；
- 小型手算样例作为交叉验证素材。

### 不直接复用

- 不让 jqfactor 自行决定 Label；
- 不把 `prices` 默认当作 close；
- 不将其结果视为唯一正确答案；
- 不让核心系统依赖 jqdatasdk；
- 不复用其旧版 pandas 兼容代码作为内部数据模型。

### 采用结论

**作为 Evaluator 外部交叉验证器和报告参考，不作为正式统计引擎。**

## 6. fkchaos/a-share-quant-sim

### 实际观察

- 仓库长期保留策略注册、结果日志和 `STRATEGIES_DISCARDED.md`，失败研究记录是最有价值的资产之一。
- 文档强调回测与模拟盘共享 `strategy_adapter`，但实际新增策略仍需在 `strategy_map.py` 和 `strategy_adapter.py` 分别注册；overlay 策略还可以绕过通用路径。因此应该借鉴目标，而不是假设代码已经完全实现单一路径。
- `wf_runner.py` 在整个 panel 上预先计算 factors，再把固定策略运行在 train+test 窗口并切分 NAV。当前通用路径没有在 Train 内拟合和冻结参数/因子选择，不能等价于本项目要求的 nested Walk-Forward。
- 默认示例 `test_days=126`、`step_days=63` 会产生重叠测试窗口，Fold 不是独立样本。
- Walk-Forward 通过标准在 runner 中直接写为正收益 Fold >= 60%、Sharpe > 0.5；本项目不应把经验门槛硬编码在执行器里。
- `constraints.py` 按代码前缀使用固定 10%/20% 规则；ST 常量存在但构造逻辑未实际使用历史 ST 名称；也没有覆盖创业板规则切换日期、上市初期无涨跌幅等完整历史规则。
- 部分测试只验证数组形状、随机 train/test 均值不同等表面性质；`test_no_future_data_leak` 并不能真正检测未来泄漏。
- Golden dataset 和回测基线机制值得学习，但 golden baseline 需要独立计算来源，不能由被测实现自我生成后直接视为正确。

### 准备借鉴

- 研究和模拟盘共享业务逻辑的方向；
- Strategy/Provider adapter；
- 失败策略档案和复验记录；
- A 股涨跌停、停牌、T+1、退市和小市值容量问题清单；
- Golden regression dataset；
- 运维和每日信号报告经验。

### 不直接复用

- 免费行情数据不作为研究级 PIT 基础；
- 不复用固定涨跌停比例和以收盘状态推断全部成交的实现；
- 不把滚动区间收益切片称为完整 Walk-Forward；
- 不复用散布在大量策略脚本中的参数和执行规则；
- 不把现有 Golden baseline 当作我们的统计真值。

### 采用结论

**A 股工程经验和失败文化很有价值；代码作为案例与反例共同研究，不作为基础框架。**

## 7. cauchy481/AlphaForge

### 实际观察

- 目录清楚地覆盖 factor registry、preprocessing、evaluation、composer 和 event-driven backtest，适合作为模块清单参考。
- Registry 是进程内字典，保存函数、分类和 lookback，但没有持久版本、实现哈希、状态事件或数据血缘。
- PreprocessingPipeline 提供 MAD、缺失填充、ZScore 和中性化。
- Neutralizer 在样本不足或异常时返回原始因子，并吞掉所有异常；这会让调用者误以为中性化已经成功，是本项目禁止的失败方式。
- IC 统计采用普通 t-stat，没有 HAC/FDR；因子诊断提供相关和 VIF。
- Rolling composer 确实只使用当前日期之前的 history dates，这个窗口写法值得参考，但仍需要明确 Label 和 embargo。
- 未发现独立的正式测试目录；项目更接近完整示例而非经过统计审计的生产实现。

### 准备借鉴

- 模块划分；
- 预处理流水线接口；
- 中性化、rolling weights、相关矩阵和 VIF 的功能清单；
- 事件驱动回测的调用形式。

### 不直接复用

- 不复制进程内 Registry；
- 不允许预处理失败后静默返回未处理数据；
- 不采用没有数据版本和 PIT 约束的 loader；
- 不复用缺乏测试保护的统计和回测实现。

### 采用结论

**借模块边界和使用体验，核心算法重新实现并独立验证。**

## 8. ICT-FinD-Lab/alphagen

### 实际观察

- `Expression`、Feature、Constant、Operator、RollingOperator 和 Parser 构成了清晰的表达式树。
- Operator 提供参数个数和类型验证，RL 环境通过有限 token/operator/time-delta 空间生成表达式。
- 主要 RL/LLM 脚本将 `non_positive_time_deltas_allowed=False`，防止生成因子使用非正时间差；但 Feature 和目标仍共享同一个 Expression 类型。
- 目标示例通过 `Ref(close, -20) / close - 1` 使用未来数据，StockData 也显式具有 `max_future_days`。这对 Label 合法，但说明权限隔离不能只依赖调用约定。
- Alpha Pool 保存表达式、权重、单因子 IC、Mutual IC 和更新历史。
- Pool 的重复过滤只检查 `mutual_ic > 0.99`，没有检查绝对值，因此强负相关的等价反向因子可能保留。
- 因子池的优化目标主要是 IC/ICIR 或其变体，并不是完整的稳健性、成本和 OOS 晋级流程。

### 准备借鉴

- 强类型表达式树和有限 operator/token 空间；
- 解析、验证、序列化和表达式 human-readable 表示；
- 因子池而不是孤立因子的研究对象；
- Mutual IC 和 pool update history；
- 生成器与计算器 adapter 的分离。

### 不直接复用

- FeatureExpression 与 LabelExpression 必须成为不同类型和进程能力；
- 公式规范化需要处理交换律、常数折叠和符号等价，不只比较字符串；
- 冗余判断使用绝对相关、多种相关和条件增量；
- 不采用 IC 最大化作为 Promotion 目标；
- 第一阶段不接入 RL/LLM 生成；
- 未确认许可证前不复制源码。

### 采用结论

**Factor DSL 和生成器接口的首要研究参考；算法生成阶段后置。**

## 9. DulyHao/AlphaForge

### 实际观察

- 仓库包含大量 AlphaGen 派生结构，核心新增集中在 GAN factor mining 和 `combine_AFF.py` 的动态组合。
- 数据固定按训练截止年、下一年 Validation、再下一年 Test 切分；属于论文复现实验，不是长期滚动 Research OS。
- 动态组合使用过去窗口的 IC/RankIC/return 指标筛选因子，再用最小二乘估计当日权重；代码用固定 `shift = 21` 避免最近标签未完全实现。
- 存储表达式使用 Python `eval` 还原；同时存在硬编码 CUDA 设备和 Qlib 路径，不适合安全生产运行。
- 未发现项目根目录许可证；测试主要来自附带的 baseline 代码，并非对动态组合主路径的独立审计。

### 准备借鉴

- 动态因子选择与动态组合分为两个阶段；
- 使用过去滚动表现而非事后 Regime 选择；
- 静态因子池与不同 rolling window 的对照实验；
- 对 selection history 和每日权重进行保存。

### 不直接复用

- 不使用 `eval` 加载表达式；
- 不复制其代码；
- 不采用单一年度 Validation/Test 作为长期 OOS；
- 不使用固定 21 天代替由 LabelSpec 推导的 purge/availability；
- 不把论文脚本作为生产调度和数据架构。

### 采用结论

**只作为 M8 动态组合研究设计参考。**

## 10. warren618/AlphaForge

### 实际观察

- README 强调 Rolling IC、Newey-West、BH-FDR、next-bar open 和 Walk-Forward，这些检查项本身值得保留。
- 当前 evaluator 对单一时间序列做滚动窗口内的 Spearman 相关，命名为 `ts_rank_ic`；它不是 A 股横截面日度 IC。
- 随后对高度重叠的 rolling correlation 序列做 Newey-West，统计语义与本项目日度横截面 IC 序列不同。
- BH-FDR 目前只汇总固定 12-bar horizon 的 p-value，没有覆盖全部 horizon/阈值/组合搜索家族。
- backtest 虽然创建 `exec_prices = df["open"]`，实际策略收益仍是 shifted position 乘 `close.pct_change()`，`exec_prices` 没有用于收益计算。因此 README 的 next-open 声明与实现不一致。
- Walk-Forward 计算了 `train_factors`，但没有把训练得到的状态传递给 Test；Test 只在 `test_df` 上重新计算因子，还可能丢失 lookback history。
- 未发现独立测试目录，`Trade` 结果也仍为 TODO。

### 准备借鉴

- 审计检查项清单；
- evaluator/registry/combo/backtest 的小型模块边界；
- FDR 和 HAC 应进入基础统计工具箱的提醒；
- next-event execution 的原则。

### 不直接复用

- 不复用 evaluator、backtest 或 Walk-Forward 实现；
- 不迁移 crypto funding、leverage 和 liquidation 逻辑；
- 不接受 README 声明代替代码验证；
- 不把滚动时序相关称为 A 股横截面 IC。

### 采用结论

**作为统计和执行审计的反例/检查清单，基本不复用代码。**

## 11. 综合采用矩阵

| 能力 | 首要参考 | 我们的处理 |
|---|---|---|
| Dataset/Processor/Workflow | Qlib | 通过 adapter 借接口思想 |
| 实验记录 | Qlib Recorder、RD-Agent Trace | 自建不可变 manifest + event log |
| Research loop | RD-Agent | M11 后置接入，严格限权 |
| 单因子报告 | jqfactor_analyzer | 独立实现，再做数值交叉验证 |
| A 股执行案例 | a-share-quant-sim | 学失败案例，规则按历史版本重做 |
| 预处理模块 | cauchy AlphaForge | 接口借鉴，失败必须显式 |
| 表达式 DSL | AlphaGen、Qlib | 自建 Feature/Label 类型隔离 DSL |
| 动态因子组合 | Duly AlphaForge | 作为 M8 待证伪算法 |
| FDR/HAC/next-event 清单 | warren AlphaForge | 重新定义统计样本后实现 |

## 12. 最终结论

没有一个参考项目能够直接成为本项目底座。最合理的组合是：

```text
Qlib 的研究接口
+ RD-Agent 的实验循环
+ jqfactor 的分析输出
+ a-share-quant-sim 的 A 股失败经验
+ AlphaGen 的表达式树
+ AlphaForge 的动态组合假设
+ 我们自己的 PIT Kernel、统计审计、执行和锁箱治理
```

复用优先级为：设计思想 > 接口适配 > 小型纯函数对照 > 源码复制。任何源码复制都必须先确认许可证、版本、测试和维护责任。
