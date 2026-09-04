# Factor Evidence Explorer 前端设计

状态：M4.7 MVP 已实现 React 首版；当前展示 M4.1～M4.5，M4.6/M6 面板等待对应资产发布。

## 1. 产品定位

Factor Evidence Explorer 是本地、只读、可重建的研究结果浏览器。它回答：

- 某个因子已经计算、处理和检验过什么；
- 在哪个 Universe、horizon、variant、fold 和数据 vintage 下得到什么结果；
- 它与其他因子是否重复、互补或只在特定 Regime 有表现；
- 它是否具备描述性、稳健性、可执行性和模型贡献证据；
- 哪些指标尚未产生、哪些样本已经暴露、每个数字来自哪个不可变资产。

Explorer 不负责计算新统计、不改变生命周期、不自动组策略、不把单因子排成“最终优胜榜”。它是 Evidence Plane 的展示投影，方便研究者形成下一轮 FeatureSet/模型假设。

## 2. MVP 技术方案

第一版由证据快照生成器和独立 React 应用组成：

```text
DuckDB views + immutable manifests
              |
              v
factor evidence snapshot (JSON + manifest)
              |
              v
frontEnd/ React + TypeScript + Vite
  sync-factor-data.mjs -> public/data/factor-explorer.json
```

同时保留内容寻址的静态报告 bundle 作为不可变审计产物。React 项目是日常交互入口，静态 bundle 是特定报告版本的可重建快照，两者使用同一数据契约。

选择这种分层的原因：

- 不引入常驻后端、账号、权限和部署复杂度；
- 可以随 Evidence Asset 重建并做内容哈希；
- 页面只读，不存在浏览器写坏数据库的路径；
- React 项目可以逐步增加路由、图表、模型页和更大规模的数据加载能力；
- 证据生产与 UI 发布解耦，前端改版不会改变 Evidence Asset；
- 本地开发、生产构建和后续远程部署使用同一应用代码。

首版不使用 CDN，字体、样式和脚本均由 Vite 本地打包。浏览器不直接连接 DuckDB，也不读取 `secrets/`、Tushare Token 或原始供应商响应。

React 开发环境启动：

```powershell
cd D:\futures_quant_strategy\frontEnd
npm.cmd install
npm.cmd run dev
```

访问 `http://127.0.0.1:5173`。`predev` 会自动从 `reports/factor_explorer/latest.json` 找到最新报告并同步数据，无需手工复制 JSON。

生产构建：

```powershell
cd D:\futures_quant_strategy\frontEnd
npm.cmd run build
npm.cmd run preview
```

生成命令：

```powershell
python scripts\build_factor_explorer.py `
  --config config\factor_explorer_current.json
```

输出：

```text
reports/factor_explorer/<report_id>/
  manifest.json
  index.html
  app.css
  app.js
  data.js
  evidence-summary.json
