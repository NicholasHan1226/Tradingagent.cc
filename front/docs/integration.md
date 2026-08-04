# TradingAgent Front Integration

## Result

The front layer reserves one direct read-only integration endpoint:

`GET /api/trading-agent/snapshot`

This endpoint is the server boundary between TradingAgent and the browser UI.
It is designed for simulated-account display first. Live account status remains
gated and must not trigger execution from the front layer.

TradingCopilot additionally exposes one symbol-scoped read-only projection:

`GET /api/trading-copilot/stock-intelligence?symbol=000400.SZ`

The API reads `runtime/tradingcopilot/stock-intelligence/<symbol>.json` plus a
detached `<symbol>.receipt.json` by default. The receipt must bind the exact
projection SHA-256, symbol, validity window, verifier version and at least one
upstream source receipt. The projection must also carry a typed evidence-strength
contract, four-layer decision readiness, A-share market rules and per-event
source/content receipts. It rejects invalid symbols, non-GET methods, demo
payloads, self-reported verification, symbol mismatch, unbound events, malformed series,
and forecast readiness contradictions. It performs no provider request and has
no fallback. A missing or rejected projection returns 404, after which the UI
shows unavailable or an explicitly enabled demo preview.

Forecast readiness is recomputed from explicit gates in the projection. An
uncalibrated forecast cannot expose probability or coverage labels. Kronos is
represented only as `kronos_challenger` and is subject to the same PIT, frozen
OOS, calibration, sample-count, interval-coverage and cost-policy gates as the
linear baseline. Formal readiness also requires receipt-bound model manifest,
PIT/revision evidence, frozen OOS, calibration, interval coverage, cost policy,
same-input baseline comparison and positive post-cost utility. A model name never
bypasses those gates.

The research terminal also reads its current A-share tracking list through
`GET /api/trading-copilot/tracking-universe`. The endpoint reads the regular,
non-symlinked server-local file `runtime/tradingcopilot/tracking-universe.json`
by default (or an explicitly configured absolute path), validates
`tradingagent.trading_copilot_tracking_universe.v1`, and serves GET only. Each
item is only a `symbol`/`name` mapping for the session's active universe; it is
not an account holding, investment recommendation, quote, or prediction. A
missing, malformed, or untrusted file returns 404 and the UI deliberately shows
“waiting for current list projection” instead of filling the list with demo
stocks. The A-share session writer is responsible for producing this artifact;
the frontend never creates or edits it.

Event evidence is exposed separately through
`GET /api/trading-copilot/event-timeline?symbol=000400.SZ`. The endpoint reads
the regular, non-symlinked pair
`runtime/tradingcopilot/event-timeline/<symbol>.json` and
`<symbol>.receipt.json` by default (or an explicitly configured absolute
directory). It serves GET only after checking the raw timeline SHA-256, symbol,
identical generation/validity fields, a current validity window, and the exact
set of source-receipt ID/SHA pairs referenced by events. Missing, expired,
malformed, or unbound artifacts return 404; the front layer never calls a
provider or invents sentiment. Empty event lists and blocked-source coverage
remain visible as evidence availability, not as a recommendation or a data
health assertion.

The private front unit sets `TRADING_COPILOT_EVENT_TIMELINE_DIR` to
`/var/lib/tradingagent/trading-copilot/event-timeline`, so its read-only route
uses the same server-local publication root as the event-timeline publisher.
The directory is not a source-tree fallback.  A releasable front candidate must
also include the generated `front/dist-server/server/tradingAgentSnapshotHttp.js`
artifact from `npm run build:all`; this artifact is intentionally not tracked in
Git, and the existing unit refuses to start when it is absent.

TradingCopilot personal state uses `GET/PUT /api/trading-copilot/state`. `GET`
returns an `ETag`; every `PUT` must send that value as `If-Match`. The server
serializes writes, rejects stale revisions with `409`, and verifies state hash,
previous-state hash, sequence and event hash across the append-only JSONL before
returning any state. Browser fallback is explicitly labelled as an unsynchronized
local draft and never silently overwrites a newer server revision.

## Current TradingAgent Surfaces

| Front result | Preferred source | Fallback / supporting source | Status |
| --- | --- | --- | --- |
| Current opportunities | compatibility read-only `signals/*.json`; A-share pending requires V1 authority/freshness or is excluded | completed queue/sim-ledger projection only | Partial; not V1 opportunity authority |
| Legacy opportunity funnel | `shared/review/opportunities/funnel_events.jsonl` | `shared/logs/opportunities/funnel_events.jsonl` | Frozen forensic history only; writer retired; excluded from current readiness |
| Positions | A-share: verified current capital snapshot -> derived execution-lineage position receipt; other markets: `signals/positions/*.json` | A-share has no fallback; generic non-A-share may use `shared/accounting/position_plan.jsonl` | Partial |

