# M3 Phase 1 独立复核记录

状态：PASS for expression compiler and in-memory runtime slice；M3 not complete

## 已验证

- 表达式使用白名单 AST 解释执行，不调用 `eval`；
- AST 自动推导完整时间依赖，并与 FactorSpec 声明逐项对账；
- 未来/任意函数、属性、下标、推导式、关键字窗口和非白名单运算被拒绝；
- 规范 AST 哈希不受无意义空格影响，伪造 implementation hash 被拒绝；
- 同一 `(factor_id, factor_version)` 不能绑定不同内容；
- FeatureRuntime 在边界拒绝 Label/Holdout 数据域；
- 个股缺失 session 不会被压缩成较短 lookback；
- 除零、缺数和非有限结果遵循 missing 语义；
- Python 插件不能在主进程表达式 runtime 中执行；
- 5 个哨兵公式与人工计算 golden case 一致。

## 验证命令与结果

```text
python -m pytest -q
73 passed

python -m ruff check .
All checks passed
```

## 未完成边界

- 尚未发布不可变 RAW factor artifact；
- 尚未把 dataset、universe、clock 和 variant 固化到 artifact 逻辑键；
- Python plugin 独立进程、资源配额和 capability token 尚未实现；
- 尚未接入 M2 正式发布数据版本；
- 本复核不包含 IC、收益、成本、统计显著性或 Alpha 有效性结论。
