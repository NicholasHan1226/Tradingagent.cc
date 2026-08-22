# TradingAgent 文档导航

> 阅读顺序：[../AGENTS.md](../AGENTS.md) → [../STATUS.md](../STATUS.md) 与本轮新鲜运行读回 → 本文件。跨仓 Controller 路由可参考本地 `../../autodev-control/AUTODEV_STATE.json`，但它不替代直接运行证据。

## Canonical 文档

| 文件 | 唯一职责 |
|---|---|
| [architecture.md](architecture.md) | 三仓边界、多市场/多服务器运行拓扑、原生币种隔离资本、模型平面、样本与原子执行架构 |
| [EVOLUTION_PROGRAM.md](EVOLUTION_PROGRAM.md) | 模拟优先的长期成熟路径、市场独立阶段与证据出口；不保存当前状态 |
| [data_contract.md](data_contract.md) | 输入、capital ledger、执行、样本、标签、KPI 和成熟度字段 |
| [system_state_matrix.md](system_state_matrix.md) | 稳定能力层级与状态字段语义；具体 current 值回到 `STATUS.md` 和同轮读回 |
| [universe_contract.md](universe_contract.md) | 市场环境池、账户可交易池、5万元可行池及主板个股零泄漏边界 |
| [capital_growth_validation.md](capital_growth_validation.md) | 样本、费用后结果、回撤、MG 消融和确定性晋降级验收 |
| [operations.md](operations.md) | A股 V1 本地与服务器旁路 sim-only 运行、候选验收、故障、退役与回滚；不证明 CNFutures 或现役生产状态 |
| [BACKLOG.md](BACKLOG.md) | 当前可执行短队列；完成、失效或只有长期设想的条目移出，不作为第二路线图 |
| [../STATUS.md](../STATUS.md) | A股、Crypto、Copilot 与暂停范围的带时间戳当前摘要；下一次材料读回整体替换 |

根 `README.md` 只做入口；根/模块 `AGENTS.md` 保存长期规则；`STATUS.md` 保存分市场当前摘要，运行结论仍由本轮读回确认。不要重新创建重复的 infrastructure、data-source、write-end、事故日记或实施计划文档。

## 更新规则

- 资本/架构/安全边界变化：更新 `architecture.md` 和最近层 `AGENTS.md`。
- schema、路径、字段或事实源变化：更新 `data_contract.md`。
- 能力层级或字段语义变化：更新 `system_state_matrix.md`；易变 source/release/runtime/receipt/consumer 事实只更新带 `observed_at` 的 `STATUS.md` 和对应机器记录。未完成复核时采用更严格状态。
- 板块权限、三层 Universe、reason code 或零泄漏边界变化：更新 `universe_contract.md`、`data_contract.md` 和对应测试入口。
- 样本、KPI、消融、成熟度或复核门槛变化：更新 `capital_growth_validation.md`。
- 命令、环境、cron、发布、故障或回滚变化：更新 `operations.md`。
- 当前测试、远端、生产、timer 或真实市场证据：写入机器状态/运行报告并按层级披露；材料变化时整体替换 `STATUS.md`，不得把它追加成事故流水。
- 后续范围：只写入 `BACKLOG.md`；不得把 backlog 写成已实现或已授权。

`superpowers/plans/` 是实现任务记录，不是 current 能力证明。计划中的勾选、进度说明或候选文件存在，都必须回到机器状态、测试和本轮运行证据分层核验；已完成或失效计划从活动入口移除，必要历史由 Git 或带日期报告保留。

## 退役约束

- 旧共享资本、旧模拟持仓/PnL、旧多账本、旧演化 writer、旧重复 cron/docs 都是退役历史，不得重新成为入口。
- 历史从 Git 和只读冻结目录审计，不在 active docs 中复制旧数值、命令或路径。
- 回滚文档只允许停止新任务、保留 append-only 新事实并切回已验证代码；不得指导恢复旧 ledger、删除事件或覆盖投影为事实。
- 本文档任务不授权 commit、push、deploy、apply cron、发邮件或真实交易。