```

`report_id` 绑定所有上游 Evidence/Label IDs、Manifest 哈希、生成器版本和语义展示配置。页面文件哈希记录在报告 Manifest 内；报告可以删除后重建，正式事实仍以 Evidence Asset 和 DuckDB 视图为准。`reports/factor_explorer/latest.json` 只是指向最新构建的便利指针，不参与报告身份。

当前首版报告：

- Report ID：`sha256:e86eab258a0e905f0e5e4296fe8ba943ef4dc8790a4898a4d1e514cb0ab9b17f`
- 13 个因子、39 条 factor×variant 路径、25 条 canonical 路径、10 个 cluster；
- 117 条 fold 记录、468 条 Regime 记录、741 对关系、25 条增量证据；
- 14 条 duplicate 路径不复制增量结论，只显示对应 canonical 路径的引用；
- M4.6 Execution 与 M6 Model Contribution 全部为 `NOT_AVAILABLE`；
- 独立静态审计为 `PASS_WITH_FINDINGS`，详见 `docs/audits/m4_7_factor_explorer_verification.md`。

## 3. 全局上下文栏

每个页面顶部固定显示当前研究上下文：

- dataset vintage；
- Universe；
- label/horizon；
- signal/entry/exit clock；
- execution constraint level；
- factor release 与 variants；
- Walk-Forward/Redundancy Evidence ID；
- 样本分类：`DESCRIPTIVE`、`RETROSPECTIVE`、`PSEUDO_OOS_EXPOSED`、`TRUE_OOS`；
- 报告生成时间和代码版本。

上下文不完整时禁止跨报告直接比较。用户切换上下文后，页面上的所有指标、状态和来源链接必须同步切换，不能混用不同 label 或 Universe。

## 4. 页面结构

### 4.1 因子总览

默认首页展示全库概况和可筛选表格。

摘要卡：

- 因子数、factor×variant 路径数；
- 数据完整性隔离数；
- standalone/model-feature/diagnostic 路由数；
- canonical 路径数和 cluster 数；
- 已具有 execution-aware 证据的比例；
- 当前报告涉及的已暴露区间。

因子表默认字段：

| 字段 | 含义 |
|---|---|
| Factor | ID、版本、variant、family |
| Coverage | 平均覆盖率和缺失警告 |
| RankIC | 均值、HAC/bootstrap 区间与证据等级 |
| Walk-Forward | 各 fold 的方向支持/证伪/未拒绝 |
| Stability | 最差 segment/fold 与同号比例 |
| Redundancy | cluster、canonical/duplicate、代表项 |
| Incremental | 条件/正交 RankIC 与增量 R² |
| Execution | gross/net、换手、成本、容量；未完成时显示 `NOT_AVAILABLE` |
| Routes | 可并存的证据路由标签 |

筛选项：factor family、source、variant、coverage、cluster、direction outcome、route、fold、Regime、execution availability。支持搜索、排序和列显示设置，但默认不提供把不同量纲揉成一个“总分”的排行榜。

### 4.2 单因子详情

详情页按以下顺序组织：

1. 身份与经济假设：FactorSpec、来源、公式、版本、方向和血缘。
2. 数据质量：覆盖率、缺失、异常值、分布与时间漂移。
3. 单因子画像：RankIC 时间摘要、分组收益、换手和 horizon 衰减。
4. Walk-Forward：Train/Validation/Test 分栏、每 fold 结果和最差 fold。
5. Regime：趋势、波动、规模、行业和流动性条件表现。
6. 冗余/增量：相关性最高邻居、cluster、canonical 路径、条件/正交 RankIC。
7. 可执行性：M4.6 的费用、滑点、成交失败、容量和资金规模敏感度。
8. 模型贡献：后续 M6 的选择频率、permutation、SHAP 和消融；MVP 仅保留明确占位。
9. Evidence lineage：全部 asset IDs、Manifest、计算窗口、审计状态和 limitations。

任何统计显著结果旁边必须同时展示方向、样本分类、FDR family 和限制条件。`DIRECTION_CONTRADICTED` 不使用“失败因子”标题；它表示原方向被证伪，但仍可能是无方向模型特征。

### 4.3 多因子对比

MVP 允许选择最多 6 条 factor×variant 路径并排比较：

- 覆盖率、RankIC、最差 fold、换手和成本；
- factor-value/IC 相关矩阵；
- cluster 与 canonical 状态；
- Regime 互补性；
- 条件/正交 RankIC；
- 后续模型选择频率和消融贡献。

页面可以导出 `FeatureSetSpec` 草案 JSON，但不能直接注册、训练或晋级。正式使用前必须通过 CLI 校验并登记选择理由、来源报告、上游 Evidence IDs 和已暴露区间。

### 4.4 Cluster 视图

- cluster 列表、成员数、代表项和平均距离；
- 层次聚类树的简化可视化；
- 簇内 factor-value/IC 相关；
- duplicate/canonical 折叠原因；
- 点击成员跳转到详情或加入对比。

Cluster 代表项只用于导航和默认计算路径，不使用奖杯、冠军或晋级视觉语言。

### 4.5 Model 页面（M6 后启用）

- 模型与冻结基线的 Walk-Forward 对照；
- 每 fold FeatureSet、参数、预测指标和 execution-aware PnL；
- 特征选择频率、重要性、SHAP 和消融；
- 最差 fold、Regime、成本和容量；
- Model Evidence ID、暴露账本和审计 findings。

## 5. 页面线框

```text
+--------------------------------------------------------------------------------+
| Alpha Research OS / Factor Explorer | Context: ALL-A · 5D · 2020-2025 EXPOSED |
+--------------------------------------------------------------------------------+
| 39 paths | 25 canonical | 10 clusters | 0 integrity blockers | M4.6 pending    |
+--------------------------------------------------------------------------------+
| Search [________]  Family [All] Variant [All] Route [All] Cluster [All]         |
+--------------------------------------------------------------------------------+
| Factor / Version | Coverage | RankIC | WF folds | Cluster | Incremental | Route |
| volume-shock...  | 98.1%    | -0.02  | S S S    | C-07    | supported   | MODEL |
| momentum...      | 97.9%    | -0.04  | C C C    | C-03    | diagnostic  | MODEL |
| ...                                                                            |
+--------------------------------------------------------------------------------+
| Selected 3: [Compare] [Export FeatureSet draft]                                |
+--------------------------------------------------------------------------------+
```

详情页示意：

```text
[factor identity + route badges]                 [evidence class + audit status]
[coverage/distribution] [RankIC] [worst fold] [turnover] [execution: NOT AVAILABLE]
[Walk-Forward fold chart]
[Regime matrix]
[correlation neighbours + cluster]
[conditional/orthogonal evidence]
[execution panel]
[model contribution panel]
[lineage and limitations]
```

## 6. 视觉和语言规则

- 红色只用于完整性 blocker、审计失败或明确不可用，不用于普通负 RankIC。
- 绿色表示“在已声明方向和当前上下文得到支持”，不写“有效 Alpha”。
- 黄色表示已暴露、诊断、证据不足、待 M4.6 或需要新 OOS。
- 蓝色/灰色表示中性信息、cluster、canonical 和尚未产生的数据。
- `0`、`NULL`、`NOT_AVAILABLE` 必须分开：零是观测值，NULL 是上游缺失，NOT_AVAILABLE 是阶段尚未运行。
- 每个图表都有窗口、样本数、单位、Universe、variant 和 Evidence ID tooltip/详情。
- 默认显示全量失败和 limitations，不提供“只看显著结果”的默认模式。

## 7. 数据快照契约

MVP 从现有视图生成标准化展示快照：

- `factor_card_index`：每个 context/entity 一行，用于首页和筛选；
- `factor_card_evidence`：指标名、值、单位、证据等级、方向结果、来源 asset/字段；
- `factor_card_fold`：Train/Validation/Test 和 exposure status；
- `factor_card_regime`：Regime 维度和值；
- `factor_card_relationship`：相关、duplicate、cluster 和 canonical 关系；
- `factor_card_execution`：M4.6 后的费用、成交、容量和资金规模；
- `model_card_*`：M6 后追加，不改变 factor card 主键。

主键至少包含：

```text
(factor_id, factor_version, variant,
 dataset_vintage, universe_version, label_spec,
 execution_spec, evidence_vintage)
