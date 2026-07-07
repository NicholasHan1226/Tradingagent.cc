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
uses SharedSignals HTTP API first; production cron/env defaults set
`SHAREDSIGNALS_API_URL=http://127.0.0.1:8082`,
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
- A-share research evidence first asks SharedSignals for same-day `rt_min` /
  `stk_mins` symbols through `get_tushare()` and only falls back to the asset
  list when no intraday rows are indexed yet.
- Optional L1 fields are passed through when SharedSignals has them:
  `bid_price`, `ask_price`, `bid_size`, `ask_size`.
- Optional futures contract lifecycle fields are passed through when
  SharedSignals has them: `last_trade_date`, `expiry_date`.

A-share reverse repo reads use SharedSignals `/market_data` for `204001.SH`.
SharedSignals owns the `repo_daily` collection and projects those rows into
`market_bars_daily`; TradingAgent treats `close` as annualized percentage yield
for research-only cash sweep estimates.

## PM Research Probability Inputs

PM has two separate read-only upstream roles:

- SharedSignals supplies PM market metadata and prices only, through
  `market_pm_markets`, `market_pm_prices` and the matching HTTP/read-model
  surfaces.
- MarketGraph supplies independent research probability only, through REST
  `GET /pm/research-probabilities` and MCP `read_pm_research_probabilities`.

`PM/research_probability.py` merges MarketGraph research rows with SharedSignals
market prices and writes TradingAgent's local
`shared/review/pm/model_probabilities.jsonl`. Accepted MarketGraph research
fields include `market_id`, `condition_id`, `slug`, `question`,
`research_probability`, `marketgraph_probability`, `confidence`,
`probability_source`, `model_reason`, `evidence_refs` and `as_of`.
Market probability for edge calculation must come from SharedSignals PM
market/price rows. MarketGraph research rows are not allowed to provide fallback
market prices.

SharedSignals rows must not be used as a PM judgment source. If a
SharedSignals PM market/price row contains `research_probability`,
`marketgraph_probability`, `forecast_probability` or similar inline fields,
TradingAgent treats them as ignored upstream noise. If MarketGraph is
unreachable or returns no PM research probability, TradingAgent clears the
local PM model-probability file and PM simulated trading safely has no
independent edge to consume.

## MarketGraph CSV Inputs

`shared.data.reader.MarketGraphCSVReader` reads these read-only CSV outputs:

- `all_weather_regime.csv`
- `intake/event_candidates.csv`
- `intake/sentiment_signals.csv`

These remain MarketGraph-derived context, not TradingAgent facts. TradingAgent uses
them only for scoring and confidence weighting.

A-share event scoring accepts MarketGraph rows with
`status=needs_review|verified|promoted|approved`. `verified` is a reviewed
MarketGraph evidence state and must not be filtered out before scoring.
Event and sentiment matching normalizes common A-share code shapes such as
`600276.SH`, `600276`, and `SH600276`, and may read `subject_code`, `ts_code`,
`symbol`, `code`, or `asset_code`. This is a matching guard only; it does not
lower confidence, status, or candidate thresholds.

`all_weather_regime.csv` is a derived regime snapshot. MarketGraph refreshes it
from formal event-log evidence through `09-AllWeather/tools/marketgraph_regime_snapshot.py`
inside the derived-refresh job; this path does not call external providers and
does not grant trading authority. If the file is missing or stale, TradingAgent
must treat the macro dimension as missing evidence instead of inventing a regime.

`intake/sentiment_signals.csv` is optional context. Sentiment rows only affect a
symbol when they carry a matching `subject_code` and an accepted sentiment state;
global or subject-less sentiment remains background context and should not be
forced into per-stock scores.

## Environment Variables

- `SHARED_SIGNALS_DB`: overrides the SQLite path.
  Default: `/opt/investment/MarketGraphRuntime/read_model/marketdata.sqlite`.
