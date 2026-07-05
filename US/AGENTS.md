# tradingagent/US

> **阅读顺序：** [AGENTS.md](../AGENTS.md) → [STATUS.md](../STATUS.md) → 本文件

## 目标
美股交易模拟盘/影子盘, 高频训练策略, 每日2次复盘。

## 现有代码
- 本仓库 `US/` 是现役美股交易研究、模拟盘和工具入口。
- 旧 `/opt/investment/US/tools/` 只作历史迁移线索，不是生产代码入口；不得恢复为独立采集或执行依赖。
- Alpaca API executor (唯一真实API执行器)
- shadow/sim/strategy 完整

## 特点
- 交易时段: 21:30-04:00 (北京时间)
- Alpaca API 可直接执行 (未来实盘)

## 工具清单 (TradingAgent tool references)

- 源目录: `tradingagent/US/`。
- 数据入口: SharedSignals API/read model 优先；Alpaca 仅作为未来受控券商/行情适配器，不替代 SharedSignals 供数层。
- 历史迁移线索: `/opt/investment/US/tools/`；如服务器仍有残留，默认按退役资产处理。
- 关键工具:
  - us_workflow.py — Daily workflow: collect → plan → scan → review
  - us_market_data.py — US stock data via Tushare
  - us_alpaca_executor.py — Alpaca Paper Trading Executor (real API)
  - us_alpaca_market_data.py — Real-time quotes & historical bars
  - us_simulator.py — Condition-card-driven paper trading
  - us_shadow_runner.py — Multi-strategy shadow runner
  - us_forward_validation.py — Out-of-sample performance tracking
  - us_strategy_promotion.py — 5-tier variant classifier
  - us_marketgraph_reader.py — Read-only MarketGraph causal/association bridge
