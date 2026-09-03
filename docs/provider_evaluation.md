# A股数据 Provider 决策与能力记录

状态：免费底座 + Tushare 兼容网关，真实 M2 审计进行中  
决策日期：2026-09-01

## 1. Owner 决策

项目当前用于个人、非商业研究：

1. AKShare 作为免费广覆盖采集源；
2. BaoStock 作为独立行情和历史交易状态核验源；
3. 已购买的 Tushare 兼容网关用于标准化数据和历史回填；
4. 交易所与巨潮资讯后续作为公告、公司行为和历史成员事件的原始证据源。

所有凭证只从进程环境显式注入，不进入 Git、FetchRequest、ProviderSpec、raw payload、checkpoint 或 artifact metadata。

## 2. 当前能力矩阵

| Provider | 数据域 | PIT 等级 | 当前用途 | 关键限制 |
|---|---|---|---|---|
| AKShare | 未复权日线 | `RECONSTRUCTED_PIT` | 免费主采集/备用 | 上游可能变更并覆盖历史修订 |
| BaoStock | 未复权日线、每日ST/交易状态 | `RECONSTRUCTED_PIT` | 独立核验 | 不提供完整历史修订链 |
| Tushare兼容网关 | 日线、复权、每日指标 | `RECONSTRUCTED_PIT` | 本地历史归档主源 | 自定义网关并非官方域名，必须保留购买条款 |
| Tushare兼容网关 | 财务表、财务指标、分红 | `RECONSTRUCTED_PIT` | PIT财务候选 | 仍需验证修订是否完整保留 |
| Tushare兼容网关 | 当前证券列表 | `CURRENT_ONLY` | 证券发现和退市清单 | 当前知道的退市日期不能回填成过去已知 |
| Tushare兼容网关 | 交易日历、指数权重 | `UNVERIFIED` | 候选/交叉验证 | 公告日、修订和历史覆盖尚未审计 |

`CURRENT_ONLY` 和 `UNVERIFIED` 继续由正式 PIT 发布器硬阻断。

## 3. 已验收的 Tushare 兼容接口

限量联网测试已成功调用：

- `trade_cal`、`daily`、`adj_factor`、`daily_basic`；
- `stock_basic` 退市列表；
- `suspend_d`、`stk_limit`、`namechange`；
- `income`、`balancesheet`、`cashflow`、`fina_indicator`；
- `dividend`。

这证明当前 Token 对核心接口可用，不证明数据全历史完整或套餐永不过期。

## 4. 本地归档规则

- 原始 API JSON 先进行确定性 gzip 压缩，再进入内容寻址 ArtifactStore；
- snapshot manifest 同时保存压缩 artifact hash、原始 payload hash和原始字节数；
- 读取时解压并复核原始 hash，压缩不能改变研究证据；
- 按 API/交易日建立可断点 checkpoint；
- 重复运行跳过已有分区，不重复请求；
- 默认保留至少 30 GiB 磁盘安全余量；
- 本地 raw 数据和派生数据禁止再分发。

已下载的数据在技术上可以离线复用；套餐到期后无法继续获取新增或修订数据。长期使用权仍以购买渠道和适用服务条款为准。

## 5. M2 尚未完成

- 扩大正常、ST、停牌和退市证券样本；
- 验证历史 Universe/指数调样公告日与生效日；
- 验证财务首次披露、修订和重述链；
- 对分红、送转、配股和复权因子逐笔对账；
- 验证完整交易日历和交易制度变更；
- 在上述门禁通过前，不发布“完整研究级 A 股数据集”。

## 6. 公开来源

- AKShare：https://github.com/akfamily/akshare
- BaoStock：https://pypi.org/project/baostock/
- Tushare 权限说明：https://tushare.pro/document/1?doc_id=290
- Tushare 服务协议：https://tushare.pro/document/1?doc_id=405
