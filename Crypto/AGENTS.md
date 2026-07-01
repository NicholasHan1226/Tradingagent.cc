# TradingAgent/Crypto

> **阅读顺序：** [AGENTS.md](../AGENTS.md) → [STATUS.md](../STATUS.md) → 本文件

## 目标
加密货币交易模拟盘/影子盘, 7x24高频训练策略, 每日2次复盘。

## 现有代码
- /opt/investment/Crypto/tools/ (21个工具)
- shadow/sim/strategy/validation 完整
- 数据源: Binance API (4端点)

## 特点
- 24/7交易, 无休市
- 5min条件监控

## 工具清单 (TradingAgent tool references)

- 源目录: `/opt/investment/Crypto/tools/` (21 个 .py 工具)
- TradingAgent 引用: `TradingAgent/Crypto/tools/` — 相对符号链接 + `manifest.csv` (filename, docstring)
- 关键工具:
  - crypto_workflow.py — Unified Crypto simulated-training workflow
  - crypto_market_data.py — Public market data adapter (Binance API)
  - crypto_simulator.py — Simulated execution for condition cards
  - crypto_shadow_runner.py — Shadow strategy layer (parallel to frozen baseline)
  - crypto_forward_validation.py — Forward validation dashboard
  - crypto_strategy_promotion.py — Strategy promotion scorecard (5-tier)
  - crypto_marketgraph_bridge.py — Crypto ↔ MarketGraph cross-market signal bridge
  - crypto_portfolio_optimizer.py — Correlation + volatility-adaptive sizing
