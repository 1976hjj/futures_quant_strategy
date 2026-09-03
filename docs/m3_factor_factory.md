# M3 最小 Factor Factory 冻结规格

状态：Phase 1 implemented；真实数据集成尚未开始

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

本阶段只输出 `RAW` 因子值，不做 winsorize、标准化、中性化或方向拟合。Python 因子可以登记元数据，但在独立进程沙箱完成前禁止执行。

## 5. 哨兵因子

首批五个公式仅用于验证机器语义，不代表已发现 Alpha：

1. 5-session 动量；
2. 1-session 反转；
3. 隔夜跳空；
4. 日内收益；
5. 5-session 成交量比率。

每个公式均有固定输入和人工计算结果。此阶段禁止使用未完成的真实历史库选择公式、方向或参数。

## 6. 后续工作

- 将 RAW factor artifact 发布到不可变存储，并冻结 dataset/universe/clock 逻辑键；
- 实现隔离的 Python plugin runner、资源限制和能力令牌；
- 接入 M2 正式发布的数据版本进行端到端验证；
- M3 完成后再进入 IC、收益和 Walk-Forward 等 M4 Evidence Factory。
