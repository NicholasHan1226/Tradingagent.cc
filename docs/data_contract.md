# SharedSignals -> TradingAgent Data Contract

## Scope

TradingAgent consumes SharedSignals as the canonical market-data API and
MarketGraph as an optional read-only research-evidence API. This contract
covers the data access layer used by screening and market validation logic. It
does not change execution, accounting, risk, or portfolio write paths.

TradingAgent must be able to complete its own simulated trading loop using
SharedSignals only: market data -> candidates -> scoring -> risk/funding gates
-> simulated fills -> ledger/review -> style evolution. MarketGraph may improve
conviction, but missing MarketGraph rows must not block this base loop.

## MarketGraph Research Inputs

MarketGraph is an optional research-evidence provider, not a market-data
collector or execution path for TradingAgent. TradingAgent may call
`MARKETGRAPH_API_URL` for read-only research evidence such as A-share regime
enhancement, event-impact graph context, and PM independent research
probabilities. If the API requires authorization, `MARKETGRAPH_API_TOKEN` must
be provided by TradingAgent's own runtime environment. TradingAgent cron must
not source MarketGraph deploy env files, so the three systems remain
independently deployable.

When MarketGraph regime, event impact, or sentiment research rows are missing
or unauthorized, A-share scoring records the enhanced research evidence as
missing and falls back to SharedSignals evidence or neutral/degraded scoring.
It must not treat missing research evidence as an execution failure, and it
must not bypass local candidate, funding, risk, or execution gates.

## Canonical SharedSignals Inputs

- SharedSignals API:
  `SHAREDSIGNALS_API_URL` is the production data entry. It reads the
  SharedSignals database/read model behind the service boundary.
- Base analysis coverage:
  TradingAgent should maximize the SharedSignals API surface already exposed by
  `SharedSignalsAPIClient`: market data, realtime 5min, fundamentals,
  reference, macro, capital flow, events, sentiment, PM markets/prices,
  crypto, associations, impacts, industry, and tushare passthrough read models.
  These inputs are consumed through the API facade; TradingAgent must not
  collect directly from providers.
- Evidence API contract check:
  `python -m shared.runtime_test.sharedsignals_evidence_contract --pretty`
  verifies the TradingAgent-facing `/macro`, `/events`, `/sentiment`, and
  `/capital_flow` endpoints. Endpoint/API/schema failures are hard failures.
  Empty but reachable evidence endpoints are recorded as `evidence_debts` by
  default because TradingAgent can degrade those dimensions to neutral scoring;
  use `--strict-empty` when validating a completed SharedSignals backfill.
- Test-only local readers must be dependency-injected. Production environment
  variables cannot enable direct SharedSignals read-model access.
- Schema reference:
  `sharedsignals_schema.py` documents the 11 canonical tables:
  `market_assets`, `market_bars_daily`, `market_bars_intraday`,
  `market_events`, `market_pm_markets`, `market_pm_prices`,
  `market_factors`, `market_ingest_runs`, `market_coverage_status`,
  `market_backfill_status`, `provider_interface_matrix`.
TradingAgent must not import SharedSignals modules, scan SharedSignals
directories, or write to SharedSignals, MarketGraph, or legacy Ashare data
directories.

## Reader API

`shared.data.reader.TradingagentDataReader` is the consumer-facing facade. It
uses SharedSignals HTTP API first; production cron/env defaults set
`SHAREDSIGNALS_API_URL=http://127.0.0.1:8082`. If the API is missing or fails,
production fail-closes instead of silently reading a sibling-system file.

`shared.data.reader.SharedSignalsReader` exposes local read-model methods only
for dependency-injected isolated tests:

- `get_bars_daily(market, symbol, start, end)`
- `get_bars_intraday(market, symbol, interval, start, end)`
- `get_events(market, symbol, start, end)`
- `get_factors(market, symbol)`
- `get_asset(market, symbol)`
- `get_coverage(market, date)`

Rows are returned as dictionaries. Missing rows return `[]` or `None` through
`TradingagentDataReader`.

Event reads use:

- API-first path: SharedSignals `/events?market=<market>&symbol=<symbol>&subject_code=<ts_code>&start=<YYYYMMDD>&end=<YYYYMMDD>`.
- If the HTTP API returns an empty shell or no filtered event candidates in
  production, TradingAgent treats evidence as missing/degraded instead of
  silently reading a sibling-system file.
- A-share code matching accepts `600276.SH`, `SH600276`, and `600276` shapes on
  the upstream event side; this is only a read-side matching guard and does not
  create, promote, or reinterpret events.

