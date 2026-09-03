# M3.3 Python 插件沙箱独立验收

验收结论：PASS。

## 冻结对象

- 因子：`conditional-close-location-python@1.0.0`；
- 插件实现哈希：`sha256:d269d15106e85010d76673f9b42fd3bda4ce342c03e544e4cc58c342a3230524`；
- 插件源码 Artifact：`sha256:320a081bdba555edb94c7825d3f081d6f47b21cdaf0269d6ca1305f304199390`；
- 沙箱策略：`restricted-python-factor-v1`；
- 真实数据输出 Artifact：`sha256:2337cc55e2365fda63400f7cb1033cab9299b233e4629157b01f77cabcec5958`。

## 验收结果

| 检查 | 结果 |
|---|---:|
| 源码与 Manifest 内容哈希 | PASS |
| FactorSpec、Catalog、插件源码实现哈希一致 | PASS |
| DuckDB 插件与因子登记一致 | PASS |
| `python -I -S`、空目录、净化环境观测 | PASS |
| Label/Holdout 构造门禁 | PASS |
| import / open / attribute / subscript / 环境变量 / while / 错误签名攻击 | 7/7 拒绝 |
| 墙钟超时杀死 | PASS |
| 同一插件版本绑定不同源码 | 拒绝 |
| 正式 M2 小窗口输入/输出 | 15/15 |
| 独立重放输出哈希一致 | PASS |

机器可读报告：`reports/m3_3_plugin_sandbox_audit.json`。

## 审计边界

本验收证明受限插件的权限、资源、身份和重放边界，没有评估预测收益。沙箱并非面向不受审查公网用户的完整虚拟机；新增 AST 语法、助手或第三方包都属于安全策略变更，必须发布新策略版本并重新进行攻击性审计。