> A股当前模拟执行只从受验证capital/execution-lineage/RunBundle投影读取；`signals/*`仅保留为非A股兼容/法证投影。前端不得启动任何执行器，也不得把旧pending queue显示成当前A股机会。
| Performance | `shared/review/{portfolio,daily,*}/{equity_snapshots,equity_series}.jsonl` or `shared/logs/sim_ledger/*/*/daily_mark_to_market.jsonl` | `shared/review/daily/daily_brief.jsonl` explicit return fields only | Partial; missing authority stays unavailable |
| Active market summaries | A-share/CNFutures dedicated authorities below plus current market-specific sim-ledger facts | no retired StyleRunner fallback; never cross-market money aggregation | Partial |
| A-share capital | `shared/logs/capital/ashare/ashare_sim_capital_latest.json` | unavailable; never infer from another market | Ready; strict authority/generation/fresh/reconcile/checksum |
| CNFutures capital | `shared/logs/capital/cn_futures/cn_futures_sim_capital_latest.json` | unavailable; never infer from A-share | Ready; strict authority/generation/fresh/reconcile/checksum |
| Crypto capital/equity | current `shared/logs/sim_ledger/crypto/*/daily_mark_to_market.jsonl` equity evidence | unavailable; positions-only and frozen style reports never synthesize capital | Partial; native USDT only, no fixed FX conversion or CNY fallback |
| A-share research evidence | `shared/review/ashare/research_evidence_latest.json` | omitted from snapshot when missing or malformed | Ready |
| A-share sample KPI / maturity | `shared/review/ashare/projection_current.json` -> hash-verified `projection_generations/<generation_id>/{sample_kpi_latest,evolution_decision_latest,market_maturity_latest}.json` | entire set omitted when pointer/manifest/file hash, recomputed generation ID, shared input SHA, authority, or explicit sim-only fields are missing/invalid; root mirrors are never a transaction fallback | Ready |
| CNFutures maturity | `shared/review/cn_futures/market_maturity_latest.json` | omitted when canonical `projection_sha256`, authority, lineage, or sim-only contract is invalid | Ready |
| Optional market pulse | Explicit TradingDatas V1 `GET /v1/catalog` + `POST /v1/query` selected from current holdings/signals | `marketPulses[]` is omitted per unavailable/degraded dataset while `marketPulseCoverage` retains exact source coverage | Fixture/mock-first candidate only; explicit base/catalog/schema/policy/dataset mapping, no legacy fallback, no fresh handoff or live claim |
| CNFutures replay evidence | `shared/review/cn_futures/replay_latest.json` | omitted from market summary when missing or malformed | Ready |
| Today paper-day summary | optional local candidate `shared/runtime/run_bundles/latest.json` plus byte-identical `shared/runtime/run_bundles/runs/<run_id>/<bundle_sha256>.json` | `paperDayRun` omitted when either file, strict manifest, component/payload/bundle hash, run identity, idempotency binding, or simulation-only flags fail; no sample fallback | Candidate active reader only; fixture CLI intentionally publishes under `shared/runtime_test/phase1_paper_fixture/`, so scheduler and active-root publication remain unverified |
| Decisions | daily review and attribution JSONL files | strategy version history | Partial |
| Risk | `shared/risk/risk_limits.yaml` | current signal and runtime evidence | Ready |
| Execution / live readiness | `shared/governance/market_lanes.yaml` and `system_state_matrix.yaml` plus market-specific simulated lineage receipts | separately authorized market-specific broker adapter readback | Gated; A-share/CNFutures/Crypto APIs remain distinct and live is disabled |

The homepage maturity panel never aggregates capital across markets. It shows
the A-share fresh 50,000 CNY account with Day 5 / Day 10 evidence reviews and
the independent CNFutures fresh 50,000 CNY account with longer-horizon
simulation maturity. Missing or invalid projections stay in an evidence-pending
state. The panel always states that automatic promotion is disabled.

## Read-Only Contract

The browser fetches `TradingAgentReadModelSnapshot` through
`createTradingAgentSnapshotClient()`.

The server can wrap a local reader with `getTradingAgentSnapshotResponse()`.
The response must use `Cache-Control: no-store`.

Display-ready fields used by the homepage:

- `performance[]`: `day`, `simulated`, `target`, `benchmark`, `opportunity`.
- The highest-trust simulated performance source is an explicit equity snapshot
  series. The reader accepts:
  - `shared/review/portfolio/equity_snapshots.jsonl`
  - `shared/review/daily/equity_snapshots.jsonl`
  - `shared/review/*/equity_snapshots.jsonl`
  - the same folders with `equity_series.jsonl`
  - `shared/logs/sim_ledger/*/*/daily_mark_to_market.jsonl`
  - `shared/logs/sim_ledger/*/*/equity_snapshots.jsonl`