```

每个展示值必须携带 `source_asset_id`、`source_relation`、`source_field` 和 `sample_classification`。不允许前端根据颜色或排序反推生命周期。

## 8. 性能边界

- 首页只载入因子级汇总，不加载逐日明细；
- 时间图使用周/月/年度或 fold 聚合，逐日数据留在 DuckDB；
- 关系矩阵按所选因子或 cluster 懒加载/预切片；
- 首版目标为 1,000 条 factor×variant 路径仍可本地浏览；
- 构建阶段输出实体数、JSON 大小、页面数量和耗时，超限时明确失败或警告；
- M4.5 O(N²) 计算成本属于证据生产层，前端不重复计算。

## 9. MVP 验收标准

- 任意符合契约的新 factor release 无需修改页面代码即可显示；
- 首页、详情、最多 6 因子对比和 cluster 页面可离线使用；
- 所有展示数值可以追溯到 DuckDB 字段和不可变 asset ID；
- M4.6/M6 未完成的面板显示 `NOT_AVAILABLE`，不伪造空结果；
- 描述性、回顾诊断、已暴露 pseudo-OOS 和真实 OOS 视觉上明确区分；
- 单因子弱、方向证伪和冗余不会显示成全局淘汰；
- 导出的 FeatureSet 只是草案，并包含报告 ID 和暴露上下文；
- 独立审计抽样复核页面值与源表一致；
- 页面无数据库写入口、无 secrets、无外部网络依赖。

## 10. 实施顺序

1. 已完成：冻结首版页面字段、展示快照和报告 Manifest。
2. 已完成：在 M2-E 回填期间实现 M4.1～M4.5 的只读 MVP，并接入 `frontEnd/` React 应用和统一流水线。
3. M2-E 发布、M4.6 完成后：自动填充 execution 面板。
4. M5 数百因子接入时：验证分页、筛选、构建时间和 1,000 路径容量。
5. M6 LightGBM 完成后：启用 Model 页面和模型贡献区块。

因此不需要等待 M2-E 才开始 Explorer；只需保证未完成阶段被准确标记，且之后通过新 report ID 追加展示，而不是覆盖历史报告。
