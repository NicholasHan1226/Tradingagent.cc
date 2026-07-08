# tradingagent/US

> **阅读顺序：** [AGENTS.md](../AGENTS.md) → [STATUS.md](../STATUS.md) → 本文件

## 目标
美股交易模拟盘/影子盘, 高频训练策略, 每日2次复盘。

## 现有代码
- 本仓库 `US/` 是现役美股交易研究、模拟盘和工具入口。
- 旧 `/opt/investment/US/tools/` 只作历史迁移线索，不是生产代码入口；不得恢复为独立采集或执行依赖。
- Alpaca API executor 是未来受控券商接口预留；默认不得作为生产行情源或自动实盘执行入口，启用前必须经过实盘安全门和 Nicholas 人工确认。
- shadow/sim/strategy 完整

## 特点
- 交易时段: 21:30-04:00 (北京时间)
- Alpaca API 只作为未来实盘/纸面券商适配器预留；当前生产模拟盘仍由 TradingAgent 本地模拟账本闭环，行情数据走 SharedSignals API。

## 工具清单 (TradingAgent tool references)

- 源目录: `tradingagent/US/`。
- 数据入口: SharedSignals API 优先；Alpaca 仅作为未来受控券商/行情适配器，不替代 SharedSignals 供数层。
- 历史迁移线索: `/opt/investment/US/tools/`；如服务器仍有残留，默认按退役资产处理。
- 关键工具:
  - us_workflow.py — Daily workflow: collect → plan → scan → review
  - us_market_data.py — 旧迁移线索；生产行情不得绕过 SharedSignals 独立调用 Tushare
  - us_alpaca_executor.py — Alpaca Paper Trading Executor (real API)
  - us_alpaca_market_data.py — Alpaca 行情适配器预留；默认不作为生产供数层
  - us_simulator.py — Condition-card-driven paper trading
  - us_shadow_runner.py — Multi-strategy shadow runner
  - us_forward_validation.py — Out-of-sample performance tracking
  - us_strategy_promotion.py — 5-tier variant classifier
  - us_marketgraph_reader.py — Read-only MarketGraph causal/association bridge
