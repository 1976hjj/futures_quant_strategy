# M3 最小 Factor Factory 冻结规格

状态：M3.1 表达式运行时、M3.2 因子资产发布、M3.3 Python 插件沙箱已完成；M3.4 的 M2-E 依赖项待补验

## 1. 本阶段目标

M3 把因子从匿名公式变成可登记、可复算、可审计的版本化资产。M3 Phase 1 只实现表达式因子的最小可信闭环，并继续让 M2 历史回填独立运行。

## 2. 表达式边界

- 解析白名单 AST，禁止 `eval`、属性读取、下标、推导式、导入和任意函数调用；
- 允许有限数值运算 `+ - * /`、一元正负号；
- 允许 `Ref`、`Mean`、`Sum`、`Std`、`Abs`、`Log`；
- `Ref`、滚动窗口只能使用正整数常量，最大 7,560 个 session；
- 除零、非有限结果和缺失依赖统一输出 missing；
- 编译器从 AST 推导每一个 `(field, relative_session)`，必须与 `FeatureExpression.dependencies` 完全一致；
- AST 的规范化 manifest 决定 `implementation_hash`，空格等表面格式不改变实现身份。

## 3. Registry 边界

逻辑键为 `(factor_id, factor_version)`。同一键允许完全相同内容重复登记，但禁止绑定不同 Spec 或实现。Registry 同时检查：

- 声明依赖与编译依赖一致；
- lookback 和 warmup 覆盖所有历史读取；
- `implementation_hash` 与规范 AST 一致；
- FactorSpec 自身不包含 Label/Holdout 域。

## 4. Runtime 边界

FactorRuntime 只接收显式字段—数据域映射的 feature view。Label 或 Holdout 字段在构造 runtime 时即被拒绝。运行时以全局 session 轴推进；个股缺行会保留为 missing，不能把停牌或缺数日压缩成“前一交易日”。

本阶段只输出 `RAW` 因子值，不做 winsorize、标准化、中性化或方向拟合。Python 因子不进入表达式运行时，只能通过 M3.3 的受限插件运行时执行。

### 4.1 Python 插件边界

- 插件只能定义一个零参数 `factor()`，并通过 `current/history/lag/rolling_*` 等白名单助手读取已声明字段；
- 静态字段访问必须与 `FactorSpec.required_fields` 完全一致，历史窗口不得超过声明 lookback；
- 禁止 import、属性访问、下标、反射、文件、网络、环境变量、子进程、异常捕获、类、装饰器和无限循环；
- 独立进程使用 `-I -S`、净化环境和空工作目录；Windows Job Object 限制为一个进程并设置内存上限；
- 墙钟超时会杀死进程，输入行数、字节数、源码大小和 AST 节点数均有限额；
- 父进程重新验证输出键、数量、数值有限性、策略版本和进程隔离观测；
- 插件源码哈希必须与 FactorSpec 一致，同一插件 ID/版本不得绑定另一份源码。

## 5. 哨兵因子

首批五个公式仅用于验证机器语义，不代表已发现 Alpha：

1. 5-session 动量；
2. 1-session 反转；
3. 隔夜跳空；
4. 日内收益；
5. 5-session 成交量比率。

每个公式均有固定输入和人工计算结果。此阶段禁止使用未完成的真实历史库选择公式、方向或参数。

## 6. 已完成的资产与真实数据验收

- M3.2 正式 RAW 发布见 `docs/m3_2_factor_assets.md`；
- M3.3 插件源码存储于 `data/factor_store/plugin_artifacts`；
- M3.3 分支型哨兵在招商银行、平安银行和五粮液 2024-03-25 至 2024-03-29 的正式 M2 数据上输出 15/15 行；
- M3.3 独立审计见 `docs/audits/m3_3_python_plugin_verification.md`；
- M2-E 发布后补验历史行业中性化，随后进入 M4；收益有效性仍未评估。
