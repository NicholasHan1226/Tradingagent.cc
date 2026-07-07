# TradingAgent Data Sources

> Status: active. Last reviewed: 2026-07-05.

TradingAgent does not collect market data directly. It consumes prepared data
from SharedSignals API first, then falls back to the local read-only read model
when configured on the same host.

## Current Sources

| Source | Owner | TradingAgent use |
|---|---|---|
| SharedSignals HTTP API | SharedSignals | preferred market data, events, assets, 5-minute bars |
| `marketdata.sqlite` read model | SharedSignals | same-host read-only fallback |
| MarketGraph research exports/API | MarketGraph | optional regime/event context and PM independent research probabilities |
| TradingAgent `signals/` and `shared/logs/` | TradingAgent | simulated execution, receipts, positions, review evidence |

## Canonical TradingAgent Reader

Use `shared.data.reader.TradingagentDataReader` for strategy, screening,
simulation and review code.

- `SHAREDSIGNALS_API_URL` should point to the SharedSignals service.
- `SHARED_SIGNALS_DB` may point to the same-host read-only SQLite model.
- `MARKETGRAPH_DATA` is optional. Leave it unset unless a MarketGraph CSV export
  is explicitly mounted for same-host operation.
- `MARKETGRAPH_API_URL` points to the MarketGraph read-only REST service for
  API/read-model research evidence such as PM research probabilities.
- `SHARED_SIGNALS_ROOT` / `SHARED_SIGNALS_CALENDAR_ROOT` are only for calendar
  compatibility imports and discovery.

## Retired Inputs

The following are historical only and must not be restored as active
dependencies:

- `/opt/investment/Ashare/data/backtest_cache/`
- `/opt/investment/Ashare/tools/`
- `/Users/nicholashan/Desktop/Investment`
- TradingAgent writes to `MarketGraph/outputs/`

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