- Equity snapshot rows should include `capital_layer=simulated` plus timestamp
  and value fields such as `timestamp`, `total_equity` or `equity`,
  `capital_base`, `realized_pnl`, `unrealized_pnl`, `target_return_pct`,
  `benchmark_return_pct`, `opportunity_gap_pct`, `max_drawdown_pct`,
  `trade_count`, and `pnl_source`. Rows marked `real_execution=true` or
  `capital_layer=real` are ignored by the simulated dashboard reader.
- Backend writer: `PYTHONPATH=/opt/investment/tradingagent python3 shared/runtime_test/write_equity_snapshots.py --pretty`
  appends one simulated mark-to-market snapshot per style ledger into
  `shared/logs/sim_ledger/<market>/<style>/daily_mark_to_market.jsonl`.
  It reads existing simulated positions. Any future external close price must
  arrive through the validated TradingDatas V1 boundary; without that evidence it marks
  missing prices as `sim_ledger_cost_fallback` rather
  than inventing return.
- If explicit equity snapshots are absent, the local reader accepts daily review
  aliases such as
  `simulated_return_pct`, `return_pct`, `pnl_pct`, `target_return_pct`,
  `benchmark_return_pct`, and `opportunity_gap_pct`.
- If daily review return fields are absent, performance remains unavailable.
  The reader never derives current returns from trade notional, position cost,
  or retired `style_performance.jsonl` / `style_comparison.json` artifacts.
- Retired StyleRunner/PerformanceTracker artifacts may remain on disk only as
  frozen forensic history. They do not affect current market summaries,
  readiness, trade counts, PnL, holdings, evolution, or execution. A `style`
  field in current SampleJournal rows is only a research grouping for the three
  active strategy sleeves, not legacy runtime authority.
- When the verified A-share capital snapshot identifies a safe current execution lineage,
  the reader derives `shared/logs/execution_lineages/<execution_lineage_id>/` from that
  snapshot rather than a date-coded constant, and may attach
  `portfolio.ashareAccount`. This object is a display-only account fact layer
  with `cashAvailable`, `marketValue`, `accountEquity`, `accountTotalPnl`,
  `accountReturnPct`, `openPositionCount`, `totalSampleCount`,
  `validationSampleCount`, `strategySampleValidCount`, optional
  `strategyTotalPnl`, optional `strategyMarketValue`, optional
  `strategyOpenPositionCount`, `source`, and `updatedAt`.
- A-share capital `updated_at` and the derived position receipt's `synced_at` must be timezone-aware,
  no more than 36 hours old, and not future-dated relative to the snapshot clock. Stale or missing
  authority is omitted/fail-closed. The dashboard generation timestamp is never substituted for
  missing market evidence.
- `portfolio.ashareAccount` separates account facts from strategy-valid
  samples. Filled A-share rows without `candidate_pool_layer=candidate` plus
  `execution_source=ashare_candidate_layer` for buys, or without
  `execution_source=ashare_rebalance_sell` for sells, remain visible as account
  facts and chain-validation samples, but do not count toward strategy PnL,
  win rate, attribution, or self-evolution.
- Strategy-valid A-share buy/sell samples must also carry fill price
  provenance from market data, such as `fill_price_source_class=market_data`
  or a `fill_evidence.fill_price_source` pointing to an order/config market
  snapshot price. Rows filled only from a signal-card/requested price remain
  account facts and chain-validation samples until market-data provenance is
  present.
- A-share account display may use `pnlSource` values
  `ashare_local_sim_account`, `ashare_local_sim_mark_to_market`, and
  `ashare_local_sim_trade_price_fallback`. These are read-only display labels;
  the front layer must not turn them into execution actions.
- If the `portfolio` summary is built from the A-share local simulation
  account fallback, it carries `pnlCurrency=CNY` and is valid only for the
  `A-share` view. A mixed/multi-market monetary portfolio is not a supported
  authority. Attaching `ashareAccount` must not leak A-share money into another
  market or the `All Markets` view.
- Trade journals and position cost are not valid performance sources by
  themselves. When only those files exist, `domains.performance.status` remains
  `empty` with a message explaining the missing PnL / return series.
- `signals[]`: `symbol`, `market`, `status`, `impact`, `confidence`,
  `reason`, `next`, `steps`, plus optional funnel fields `stage`,
  `stageTimes`, and `stageLatencyMinutes`.
