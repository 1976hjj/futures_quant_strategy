# M1 独立复核记录

复核日期：2026-09-01  
复核对象：冻结的 M1 接口、实现和敌对测试  
规格基线：`docs/m1_integrity_kernel.md`

## 1. 结论

M1 Gate 通过。没有遗留 BLOCKER 或 MAJOR finding，可以进入 M2 数据源能力评估与 PIT Data Factory。

本结论只证明 M1 研究完整性边界按冻结规格工作，不证明任何真实数据、因子或策略有效。

## 2. 门禁证据

| Gate | 独立证据 | 结果 |
|---|---|---|
| Feature/Label 隔离 | 正数 future dependency、Label/Holdout domain、伪造 model copy 和无 capability Label runtime 均被拒绝 | PASS |
| manifest/hash | 使用标准库 `hashlib` 从 canonical bytes 独立复算 payload 与 manifest SHA-256 | PASS |
| dirty tree | 临时 Git 仓库分别验证 clean、未跟踪内容变化和 fingerprint 变化；当前仓库捕获为 dirty | PASS |
| 敌对数据 | 提前披露、cutoff 后可用、延迟摄取、同收盘成交和信息前成交均命中具名 blocker | PASS |
| Holdout | 无 capability、actor 错绑、伪造、撤销和跨进程序列化均被拒绝；成功读取先写 hash-chain ledger | PASS |
| purge 定位 | Train→Validation 与 Validation→Test 的跨界样本均返回 sample ID、边界和 label end | PASS |
| Experiment 不可变 | 相同 ID+相同 Spec 幂等；相同 ID+修改 Spec 触发 `ARTIFACT_IMMUTABILITY` | PASS |
| 静态质量 | Ruff lint/format、Python compile、TOML parse | PASS |

最终验证命令：

```powershell
ruff check .
ruff format --check .
python -m pytest -q
python -m compileall -q src tests
```

## 3. 审计中发现并修复

### M1-FINDING-001：已构造模型可能绕过嵌套校验

- 等级：BLOCKER（已关闭）
- 风险：调用 `model_copy(update=...)` 可构造未经 validator 重新执行的对象；
- 修复：所有 FrozenSpec 开启 instance revalidation，Feature/Label runtime 在信任边界再次 `model_validate`；
- 回归：伪造 future dependency 测试。

### M1-FINDING-002：实验逻辑 ID 未绑定唯一规格

- 等级：BLOCKER（已关闭）
- 风险：只做内容寻址时，同一 `experiment_id` 可能对应多个不同内容对象；
- 修复：增加只追加 experiment identity binding；
- 回归：相同 ID 修改 hypothesis 必须冲突。

### M1-FINDING-003：artifact 引用字段可与 manifest 不一致

- 等级：MAJOR（已关闭）
- 风险：调用者自行构造的 `ArtifactRef` 可能谎报大小或媒体类型；
- 修复：读取 manifest 时交叉验证 artifact ID、字节数和媒体类型。

## 4. 已知边界

1. M1 的表达式 `formula` 是不可执行说明字段；系统只执行 typed dependency 门禁。公式 AST、编译和“公式与依赖声明一致”检查属于 M3。
2. Holdout vault 当前只存在于 authority 进程内存，capability 与 vault 禁止序列化。持久化加密、独立服务身份及 OS/container 隔离属于 M10。
3. Artifact 和 exposure ledger 能检测同用户外部篡改，但本地文件系统的强制只追加权限需要正式部署环境提供。
4. M1 敌对数据为人工固定小样本；真实 Provider 的披露修订、退市、ST、公司行为和历史交易规则审计属于 M2。
5. 实验 ID 的四位 Base32 后缀需要后续 registry 做原子冲突重试；M1 已保证 ID 一旦注册不能绑定第二份规格。

以上边界均禁止被解释为已完成 M2、M3 或 M10 能力。
