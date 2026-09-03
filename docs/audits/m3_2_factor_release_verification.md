# M3.2 因子资产发布独立验收

验收结论：PASS。

## 冻结对象

- Release ID：`sha256:2faab50ecfd39544d40c7c451b9f9325cdab57f075d12b6a52a50593fe18778e`；
- Manifest 哈希：`sha256:1bbf013a4cc0c8645861c80010a2ced8556c75f42949587fdf087c53b6d2ce03`；
- Parquet 哈希：`sha256:6e7b9b576c22332899b02597a0bbd007ecb5cbf3da69f9935e9dac3698454c6f`；
- 范围：2024-01-02 至 2024-03-29，ALL-A-PIT，13 个因子。

## 验收结果

| 检查 | 结果 |
|---|---:|
| Parquet 与 Manifest 哈希一致 | PASS |
| 质量摘要与 Manifest 哈希一致 | PASS |
| DuckDB 发布登记与 Manifest 一致 | PASS |
| 行数 3,944,590 / 58 日 / 5,250 证券 / 13 因子 | PASS |
| 重复逻辑键 | 0 |
| 非有限非空值 | 0 |
| ALL-A-PIT 越界记录 | 0 |
| 信号钟日期错误 | 0 |
| M2-A～M2-D 当前 checkpoint 血缘一致 | PASS |
| 13 个因子均存在于 Registry | PASS |
| 相同请求再次执行命中缓存 | PASS |

机器可读证据：`reports/m3_2_factor_release_audit.json`。

## 审计边界

本验收没有读取未来标签，也没有检验收益有效性。`quality_status=PASS` 仅表示资产结构、数值有限性、Universe、时间和血缘满足发布条件，不表示任何因子具有预测能力。
