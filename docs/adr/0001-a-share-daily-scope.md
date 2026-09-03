# ADR-0001：第一版限定为 A 股日频横截面研究

- 状态：Accepted
- 日期：2026-09-01

## 背景

仓库名称包含 futures，但项目章程描述的是 A 股横截面因子系统。期货和股票的数据、Universe、标签、换月、保证金和交易规则差异巨大。

## 决策

第一版只建设 A 股日频 Alpha Research OS。内部稳定标识、Spec 和 artifact 接口避免不必要的市场硬编码，但不为尚未进入范围的期货提前建设抽象。

## 后果

- P0 测试围绕 A 股 PIT、T+1、涨跌停、停牌、ST、退市和公司行为；
- Long-short 仅作为研究诊断，是否可交易必须单独证明；
- 未来增加其他市场时通过新的 market data、calendar、universe 和 execution adapter 实现。

