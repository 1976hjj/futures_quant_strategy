# M2-C 公司行动与复权对账

状态：真实回填、仓库发布、复权对账和独立审计已完成；未通过逐笔门禁的记录已隔离。

## 1. 目标

M2-C 不把供应商复权因子当成不可质疑的真值。它建立一条可追溯的对账链：

```text
分红/送股/转增方案及修订
-> 公告日与实施公告日（什么时候可知）
-> 登记日、除权除息日、派息日（什么时候生效）
-> 原始价格与前收口径
-> 复权因子跳变
-> 对账结论、差异原因与隔离标志
```

## 2. 回填单位

`dividend` 要求至少指定一个查询条件。系统按 `ts_code` 分区，每只 A 股一个不可变快照，覆盖它的全部方案历史。优点是请求数少于按自然日分区，且单股行数如接近供应商上限可立即停止，不会静默截断。

启动命令：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts\start_corporate_action_dashboard.ps1

powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts\resume_tushare_corporate_action_backfill.ps1
```

监控地址：`http://127.0.0.1:8767`

## 3. 必须保留的时间

- `ann_date`：预案/决案公告，用于方案知情时间；
- `imp_ann_date`：实施公告，用于确定方案已进入实施阶段；
- `record_date`：股权登记日；
- `ex_date`：除权除息生效日；
- `pay_date`：现金派息日；
- `div_listdate`：红股上市日。

空日期不自动猜测。预案、股东大会通过、实施和终止等不同 `div_proc` 记录不能只保留最后一条；它们是修订链的证据。

## 4. 验收门禁

- checkpoint 与所有 payload 哈希一致；
- 每个分区只包含自己的 `ts_code`；
- 所有原始方案和修订保留，研究层另建“当时可知”视图；
- 实施事件的 `ex_date` 与复权因子跳变逐笔对账；
- 有事件无跳变、有跳变无事件、日期错位和数值偏差分类报告；
- 未证明对账通过前，复权收益不得成为正式因子标签。

## 5. 边界

M2-C 先覆盖分红、送股和转增这三类 `dividend` 可见事件。配股、拆并股、吸收合并与代码迁移如果不在该接口完整表达，必须作为单独数据域补建，不得从价格跳变反推成“已知事件”。

## 6. 发布对象与门禁

- `raw.corporate_actions`：321,942 条原始公告/方案记录；
- `research.corporate_action_announcements`：去完全重复后的公告与可知日期；
- `research.corporate_action_event_candidates`：同一除权日的经济方案候选；
- `research.corporate_action_events`：每只证券每个除权日的已选实施事件；
- `research.adjustment_factor_jumps`：所有复权因子跳变及行情参考比；
- `research.corporate_action_reconciliation`：全量对账结果与诊断类别；
- `research.corporate_action_reconciliation_approved`：同日匹配、事前可知、因子对行情和事件对参考价均通过的白名单；
- `research.corporate_action_reconciliation_exceptions`：其余记录，只供调查，不得用于正式复权收益。

机器可读审计结果：`reports/m2c_corporate_action_audit.json`。
