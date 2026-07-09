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
- 若某个 `shared/logs/sim_ledger/<market>/<style>/positions.json` 已被标记 `exclude_from_dashboard=true`，该风格目录视为隔离状态；后续 `daily_mark_to_market.jsonl` 必须继承该隔离标记，生产看板读取同目录 `daily_mark_to_market.jsonl`、`equity_snapshots.jsonl`、`trade_journal.jsonl` 与对应 `shared/review/<market>/style_performance.jsonl` 风格行时也必须跳过，直到该风格账本被明确重建为干净状态。
- SharedSignals 行情行是数据，不是交易信号。通用多市场 `run_sim.py` 只能消费显式 `buy/sell` 信号或市场专属策略生成的 `signal_source=explicit_strategy_signal`；如需用价格行做人工烟测，必须设置 `TRADINGAGENT_SIM_ALLOW_PRICE_ONLY_SIGNALS=1`，且烟测样本默认排除看板和复盘口径。
- A股 simulated 无成交/无订单不能只返回总数；`no_trade_explanation` 必须保留候选层计数、逐候选门禁去向、资金计划决策和组合构建摘要，能解释“有 candidate 但 0 order”到底卡在价格、风控、目标持仓已满、现金不足、整手预算、重复幂等还是执行跳过。健康检查、开盘首样本验收和无交易汇总必须把 `orders == 0` 但缺上述证据的当日日志判为 incomplete/warn，空候选时 `candidate_decision_trace` 可为空列表但字段必须存在，`capital_plan_decision` 与 `portfolio_decision` 仍必须存在；不能仅凭 `category` 放行为科学空跑。动态资金计划主动防御导致 0 order 时分类为 `capital_plan_defensive`，仍必须带 `candidate_decision_trace`、`capital_plan_decision` 和 `portfolio_decision`；`capital_plan_decision.capacity_reason` 必须区分 `target_positions_reached`、`defensive_no_target_positions`、`insufficient_investable_cash` 等具体容量原因，避免把满仓/防御/现金不足混成一个旧的 `capital_plan_capacity_zero`。健康检查必须按本交易日样本判断，历史成交只能证明账本可读，不能证明今天交易闭环正常。
- A股资金维度在六维评分内的规范字段是 `capital`；对外邮件、旧复盘或展示层若仍使用 `moneyflow`，只能作为 `capital` 的兼容别名，不得形成第二套资金分。
- `shared/review/data/` 是复盘证据唯一写入面; `outputs/` 只放可再生产物, 不回写事实。
- `shared/signals/` 若仍存在视为废弃兼容路径, 只能重定向或只读迁移, 不再新增事实写入。
- `executions/` 相关事实应归并到 `signals/filled/` 与 `shared/accounting/`, 不再形成平行账本。
- `/opt/investment/Ashare/data/` 属旧系统只读输入, TradingAgent 禁止向该目录写入。
