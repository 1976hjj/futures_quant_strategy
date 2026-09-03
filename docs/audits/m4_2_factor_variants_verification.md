# M4.2 公司行动修正与 processed 资产独立验收

状态：PASS  
验收日期：2026-09-04

## 验收范围

- 三个跨日价格因子由 v1 切换到公司行动一致的 v2，旧值未被覆盖；
- corrected RAW、去极值 Z-score、规模中性化发布身份与父级血缘；
- Parquet 哈希、数据库 Manifest、重复键、非有限值与覆盖率；
- 横截面中心/尺度、规模正交性和独立 Python 重算；
- 三套描述性 Evidence Bundle 的变体身份和文件哈希。

## 结果

Q1 数据存在 18 个复权因子跳变。新定义相对旧定义改变 509 个动量值、84 个短期反转值和 18 个隔夜跳空值。三个旧 v1 已在 `metadata.factor_version_disposition` 登记为 `SUPERSEDED_DIAGNOSTIC`，三个 v2 已进入不可变 Factor Registry。

两个 processed 发布均为 3,944,590 行、3,868,076 个非空值。逐日 Z-score 均值与标准差误差处于 `1e-14` 以下，规模中性版本对当日 `log(total_mv)` 的最大残余相关为 `3.44e-14`。独立 Python 完整截面重算与 DuckDB 物化值在浮点误差内一致。

## 结论边界

PASS 只说明口径、计算、血缘和描述性证据链符合冻结规格，不说明任何因子可投资。行业中性化因缺少已发布的 M2-E 历史分类而未执行；当前行业倒填被明确禁止。Q1 仍是开发期短样本，全部统计保持 `DESCRIPTIVE_ONLY_NOT_OOS`。
