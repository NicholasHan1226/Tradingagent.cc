# TradingAgent 文档导航

> 阅读顺序：[../AGENTS.md](../AGENTS.md) → [../STATUS.md](../STATUS.md) → 本文件。

## Canonical 文档

| 文件 | 唯一职责 |
|---|---|
| [architecture.md](architecture.md) | 三仓边界、双市场独立资本、样本与原子执行架构 |
| [data_contract.md](data_contract.md) | 输入、capital ledger、执行、样本、标签、KPI 和成熟度字段 |
| [capital_growth_validation.md](capital_growth_validation.md) | 样本、费用后结果、回撤、MG 消融和人工复核验收 |
| [operations.md](operations.md) | sim-only 运行、双 capital root、样本/会话验收、故障与回滚 |
| [BACKLOG.md](BACKLOG.md) | 范围冻结后移出的发布、终端、人工实盘规格和长期统计工作 |

根 `README.md` 只做入口；根/模块 `AGENTS.md` 保存长期规则；`STATUS.md` 保存当前证据和阻塞。不要重新创建重复的 infrastructure、data-source、write-end、事故日记或实施计划文档。

## 更新规则

- 资本/架构/安全边界变化：更新 `architecture.md` 和最近层 `AGENTS.md`。
- schema、路径、字段或事实源变化：更新 `data_contract.md`。
- 样本、KPI、消融、成熟度或复核门槛变化：更新 `capital_growth_validation.md`。
- 命令、环境、cron、发布、故障或回滚变化：更新 `operations.md`。
- 当前测试、远端、生产、cron 或真实市场证据：只更新 `STATUS.md`，并按层级披露。
- 后续范围：只写入 `BACKLOG.md`；不得把 backlog 写成已实现或已授权。

## 退役约束

- 旧共享资本、旧模拟持仓/PnL、旧多账本、旧演化 writer、旧重复 cron/docs 都是退役历史，不得重新成为入口。
- 历史从 Git 和只读冻结目录审计，不在 active docs 中复制旧数值、命令或路径。
- 回滚文档只允许停止新任务、保留 append-only 新事实并切回已验证代码；不得指导恢复旧 ledger、删除事件或覆盖投影为事实。
- 本文档任务不授权 commit、push、deploy、apply cron、发邮件或真实交易。