- `marketSummaries[]`: one read-only status row per active dashboard market.
- `marketPulses[]`: optional representative-instrument rows enriched only through the
  configured TradingDatas V1 catalog/query boundary. Each row contains `market`, `symbol`,
  `lastPrice`, optional `changePct/high/low/volume/updatedAt`, `freshness`, sourced `points[]`,
  and a provider-neutral source identity composed from dataset ID and receipt ID. The reader
  selects at most one current holding or signal symbol per market, requests at most 24 rows,
  times out after 900ms, caches for 15 seconds, and never calls a provider, write route,
  legacy endpoint or sibling SQLite. Catalog version, dataset identity, `as_of`, freshness,
  quality, lineage and receipt are verified before a pulse can be displayed. Each returned row
  must explicitly match the requested entity, and its timestamp cannot exceed either
  `metadata.data_through` or the decision time.
- `marketPulseCoverage`: optional read-only diagnostics for `A-share`, `CNFutures`, and `Crypto`. It contains `entries[]` with `sourced`, `no_representative`, `unavailable`, or `degraded` status plus `requestedCount`, `sourcedCount`, `cacheState`, `fetchedAt`, and `sourceLatencyMs`. A cached result preserves its original fetch time and labels its cache state rather than pretending to be a new source read.
- `marketPulseCoverageHistory`: optional bounded in-process observations of fresh TradingDatas reads. It retains at most 12 entries, adds no sample on cache hit, and resets on snapshot-service restart. It is terminal observability only, not a durable health or SLA history.
- `paperDayRun`: optional read-only summary of the latest explicitly published
  local A-share paper-day RunBundle. It carries `environment=local_candidate`,
  `productionVerified=false`, stage progress, dataset evidence state, simulation
  execution eligibility, candidate/decision/simulated-order/fill counts,
  no-trade reasons, risk blocks, Champion manifest identity, and the LLM
  evidence-only state. The reader accepts only
  `contract_id=tradingagent.paper_day_loop.v1`, `market=ashare`,
  `account_type=simulated`, and `real_trading_enabled=false`, with ordered stage
  receipts. Missing or unsafe input omits the field. The browser then renders an
  honest unavailable state; it never substitutes fixture or demo execution.
  Overall evidence may display `degraded` when optional context is explicitly
  deweighted, while simulation eligibility still requires at least one accepted
  `required_execution` dataset, `execution_eligible=true`, valid position
  authority, and no run-level risk block.
  The reader combines existing signals, holdings, verified market-specific
  capital, current simulated ledgers, and explicit equity/return authorities.
  Frozen StyleRunner artifacts are excluded. This lets the front show why a
  selected market has data, partial data, or no data without inventing trades.
- `marketPulses[]`: optional sourced price context for representative symbols already present
  in holdings or signals. The snapshot server requires all five explicit inputs:
  `TRADINGDATAS_API_URL`, `TRADINGDATAS_CATALOG_VERSION`,
  `TRADINGDATAS_SCHEMA_MAJOR`, `TRADINGDATAS_ACCESS_POLICY_ID` (a local
  cache/audit namespace, never an HTTP auth header), and
  `TRADINGDATAS_MARKET_PULSE_DATASET_IDS_JSON`. Missing or invalid config performs no HTTP
  call. A failed, stale, degraded, identity-mismatched or ambiguous V1 response omits the pulse;
  it does not fail the snapshot, synthesize movement, or try a legacy route.
- CNFutures current runtime status uses the latest actionable row from
  `shared/review/data/cn_futures_sim_reviews.jsonl` as the authoritative
  source. Frozen `shared/review/cn_futures/style_comparison.json` is ignored;
  it cannot supplement or alter latest review counts.
- CNFutures market summaries may attach `cnFuturesReplayEvidence` from
  `shared/review/cn_futures/replay_latest.json`. This object is display-only
  and separates historical actionable replay counts from currently executable
  candidates with `execution_eligible` and non-executable reasons.
- Crypto performance requires its own current USDT authority and current
  explicit equity snapshots. The front never converts a retired style artifact
  into current CNY PnL and never divides one market's PnL by another market's
  capital base. `capitalBase`, equity, realized/unrealized PnL, holdings and
  simulated trade notional remain native `USDT`; missing authority stays
  unavailable, and `USD` or `CNY` is never used as a display alias for USDT.
  The active front supports only `CNY` for A-share/CNFutures and `USDT` for
  Crypto. A retired `USD` payload renders unavailable instead of falling back
  to `$`. Multiple Crypto ledger/account directories also remain unavailable
  until an explicit shared-account authority permits aggregation.
- Market switching is strict. Selecting `A-share`, `CNFutures`, or `Crypto`
  filters signals and holdings to that market. It must not fall
  back to all-market rows when the selected market has no records.
