# Tradings/US

## 目标
美股交易模拟盘/影子盘, 高频训练策略, 每日2次复盘。

## 现有代码
- /opt/investment/US/tools/ (20个工具)
- Alpaca API executor (唯一真实API执行器)
- shadow/sim/strategy 完整

## 特点
- 交易时段: 21:30-04:00 (北京时间)
- Alpaca API 可直接执行 (未来实盘)

## 工具清单 (Tradings tool references)

- 源目录: `/opt/investment/US/tools/` (20 个 .py 工具)
- Tradings 引用: `Tradings/US/tools/` — 相对符号链接 + `manifest.csv` (filename, docstring)
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
