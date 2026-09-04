# Alpha Research OS 量化知识与术语库

状态：Living Document  
范围：A 股、日频、横截面因子研究  
维护原则：开发中首次出现的新概念、公式、统计口径和交易假设，应先在此定义或链接，再进入正式实现。

## 1. 文档定位

这不是普通的中英文名词表，而是本项目的共同语义基线。每个概念尽量回答：它是什么、项目采用什么口径、处于研究链路哪里、最容易犯什么错误。

若本文与 Research Constitution、冻结 Spec 或 ADR 冲突，以正式规则和机器可读契约为准。本文负责解释，不替代规则。

完整链路：

    Provider/API
    → Raw Snapshot
    → Normalization/PIT Reconstruction
    → Data Audit
    → Dataset Version + Historical Universe
    → FactorSpec + Factor Runtime
    → RAW Factor Artifact
    → Preprocessing Variants
    → Label + Evaluation
    → Robustness + Multiple Testing + Redundancy
    → Factor State
    → Portfolio + Orders + Fills + Costs
    → Walk-Forward + Holdout + Monitoring

## 2. 数据、时间与 Point-in-Time

### Point-in-Time（PIT）

在任一历史决策时点，只能看到当时已经公开且系统能够获得的信息。一份 2023 年年报若在 2024 年 4 月发布，2024 年 4 月以前不得使用。PIT 不等于“记录带有历史日期”。

项目口径：正式查询必须带 as-of 语义，并根据 available_at 选择当时可见的最新修订。

### 四类核心时间

| 字段 | 含义 | 示例 |
|---|---|---|
| event_time | 经济事件所属时间 | 财报报告期末、行情所属交易日 |
| published_at | 来源正式发布时间 | 公告披露时间 |
| available_at | 按项目规则可用于决策的最早时间 | 盘后公告次一可用时点 |
| ingested_at | 系统实际获取时间 | API 请求完成时间 |

最危险的错误是用 event_time 代替 available_at，造成财务前视。

### As-of Query

给定决策时点，对每个逻辑事实选择 available_at 不晚于 as-of 的最新修订。未来发布的更正、重述和状态变化不能倒灌到过去。

### Revision 与 Restatement

同一逻辑事实可能有多个版本。修订链必须追加保存，不能用最新值覆盖旧值。财务重述、公告更正和供应商历史回刷都需要审计。

### Bitemporal Data（双时态数据）

同时表达“事实在哪段时间有效”和“系统在什么时候知道它”。本项目以事件/有效期时间加 available_at 表达双时态。

### Raw Snapshot

Provider 原始响应的不可变快照，包括请求、检索时间、原始字节哈希、来源和许可证。Raw 是证据，但不表示内容已经可信。

### Normalized Record

把不同来源映射到统一字段、类型、单位和主键的记录。标准化不自动等于 PIT-safe。

### Dataset Version

通过审计后发布的不可变数据版本，绑定原始快照、转换代码、PIT 规则、复权规则、覆盖范围、分区哈希和审计状态。同一版本不得原地修复。

### Artifact 与 Content-addressed Storage

Artifact 是不可变研究产物，身份由内容哈希决定。相同内容得到相同地址，任何字节变化都会产生新地址。大对象放 Artifact Store，小型身份与元数据放 Registry。

### Data Lineage（数据血缘）

从研究结果反向追踪到因子版本、数据版本、转换代码、Raw Snapshot 和 Provider 请求的完整路径。没有血缘的结果不能成为正式证据。

### Data Domain

本项目区分行情、财务、证券主数据、交易状态、股票池、公司行为、交易日历、Label 和 Holdout。Feature runtime 不得读取 Label/Holdout。

### Security Master

证券身份主表，记录稳定 instrument_id、交易所代码、上市/退市日期和代码变更。名称和代码可能变化，不能作为永久身份。

### Trading Calendar 与 Session

交易日历定义 session。Ref(x, 5) 表示五个交易 session 前，不是五个自然日前。个股停牌或缺行不能自动把窗口压缩成“前一条记录”。

### Historical Universe

每个历史时点真实属于研究范围的证券集合。必须包括后来退市的股票，并按当时的上市、ST、停牌、行业和可交易状态构建。

### Survivorship Bias（幸存者偏差）

用今天仍上市或仍在指数中的股票回测过去，会删除失败公司并夸大结果。本项目要求退市证券保留在历史 Universe。

