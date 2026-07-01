# TradingAgent/PM

> **阅读顺序：** [AGENTS.md](../AGENTS.md) → [STATUS.md](../STATUS.md) → 本文件

## 目标
预测市场交易模拟盘/影子盘, 高频训练策略, 每日2次复盘。

## 现有代码
- /opt/investment/PredictionMarkets/tools/ (20个工具)
- CLOB sandbox + Dune Analytics
- shadow/sim/strategy 完整

## 特点
- 24/7, Polymarket
- 概率交易, 非方向性

## 工具清单 (TradingAgent tool references)

- 源目录: `/opt/investment/PredictionMarkets/tools/` (20 个 .py 工具)
- TradingAgent 引用: `TradingAgent/PM/tools/` — 相对符号链接 + `manifest.csv` (filename, docstring)
- 关键工具:
  - pm_workflow.py — Unified PredictionMarkets simulated-training workflow
  - pm_market_data.py — Read-only Polymarket market-data collection
  - pm_clob_sandbox.py — Polymarket CLOB trading sandbox (research only)
  - pm_simulator.py — YES/NO bets, settlement, early exit
  - pm_shadow_runner.py — Edge × kelly × hold variant comparison
  - pm_forward_validation.py — Daily Brier + P&L + calibration report
  - pm_prediction_model.py — Probability estimates with alpha
  - pm_marketgraph_bridge.py — PredictionMarkets → MarketGraph causal impact bridge
  - pm_historical_replay.py — Calibrate strategy on resolved markets
