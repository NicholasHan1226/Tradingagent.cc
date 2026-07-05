# tradingagent/PM

> **阅读顺序：** [AGENTS.md](../AGENTS.md) → [STATUS.md](../STATUS.md) → 本文件

## 目标
预测市场交易模拟盘/影子盘, 高频训练策略, 每日2次复盘。

## 现有代码
- 本仓库 `PM/` 是现役预测市场交易研究、模拟盘和工具入口。
- 旧 `/opt/investment/PredictionMarkets/tools/` 只作历史迁移线索，不是生产代码入口；不得恢复为独立采集或执行依赖。
- CLOB sandbox + Dune Analytics
- shadow/sim/strategy 完整

## 特点
- 24/7, Polymarket
- 概率交易, 非方向性

## 工具清单 (TradingAgent tool references)

- 源目录: `tradingagent/PM/`。
- 数据入口: SharedSignals API/read model 优先；CLOB sandbox 仅作模拟/研究适配，不替代 SharedSignals 供数层。
- 历史迁移线索: `/opt/investment/PredictionMarkets/tools/`；如服务器仍有残留，默认按退役资产处理。
- 关键工具:
  - pm_workflow.py — Unified PredictionMarkets simulated-training workflow
  - pm_market_data.py — Read-only Polymarket market-data collection
  - pm_clob_sandbox.py — Polymarket CLOB trading sandbox (research only)
  - pm_simulator.py — YES/NO bets, settlement, early exit
  - pm_shadow_runner.py — Edge × kelly × hold variant comparison
  - report.py — Daily Brier + P&L shadow report
  - validation.py — Forward validation with calibration and Brier tracking
  - promotion.py — Strategy promotion scorecard (research→shadow→sim)
  - pm_prediction_model.py — Probability estimates with alpha
  - pm_marketgraph_bridge.py — PredictionMarkets → MarketGraph causal impact bridge
  - pm_historical_replay.py — Calibrate strategy on resolved markets