### Look-ahead Bias（前视偏差）

使用决策时点以后信息，包括未来价格、未来公告、事后行业、未来指数成分、全样本确定方向等。

### Selection Bias（选择偏差）

对样本、参数或结果进行筛选，使留下的结果优于真实总体。只报告成功因子也是选择偏差。

### Corporate Action（公司行为）

分红、送股、转增、配股、拆并股和回购等改变价格、股本或现金流的事件。原始价格、事件和复权派生值必须可追溯。

### 复权因子、前复权与后复权

复权因子用于把跨公司行为的价格变为可比较序列。前复权以较新价格为锚，历史值会随新事件变化；后复权以较早价格为锚。项目不把供应商现成复权价当作永恒真相，而保存原始价、事件和有版本的复权规则。

### 公司行动一致价格（Corporate-action-consistent Price）

本项目当前用 `raw_price × adj_factor` 构造跨交易日可比价格。它只用于明确声明该口径的因子与标签，不能覆盖原始价格。若公式跨越除权日却直接使用未复权价格，分红送转造成的机械跳变可能被误认为动量、反转或隔夜信息。

### Price Return 与 Total Return

Price Return 只考虑价格变化；Total Return 还包含现金分红等股东收益。Label、组合估值和业绩比较必须声明采用哪种收益。

### 数据质量门禁

BLOCKER 会阻断依赖该数据域的正式研究；ERROR 表示严重问题；WARNING 允许继续但必须披露。基础设施失败不能解释为因子失败。

## 3. A 股市场与可交易性

### T+1

A 股普通股票当日买入通常不能当日卖出。组合模拟必须维护可卖数量，不能只用目标权重差直接成交。

### 涨跌停

证券在历史制度和板块下有不同价格限制。触及涨跌停不等于一定无法成交，但成交概率、排队和封单状态必须用明确规则建模。

### 停复牌

停牌时无正常成交。因子值可缺失或延续必须由 Spec 决定，组合不能假设以收盘价自由调仓。

### ST、退市整理期与上市天数

这些是随时间变化的证券状态。是否排除必须使用历史状态，不能用今天状态回填过去。

### Lot Size（交易单位）

A 股买入通常以一手为单位，卖出可能存在零股规则。权重到订单的转换需要处理取整和残余现金。

### Signal、Target、Order、Fill、Position

| 概念 | 含义 |
|---|---|
| Signal | 在冻结时点产生的预测或排序 |
| Target | 根据信号和约束得到的目标持仓 |
| Order | 为接近目标持仓而生成的委托 |
| Fill | 在价格、流动性和交易规则下实际成交 |
| Position | 成交后真实持仓 |

不得把“未来收益乘因子”当作最终可交易回测。

### Signal Cutoff

信号冻结时点，如盘前、开盘、收盘或盘后。使用收盘数据产生的信号不能假设以同一收盘价成交。

### Slippage、Commission、Tax、Impact

Slippage 是假设价与成交价偏差；Commission 是佣金；Tax 包括适用税费；Market Impact 是订单自身对价格的冲击。成本必须按买卖方向、历史规则、流动性和订单规模建模。

### Liquidity 与 Capacity

Liquidity 描述资产能否低成本成交；Capacity 描述策略在收益明显受冲击前可承载的资金规模。小盘因子可能统计有效但容量很低。

### Turnover

一定周期内组合买卖规模相对资产的比例。高换手会放大成本、冲击和信号衰减风险。必须明确单边还是双边口径。

## 4. 因子与 Factor Factory

### Alpha、Factor 与 Feature

Factor 是对证券在某时点的可计算特征；Feature 是模型可用输入；Alpha 通常指对未来收益有增量预测价值的信号。一个 Factor 在未经过 OOS 和成本验证前不能称为可靠 Alpha。

### Cross-sectional Factor

同一时点在多只股票之间比较的因子，如估值、动量和质量。本项目第一版以日频横截面因子为主。

### Time-series Signal

针对单一资产沿时间判断状态或方向的信号。它与横截面排名的统计假设不同，不能混用评价口径。

### FactorSpec

因子的完整契约，包括身份、版本、经济逻辑、公式或入口、数据依赖、lookback、时钟、缺失处理、方向、实现哈希、来源和测试。

### Factor Registry

