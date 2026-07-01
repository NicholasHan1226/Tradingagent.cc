# TradingAgent Docs — 文档导航

> **阅读顺序：** 先读 [../AGENTS.md](../AGENTS.md) → [../STATUS.md](../STATUS.md) 了解规则和当前状态，再按需查阅本目录文档。

## 文档分类

### 活跃的参考文档（反映当前架构）

| 文件 | 用途 |
|------|------|
| [HANDOFF_架构对齐_20260630.md](HANDOFF_架构对齐_20260630.md) | 6/30 架构审查快照（已标"部分过时"，读前先看 warning banner） |
| [runtime_incidents_20260701.md](runtime_incidents_20260701.md) | 7/1 运行时事件时间线和修复复盘 |
| [data_contract.md](data_contract.md) | 数据契约定义 |
| [data_sources.md](data_sources.md) | 数据源接入说明 |
| [email_setup.md](email_setup.md) | 邮件通道配置 |
| [write_end_contract.md](write_end_contract.md) | 写端单一真相源合同 |
| [INFRASTRUCTURE.md](INFRASTRUCTURE.md) | 基础设施说明 |
| [repo_structure.md](repo_structure.md) | 仓库结构说明 |
| [shadow_market_configs.md](shadow_market_configs.md) | 影子盘市场配置 |
| [../shared/automation_tasks.md](../shared/automation_tasks.md) | Cron 自动化任务计划（36 任务） |
| [../shared/cron_migration.md](../shared/cron_migration.md) | Cron 迁移记录（含 tradingagent/MarketGraph 任务归属） |
| [../shared/orchestrator_design.md](../shared/orchestrator_design.md) | 调度编排器设计 |

### 已归档（历史，仅供参考）

| 文件 | 原因 |
|------|------|
| [archive/BATCH_PLAN_20260630.md](archive/BATCH_PLAN_20260630.md) | 6/30 待确认开发计划草案 |

## 规则优先级

1. [../AGENTS.md](../AGENTS.md) — TradingAgent 总规则（最高优先级）
2. 各市场/模块 AGENTS.md（Ashare/, Crypto/, shared/ 等）
3. 本目录活跃参考文档（补充背景和交接说明）
4. 本目录 `archive/` — 仅供参考，不代表当前状态

## 对 agent 的关键提示

- HANDOFF 文档顶部有 warning banner，列出了 4 项已知过时/矛盾点，**读正文前先读 banner**
- runtime_incidents_20260701.md 是 7/1 事故链的详细时间线，理解永久护栏规则的背景时查阅
- 所有运行时状态以 AGENTS.md 和 live 服务器为准，不以本目录任何文档为准