- `MARKETGRAPH_DATA`: overrides the MarketGraph CSV root.
  Default: unset, which disables local CSV fallback. Configure it only in a
  same-host deployment or for an explicitly mounted MarketGraph export
  directory. The value must point to the repo/export root, not the `data/`
  subdirectory.
- `SHARED_SIGNALS_ROOT`: overrides SharedSignals root for importing
  `reference/market_calendar.py`.
  Default: `/opt/investment/SharedSignals`.
- `SHARED_SIGNALS_CALENDAR_ROOT`: optional override for A-share trading
  calendar file discovery. Default: same as `SHARED_SIGNALS_ROOT`.
- `MARKETGRAPH_API_URL`: MarketGraph read-only REST API. Default:
  `http://127.0.0.1:8080` on the combined host.
- `MARKETGRAPH_API_TOKEN`: optional bearer token loaded from the environment;
  never hard-code it in repository files.

TradingAgent receipts default to `signals/sim_execution_receipts.jsonl`.
MarketGraph `outputs/` receipt files are historical compatibility inputs only
when they already exist; new simulated execution receipts must not be written to
MarketGraph.

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
  5-minute rows already collected by SharedSignals. The preferred upstream is
  Tushare/QuickSync `rt_fut_min`; if that provider lacks permission,
  SharedSignals may fill the same read model from its AKShare/Sina fallback with
  `provider="akshare_sina_rt_fut_min"`. `fut_daily` remains a daily
  fallback/review input and must not be described as 5-minute execution data.
- API health is not trading eligibility. A degraded SharedSignals API response
  may still leave a usable SQLite read model, and a healthy API response does
  not prove real-time, tradable, or account-authorized data.
- Review records are appended under `shared/review/data/`; signal-state writes
  go through `signals/` only. CNFutures does not write back into SharedSignals
  or MarketGraph.
- The live-chain checker reads the actual wrapper cron log
  `shared/logs/cron/job_cn_futures_sim.log`, with legacy
  `cn_futures_sim.log` kept only as a fallback. The simulation entrypoint must
  emit a structured JSON result even on unexpected simulated-run errors so
  health checks can surface the cause instead of reporting an unreadable log.
- CNFutures review rows may include `hold_count` and `hold_reason_summary` for
  simulated no-trade decisions. These fields describe strategy/filter reasons
  such as weak signal quality or session restrictions; they are not rejected
  orders and must not be routed to execution queues.
- CNFutures review rows may include `forward_label_summary` and
  `dynamic_threshold_candidates` for simulated strategy calibration. Live rows
  may be `pending_future_bars` until enough later 5-minute bars exist; these
  fields are review/evolution metadata only and must not be treated as broker
  instructions.
- CNFutures post-session calibration may write
  `shared/review/cn_futures/forward_labels.jsonl` and
  `shared/review/cn_futures/win_rate_calibration_report.{json,md}`. These are
  derived review artifacts from simulated signal cards and SharedSignals bars;
  they do not rewrite execution state or SharedSignals data.
- Dashboard consumption should use
  `shared/review/cn_futures/observation_report.json`. The stable UI-facing
  surface is `schema_version`, `dashboard`, `next_validation`,
  `observation_phase`, `alerts`, `data`, `simulation`, `styles`, `evolution`,
  and `real_trading_enabled=false`; nested `source_live_check` remains a debug
  surface for operators.
- The TradingAgent front read model maps `cn_futures` and standard China
  futures exchange suffixes (`.CFFEX`, `.SHFE`, `.DCE`, `.CZCE`, `.INE`,
  `.GFEX`) to the dashboard market label `CNFutures`.

CNFutures has no separate shadow layer. Multi-style testing is represented by
isolated simulated accounts and strategy styles with `capital_layer=simulated`
and `real_trading_enabled=false`.

## Calendar Priority

A-share trading-day checks use this order:

1. SharedSignals `reference/market_calendar.py`.
2. Existing external trade calendar files discovered by `Ashare/t_plus_1.py`.
3. Conservative built-in 2026 weekday/holiday fallback.

The fallback is a continuity guard, not a canonical market calendar.