因子身份登记簿。同一 factor_id 与 factor_version 不能绑定不同内容；公式改变必须发布新版本。

### Expression DSL 与 AST

DSL 是受限公式语言；AST 是解析后的结构树。项目从 AST 自动推导时间依赖并生成实现哈希，不使用任意 eval。

### Python Factor Plugin

当条件分支或复杂过程无法由 Expression DSL 清晰表达时使用的受限实现。它不是任意 Python 脚本：只能读取 FactorSpec 声明的历史 Feature，通过独立沙箱进程返回单个数值或缺失值。

### Sandbox（沙箱）

限制不可信代码权限和资源的执行边界。本项目同时使用语言白名单、独立进程、隔离模式、净化环境、空工作目录、内存/子进程限制、超时和协议校验。沙箱通过只说明已知逃逸面受到控制，不等于允许无审查的第三方任意代码。

### Computation Key

由因子实现、数据 checkpoint、Universe、日期、变体、预处理和信号钟等共同生成的内容哈希。同一键应复用同一资产；任一语义输入改变都必须产生新键，不能覆盖旧结果。

### Lookback 与 Warmup

Lookback 是一次计算所需的观测窗口；Warmup 是产生第一个有效值前需要的历史 session 数。窗口不足不得偷偷用较短样本计算。

### RAW Factor Value

公式直接计算出的原始因子值，尚未去极值、标准化、中性化或拟合方向。RAW 必须永久保留，后续处理不得覆盖。

### Factor Variant

同一 RAW 因子的不同派生版本，例如 WINSORIZED、ZSCORE、INDUSTRY_NEUTRALIZED。每个 variant 都是独立 Artifact，并保留父级血缘。

### Winsorization（缩尾）

把极端值截到给定分位点或稳健边界。阈值必须只在允许样本拟合并按截面/时间口径声明。缩尾不是删除坏数据的替代品。

M4.2 使用当日截面的 `median ± 5 × 1.4826 × MAD`。其中 MAD 是绝对偏差的中位数，1.4826 是正态分布一致性系数；MAD 为 0 时保留有限原值，避免把离散因子整列压平。

### Standardization（标准化）

常见 z-score：

    z_i = (x_i - mean(x)) / std(x)

本项目通常按交易日横截面计算。标准化改变尺度，不创造预测信息。

### Rank 与 Quantile

Rank 把原值转换为横截面次序；Quantile 将证券分入若干组。必须规定并列值、缺失值、小样本和分位边界处理。

### Neutralization（中性化）

通过回归或分组方法去除行业、市值等暴露。残差是新的 processed variant。中性化变量和参数必须是当时可知并在允许样本拟合。

M4.2 的 `SIZE_NEUTRALIZED` 是每日把去极值 Z-score 对 `log(total_mv)` 做含截距截面回归，再对残差标准化。它去除线性规模暴露，不等于已经消除所有规模、流动性或行业效应。

### Exposure（暴露）

因子或组合对行业、市值、Beta、流动性、波动率等系统性维度的敏感度。表面不同的因子可能共享同一底层暴露。

### Orthogonalization 与 Residualization

把候选因子对已有因子回归并使用残差，检查其独立信息。顺序会影响结果，不能把正交化后的改善自动解释为经济独立性。

### Conditional / Orthogonal RankIC（条件 / 正交 RankIC）

先在每日横截面把候选因子秩对冻结的控制因子集合做投影，再衡量残差与未来收益秩的关系。半偏相关只残差化候选因子；偏相关同时剔除标签对控制集合的线性关系。它回答“相对当前控制集合还剩多少秩信息”，不证明因果独立，也不等于可交易 Alpha。

### Incremental R²（增量决定系数）

把候选因子加入冻结控制集合后，横截面收益秩回归的 R² 增量。它始终非负，因此必须与有方向的正交 RankIC、稳健推断和样本暴露状态一起解释，不能单独用于晋级。

### Factor Cluster 与 Mechanical Representative

Factor Cluster 是按冻结相关距离和层次聚类规则形成的信息簇。Mechanical Representative 是由 medoid、覆盖率和固定平局规则选出的查询代表，只用于减少重复路径；它不是收益最优者，也不自动进入 Core Pool。

### Exposed Research Sample

已经被研究流程读取或参与选择的历史区间。它仍可用于诊断、证伪和生成后续假设，但不能重新包装成未见 Holdout、确认性 OOS 或独立重复证据。M4.5 的 2020～2025 全窗口属于此类。

