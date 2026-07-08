# tradingagent/Crypto

> **阅读顺序：** [AGENTS.md](../AGENTS.md) → [STATUS.md](../STATUS.md) → 本文件

## 目标
加密货币交易模拟盘/影子盘, 7x24高频训练策略, 每日2次复盘。

## 现有代码
- `tradingagent/Crypto/` 内为现役实体代码，不再依赖 `/opt/investment/Crypto/tools/` 旧目录。
- shadow/sim/strategy/validation 完整。
- 数据源只读 SharedSignals API；Crypto 上游 Binance 采集归 SharedSignals。

## 特点
- 24/7交易, 无休市
- 5min条件监控

## 工具清单 (TradingAgent tool references)

- 源目录: `tradingagent/Crypto/` 实体模块。
- TradingAgent 引用: `tradingagent/Crypto/tools/manifest.csv` 仅作历史工具清单/审计索引，不是运行时代码入口。
- 关键工具:
  - crypto_workflow.py — Unified Crypto simulated-training workflow
  - crypto_market_data.py — Public market data adapter (Binance API)
  - crypto_simulator.py — Simulated execution for condition cards
  - crypto_shadow_runner.py — Shadow strategy layer (parallel to frozen baseline)
  - report.py — Daily shadow recap and no-empty-trigger delivery policy
  - validation.py — Forward validation dashboard and sample-quality scorecard
  - promotion.py — Strategy promotion scorecard (5-tier shadow→sim)
  - crypto_marketgraph_bridge.py — Crypto ↔ MarketGraph cross-market signal bridge
  - crypto_portfolio_optimizer.py — Correlation + volatility-adaptive sizing
