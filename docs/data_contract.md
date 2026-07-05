# SharedSignals -> TradingAgent Data Contract

## Scope

TradingAgent consumes SharedSignals and MarketGraph as read-only upstream data.
This contract covers the data access layer used by screening and A-share T+1
calendar logic. It does not change execution, accounting, risk, or portfolio
write paths.

## Canonical SharedSignals Inputs

- SQLite read model:
  `/opt/investment/MarketGraphRuntime/read_model/marketdata.sqlite`
- Schema reference:
  `sharedsignals_schema.py` documents the 11 canonical tables:
  `market_assets`, `market_bars_daily`, `market_bars_intraday`,
  `market_events`, `market_pm_markets`, `market_pm_prices`,
  `market_factors`, `market_ingest_runs`, `market_coverage_status`,
  `market_backfill_status`, `provider_interface_matrix`.
- Trading calendar:
  `SharedSignals/reference/market_calendar.py`.

TradingAgent opens SQLite with `mode=ro`. It must not write to SharedSignals,
MarketGraph, or legacy Ashare data directories.

## Reader API

`shared.data.reader.TradingagentDataReader` is the consumer-facing facade. It
uses SharedSignals HTTP API first when `SHAREDSIGNALS_API_URL` is configured,
then falls back to the read-only SQLite reader on API failure or empty API
shells.

`shared.data.reader.SharedSignalsReader` exposes the SQLite fallback methods:

- `get_bars_daily(market, symbol, start, end)`
- `get_bars_intraday(market, symbol, interval, start, end)`
- `get_events(market, symbol, start, end)`
- `get_factors(market, symbol)`
- `get_asset(market, symbol)`
- `get_coverage(market, date)`

Rows are returned as dictionaries. Missing rows return `[]` or `None` through
`TradingagentDataReader`.

5-minute intraday reads use:

- API-first path: SharedSignals `/realtime_5min?market=<market>&ts_code=<symbol>&date=<YYYYMMDD>`.
- SQLite fallback: `market_bars_intraday` filtered by `market`, `symbol`, and
  `interval`.
- Optional L1 fields are passed through when SharedSignals has them:
  `bid_price`, `ask_price`, `bid_size`, `ask_size`.
- Optional futures contract lifecycle fields are passed through when
  SharedSignals has them: `last_trade_date`, `expiry_date`.

## MarketGraph CSV Inputs

`shared.data.reader.MarketGraphCSVReader` reads these read-only CSV outputs:

- `all_weather_regime.csv`
- `intake/event_candidates.csv`
- `intake/sentiment_signals.csv`

These remain MarketGraph-derived context, not TradingAgent facts. TradingAgent uses
them only for scoring and confidence weighting.

## Environment Variables

- `SHARED_SIGNALS_DB`: overrides the SQLite path.
  Default: `/opt/investment/MarketGraphRuntime/read_model/marketdata.sqlite`.
- `MARKETGRAPH_DATA`: overrides the MarketGraph CSV root.
  Default: `/opt/investment/MarketGraph/data`.
- `SHARED_SIGNALS_ROOT`: overrides SharedSignals root for importing
  `reference/market_calendar.py`.
  Default: `/opt/investment/SharedSignals`.

## Fail-Safe Behavior

`TradingAgentDataReader` is the consumer-facing facade. It catches missing files,
SQLite errors, absent tables, import failures, and upstream exceptions.

- List-style reads return `[]`.
- Single-row reads return `None`.
- The reader sets `stale=True` and records source-level errors.
- Six-dimension scoring treats missing dimensions as neutral `0.5`, preserving
  the existing weighted scoring behavior.

Data failure must reduce confidence and freshness; it must not crash screening
or silently create new facts.

## Freshness

Freshness is carried at the reader level today:

- `stale=False`: requested upstream reads returned data.
- `stale=True`: at least one requested source was unavailable, empty, or raised.
- `errors`: source names and exception summaries for degraded reads.

Downstream reports should surface stale status when it affects candidate scores
or real-money eligibility. Stale screening data is suitable for shadow or
simulation training only unless separately confirmed by current data.

## CNFutures Inputs

CNFutures uses SharedSignals as the only market-data ingestion layer. It must
not call Tushare, CTP, SimNow, or exchange feeds directly from TradingAgent.

- Asset universe: `market_assets` with `market="Futures"`.
- Daily bars: `market_bars_daily` with `market="Futures"`.
- 5-minute bars: `market_bars_intraday` with `market="Futures"` and
  `interval="5min"`.
- API path: SharedSignals `/realtime_5min?market=Futures&ts_code=<contract>`;
  SQLite remains a same-host read-only fallback.
- Optional L1 and lifecycle fields from SharedSignals are preserved through the
  CNFutures runner into simulated order receipts: `bid_price`, `ask_price`,
  `bid_size`, `ask_size`, `last_trade_date`, and `expiry_date`.
- Reader mapping: `CNFutures` internal market is `cn_futures`; upstream reader
  market remains `Futures`.
- Current cadence assumption: intraday CNFutures simulation must use
  `rt_fut_min` rows already collected by SharedSignals. `fut_daily` remains a
  daily fallback/review input and must not be described as 5-minute execution
  data.
- API health is not trading eligibility. A degraded SharedSignals API response
  may still leave a usable SQLite read model, and a healthy API response does
  not prove real-time, tradable, or account-authorized data.
- Review records are appended under `shared/review/data/`; signal-state writes
  go through `signals/` only. CNFutures does not write back into SharedSignals
  or MarketGraph.
- Dashboard consumption should use
  `shared/review/cn_futures/observation_report.json`. The stable UI-facing
  surface is `schema_version`, `dashboard`, `next_validation`,
  `observation_phase`, `alerts`, `data`, `simulation`, `styles`, `evolution`,
  and `real_trading_enabled=false`; nested `source_live_check` remains a debug
  surface for operators.

CNFutures has no separate shadow layer. Multi-style testing is represented by
isolated simulated accounts and strategy styles with `capital_layer=simulated`
and `real_trading_enabled=false`.

## Calendar Priority

A-share trading-day checks use this order:

1. SharedSignals `reference/market_calendar.py`.
2. Existing external trade calendar files discovered by `Ashare/t_plus_1.py`.
3. Conservative built-in 2026 weekday/holiday fallback.

The fallback is a continuity guard, not a canonical market calendar.