### Coverage

某日具有有效因子值且进入目标 Universe 的证券比例。覆盖率突然变化可能意味着数据故障或因子适用范围漂移。

### Missing、Infinity 与 Outlier

缺失、无穷和异常值是三类不同问题。每个 FactorSpec 必须声明传播、置空、截断或拒绝规则，禁止评价器临时决定。

### Factor Direction

因子值越大预期收益越高或越低。若没有预先经济方向，只能在 Train 内拟合并冻结，不能看完整样本后翻转。

### Factor Decay

预测力随持有期增加而衰减的速度，也可指因子随历史时期逐渐失效。常通过不同 horizon IC、收益和换手分析。

### Crowding（拥挤）

大量资金采用相似信号，导致抢跑、估值偏离、成交冲击和集中平仓风险。逻辑仍成立时，可交易收益也可能因拥挤消失。

### Sentinel Factor

用于校准系统语义的简单因子，必须可以人工计算。它证明机器算对，不证明市场上存在 Alpha。

## 5. Label、样本与实验设计

### Label

研究希望预测的未来结果，例如下一可成交开盘到五日后收盘的总收益。Label 必须声明信号时点、入场、出场、horizon、不可交易处理和尾部截断。

### Label Release

按冻结 LabelSpec、数据血缘、Universe、日期和执行约束生成的不可变标签资产。失效标签及原因也必须保存，不能只保留“恰好能算出收益”的样本。

### Horizon

Label 从入场到出场的 session 数。1D、5D、10D、20D 是不同研究问题，也属于不同搜索尝试。

### Pearson IC 与 RankIC

Pearson IC 是同一交易日因子值与未来收益的线性相关；RankIC 是两者横截面秩的相关，对极端值通常更稳健。日度 IC 序列有时间相关性，均值不能直接配普通独立样本 t 检验。

### Quantile Return（分组收益）

按当日因子值把股票分为若干组，观察各组未来收益。高组减低组只是描述性方向诊断；没有成交限制、成本和 OOS 时不能称为可交易收益。

### Descriptive Evidence

描述当前样本中发生了什么的证据，例如覆盖率、IC 和分组收益。它没有自动解决参数搜索、多重检验、相关样本和样本外验证，因此不等于因子有效性结论。

### Overlapping Label

相邻日期的多日收益区间重叠，因此样本高度相关，不能当作独立观测。

### Train、Validation、Test

| 样本 | 允许用途 |
|---|---|
| Train | 拟合方向、参数、预处理和模型 |
| Validation | 在预注册范围内选择方案 |
| Test | 对冻结方案做一次独立评估，不参与选择 |

反复查看 Test 并修改方案后，它已经变成 Validation。

### Out-of-Sample（OOS）

在任何相关选择中未使用的样本。时间序列研究的随机切分通常会泄漏未来结构，因此采用按时间顺序切分。

### Holdout / Lockbox

受权限保护、在最终确认前不可读取的样本。任何读取都写入不可逆暴露账本；看过以后不能重新命名为“未见样本”。

### Purge

从切分边界附近删除会跨越边界的训练样本，避免其 forward label 与后段样本重叠。

### Embargo

在边界后额外留出隔离期，降低相邻样本、特征和交易状态造成的信息渗透。

### Walk-Forward

按时间滚动或扩展训练窗口，在后续区间验证，模拟研究与部署随时间推进。每个 fold 的拟合与选择只能使用当时之前的数据。

### Pseudo-OOS（伪样本外）

研究流程在读取前冻结了公式、方向和切分，但测试数据已经存在于本地历史库，且不能证明研究者此前从未受该时期影响。它比普通全样本回看严格，但证据等级低于真正未发生的前瞻 OOS。M4.4 的 WF-2025 属于首次研究读取后的 pseudo-OOS，读取后必须永久标记为已暴露。

### Holdout Exposure Ledger（样本暴露账本）

记录哪个冻结实验在什么时间、以什么目的首次读取了哪个测试区间。事件一经写入不可撤销；失败后不得删除记录或把同一区间重新命名为未见 Holdout。

### Direction Lock（因子方向冻结）

在测试前确定因子是正向还是负向。方向可来自经济假设的预先声明，或只用训练期估计；不得看 Validation/Test 后翻转符号。双侧检验显著但有向效应为负，表示预定方向被证伪，而不是“发现了有效因子”。

