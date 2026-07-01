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

`shared.data.reader.SharedSignalsReader` exposes:

- `get_bars_daily(market, symbol, start, end)`
- `get_bars_intraday(market, symbol, interval, start, end)`
- `get_events(market, symbol, start, end)`
- `get_factors(market, symbol)`
- `get_asset(market, symbol)`
- `get_coverage(market, date)`

Rows are returned as dictionaries. Missing rows return `[]` or `None` through
`TradingAgentDataReader`.

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

## Calendar Priority

A-share trading-day checks use this order:

1. SharedSignals `reference/market_calendar.py`.
2. Existing external trade calendar files discovered by `Ashare/t_plus_1.py`.
3. Conservative built-in 2026 weekday/holiday fallback.

The fallback is a continuity guard, not a canonical market calendar.
