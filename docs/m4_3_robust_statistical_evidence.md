# M4.3 稳健统计证据与多重检验

状态：已发布并通过独立复算；结论级别为 `DIAGNOSTIC_ONLY_NOT_OOS`。

## 1. 冻结的检验家族

本阶段没有根据 M4.1/M4.2 的结果挑参数。统计规格在运行前固定为：

| 项目 | 冻结值 |
|---|---|
| 主指标 | 日度 RankIC 均值 |
| 假设 | 双侧，零假设为均值等于 0 |
| HAC | Newey–West，Bartlett 核，最大滞后 5 |
| Bootstrap | 循环 moving-block，块长 5，10,000 次 |
| 置信水平 | 95% |
| 随机种子 | `20260904`，再按变体和因子确定性派生 |
| 稳定性 | 按观测次序等量切为 3 段 |
| 多重检验 | Benjamini–Hochberg，FDR `alpha=0.05` |
| 检验家族 | 13 个因子 × 3 个变体 = 39 个假设 |

三个变体是 corrected `RAW`、`WINSORIZED_ZSCORE` 和 `SIZE_NEUTRALIZED`。它们使用相同 Label Release，防止样本变化污染横向比较。即使 RAW 与去极值 Z-score 排名几乎一致，也必须作为预先声明的不同研究路径进入同一个家族。

## 2. 发布资产

- Robustness ID：`sha256:a8368caf70682a1918fb3f2c7380e510b2b62a169db1e142a603d0d601337eaa`
- 物理目录：`data/evidence_store/robustness/a8368caf70682a1918fb3f2c7380e510b2b62a169db1e142a603d0d601337eaa/`
- `hypothesis_statistics.parquet`：39 行；
- `stability_segments.parquet`：117 行；
- `family_summary.parquet`：1 行；
- 数据库入口：`research.factor_robustness_summary`、`raw.factor_stability_segments`、`research.multiple_testing_family_summary`。

计算身份绑定三份上游 Evidence Manifest 哈希、Label Release、完整统计参数、随机种子和检验家族 ID。任意参数或成员改变都会产生新的 Robustness ID。

## 3. 当前结果

| 口径 | 最小原始 p 值 | 最小 BH q 值 | FDR 拒绝数 |
|---|---:|---:|---:|
| Moving-block bootstrap | 0.04190 | 0.50175 | 0 / 39 |
| HAC / Newey–West | 0.05288 | 0.54647 | 0 / 39 |

原始 bootstrap p 值最小的是 `book-to-price` 的去极值 Z-score 版本，但它进入完整家族后的 q 值为 0.50175，远高于 0.05。不能只报告未校正 p 值并称为发现。

“0 个拒绝”表示这 58 个交易日无法提供通过当前门禁的证据。它不证明因子永远无效，也不允许反过来缩小检验家族、改成单侧、调整 block length 或挑选某一变体后重新包装成独立发现。

## 4. 独立验收

审计程序没有调用生产统计函数，而是独立重算：

- RAW `book-to-price` 的 Bartlett HAC 方差、标准误、统计量和双侧 p 值；
- 同一序列的循环块抽样、中心化双侧 p 值和 percentile 区间；
- 全部 39 个 HAC/Bootstrap p 值的 BH 单调调整；
- 39 个唯一假设、117 个稳定性分段、概率边界、文件哈希和数据库 Manifest。

验收状态为 PASS，报告位于 `reports/m4_3_robustness_audit.json`，说明见 `docs/audits/m4_3_robustness_verification.md`。

## 5. 不能从本阶段推出什么

- 没有长期样本，因此不能声称跨市场环境稳定；
- 没有 Walk-Forward/OOS，因此不能声称泛化；
- Label 尚未纳入涨跌停、退市替代收益和交易成本；
- 相关变体下的 BH 控制仍须谨慎解释；
- 尚未完成因子冗余、增量价值、容量和组合研究。

下一步应做 M4.4：扩展冻结时间窗口和 regime 稳定性诊断，并设计严格的 Walk-Forward/OOS 契约。M2-E 发布后还需要生成新的涨跌停约束 Label，而不是修改当前证据。