### Nested Walk-Forward

外层评估泛化，内层选择参数和因子。可避免使用外层测试结果调参，但计算成本更高。

### ExperimentSpec

实验冻结契约，绑定假设、Git 状态、Dataset、Universe、Factor、Label、预处理、切分、评价器、成本模型、搜索空间和随机种子。

### Reproducibility（可复现性）

相同冻结输入、代码、环境和随机种子应得到相同输出。只保存 notebook 或最终图表不等于可复现。

## 6. 因子评价与统计推断

### Information Coefficient（IC）

某日因子值与未来收益在横截面上的相关系数。Pearson IC 衡量线性相关，对极端值敏感。

    IC_t = Corr(factor_t, forward_return_t)

### RankIC

因子排名与未来收益排名的 Spearman 相关，更关注单调关系，通常比 Pearson IC 对极端值稳健。

### IC Mean、IC Standard Deviation 与 ICIR

    ICIR = mean(IC_t) / std(IC_t)

必须声明是否年化、采用什么频率和自由度。ICIR 高不代表可交易，因为还未考虑换手、成本和容量。

### IC Hit Rate

IC 与预期方向一致的日期比例。胜率需要和效应大小、样本相关性及市场状态一起看。

### Quantile/Layered Return

按因子分组后比较各组未来收益。关注组间单调性、头尾差、覆盖率和换手，而不是只看最佳一组。

### Long-Short Spread

高分组收益减低分组收益。它是诊断统计，不天然等于 A 股可执行组合，尤其需要处理融券约束和成本。

### Monotonicity

因子分组收益是否随分位有序变化。只有头尾两组有差而中间混乱，可能表示阈值效应、异常值或数据问题。

### t-statistic、p-value 与 Confidence Interval

t 值描述估计值相对标准误的大小；p-value 是在零假设成立时观察到当前或更极端结果的概率，不是“因子无效的概率”；置信区间表达估计不确定性。单个 p-value 不能证明因子有效。

### Autocorrelation

IC、收益和因子值在时间上的相关性。自相关会降低有效独立样本数，使普通标准误过于乐观。

### HAC / Newey-West

对异方差和一定阶数自相关稳健的标准误估计。滞后阶数必须预先规定或采用稳定规则，不能为提高显著性事后挑选。

M4.3 对日度 RankIC 使用 Bartlett 核和固定 5 阶滞后，以覆盖当前 5-session 重叠标签。报告的双侧 p 值使用标准正态渐近参考，因此短样本下必须与块自助法共同阅读。

### Bootstrap

通过重采样估计统计量分布。金融时间序列通常需要 block bootstrap 保留局部相关结构，不能默认逐日独立抽样。

M4.3 使用循环 moving-block bootstrap：从首尾相接的日度 IC 序列抽取连续 5 日块，拼出等长样本。置信区间来自原序列重采样均值；零假设 p 值来自中心化后的重采样分布。次数和随机种子都进入不可变规格。

### Permutation Test

在零假设下重新排列标签或信号构建经验分布。置换方式必须保持时间、横截面和行业结构中需要保留的依赖。

### Multiple Testing（多重检验）

尝试大量因子、参数、窗口、Universe 和 horizon 后，偶然优秀结果必然增多。所有相关尝试必须进入预注册检验家族，包括失败尝试。

### Family-wise Error Rate（FWER）

控制一个检验家族中至少出现一次假阳性的概率。Bonferroni 简单保守，Holm 通常更有力。

### False Discovery Rate（FDR）

控制被判定为发现的结果中，预期假发现比例。Benjamini-Hochberg 是常见方法，但相关检验结构仍需审计。

BH 调整后的 `q-value` 是在同一检验家族及排序下得到的多重检验阈值。它不是单个因子为真的概率；新增因子、窗口、Universe 或变体会改变家族，从而必须产生新的统计资产。

### 时间分段稳定性

按预先冻结的时间边界分别计算效应，检查方向是否反转、最差阶段以及阶段差异。M4.3 的三段切分只有约 58 个交易日，因此只能发现明显不稳定，不能证明“常年稳定”。

### Data Snooping 与 p-hacking

反复查看结果后调整公式、样本、方向或统计口径，直到显著。即使每次修改看似合理，整体仍构成搜索。

