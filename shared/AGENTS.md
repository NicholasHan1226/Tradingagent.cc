# tradingagent/shared

> **阅读顺序：** [../AGENTS.md](../AGENTS.md) → [../STATUS.md](../STATUS.md) → 本文件

## 目标
跨市场共享的交易逻辑: 筛选→对抗→风控→组合→执行→复盘→通知→记账。

## 文件结构
- screening/ — 六维打分(宏观/事件/基本面/资金/技术/情绪) + 条件订单 + 候选池
- adversarial/ — 多空对辩 + 压力测试 + 历史类比
- risk/ — 事前风控 + 持仓监控 + 黑天鹅应急
- portfolio/ — 组合构建 + 仓位分配 + 再平衡 + 退出
- execution/ — Hermes同花顺 + 影子盘 + 模拟盘
- notify/ — 11类邮件模版 + 告警路由
- review/ — 日复盘(2次) + 周复盘 + 月复盘 + 归因 + 基准
- accounting/ — 资金记账 + 持仓记账 + 对账 + 审计
- benchmark/ — 沪深300/买入持有基准

## 原则
- 权重式打分, 不设硬门禁
- 条件驱动, 主动发现
- 降权不硬拒, 但有底线(单股<15%)

## 写入端单一事实源
- TradingAgent 写入端必须遵守单一事实源, 详细契约见 `docs/write_end_contract.md`。
- A 股模拟盘默认走服务器本地 paper fill：`job_ashare_sim_exec → Ashare/sim_executor.py → shared/execution/sim_broker.py → shared/logs/sim_ledger/ashare`。本地闭环直接生成 `filled`/`positions` 等 simulated 状态，不依赖 Mac Mini Hermes。
- Hermes/同花顺 GUI 执行桥仅作为第二路径，只有显式设置 `ASHARE_SIM_HERMES_ENABLED=1` 时，服务器才把同一模拟信号投递到 `signals/pending/`，由 Mac Mini 领取、执行并回写；MarketGraph 只能提供研究/信号输入，不能直连执行端。
- `signals/` 是执行队列唯一写入面:
  - `signals/pending/` 待执行
  - `signals/filled/` 已成交
  - `signals/cancelled/` 已撤销
  - `signals/positions/` 当前持仓快照
- `shared/accounting/` 是资金与账本唯一写入面。
- 维护、回补、烟测、修复重跑和 bootstrap 样本若会写入 `shared/logs/sim_ledger/` 或 `shared/review/<market>/`，必须写入 `exclude_from_dashboard=true`，或在 `run_context` / `run_mode` / `run_source` / `sample_type` 中标记 `maintenance`、`backfill`、`smoke`、`repair`、`bootstrap` 或 `dry-run`；生产看板会跳过这些样本，避免维护重跑污染交易量、PnL、复盘和演化输入。
- `shared/review/data/` 是复盘证据唯一写入面; `outputs/` 只放可再生产物, 不回写事实。
- `shared/signals/` 若仍存在视为废弃兼容路径, 只能重定向或只读迁移, 不再新增事实写入。
- `executions/` 相关事实应归并到 `signals/filled/` 与 `shared/accounting/`, 不再形成平行账本。
- `/opt/investment/Ashare/data/` 属旧系统只读输入, TradingAgent 禁止向该目录写入。
