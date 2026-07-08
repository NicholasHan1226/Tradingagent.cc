# TradingAgent Data Sources

> Status: active. Last reviewed: 2026-07-05.

TradingAgent does not collect market data directly. Production consumes
prepared data from SharedSignals API and fails closed when the API is
unavailable. Local read-model access is reserved for explicit tests or
emergency diagnostics.

## Current Sources

| Source | Owner | TradingAgent use |
|---|---|---|
| SharedSignals HTTP API | SharedSignals | preferred market data, events, assets, 5-minute bars |
| `marketdata.sqlite` read model | SharedSignals | explicit local test/diagnostic fallback only |
| MarketGraph API | MarketGraph | optional regime/event context and PM independent research probabilities |
| TradingAgent `signals/` and `shared/logs/` | TradingAgent | simulated execution, receipts, positions, review evidence |

## Canonical TradingAgent Reader

Use `shared.data.reader.TradingagentDataReader` for strategy, screening,
simulation and review code.

- `SHAREDSIGNALS_API_URL` should point to the SharedSignals service.
- `SHARED_SIGNALS_DB` is only used when
  `TRADINGAGENT_ALLOW_SHARED_SIGNALS_SQLITE=1` is explicitly set.
- `MARKETGRAPH_DATA` is retired for production use; use `MARKETGRAPH_API_URL`.
- `MARKETGRAPH_API_URL` points to the MarketGraph read-only REST service for
  API/read-model research evidence such as PM research probabilities.
- Trading calendar checks use `TradingagentDataReader.is_trading_day`; do not
  scan SharedSignals directories for calendar files.

## Retired Inputs

The following are historical only and must not be restored as active
dependencies:

- `/opt/investment/Ashare/data/backtest_cache/`
- `/opt/investment/Ashare/tools/`
- `/Users/nicholashan/Desktop/Investment`
- TradingAgent writes to its own `signals/`, `shared/logs/` and
  `shared/review/` paths only.

Historical incident logs may mention these paths for audit context. New code,
cron templates and active documents should use the current sources above.

## Market Responsibilities

- A-share: SharedSignals `stock_basic`, daily bars and 5-minute bars feed
  TradingAgent simulation; TradingAgent writes server-local simulated receipts
  under `signals/`.
- Crypto / US: SharedSignals feeds the five-minute simulated loops;
  TradingAgent keeps simulated ledgers and style review outputs locally.
- PM: SharedSignals feeds market/prices; MarketGraph unified API/read model
  feeds independent research probabilities; TradingAgent combines them locally
  for simulated edge gating and ignores PM judgment fields embedded in
  SharedSignals rows.
- CNFutures: SharedSignals owns futures data collection; TradingAgent consumes
  Futures 5-minute bars for simulated-only execution and review.
- HK: currently paused for production scheduling. Code remains fail-closed and
  requires explicit enable flags before manual use.

For the detailed field contract, use `docs/data_contract.md`.