### Effect Size

经济效应大小，例如 IC、头尾收益差和净成本收益。统计显著但效应太小，可能没有实际价值。

### Robustness（稳健性）

结论在合理的时期、Universe、行业、horizon、参数、成本和数据源变化下是否保持。稳健性不是无限试验后挑出最好子样本。

### Regime

市场状态，如波动、流动性或风险偏好环境。事后用完整路径定义牛熊只能诊断；若用于交易，必须是 PIT 可计算模型并重新 OOS 验证。

### Conditional IC

在指定状态、行业或规模组内计算 IC，用于理解因子何时有效。条件必须预注册或明确标为事后诊断。

### Redundancy 与 Incremental Value

Redundancy 表示候选因子与已有因子提供重复信息；Incremental Value 表示在控制已有因子后仍有稳定增量。相关性低只是必要参考，不是充分证明。

### VIF

方差膨胀因子，衡量回归解释变量共线性。VIF 高表示系数不稳定，但不能单独决定因子是否淘汰。

### Suspicious Result Audit

异常高 IC、Sharpe 或收益必须先假设可能有错，检查泄漏、Universe、复权、成交、成本、搜索和 Holdout 暴露，再讨论 Alpha。

## 7. 组合、风险与绩效

### Equal Weight、IC Weight 与 ICIR Weight

等权是重要基线；IC/ICIR 加权使用历史预测力调整权重。任何动态权重只能用当时可知的训练窗口数据。

### Linear Regression、Ridge、LASSO、Elastic Net

线性回归组合因子；Ridge 用 L2 收缩稳定共线系数；LASSO 用 L1 产生稀疏选择；Elastic Net 混合二者。复杂模型必须证明相对简单基线的 OOS 增量。

### Optimization Objective

组合优化目标可能是最大化预期收益、信息比率或最小化风险，同时满足行业、风格、个股、换手和流动性约束。目标函数的漂亮解不保证输入估计可靠。

### Constraint

个股权重、行业偏离、风格暴露、总杠杆、净暴露、换手和成交占比等限制。约束应反映真实投资授权，而非事后修饰回测。

### Benchmark 与 Active Return

Benchmark 是比较基准；Active Return 是组合收益减基准收益。基准成分与权重也必须 PIT。

### Beta 与 Alpha（回归语义）

Beta 是对市场或风险因子的敏感度；回归 Alpha 是控制指定风险因子后的截距。遗漏风险变量会使“Alpha”包含未控制暴露。

### Volatility、Covariance 与 Correlation

Volatility 衡量波动；Covariance/Correlation 描述资产或因子共同变化。估计窗口、频率、缺失处理和收缩方法都会影响风险模型。

### Drawdown 与 Maximum Drawdown

Drawdown 是净值从历史峰值的回撤；最大回撤是样本期最大峰谷损失。它强烈依赖样本路径，不能仅靠一个数字外推未来。

### Sharpe Ratio

    Sharpe = mean(excess_return) / std(return)

年化必须匹配数据频率。自相关、非正态、杠杆和选择偏差都会使 Sharpe 看起来过高。

### Information Ratio

主动收益均值除以跟踪误差，用于衡量相对基准的风险调整表现。

### CAGR

复合年化增长率，反映起点到终点的几何增长，不描述中间风险。

### Attribution（归因）

把组合收益解释为市场、行业、风格、选股、交易和成本贡献。正式收益必须能追溯到订单、成交和持仓变化。

## 8. 因子生命周期与治理

### 证据路由与生命周期状态

| 状态 | 含义 |
|---|---|
| QUARANTINED_INTEGRITY_FAILURE | 存在未来数据、PIT、实现或数据完整性 blocker，禁止训练和交易 |
| STANDALONE_ELIGIBLE | 可进入独立因子/简单组合候选，不等于 CORE |
| MODEL_FEATURE_ELIGIBLE | 可进入严格隔离的模型研究，不表示单因子有效 |
| DIAGNOSTIC_ONLY | 仅用于理解、证伪或提出新假设 |
| CANONICALIZED_REDUNDANT | 默认折叠重复计算，原资产和历史证据仍保留 |
| REQUIRES_NEW_OOS | 当前结论使用过暴露样本，需要新 vintage 才能确认 |
| REJECTED / RETIRED_IN_CONTEXT | 在指定假设或上下文中未通过/停止使用，不是全局删除 |
| WATCH | 值得继续观察但不可视为核心 |
| CORE | 在指定上下文中通过规定证据门禁 |
| DECAYED | 预测力、稳定性或可交易性显著衰减 |
| RETIRED | 已停止正式使用但保留全部历史 |

