# ADR-0003：长期系统采用滚动锁箱批次

- 状态：Accepted
- 日期：2026-09-01

## 背景

“最终 Holdout 只打开一次”适合一次性研究，但长期 Research OS 会不断产生新数据和新假设。反复查看同一段历史会使它失去 OOS 身份。

## 决策

采用 rolling holdout vintages：新到数据按批次隔离，在预定时间和权限下释放一次。所有读取写入不可逆 exposure ledger；释放后的区间只能称为已暴露 OOS，不得再次包装为未见样本。

## 后果

- Holdout 不通过普通路径挂载给研究进程；
- ExperimentSpec 必须声明允许访问的 vintage；
- Agent 永远不获得未释放 vintage 的 capability；
- Holdout 失败后的修改只能在未来新 vintage 上重新检验。

