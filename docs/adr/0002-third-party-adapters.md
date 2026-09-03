# ADR-0002：第三方量化框架只能通过适配器进入

- 状态：Accepted
- 日期：2026-09-01

## 背景

Qlib、jqfactor_analyzer、RD-Agent 和 AlphaGen 各自包含有价值的能力，也各自带有不同的标签、成交、数据和研究目标假设。

## 决策

Research Kernel、PIT Data Contract、FactorSpec、ExperimentSpec、Evidence Bundle、ExecutionSpec 和 Holdout 治理由本项目拥有。第三方框架只能通过明确 adapter 使用，不能成为这些核心语义的唯一来源。

## 后果

- Qlib 用于表达式/模型/workflow 对照，不拥有正式数据版本；
- jqfactor 用于数值交叉验证，不定义正式 Label；
- RD-Agent 和 AlphaGen 后期作为 proposal provider，不绕过 gate；
- adapter 必须记录外部版本和转换差异。

