# Risk Positioning Validation

本目录独立研究 **Risk Score → Position Sizing**，不接入多因子选股，不修改 Alpha 研究体系、页面或后端。

当前状态：**第一版伪OOS验证已完成。结论为混合/未通过增量择时验证，暂不接入Alpha。**

最新约束以 [04 第一版冻结规则](04_frozen_rules_v1.md) 为准。01–03保留首次审查背景；其中要求用户提供五维名称/权重、优先固定阈值归一化、将Train Fixed仅作附表的建议，已由04更新。

| 报告 | 内容 |
| --- | --- |
| [04 第一版冻结规则](04_frozen_rules_v1.md) | 用户确认的五维权重、滚动分位数、OOS基线及公平比较约束 |
| [05 实证结果报告](05_results_report.md) | 2018–2026伪OOS对比、三个核心问题及限制 |
| [06 压力与稳健性](06_stress_and_robustness_report.md) | 熊市、暴跌、高波动、震荡、反弹、换手和Bootstrap诊断 |
| [01 方案审查](01_proposal_review.md) | 原需求的合理性、关键缺口、项目现状 |
| [02 实验协议草案](02_experiment_protocol.md) | 六策略、执行时序、指标、压力测试和判定建议 |
| [03 数据与交付清单](03_data_and_deliverables.md) | 五维定义模板、数据审计、结果文件和推进顺序 |

2026-09-13。正式运行产物位于 `runs/RPV-20260913-v1/`。本次是历史伪OOS研究，不代表 `VALIDATED`、`TRADABLE` 或 `DEPLOYABLE`。
