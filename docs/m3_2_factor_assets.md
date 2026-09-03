# M3.2 因子资产持久化与发布

状态：表达式因子的第一版正式发布链路已完成；Python 插件沙箱属于 M3.3，不在本阶段执行。

## 1. 在整条链路中的位置

M3-A 回答“因子是什么、公式能否安全运行”；M3.2 回答“某批因子值究竟由哪版公式、哪版数据、哪个股票池和哪段时间算出，能否永久复现”。它把临时计算结果变为带身份、血缘、质量证据和缓存语义的研究资产，供 M4 Evidence Factory 使用。

```text
M2 已审计数据 + Factor Registry + ALL-A-PIT Universe
                    |
                    v
          immutable computation key
                    |
                    v
       DuckDB feature SQL -> RAW Parquet
                    |
          quality gate + file hashes
                    |
                    v
      Release Manifest + DuckDB registry
                    |
                    v
              M4 因子评价
```

## 2. 资产身份

`release_id` 等于完整请求的内容哈希，不是人工流水号。以下任一项目变化都会产生新发布，禁止静默覆盖旧文件：

- 因子 ID、版本、规范哈希、实现哈希或目录条目哈希；
- M2-A～M2-D checkpoint 哈希；
- Universe ID 与版本；
- 开始/结束日期；
- RAW/processed 变体与预处理版本；
- 计算引擎版本与信号钟版本。

同一请求再次运行时，会核验 Manifest 和 Parquet 哈希后直接命中缓存。缓存损坏或身份不一致会失败，不会将旧结果冒充新结果。

## 3. 存储与查询

物理资产：

```text
data/factor_store/releases/<release-id-without-prefix>/
|- manifest.json
|- quality_summary.json
`- raw_factor_values.parquet
```

DuckDB 对象：

- `metadata.factor_registry`：因子规范与实现登记；
- `metadata.factor_release_manifest`：发布身份、范围、文件哈希和请求正文；
- `metadata.factor_quality_summary`：逐因子覆盖率和数值范围；
- `raw.factor_values`：全部正式发布 Parquet 的原始读取视图；
- `research.factor_values_raw`：研究入口，并显式给出 `is_present`。

因子值逻辑主键为 `(release_id, session, instrument_id, factor_id, factor_version, variant)`。

## 4. 时间和缺失值规则

- 股票必须在当日 `research.universe_daily.eligible_for_signal = true`；
- 财务记录必须满足 `valid_from < signal session`，对同日披露采取保守的下一交易日可用规则；
- `available_at` 按中国时区记录为信号日 15:00；
- 滚动因子会自动读取必要的历史 warm-up，但只发布请求窗口；
- 除数、对数和数据缺口产生的无效值统一保存为 `NULL`，不做隐式填充；
- RAW 资产不包含去极值、标准化或中性化，processed 资产必须使用新的变体身份另行发布。

## 5. 首个正式发布

- 窗口：2024-01-02 至 2024-03-29；
- Universe：`ALL-A-PIT`；
- 交易日：58；
- 证券：5,250；
- 因子：13；
- 行数：3,944,590；
- 重复逻辑键：0；
- 非有限非空值：0；
- Universe 越界：0；
- 时间钟错误：0；
- Release ID：`sha256:2faab50ecfd39544d40c7c451b9f9325cdab57f075d12b6a52a50593fe18778e`。

最低覆盖率是 `earnings-yield` 的 77.90%，主要来自非正 PE 或基础数据缺失；这类缺失被保留，不能把质量门禁通过误解为因子有效。

## 6. 运行与审计

```powershell
$env:PYTHONPATH = 'src'
python scripts/publish_factor_release.py --start 2024-01-02 --end 2024-03-29
python scripts/audit_factor_release.py `
  --release-id sha256:2faab50ecfd39544d40c7c451b9f9325cdab57f075d12b6a52a50593fe18778e
```

审计报告位于 `reports/m3_2_factor_release_audit.json`。本阶段只证明计算、持久化、身份和血缘可靠；IC、分层收益、换手、成本、稳定性和 OOS 属于 M4，尚未据此筛选或晋升因子。
