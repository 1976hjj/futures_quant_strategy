# M4.2 公司行动修正与 processed 因子资产

状态：已发布并通过独立数值审计；证据仍为 `DESCRIPTIVE_ONLY_NOT_OOS`。

## 1. 为什么要产生新版本

M3 首版的 20 日动量、5 日反转和隔夜跳空直接使用跨日未复权价格比。除权除息会令原始价格机械跳变，因此这三个 v1 只能保留作系统诊断，不能继续作为候选解释。M4.2 不覆盖历史，而是登记如下替代关系：

| 因子 | 旧版本 | 新版本 | 新公式 |
|---|---:|---:|---|
| `price-momentum-20` | 1.0.0 | 2.0.0 | `adjusted_close / Ref(adjusted_close, 20) - 1` |
| `short-reversal-5` | 1.0.0 | 2.0.0 | `-(adjusted_close / Ref(adjusted_close, 5) - 1)` |
| `overnight-gap-1` | 1.0.0 | 2.0.0 | `adjusted_open / Ref(adjusted_close, 1) - 1` |

`adjusted_open/close = raw open/close × adj_factor`。日内价格比与基于供应商除权参考价 `pre_close` 的日收益不跨越错误价格口径，因此保持 v1。

Q1 窗口发现 18 个复权因子跳变事件。新旧定义分别改变 509 个动量值、84 个短期反转值和 18 个隔夜跳空值。这个计数证明修正确实生效，不代表新版本收益更好。

## 2. 三层不可变资产

| Variant | Release ID | 行数 | 非空数 |
|---|---|---:|---:|
| `RAW` | `sha256:85655b0bb661e845df51c0e20aab0223afadac4cee45c9f2e5a6d4c8d42c2aa9` | 3,944,590 | 由各因子 warmup/源数据决定 |
| `WINSORIZED_ZSCORE` | `sha256:6e9bc1fd807f5f20fcd24fdc9846d1a4554570d8e92823636f33313d6fccd73b` | 3,944,590 | 3,868,076 |
| `SIZE_NEUTRALIZED` | `sha256:68f0c0a8a39921382325792e653b20daff8771d4294cd68be36b3bbc3afa2de2` | 3,944,590 | 3,868,076 |

两个 processed 发布都直接绑定修正 RAW 的发布 ID、Parquet 哈希、数据 checkpoint、Universe、日期与完整预处理规范。修改 MAD 阈值、最小截面数或中性化变量都会产生新的计算键。

## 3. 预处理语义

每个交易日、每个因子独立执行：

1. 用 `median ± 5 × 1.4826 × MAD` 缩尾，MAD 为 0 时保留有限原值；
2. 用总体标准差做横截面 Z-score，少于 20 个有效值或标准差为 0 时输出缺失；
3. 规模中性版本把 Z-score 对当日 `log(total_mv)` 做含截距 OLS；
4. 对残差再次 Z-score；市值缺失的证券保持缺失。

行业中性化没有启用。只有 M2-E 的历史申万成员以 PIT 资产发布后，才会产生新的 `INDUSTRY_SIZE_NEUTRALIZED` 变体。

## 4. 验收结果

- 去极值 Z-score 的逐日均值绝对误差最大 `6.11e-15`，标准差偏离 1 最大 `3.11e-15`；
- 规模中性值与 `log(total_mv)` 的逐日线性相关绝对值最大 `3.44e-14`；
- 独立 Python 对单日完整截面重算，两个变体最大值误差分别为 `8.88e-16` 和 `3.72e-15`；
- 三个 Evidence Bundle 均使用同一 corrected RAW 标签发布，避免标签样本差异污染比较；
- 所有资产哈希、父级血缘、数据库登记、重复键和非有限值门禁通过。

审计入口为 `scripts/audit_m4_2_factor_variants.py`，审计记录见 `docs/audits/m4_2_factor_variants_verification.md`。

## 5. 证据边界

三套证据仍只覆盖 2024 年一季度，且标签尚未加入涨跌停价、退市替代收益和交易成本。它们可以比较“同一短窗口内不同口径的统计变化”，不能用于认定因子有效、选择最佳预处理、声称样本外稳定或晋级 Core Pool。下一步应进入 M4.3：HAC/block bootstrap、时间稳定性与多重检验；涨跌停约束在 M2-E 发布后补验。