5-minute intraday reads use:

- API-first path: SharedSignals `/realtime_5min?market=<market>&ts_code=<symbol>&symbol=<symbol>&date=<YYYYMMDD>&trade_date=<YYYYMMDD>`.
- TradingAgent must still filter returned rows by the requested symbol/ts_code
  because SharedSignals may return a market-level batch when an endpoint version
  does not support one of the symbol parameter aliases.
- A-share research evidence first asks SharedSignals for same-day `rt_min` /
  `stk_mins` symbols through `get_tushare()` and only falls back to the asset
  list when no intraday rows are indexed yet.
- Optional L1 fields are passed through when SharedSignals has them:
  `bid_price`, `ask_price`, `bid_size`, `ask_size`.
- Optional futures contract lifecycle fields are passed through when
  SharedSignals has them: `last_trade_date`, `expiry_date`.

A-share pre-open daily coverage and liquidity ranking use:

- API-first path: SharedSignals `/tushare?api_name=daily&limit=<N>`, consumed
  through `TradingagentDataReader.get_latest_daily_batch("Ashare")`.
- This endpoint reads SharedSignals' existing `market_bars_daily` read model;
  it must not call live Tushare from TradingAgent.
- The pre-open dry-run uses the latest available ordinary A-share date from
  those rows to count coverage and sort candidate universe by `amount`
  (Tushare thousand-CNY units). It then uses single-symbol `/market_data` rows
  only for detailed scoring and execution-gate prices.
- The latest daily batch must cover at least 90% of the API-visible ordinary
  A-share asset universe and must not lag the most recent completed 5-minute
  session evidence. Either condition failing blocks new simulated buys.
- If the batch API is unavailable, TradingAgent fails closed; it does not read
  sibling-system files.

Post-close A-share valuation first runs at 17:40 and has one bounded retry at
22:40 because SharedSignals EOD collection duration varies with provider volume.
An already successful trade date is skipped idempotently.
It requires an exact target-date daily close for every open position before it
refreshes the current 50,000 main account, forward labels, portfolio evolution,
or the close review. Archived 100,000/200,000 tier replays are not production
inputs.

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

## MarketGraph Research Inputs

`shared.data.reader.MarketGraphCSVReader` is a compatibility class name only.
Current MarketGraph research evidence must come through a public API
boundary, not direct CSV files:

- `all_weather_regime.csv`
- REST `/contract?table_id=association_impact_relations` / MCP
  `read_contract_table(table_id="association_impact_relations")`
- `intake/sentiment_signals.csv`

These remain MarketGraph-derived context, not TradingAgent facts. TradingAgent uses
them only for scoring and confidence weighting.

A-share event scoring uses formal MarketGraph impact relations first. Raw
SharedSignals event rows are source announcements and are not directional
impact evidence unless they already carry explicit impact direction fields.
`association_impact_relations` rows are normalized to stock-scoped event rows
with `subject_code=target_id`, `subject_type=stock`, `status=verified`, and
`proposed_impact_hint=polarity`.

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
- API path: SharedSignals `/realtime_5min?market=Futures&ts_code=<contract>&symbol=<contract>`;
  SQLite remains a same-host read-only diagnostic path only with the explicit
  diagnostic switch.
- Optional L1 and lifecycle fields from SharedSignals are preserved through the
  CNFutures runner into simulated order receipts: `bid_price`, `ask_price`,
  `bid_size`, `ask_size`, `last_trade_date`, and `expiry_date`.
- Reader mapping: `CNFutures` internal market is `cn_futures`; upstream reader
  market remains `Futures`.
- Current cadence assumption: intraday CNFutures simulation must use
  5-minute rows already collected by SharedSignals. The preferred upstream is
  Tushare/QuickSync `rt_fut_min`; if that provider lacks permission, the
  5-minute chain must fail/degrade visibly until a separately governed
  collector is added. `fut_daily` remains a daily review input and must not be
  described as 5-minute execution data.
- API health is not trading eligibility. A degraded SharedSignals API response
  may still leave a usable SQLite read model, and a healthy API response does
  not prove real-time, tradable, or account-authorized data.
- Review records are appended under `shared/review/data/`; signal-state writes
  go through `signals/` only. CNFutures does not write back into SharedSignals
  or MarketGraph.
- The live-chain checker reads the actual wrapper cron log
  `shared/logs/cron/job_cn_futures_sim.log`. The simulation entrypoint must
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