- `All Markets` never has a monetary performance curve. It may aggregate only
  non-monetary counts and health. A selected market may show its own history or
  a single current-return point from its `marketSummaries[]` row; it must never
  use another market's curve or a cross-market capital base.
- Supported dashboard market labels are exactly `A-share`, `CNFutures`, and
  `Crypto`, plus the non-monetary `All Markets` aggregate. CN futures symbols
  such as `IF2601.CFFEX` and backend market labels such as `cn_futures` map to
  `CNFutures`; rows from retired market directories are ignored.
- Compatibility signal timestamps may be supplied as `discovered_at`,
  `scored_at`, `debated_at`, `risk_checked_at`, and `triggered_at`. Mapping them
  into `发现 / 研判 / 风控 / 待确认 / 结果` creates a derived queue projection,
  not proof that each explicit event happened.
- `funnelEvents[]` is a read-only display union with distinct source classes.
  The two historical JSONL paths are parsed only as
  `legacy_frozen_opportunity_log`; `signal_queue` is a derived compatibility
  projection; `sim_ledger` is completed replay. Legacy events cannot make
  signals/risk ready, drive the current heartbeat, or link current holdings/PnL.
  Future current opportunity display requires a separately verified
  OpportunityLedger read-only projection; it is not implemented here.
- Explicit opportunity event rows may use either snake_case or camelCase:
  `opportunity_id/opportunityId`, `event_id/id`, `symbol/ts_code`, `market`,
  `stage`, `status`, `timestamp/at/ts/created_at/updated_at`,
  `latency_minutes/latencyMinutes`, `terminal`, `label`, and `reason`.
  English stage/status values such as `discovered`, `research`, `risk`,
  `pending`, `filled`, `blocked`, `missed`, and `cancelled` are normalized to
  the Chinese display stages and outcomes.
- `opportunityId` is the stable display key for one opportunity. The front
  groups funnel particles by this id first, then falls back to market + symbol
  only when older records do not provide it. Upstream signal rows may provide
  `opportunity_id`, `signal_id`, `trace_id`, `id`, `card_id`, or `order_id`;
- The presentation URL may include `opportunity=<opportunityId>`. This key only selects and filters existing `funnelEvents[]`; it never creates a relationship, changes queue state or becomes an execution parameter.
- `holdings[]` may carry optional `opportunityId`, `realizedPnl`, and `unrealizedPnl` only when explicit source fields provide them. The linked opportunity strip joins signals and holdings exclusively on equal explicit IDs; matching symbols alone must not create PnL attribution.
- `signals[]` and `holdings[]` may carry `marketDataSymbol` only from `market_data_symbol` / `marketDataSymbol` source fields. Non-A-share pulse requests require this field. A-share uses its exchange-qualified `ts_code` as its compatible explicit identifier. A-share local simulated position rows may expose `order_id` only when all recorded buy origins for the aggregated open position agree; the reader treats that order ID as an explicit correlation key and keeps mixed-origin rows unlinked.
  otherwise the read model derives a stable id from market, symbol, queue
  bucket, and filename without exposing server paths.
- `sequence` should increase from discovery to result. The current event stages
  map to `1=发现`, `2=研判`, `3=风控`, `4=待确认`, `5=结果`. `terminal=true`
  marks the event that ends the current path, such as a fill, block, review, or
  final result.
- Simulated ledger rows may provide `opportunity_id`, `signal_id`, `trace_id`,
  `order_id`, or `card_id`. When one of these matches the queue row, the reader
  keeps discovery, risk, and final result in one visible funnel path instead of
  showing duplicate opportunities.
- Missed, expired, failed, and cancelled rows are terminal review outcomes.
  They should be shown as review/abandoned results, not counted as current
  pending opportunities.
- The homepage treats only an explicit, current event source as a future true
  opportunity funnel. Current code has no such source: legacy JSONL stays
  frozen, queue rows remain derived projections, and sim-ledger rows remain
  completed replay. Equal symbol alone never upgrades or joins these classes.
- Homepage view portfolio: the browser derives the visible portfolio from the
  active market. `All Markets` returns no monetary portfolio; it displays
  non-monetary counts/health only. `A-share` may show
  `portfolio.ashareAccount`; CNFutures and other selected markets derive a
  compact portfolio from their own `marketSummaries[]` row. The header,
  realtime-return panel, and summary rail must use that same single-market
  identity.
- `ashareResearchEvidence`: optional read-only homepage rail input from
  `shared/review/ashare/research_evidence_latest.json`. It summarizes opening
  auction or `first_5m_proxy` evidence, closing momentum candidates, labels,
  204001 cash-management suggestions, and style shadow attribution. Shadow
  attribution is not spendable capital. The front layer must treat all of it
  as display evidence only and must never turn it into orders, queue writes,
  emails, or callbacks.