状态是 factor version、Universe、horizon、variant、cost model 和 regime context 的函数，不是因子的永久全局标签。

### Promotion Gate

晋级前必须通过的确定性硬条件，例如无泄漏、严格 OOS、成本测试、容量测试和审计无 blocker。高分不能抵消硬门禁。

### Factor Health

持续监控滚动 IC、覆盖率、换手、成本、行业/风格暴露、相关性、收益集中度和容量。健康度用于降权或复验，不承诺准确预测所有失效。

### Decay、Disable、Retire

Decay 是证据恶化；Disable 是暂时禁止运行或入组；Retire 是正式退出。基础设施或数据故障应标为 INVALID_RUN，不能误判为因子衰减。

### Factor Graveyard

永久保存失败、重复、衰减和淘汰假设及其上下文证据，防止未来换名字重新挖掘同一结果。Graveyard 是研究记忆而不是物理删除：单因子弱、方向证伪、线性冗余或某个模型下无贡献，不会自动禁止该特征进入其他严格隔离的模型实验；未解除的完整性 blocker 除外。

### Factor Evidence Card / Model Evidence Card

Evidence Card 是不可变证据的可重建、只读投影。Factor Card 展示单因子质量、预测、稳定性、相关、增量和可执行性；Model Card 展示 FeatureSet、fold、基线、重要性、消融、OOS 和成交证据。展示和筛选产生研究暴露，不能把看过的数据继续称为未见 OOS。

### FeatureSetSpec

模型输入特征集合的不可变规范，记录因子版本/变体、筛选和预处理规则、cluster/canonical 关系、来源证据以及选择时已经暴露的数据。不同 FeatureSet 是不同实验输入，不能只保存最终胜出集合。

### Contextual Status

同一因子可在大盘股 5D horizon 为 CORE，在小盘股 1D horizon 为 REJECTED。任何状态必须带完整上下文。

### Audit Trail

从提案、登记、实验、失败、复验到状态变化的追加式事件链。历史事件和失败记录不得删除。

## 9. 本项目常用缩写

| 缩写 | 全称 | 项目含义 |
|---|---|---|
| PIT | Point-in-Time | 历史时点可知 |
| OOS | Out-of-Sample | 未参与相关选择的样本外 |
| IC | Information Coefficient | 因子与未来收益横截面相关 |
| ICIR | IC Information Ratio | IC 均值与波动之比 |
| FDR | False Discovery Rate | 假发现率 |
| FWER | Family-wise Error Rate | 家族至少一次假阳性概率 |
| HAC | Heteroskedasticity and Autocorrelation Consistent | 异方差自相关稳健标准误 |
| AST | Abstract Syntax Tree | 公式抽象语法树 |
| DSL | Domain-specific Language | 受限因子表达式语言 |
| ADV | Average Daily Volume/Value | 平均日成交量或成交额，使用时必须说明 |
| AUM | Assets Under Management | 管理资产规模 |
| NAV | Net Asset Value | 组合净值 |
| CAGR | Compound Annual Growth Rate | 复合年化增长率 |
| VIF | Variance Inflation Factor | 方差膨胀因子 |

## 10. 研究结论的标准措辞

为避免把统计结果写成事实，项目统一采用以下层级：

- “计算正确”：人工样例、单元测试和交叉实现一致；
- “样本内相关”：仅描述 Train 结果；
- “验证期稳定”：通过冻结 Validation，不等于最终 OOS；
- “OOS 有证据”：通过冻结 Test/Walk-Forward 统计门禁；
- “成本后可交易”：通过成交、成本、流动性测试；
- “可部署”：进一步通过容量、监控、审批和运行门禁。

禁止仅凭历史回测使用“长期有效”“稳定盈利”“不会失效”等确定性表述。

## 11. 新术语登记模板

新增概念时使用：

    ### 中文名 / English Name

    定义：
    项目口径：
    所属链路：
    公式或单位：
    常见误区：
    对应 Spec/模块：
    参考来源：

每次增加实现、数据表、评价指标、交易规则或治理状态时，应同步检查本文是否需要更新。
