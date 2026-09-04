# Milestone 验收与独立审计门禁

## 1. 通用完成定义

一个 Milestone 只有同时满足以下条件才算完成：

- 规格在实现前已冻结；
- 实现没有绕过 Research Constitution；
- 单元、集成、性质和回归测试按风险覆盖；
- 输出绑定代码、数据、配置和环境版本；
- 已知限制进入结构化记录；
- 独立验证先产生 findings；
- blocker 已关闭或 Milestone 明确判定失败。

“代码可以运行”不是完成定义。

## 2. 审计角色分离

### Builder

- 按冻结规格实现；
- 编写正常路径和失败路径测试；
- 不自行降低 gate。

### Verifier

- 从接口契约和测试向实现追踪；
- 使用固定输入复算关键结果；
- 检查可复现性和 artifact 哈希；
- 不直接修改被验证 artifact。

### Statistical Auditor

- 假设优秀结果来自错误或选择偏差；
- 检查 PIT、Universe、标签、成交、复权、缺失、参数搜索、多重检验和 OOS；
- 使用合成反例、扰动、负控制和替代实现；
- 先提交 findings，再创建修复任务。

同一工具可以承担不同角色，但审计应使用新的任务上下文、冻结输入和不同的验证方法。角色提示本身不构成独立性。

## 3. 审计结果等级

- `BLOCKER`：结果不可使用，相关研究必须停止；
- `MAJOR`：核心结论不可靠，禁止晋级；
- `MINOR`：不改变主要结论，但必须跟踪；
- `NOTE`：改进建议。

## 4. 固定审计清单

- `AUDIT-DATA-PIT`
- `AUDIT-UNIVERSE-SURVIVORSHIP`
- `AUDIT-CORPORATE-ACTION`
- `AUDIT-FACTOR-LOOKAHEAD`
- `AUDIT-LABEL-ALIGNMENT`
- `AUDIT-SIGNAL-EXECUTION`
- `AUDIT-PREPROCESS-FIT`
- `AUDIT-WALK-FORWARD`
- `AUDIT-MULTIPLE-TESTING`
- `AUDIT-COST-LIQUIDITY-CAPACITY`
- `AUDIT-STYLE-EXPOSURE`
- `AUDIT-HOLDOUT-ACCESS`
- `AUDIT-REPRODUCIBILITY`

## 5. M0 Gate

- [x] 项目范围明确为 A 股日频；
- [x] 参考项目固定到 commit；
- [x] 每个项目列出可借鉴、不可照搬和代码风险；
- [x] 架构、数据和实验契约一致；
- [x] 研究宪法有 Markdown 和机器可读版本；
- [x] 后续 Milestone 有完成定义；
- [x] 最小骨架不存在虚假业务实现；
- [x] 当前工作树已有用户改动被保留。

## 6. M1 Gate

- [x] Feature/Label 类型和运行权限隔离；
- [x] manifest 和哈希可复算；
- [x] dirty tree 被记录；
- [x] 敌对合成数据能触发预期 blocker；
- [x] 未授权进程不能读取 Holdout；
- [x] 重叠标签跨边界时 purge 测试失败后可定位。

M1 证据、审计结论和边界见 `docs/audits/m1_verification.md`。

## 7. M2 Gate

- [ ] Provider 能力和许可证记录完整；
- [ ] raw snapshot 不可变；
- [ ] PIT as-of 查询有黄金样例；
- [ ] 退市、ST、停牌、股票池、行业和财务修订可按历史重放；
- [ ] 公司行为和收益对账；
- [ ] Data Audit blocker 能阻断依赖实验。

## 8. M3/M4 Gate

- [ ] 每个因子有 metadata、实现哈希和人工样例；
- [ ] RAW 不被预处理覆盖；
- [ ] 因子方向不使用 Validation/Test；
- [ ] IC 对齐用手算和外部实现交叉验证；
- [ ] 不同 horizon/variant 纳入预注册检验家族；
- [ ] 异常高结果自动生成 Suspicious Result Audit；
- [ ] 失败实验进入不可删除记录。

## 9. M5 以后附加门禁

- 批量因子导入必须逐公式验证；
- 任意发布因子可以自动生成可追溯的 Factor Evidence Card，不需要逐因子编写报告代码；
- 单因子不显著、方向证伪或线性冗余不得自动产生跨模型永久淘汰；
- 只有 PIT、未来数据、实现或不可修复数据错误可以阻止特征进入模型研究；
- FeatureSet 必须版本化，且记录筛选使用的数据范围和暴露状态；
- 预处理、去重、筛选和调参必须在对应 Train/Validation fold 内拟合；
- 动态选择必须与静态基线做相同 OOS 比较；
- LightGBM 等复杂模型必须证明相对简单基线的稳定增量；
- 模型报告必须提供 fold 特征选择、重要性、消融、不确定性和最差 fold，不得只展示最优汇总；
- SHAP、importance 或单次消融不能单独作为 Alpha 或因子生命周期结论；
- Portfolio 收益必须可以追踪到订单和成交；
- Holdout 暴露后永久登记；
- Paper Trading 与历史回放共享核心业务逻辑。
