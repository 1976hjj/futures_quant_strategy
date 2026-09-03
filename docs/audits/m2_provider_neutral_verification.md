# M2 Provider-neutral 竖切片复核

复核日期：2026-09-01  
状态：PASS for synthetic vertical slice; M2 not complete

## 1. 已通过

- Provider capability 与 license 都有冻结、可哈希契约；
- 禁止未声明字段、禁止许可证不允许的 raw/derived storage；
- ProviderSpec、license、request、原始 payload 和 snapshot manifest 分别内容寻址；
- 财务更正只在实际可用时间后替换旧版本；
- 历史 ST 和退市 Universe 按有效时间与知识时间回放；
- OHLC、raw lineage、record hash、公司行为参考价和 survivorship blocker 能阻断发布；
- `CURRENT_ONLY`/`UNVERIFIED` capability 不能发布正式 PIT Dataset；
- Parquet partition、DatasetSpec 和 dataset version 均不可原地覆盖；
- 重复发布相同内容幂等。

## 2. 合成数据覆盖

固定样本包含 11 条记录、7 个数据域：

- 正常上市与后来退市证券；
- 历史 Universe 成员；
- 非 ST→ST 状态变化；
- 原始日线；
- 财务首次披露与后续重述；
- 分红送转参考价；
- 交易日历。

## 3. 尚未通过，因此 M2 不能关闭

- 没有项目所有者确认的真实 Provider、凭证、套餐和许可证；
- 没有真实退市/ST/指数调样/财报重述/公司行为审计包；
- 没有真实公司行为现金流与持仓收益完整对账；
- 没有证明首个真实 Provider 是否回填或覆盖历史修订；
- 没有正式交易日历完整性和跨市场特殊交易安排验证。

下一门禁是 `docs/provider_evaluation.md` 中的 Provider 选择与小型真实审计包，不能以合成测试替代。

## 4. 2026-09-01 真实源进展

Owner 已选择 AKShare + BaoStock 免费底座，并提供一个已购 Tushare 兼容网关用于个人研究。日线、复权、每日指标和核心财务接口已完成限量权限探测；压缩 raw snapshot 和断点归档已通过重复运行验收。该进展不等于财务修订、退市 Universe、公司行为和完整交易日历门禁已经关闭。