- Canonical A-share sample evidence is the complete generation selected by
  `projection_current.json`, including `sample_kpi_latest.json`,
  `evolution_decision_latest.json`, and `market_maturity_latest.json`, all
  derived from one SampleJournal input hash. The pointer seals the generation
  manifest content SHA, and the reader recomputes the generation ID from that
  input SHA plus the canonical three-file SHA map; a missing/invalid pointer,
  forged ID, any manifest/file tamper, or a missing explicit false safety field
  omits the whole set. Root-level mirrors are compatibility outputs, not a
  frontend transaction point. The retired
  forward-validation projection is not an evolution authority and must not be
  used to authorize risk or live transition.
- The homepage may animate explicit current stage movement only after the
  OpportunityLedger projection has its own current/fresh/authority contract.
  Until then it labels queue-derived paths as status projections, old JSONL as
  frozen history, and simulated-ledger paths as completed replay; it does not
  infer upstream drop-off.
- If `funnelEvents[]` and `signals[]` are both empty but `holdings[]` exists,
  the homepage renders a holding flow (`当前持仓 / 收益贡献 / 风险检查 / 继续持有 /
  复盘记录`). This is deliberately not labeled as a new-opportunity funnel. It
  answers what is happening to the current account while preserving the fact
  that no new trade signal is available. The holding flow should summarize
  position count, positive contributors, watch items, and risk state instead
  of showing internal system wording.
- `holdings[]`: `symbol`, `market`, `weight`, `pnl`, `risk`, and `role`.
  `weight` may be a percentage such as `12.8%` or a formatted exposure amount
  such as `$1,022` / `¥7,207`; frontend summaries must parse the unit before
  aggregating. Mixed amount/percentage batches should show a waiting or
  normalization state instead of pretending both units share one allocation
  scale.
- A-share server-local simulated trades are valid strategy samples only when
  both candidate provenance and fill-price provenance exist. The local ledger
  and signed receipts should carry `candidate_pool_layer`, `execution_source`,
  `fill_price_source`, `fill_price_source_class`, and `fill_evidence`; missing
  fill provenance is a chain-validation sample, not strategy PnL.

## Result-First Panel Rules

Every primary page should start with a compact summary board before detailed
charts or tables. The summary board must answer the user's first question for
that page, such as current return, actionable opportunities, position
contribution, decision throughput, risk boundary, or review outcome.

Panel numbers must be derived from the snapshot fields passed into the React
page, not from production-looking constants in component files. If a source is
missing, show a clear empty or waiting state instead of substituting sample
return, opportunity, position, attribution, or risk values.

Sample dashboard data is allowed only for local development or explicit
`VITE_TRADING_AGENT_DEMO_PREVIEW=1` review. Production builds must show a
waiting/unavailable state when the snapshot API is unavailable; they must not
display sample money, opportunities, holdings, or funnel events as if they were
live results.

The homepage funnel is a read-only result view, not a decorative or execution
flow. It preserves source identity: explicit current events (future contract),
derived queue projection, legacy frozen history, and completed replay must not
be merged into one live claim. When no verified current source exists, the
frontend shows the appropriate projection/replay/history/waiting label rather
than claiming a full opportunity funnel.

When the view falls back to holdings, the frontend must show it as a holding
flow, not as a trade-signal funnel. The visual style should stay close to the
Hyperliquid-inspired surface: dark base, hairline borders, restrained cyan for
healthy state, amber/red only for watch or blocked states, and no decorative
glow blocks.

## Authenticated Internal-Use Deployment

The preferred first production shape is to keep the dashboard frontend and the
read-only snapshot API on the TradingAgent production server. This is a
single-user internal system: a local browser may use the server-local surface,
while `tradingagent.cc` may provide convenient remote access only after
Cloudflare Access or equivalent single-user authentication. The browser does
not read the filesystem directly. It loads a static Vite build through the
authenticated edge and Nginx, then fetches one same-origin snapshot route:

`GET /api/trading-agent/snapshot`

Recommended production shape on the TradingAgent production host:

1. Nginx serves `front/dist` as the frontend.
2. Nginx proxies `/api/trading-agent/snapshot` to
   `127.0.0.1:8787/api/trading-agent/snapshot`.
3. The Node snapshot service reads the verified TradingAgent workspace.
4. The API returns only display-ready snapshot JSON.

Last documented deployment shape (not revalidated by the local capital-growth refactor):

