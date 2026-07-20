# tradingagent/Crypto

> **阅读顺序：** [AGENTS.md](../AGENTS.md) → [STATUS.md](../STATUS.md) → 本文件

## 目标
加密货币交易模拟盘/影子盘，按 7x24 市场语义建设持续观察和复盘能力。当前只验证 fixture/mock 下的候选模块；监控频率、数据覆盖和策略有效性必须由后续样本证明，不能把“高频训练”写成已实现能力。

## 现有代码
- `tradingagent/Crypto/` 内为现役实体代码，不再依赖 `/opt/investment/Crypto/tools/` 旧目录。
- shadow/sim/strategy/validation 源码模块存在于当前隔离候选，但尚无 fresh TradingDatas、已安装 scheduler、Testnet 或 live 验收，不能称为完整闭环或生产能力。
- 数据源只读 TradingDatas 的 `GET /v1/catalog` 与 `POST /v1/query`；TradingDatas fresh handoff 前只允许显式 fixture/mock。不得由 Crypto 直接调用 Binance、读取 TradingDatas SQLite，或回退到 `/tushare`、`/source_status`、provider 专用 route。

## 特点
- 目标市场语义为 24/7、无交易所统一休市；当前不表示全天候任务已安装。
- 5min 条件监控是待验证目标频率，不是已部署 SLA。
- server-local paper、Binance Spot Testnet 和未来 Binance Spot Live 是三份不同合同、账户与凭据域；不能靠切换 base URL 或环境变量升级。当前 `REAL_TRADING_ENABLED=false`，Live adapter 未实现。

## 工具清单 (TradingAgent tool references)

- 源目录: `tradingagent/Crypto/` 实体模块。
- TradingAgent 引用: `tradingagent/Crypto/tools/manifest.csv` 仅作历史工具清单/审计索引，不是运行时代码入口。
- 关键工具:
  - crypto_workflow.py — Unified Crypto simulated-training workflow
  - crypto_market_data.py — 旧 provider 专用行情适配器线索；不得成为当前数据入口或 fallback
  - crypto_simulator.py — Simulated execution for condition cards
  - crypto_shadow_runner.py — Shadow strategy layer (parallel to frozen baseline)
  - report.py — Daily shadow recap and no-empty-trigger delivery policy
  - validation.py — Forward validation dashboard and sample-quality scorecard
  - promotion.py — Strategy promotion scorecard (5-tier shadow→sim)
  - crypto_marketgraph_bridge.py — Crypto ↔ MarketGraph cross-market signal bridge
  - crypto_portfolio_optimizer.py — Correlation + volatility-adaptive sizing
