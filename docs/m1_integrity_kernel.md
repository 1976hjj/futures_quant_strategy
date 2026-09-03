# M1 Research Integrity Kernel 冻结规格

状态：Frozen for M1 build  
冻结日期：2026-09-01  
适用范围：M1 实现与 P0 审计测试

## 1. 目标

M1 只建立研究完整性内核，不计算真实因子、不接入真实数据源、不输出收益结论。

冻结交付物：

1. `DatasetSpec`、`UniverseSpec`、`LabelSpec`、`FactorSpec`、`ExperimentSpec`；
2. Feature、Label 与 Holdout 的类型和 capability 隔离；
3. 规范 JSON、SHA-256、实验 ID、Git 工作树身份；
4. 内容寻址且只追加的 artifact 接口；
5. Holdout capability、撤销和不可逆暴露账本；
6. 敌对合成数据与 PIT、成交时钟、标签边界审计。

## 2. 时间约定

表达式依赖使用 `relative_session`：

- `0`：决策时点已知的本期数据；
- 负数：历史数据；
- 正数：未来数据。

`FeatureExpression` 只允许 `relative_session <= 0`，且禁止 Label/Holdout 数据域；
`LabelExpression` 必须至少包含一个未来依赖，只能交给持有 Label capability 的运行时。

所有 datetime 必须带时区。规范化时统一转换为 UTC，禁止 NaN、Infinity 和依赖本地时区的时间。

## 3. 内容身份

- 内容哈希：`sha256:<64 lowercase hex>`；
- 实验 ID：`EXP-YYYYMMDD-XXXX`；
- 同一对象的规范 JSON 字节必须跨字典插入顺序稳定；
- artifact payload 与 manifest 分别内容寻址；
- 已存在路径只能验证相同内容，不允许覆盖。
- 同一 `experiment_id` 只能绑定一个 ExperimentSpec manifest；任何修改必须使用新 ID。

Git 身份必须包含 commit、dirty 标记、porcelain 状态条目及包含未跟踪文件内容的工作树指纹。

## 4. 权限模型

capability 由单一 authority 签发、登记、校验和撤销。调用者自行构造同形对象不产生权限。

- Feature runtime 无 Label/Holdout scope；
- Label runtime 必须持有 `READ_LABEL`；
- Holdout read 必须持有绑定 actor 和 vintage 的 `READ_HOLDOUT`；
- capability 和 authority 禁止序列化到子进程；
- 每次成功读取 Holdout 必须先追加暴露事件，再返回内容。

M1 的 Holdout vault 为 authority 进程内存对象，避免向普通研究进程暴露文件路径。持久化加密 vault、独立服务身份和 OS/container 隔离属于 M10 的部署加固范围，不能把 M1 的应用层 capability 宣称为操作系统级安全边界。

## 5. P0 blocker 代码

| 代码 | 规则 | 含义 |
|---|---|---|
| `FEATURE_FUTURE_ACCESS` | RULE-001/005 | Feature 声明未来依赖 |
| `FEATURE_DOMAIN_ACCESS` | RULE-005 | Feature 读取 Label/Holdout 域 |
| `PIT_NOT_AVAILABLE` | RULE-002 | 信号包含 cutoff 后才可用的记录 |
| `AVAILABILITY_PRECEDES_PUBLICATION` | RULE-002/004 | 可用时间早于公开时间 |
| `PIT_NOT_INGESTED` | RULE-002 | 严格接收模拟包含尚未摄取记录 |
| `SAME_CLOSE_EXECUTION` | RULE-003 | 收盘信息按同一收盘成交 |
| `EXECUTION_BEFORE_INFORMATION` | RULE-003 | 成交早于信息可用时点 |
| `LABEL_BOUNDARY_OVERLAP` | RULE-013 | forward label 跨 split 边界 |
| `HOLDOUT_ACCESS_DENIED` | RULE-016/035 | capability 缺失、错误或已撤销 |
| `ARTIFACT_IMMUTABILITY` | RULE-027 | 已有内容地址出现不同字节 |

每个 finding 必须包含 `audit_id`、`rule_id`、严重级别、可定位对象和结构化 evidence。

## 6. 完成门禁

以 `docs/milestone_gates.md` 的 M1 Gate 为权威。测试通过只是必要条件；最终还要独立复算 manifest/hash、核对 dirty tree、检查敌对样本命中的 blocker，并记录已知限制。