- Host: `8.138.181.177`
- Workspace: `/opt/investment/tradingagent`
- Front source: `/opt/investment/tradingagent/front`
- Node runtime: `/opt/investment/tools/node-v24.4.1/bin/node`
- Service: `tradingagent-front-api.service`
- Nginx site: `/etc/nginx/sites-available/tradingagent-front`
- Internal API: `127.0.0.1:8787`
- Historical server names: `dashboard.tradingagent.cc`, `tradingagent.cc`,
  `www.tradingagent.cc`; the preferred personal entry is `tradingagent.cc`.
- DNS, Tunnel, Access policy, service, and Nginx runtime state must be verified
  independently during an explicitly authorized release. A reachable domain
  without a verified single-user policy is a blocker, not production proof.

When the frontend and API share the same domain, the frontend can use the
same-origin route:

`VITE_TRADING_AGENT_SNAPSHOT_URL=/api/trading-agent/snapshot`

Do not put secrets, local filesystem paths, execution tokens, account
credentials, or order mutation routes in Vite environment variables. `VITE_*`
values are public browser configuration.

## Hosted Snapshot API

The repository includes a standalone Node API server for the production
snapshot route:

```bash
npm run build:api
FINANCE_WORKSPACE_ROOT=/opt/investment/tradingagent \
TRADING_AGENT_SNAPSHOT_HOST=127.0.0.1 \
TRADING_AGENT_SNAPSHOT_PORT=8787 \
TRADING_AGENT_SNAPSHOT_CORS_ORIGINS=https://tradingagent.cc \
/opt/investment/tools/node-v24.4.1/bin/node dist-server/server/tradingAgentSnapshotHttp.js
```

The server rejects non-loopback listen hosts (including `0.0.0.0`) and wildcard
CORS. Cloudflare Tunnel or same-host Nginx must reach the API through
`127.0.0.1`; remote browser origins must be listed exactly.

Routes:

- `GET /healthz`
- `GET /api/trading-agent/snapshot`
- `OPTIONS /api/trading-agent/snapshot`

Security boundary:

- Keep the API bound to `127.0.0.1` behind a reverse proxy when possible.
- Use HTTPS at the proxy layer.
- Require `Authorization: Bearer <token>` when the endpoint is not fully
  private. If a token is enabled and the browser uses a same-origin route,
  inject the token at the proxy layer; do not send it from browser JavaScript.
- Allow only the dashboard origin in
  `TRADING_AGENT_SNAPSHOT_CORS_ORIGINS`.
- Never expose TradingAgent execution, order mutation, callback, account,
  credential, or 2FA routes through this API.
- Never put `TRADING_AGENT_SNAPSHOT_API_TOKEN` in a `VITE_*` variable.

Frontend configuration:

```bash
VITE_TRADING_AGENT_SNAPSHOT_URL=/api/trading-agent/snapshot
npm run build
```

If the frontend and API share the same domain and path, the frontend can also
omit `VITE_TRADING_AGENT_SNAPSHOT_URL` and use the same-origin
`/api/trading-agent/snapshot` fallback.

## Nginx Shape

Example route shape for the production server:

```nginx
server {
  listen 443 ssl;
  server_name tradingagent.cc;

  root /opt/investment/tradingagent/front/dist;
  index index.html;

  location / {
    try_files $uri /index.html;
  }

  location /api/trading-agent/snapshot {
    proxy_pass http://127.0.0.1:8787/api/trading-agent/snapshot;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Authorization "Bearer server-only-token";
  }
}
```

If the internal API is bound to `127.0.0.1` and only reachable through the same
server Nginx process, the token can be omitted by leaving
`TRADING_AGENT_SNAPSHOT_API_TOKEN` unset. If the token is set, Nginx must inject
the `Authorization` header as shown above.

## Production Service Shape

Keep the API as a local service and let Nginx handle only the authenticated
remote HTTPS surface. Cloudflare Access or equivalent authentication must run
before this route; Nginx availability alone is not an authorization boundary.
The canonical unit is tracked at
`deploy/systemd/tradingagent-front-api.service`; the installed
`/etc/systemd/system/tradingagent-front-api.service` must be byte-identical to
that file and must have no drop-ins.

The service process uses the dedicated `tradingagent:tradingagent` primary
identity and executes immutable release bytes. During the legacy projection
migration it retains `marketgraph` only as a supplementary Unix read group for
existing simulation artifacts. The systemd sandbox grants no writable legacy
path and makes `/run/secrets/tradingagent` inaccessible to the front process;
this compatibility group is not a MarketGraph service/API dependency.
The front unit explicitly leaves all TradingDatas settings empty and never
reads `/run/secrets/tradingagent/tradingdatas-read.token`; authenticated
TradingDatas collection belongs to the separate A-share observation worker.
Remove the supplementary group only after the front reads a dedicated
TradingAgent projection root and a fresh snapshot parity check passes.

