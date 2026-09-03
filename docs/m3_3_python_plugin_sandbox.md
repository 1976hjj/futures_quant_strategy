# M3.3 受限 Python 因子插件沙箱

状态：第一版已完成并通过真实数据重放与攻击性审计。

## 1. 为什么需要它

Expression DSL 适合简单、透明、容易审计的公式，但无法自然表达所有条件分支和复杂过程。M3.3 提供第二条执行路径，同时坚持两个边界：插件只能看到明确授权的历史 Feature；插件的代码、输入和输出都不能绕过 Research Kernel。

这不是通用 Python 托管平台，也不承诺安全运行完全任意的第三方 Python 包。插件必须先通过受限语言验证。

## 2. 多层控制

| 层 | 控制 |
|---|---|
| 源码 | 只允许一个零参数 `factor()`；AST 节点和源码大小有限额 |
| 数据 | 字段必须是字面量，并与 `FactorSpec.required_fields` 完全一致 |
| 时间 | `lag/history/rolling_*` 不得超过声明 lookback，不提供未来读取助手 |
| 权限 | Label/Holdout 域在父进程构造 runtime 时即被拒绝 |
| 语言 | 禁止 import、attribute、subscript、反射、文件、网络、环境变量、类和异常逃逸 |
| 进程 | 使用独立 `python -I -S`、空工作目录和仅含运行所需项的环境 |
| 资源 | Windows Job Object 限制一个进程和内存；另有墙钟超时、行数及字节预算 |
| 协议 | 父进程核验沙箱策略、隔离标志、环境、输出键、行数、类型和有限值 |
| 身份 | FactorSpec 实现哈希绑定完整插件契约；同 ID/版本源码不可覆盖 |

插件可使用的核心助手包括：`current`、`lag`、`history`、`rolling_mean`、`rolling_sum`、`rolling_std`、`safe_div`、`is_missing`、`log`、`sqrt` 和 `exp`。

## 3. 持久化和登记

- 源码 Artifact Store：`data/factor_store/plugin_artifacts`；
- 插件登记：`metadata.python_plugin_registry`；
- 因子登记：`metadata.factor_registry`；
- 真实小窗口证据：`artifacts/m3_3_plugin_slice`；
- 机器审计：`reports/m3_3_plugin_sandbox_audit.json`。

插件 ID 与版本绑定源码 Artifact、Manifest、实现哈希、入口和沙箱策略。源码变化必须使用新版本。

## 4. 真实数据验收

哨兵因子 `conditional-close-location-python@1.0.0` 使用条件分支，把收盘价在当日振幅中的极端位置映射为 `-1/0/1`。它用于证明 DSL 之外的执行路径，不代表已经发现 Alpha。

- 数据：正式 M2 `research.universe_daily` 与 `research.market_daily`；
- 证券：招商银行、平安银行、五粮液；
- 日期：2024-03-25 至 2024-03-29；
- 输入/输出：15/15 行，非空 15 行；
- 冻结输出 Artifact：`sha256:2337cc55e2365fda63400f7cb1033cab9299b233e4629157b01f77cabcec5958`；
- 独立重放输出哈希完全一致；
- 7 类逃逸探针全部被拒绝；
- 超时攻击测试确认工作进程会被杀死。

## 5. 当前边界

- 不允许 NumPy、pandas 或任意第三方包；需要这些能力时应优先扩充受审计助手，而不是开放 import；
- 不允许插件直接查询 DuckDB或读取 Parquet，数据必须由父进程最小化后传入；
- 当前完成的是单机研究沙箱，不是面向恶意公网租户的容器平台；
- 插件因子仍须进入 M4 完整评价，不能因为成功执行而晋升为 VALIDATED 或 CORE。