Production verification:

- `curl http://127.0.0.1:8787/healthz` returns `ok`.
- `curl http://127.0.0.1:8787/api/trading-agent/snapshot` returns JSON when
  the service is bound to localhost and token auth is unset.
- A request to `tradingagent.cc` without an authenticated session is denied or redirected to the identity gate; it never returns dashboard data anonymously.
- The authenticated Nicholas session loads the React app through `tradingagent.cc`.
- The authenticated same-origin `/api/trading-agent/snapshot` route returns JSON, while a direct API hostname is absent or denied.
- The snapshot response reports simulated display data and does not expose
  execution, account, credential, callback, or mutation routes.

Rollback:

- Keep the previous `front/dist` and `front/dist-server` build directories or
  redeploy the previous Git commit.
- Restart only the local snapshot API service after rolling back server files.
- Nginx can be reverted independently because it only serves static files and
  proxies the read-only route.

The route may read:

- compatibility `signals/{pending,claimed,running,filled,cancelled,expired,failed,partial}/*.json` under the source restrictions above
- frozen forensic `shared/{review,logs}/opportunities/funnel_events.jsonl`, never as current readiness
- `signals/positions/*.json`
- `shared/accounting/position_plan.jsonl`
- `shared/review/daily/daily_brief.jsonl`
- frozen `shared/review/*/{style_performance.jsonl,style_comparison.json}` may
  remain on disk for forensic audit but are deliberately not read by this route
- `shared/review/attribution/*.jsonl`
- `shared/logs/sim_ledger/*/*/{positions.json,trade_journal.jsonl}`
- `shared/logs/capital/ashare/ashare_sim_capital_latest.json`, then
  `shared/logs/execution_lineages/<verified execution_lineage_id>/{simulated_ashare_positions.json,local_sim_trades.jsonl}`
- `shared/risk/risk_limits.yaml`
- `shared/runtime/run_bundles/latest.json` as an optional, simulation-only local candidate snapshot

The route must not:

- write to `signals/`
- claim, cancel, expire, fill, or mutate signal cards
- import execution routers as action surfaces
- send orders, emails, webhooks, or account callbacks
- merge different account layers into one result number
- turn `paperDayRun` or LLM evidence into an order, queue mutation, approval, or production-readiness claim

## Current Gap

The local Vite dev/preview runtimes and server-side read-only API use the same
snapshot contract. Local preview data is allowed only in development or when
`VITE_TRADING_AGENT_DEMO_PREVIEW=1` is explicitly set. A production endpoint
failure must render unavailable/waiting state; it must never activate preview
returns, opportunities, or holdings. A valid empty domain remains a real empty
state.

The repository supports a static frontend plus the server-side snapshot API,
but this refactor did not verify which hosting path is currently live. Pages,
Tunnel, Nginx, service runtime, DNS, Access policy, and the authenticated remote
route are separate release checks. Every shape must preserve the same read-only
boundary: no anonymous access, execution, callback, or order mutation route
belongs to this dashboard.

The data gap is now narrower: verified server-local simulated ledger positions
can feed holdings, while trade journals feed completed replay rather than a
current opportunity funnel. Current return curves still require explicit equity
snapshots or daily-review return fields; frozen StyleRunner artifacts cannot
fill that gap. `midday_review.jsonl`, strategy/factor attribution JSONL,
`risk_limits.yaml`, richer per-signal stage records, and normalized
mark-to-market return series still need upstream data before the UI should
present them as complete panels. The frontend must not infer returns from trade
notional or cost basis.

The Today panel reader is implemented. It treats the projection as untrusted input: it requires
strict keys, recomputes component/payload/bundle hashes and the run ID, checks the receipt and
idempotency bindings, and requires a byte-identical immutable mirror under
`shared/runtime/run_bundles/runs/<run_id>/<bundle_sha256>.json`. The Python local candidate includes
a durable event store, `LocalRunBundlePublisher`, explicit offline composition,
and `tools/run_phase1_paper_fixture.py`. That CLI is deliberately restricted to
frozen responses and publishes a non-authoritative projection to
`<output-root>/shared/runtime_test/phase1_paper_fixture/run_bundles/latest.json`.
It intentionally does not target the active reader path and cannot create a “Today” run.
There is no accepted
scheduler, fresh TradingDatas handoff, or real paper-session readback.
Until those are independently implemented and verified, the expected
user-facing state is “Today RunBundle unavailable”. Repository tests, fixture
fills and isolated local artifacts are not production or market-runtime
evidence and must never be manually copied to manufacture a current run.
