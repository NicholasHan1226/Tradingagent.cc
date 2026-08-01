# AShare fixture 20-day loop

`twenty_day_fixture_loop.py` is an offline, fixture-only minimum vertical
slice for exactly 20 supplied fixture-session dates. Each date records evidence
eligibility, the mainboard-only universe, candidate/risk result, a simulated
counterfactual receipt or a reason code, fixture reconciliation, and
sample/review entries.

Each day carries a structured fixture Evidence Gate: exact TradingDatas routes
`GET /v1/catalog` and `POST /v1/query`, `ready` state, non-degraded fresh
and valid quality, lineage/receipt IDs, calendar eligibility/lineage, and a
timezone-aware available time plus decision time on the same Shanghai trading
session. Available evidence must not be later than the decision time. Any
missing, stale, degraded, failed, future, or mistyped field blocks candidate
selection and orders. Invalid evidence creates one `data_reject` sample only;
it creates no universe observation, candidate, or simulated fill. No dataset ID
is invented. It has no external market-data client, network access, SQLite
access, broker, LLM, scheduler, outbox, or runtime-ledger write side effect. It
does read the canonical A-share policy as its single capital/risk authority.

All output is a non-authoritative fixture-only counterfactual: receipts use
`simulated_filled`, set `execution_authority=false` and `durable=false`, and
have null `capital_commit_id` and `outbox_id`. Samples are never
`execution_eligible` or training-eligible; `simulated_fill_observed` is only a
closed-loop statistic. Fixture reconciliation is explicitly
`fixture_reconciled` (or blocked), never durable. A future market runtime may
become execution-eligible only after market capital commit, durable
outbox/ledger, and complete execution lineage exist. This fixture validates
mathematics, gates, and the 20-session loop only.

Each `FixtureDay` must explicitly supply a current `mark_prices` mapping for
every open position. Cost basis and mark are separate: reconciliation reports
realized/unrealized PnL, market value, equity, and gross exposure from current
marks. A missing or invalid mark blocks reconciliation and all new simulated
orders for that day; it never silently reuses the entry price. Every held symbol
also needs a same-day canonical mainboard row with `suspended=false`, standard
price-limit regime authority, valid tick-aligned `previous_close_cny`, and a
mark within canonical price limits. Held rows are valuation evidence only and
are excluded from candidate selection.

The loop keeps `ashare-capital-v1` at 50,000 CNY simulated capital and applies
mainboard-only filtering, canonical capital-policy lots/limits, deterministic
fixture weekday/session checks, T+1 state-machine math, versioned
execution-reality fees plus conservative per-side slippage, a no-trade score
band, single-name/gross/cash limits, and explicit no-trade reasons. Each
tradable fixture row must provide a positive finite `previous_close_cny`; the
canonical price-limit bounds then gate reference price, conservative fill, and
current mark at valid ticks, including the previous close itself. It must also
explicitly declare `price_limit_regime=standard_mainboard`,
`price_limit_exempt=false`, and `new_listing=false`; new-listing/no-limit,
exempt, unknown, and other incomplete trading regimes are unsupported and fail
closed. Boolean, NaN, and infinite numeric inputs are invalid. Tradable rows must also provide a
positive integer `bar_volume_shares` in shares; canonical execution-reality
bar participation capacity caps each simulated fill and capacity below one
100-share lot fails closed. The
`ashare_regular` decision gate reads canonical execution-reality sessions: only
continuous auction 09:30:00-11:30:00 or 13:00:00-14:57:00 Shanghai time is
supported, with exact endpoints only. Opening/closing auctions and their gaps
are rejected. An evidence-rejected day does not consume marks or produce a
valuation. It is a bootstrap, not a real
exchange-calendar assertion: until fresh
TradingDatas calendar handoff, all 20 sessions declare
`calendar_authoritative=false`, `real_session_verified=false`, and are neither
training- nor promotion-eligible. ChiNext, STAR, and Beijing individual equities are
fully excluded. Only canonical context-only indices and sector/industry
aggregates may enter context; they never enter candidate selection or receipts.
Promotion, risk expansion, LLM influence, and real trading remain disabled.

## Event, announcement, and sentiment evidence

`event_evidence.py` is a fixture/mock-first, non-production A-share consumer
port for announcements, news, investor Q&A, and research evidence. It has no
default transport, token, runtime hook, persistence path, or model invocation.
The only wire routes are `GET /v1/catalog` and `POST /v1/query`; direct SQLite,
provider routes, `/tushare`, `/source_status`, and legacy data fallbacks are
forbidden.

The first catalog-validated profiles are:

- primary: `cn.dataset.anns_d`, `cn.dataset.cctv_news`,
  `cn.dataset.irm_qa_sh`, `cn.dataset.irm_qa_sz`, and
  `cn.dataset.research_report`;
- optional, explicitly coverage-degrading: `cn.dataset.disclosure_date`,
  `cn.dataset.report_rc`, `cn.dataset.broker_recommend`, and
  `cn.dataset.stk_surv`;
- forbidden fallbacks while paused: `cn.dataset.news` and
  `cn.dataset.major_news`.

Dataset IDs, schema, selectable fields, ordering, pagination bounds, and row
identity must be present in the exact active catalog row. Each event profile
stores the canonical seven-field `dataset_contract_fingerprint` and a separate
TA-owned `consumer_profile_sha256`; its initial profile is catalog-derived, not
an external fingerprint pin. A later read must match that passed-in profile's
dataset fingerprint exactly. Global catalog-version changes are retained as
expected/observed/drift audit evidence only: unrelated dataset changes do not
block this consumer, while target-row contract, identity, missing-row, or
duplicate-row drift fails closed. The injected runtime client must explicitly
use `catalog_version_policy="evidence_only"`, so every query remains bound to
the immediately observed catalog version. An optional dataset must also be in
the injected client's explicit allow-list before it becomes a queryable profile.
Query fields and operators are restricted to the frozen catalog allow-list.
Every bounded query is replayed. Catalog drift, pagination/cursor failure,
duplicate identity, or same-observation drift fails closed. An accepted query envelope must be
`ready`, fresh, valid, non-degraded, and carry a complete provider-neutral
lineage, receipt, `data_through`, and timezone-aware `observed_at`.
`available_at` is exactly the envelope `observed_at`; the adapter never
backfills or guesses an earlier known time. Date-only historical event fields
remain date-precision evidence. An instant event time records only that the
event-time field has instant precision (`event_time_instant_proven=true`); it
does not prove historical availability. All fixture snapshots keep
`historical_known_time_proven=false` and `pit_feature_eligible=false` until the
formal handoff. Provider-native `pub_time` values without an explicit offset
are normalized only for the two frozen investor-Q&A datasets using their
declared `Asia/Shanghai` semantics; the raw TradingDatas row is not rewritten.

Provider-native rows map to immutable `EventEvidenceSnapshot` records with
event time/precision, availability, canonical mainboard symbol or context
entity, title/content/URL/source, evidence reference, receipt, lineage hashes,
and deterministic completeness confidence. That confidence is not a
calibrated probability. Restricted individual ChiNext, STAR, or Beijing
symbols fail closed. Invalid evidence produces only an
`AshareEvidenceAuditRecord`. Both accepted snapshots and audit rejections retain
expected/observed catalog-version evidence, derived drift state, and the two
profile hashes. It cannot become a feature, candidate, trained
sample, order, or promotion input.

`SentimentEvidenceSnapshot` applies a transparent keyword baseline and an
explicit primary-dataset coverage weight. Missing evidence reduces the shadow
score; it never falls back to a paused dataset. The result is a
counterfactual-only score with `calibrated_probability=null` and all candidate,
execution, training, promotion, risk, position, and live-trading authority
flags false. Accepted shadow observations can bind to the existing
`InMemoryDecisionLedger` only as `SHADOW_ONLY` with zero notional and zero
fills.
Coverage distinguishes an exchange-specific investor-Q&A source that is not
applicable from one that is genuinely missing: an SH symbol is not penalized
for the SZ-only profile (and vice versa).

This is an independently activatable, read-only **event/announcement shadow**
consumer: a newly catalog-validated allow-listed profile may be used only after
its own formal catalog/query parity, metadata, receipt, lineage and PIT gates
pass. Its rejection is isolated to event-derived shadow evidence and cannot
enable or suppress the core minute baseline. `flow` is intentionally different
today: it accepts only an injected `MinuteAuxiliaryEvidence` value for the flow
counterfactual sleeve and has no catalog-bound TradingDatas moneyflow adapter.
A future moneyflow dataset is therefore not auto-enabled, cannot be treated as
formal parity, and cannot affect the baseline/dynamic-position sleeves until it
receives the same dedicated catalog/query consumer and shadow-parity proof.
`cn.equity.daily` and `cn.dataset.rt_min` remain core inputs for their
respective session/minute paths; failed evidence correctly blocks only the
dependent session or minute observation rather than being silently downgraded.

## Moneyflow shadow evidence

`moneyflow_evidence.py` is a separate, fixture/mock-first AShare consumer for
the two independent source variants `cn.dataset.moneyflow` and
`cn.dataset.moneyflow_ths`. It has the same fixed V1 routes, injected-client
boundary, catalog-bound profile, bounded pagination, same-observation replay,
provider-neutral receipt/lineage, freshness/quality, and PIT gates as the
event adapter. A profile is derived from the exact supplied catalog row;
current production availability, schema, selectable fields, order, filters,
and page limit are never baked into TA.
Its pagination identity has `identity_source=catalog.default_order`: every
non-empty order term must be exactly `field:asc` or `field:desc`, name a unique
catalog field, and collectively include the catalog-selected symbol and
source-time fields. TA never synthesizes an identity from merely present row
fields; an incomplete or malformed order rejects the source before any query.

Each source is queried and audited independently. A stale, degraded, empty,
failed, receipt/lineage-incomplete, drifted, or PIT-late source produces no
moneyflow feature and does not substitute the other source. It cannot suppress
an otherwise valid minute baseline or dynamic-position shadow sleeve. Accepted
records are zero-notional counterfactual features with raw moneyflow semantics,
not calibrated probabilities; every candidate, execution, training,
promotion, risk, position, live-trading, and LLM authority remains false.
An explicit caller may project an accepted record only into the existing
`flow` counterfactual sleeve. This adapter is not wired to the minute runner,
scale-500 runtime, timer, production release, or any order path pending its
own formal TradingDatas shadow-parity handoff. `REAL_TRADING_ENABLED=false`.

For optional offline LLM review, an instant-proven event can be projected into
the existing shared `EvidenceArtifact` / `LLMEvidenceRequest` schema. This
projection only constructs an immutable request object; it does not instantiate
the gateway, DeepSeek adapter, journal, or any network transport. The fixture
artifact intentionally has no external source-authority receipt, so the shared
gateway cannot transmit it until a later formal TradingDatas handoff supplies
and verifies that authority. LLM output remains shadow evidence and can never
set an order, position, risk limit, capital action, or promotion state.

Until TradingDatas publishes and independently validates the formal catalog
handoff and real query parity for these profiles, this entire adapter remains
fixture/mock-only and is not connected to the scale-500 minute initializer,
runner, units, environment, timers, production release, or real trading.
`REAL_TRADING_ENABLED=false`.

## Five-minute simulation adapter

`minute_data.py` and `minute_paper.py` are the A-share lane's mock-ready
five-minute vertical slice. They do not assert that TradingDatas currently has
a production-ready minute dataset.

### TradingDatas consumer boundary

- The only data-plane routes are `GET /v1/catalog` and `POST /v1/query`.
- `MinuteDatasetProfile` must be built from one exact active catalog row. The
  dataset ID, `schema_major`, default fields, default order and page-size limit
  are not hard-coded in TA.
- Provider-native rows remain rows. Receipt, lineage, freshness,
  `data_through`, and `observed_at` remain response-envelope evidence.
- If the minute dataset exposes only `ts_code/time/OHLCV/amount`, TA binds
  previous-close and suspension state from a separate, same-session
  `MinuteReferenceFact`; absence, symbol/date mismatch, or invalid reference
  evidence fails closed. TA never asks TradingDatas to copy envelope evidence
  or trading-state guesses into provider-native rows.
- Volume/amount conversion factors and raw-unadjusted execution-price semantics
  are explicit profile inputs. TA does not guess whether a provider reports
  shares, lots, yuan, or thousands of yuan.
- The first supported frequency is exactly five minutes. Timestamp field,
  timestamp format and bar-start/bar-end semantics must be supplied by the
  frozen TA profile after the formal TradingDatas handoff.
- No SQLite, file, port 8082, `/tushare`, `/source_status`, provider-specific
  route or fallback exists in the adapter.
- Every bounded read is replayed. Pagination, cross-page identity,
  catalog-version drift and same-observation mismatch fail closed.
- Query filters must use only the field/operator allow-list in the frozen
  catalog row; a provider-private or undeclared filter is rejected.

`MinuteBarEvidence` accepts only a completed mainboard common-stock bar with:

- an exchange-calendar eligible Shanghai trade date;
- an exact morning or afternoon session interval that does not cross lunch,
  close or a trade date;
- `bar_end <= data_through <= observed_at == available_at <= decision_time`;
- an explicit latency tier: `low_latency_execution` remains capped at 30
  seconds; the non-production `delayed_paper` observation/simulation tier is
  capped at 720 seconds and is permanently ineligible to claim low-latency
  execution evidence;
- positive valid OHLC prices, nonnegative amount, positive volume, no
  suspension, and no duplicate/conflicting `(symbol, bar_end)` identity;
- envelope state `ready`, `degraded=false`, freshness `fresh/stale=false`,
  quality `valid`, complete provider-neutral lineage, receipt and timestamps.

Rejected data creates `MinuteEvidenceAuditRecord` only. It is never feature-,
candidate-, or execution-eligible. The delayed tier exists solely because the
current TradingDatas/QuickSync five-minute source is observed about one
to two completed bars late. It preserves the real `observed_at/available_at`
timestamp and never rewrites it to satisfy the 30-second gate. The wider
delayed-paper ceiling does not relax exact requested-bar coverage, Universe
identity, metadata, replay or same-observation requirements: partial, mixed
bar-end or degraded snapshots still fail closed.

### Small-account fixture simulation

The operating policy reads the canonical `ashare-capital-v1` projection:

- 50,000 CNY initial capital;
- 15% single-name cap, 90% stock-gross hard cap and 100-share buy lots;
- 10 symbols for the transport canary, up to 500 for the first broad
  research-paper scan, and up to 6,000 only after full-mainboard sharded
  minute parity;
- up to six actively used positions while the canonical authority retains its
  eight-position safety capacity;
- cash as a formal state and canonical 1,000/2,000 CNY no-trade/economic-order
  thresholds.

`MinuteExecutionPair` forbids same-bar and already-started-bar execution. It
uses the first five-minute bar whose **start is strictly after the actual
decision timestamp**. With a one-bar-delayed source this can be later than the
naive `t+1` bar; the extra delay is intentional and removes look-ahead. The
lunch transition is handled explicitly and the unsupported closing auction is
not used.

`MinuteFixturePaperBook` applies next-bar open plus conservative canonical
slippage, high/low and price-limit bounds, 10% bar-participation capacity,
partial/nonfill/reject receipts, minimum commission, sell tax and transfer
fee, T+1 sellable quantities, cash/position conservation, exact marks,
restart-state hashing and idempotent order replay. It is explicitly:

```text
authority_tier = non_production_fixture
durable = false
real_trading_enabled = false
broker_order_id = null
```

It is a mock verifier, not a replacement for `MarketCapitalLedger`. Completed
five-minute bars are retrospective fill evidence: the modeled fill time is the
next bar open, while settlement occurs only after the completed bar becomes
available. The existing live-quote capital stage requires contemporaneous quote
evidence, so the durable minute settlement adapter must be frozen separately
after the real TradingDatas minute handoff; no timestamp is rewritten to make a
completed bar look like a live quote.

All fill, nonfill, data/model/human rejection, insufficient-capital and
ranked-not-traded outcomes are translated into the existing
`DecisionExposureRecord` / `InMemoryDecisionLedger` contract. This preserves
one decision-ledger vocabulary for actual paper fills and the base, event,
flow and dynamic-position counterfactual books.

### Automatic fixture research loop

`minute_research.py` and `minute_loop.py` connect the accepted evidence and
paper contracts without adding a runtime, broker, data fallback or second
capital authority.

`minute_paper_runner.py` adds a small one-shot entrypoint for prospective
accumulation. Each invocation queries one exact completed bar, updates one
atomic `0600` research-fixture bundle, and emits reconciliation/decision
summaries. The bundle is restartable research state only:

```text
authority_tier = non_production_fixture
capital_authority = false
execution_authority = false
real_trading_enabled = false
```

The runner may begin after one fresh previous-close/trading-state reference
snapshot and a successful exact-bar canary. Five consecutive trading days are
now a stability and scaling review gate, not a prerequisite for collecting the
first simulated decisions. It still cannot enable a broker, real order, live
transition or the strict 30-second execution tier.

`minute_auto_runner.py` is the minimal scheduler-facing wrapper. It derives
only the single bar expected behind the frozen five-minute provider lag,
requires continuity with the last atomic bundle, and then calls the same
one-shot runner. It never catches up a gap with late information. A missing
daily session directory is a safe no-op; a missing intermediate bar, malformed
state, duplicate concurrent process, degraded dataset or a snapshot that does
not exactly cover the reviewed universe fails closed. The wrapper does not
prepare the next day's universe or reference facts, so a day cannot begin
automatically until those inputs have been frozen under that day's private
`0700` directory.

`minute_day_report.py` is a read-only, secret-free candidate end-of-day
projection of an already-written delayed-paper state bundle. It verifies the
bundle/session/hash continuity before reporting expected, observed and missing
five-minute slots, decision outcomes, simulated costs, T+1 positions,
reconciliation, and the four fixture shadow books. It grants no execution,
training, promotion, durable-ledger, or real-trading authority.
Its compact `operational_readiness` projection exposes the deterministic
`evidence_rejections`, `reconciliation_incomplete`, and
`session_incomplete` blockers, so daily accumulation can distinguish a clean
fixture day from an incomplete one without treating either result as trading
or training authority.
The baseline sleeve is the only source for top-level candidate, simulated-fill
and fee KPIs. Event, flow and dynamic sleeves are separately labelled as a
non-comparable counterfactual aggregate, so four independent fixture books
cannot be mistaken for one account's turnover, cost or PnL. New bundles retain
one receipt per accepted bar; the report sums those audit rejections and marks
legacy bundles whose earlier per-bar receipt history is unavailable.

`minute_offline_learning.py` is a separate post-close, fixture-only projection.
It appends verified day summaries only to an A-share-local learning journal,
never to the shared `SampleJournal`; incomplete sessions are recorded as
blocked and create no training sample. KPI, calibration, missed-opportunity and
Challenger fields remain review-only: calibration has no forward-label
authority, and automatic model change, promotion, risk expansion and real
trading are all false. The tracked `Ashare/systemd/` service/timer are merely
disabled-by-default installation candidates and are not deployed runtime units.
The projection requires receipt history for every accepted bar: a legacy bundle
with only its final receipt is blocked because earlier evidence rejections
cannot be disproved. Its deterministic `forward_label_state` records the
`m30/m60/close/1d/3d/5d` requirement as blocked until a future authoritative
TradingDatas daily receipt exists; it neither queries for nor appends labels.
For offline review only, a separate pre-registered local profile may recognise
a contiguous segment whose minimum length is derived from its declared feature
window plus label horizon. It never crosses `session_gaps`, and it still needs
an explicit fixture label receipt for that exact profile. This is a local
review eligibility signal, not `training_eligible`: shared SampleJournal,
training, calibration and promotion authority remain false.

`minute_session_initializer.py` closes that pre-open preparation gap without
adding a provider route. At 09:20 it reads the current TradingDatas catalog,
proves the target date is open, obtains `pretrade_date`, and fetches only the
reviewed universe's prior-session `daily.close` rows through `/v1/query`. It
revalidates the current `rt_min` catalog contract, copies the already-reviewed
universe, and atomically publishes only `minute-manifest.json`,
`reference-facts.json`, and `universe.json`. It never creates
`state-bundle.json`.

For a reviewed scale transition, the initializer accepts an explicit absolute
`--universe-source` artifact. It derives `page_limit`, `max_rows` and
`max_pages` from that universe and the current catalog page limit, then binds
the canonical universe SHA into the published manifest. A 500-symbol artifact
therefore uses a 500-row budget when the catalog permits it; it is not silently
truncated to the preceding 30-symbol canary. The supplied artifact must still
pass the main-board, listing-age and risk checks, have complete prior-close
coverage, and match every exact minute snapshot before any simulated step can
proceed. The same absolute path can be supplied to the scheduled initializer as
`ASHARE_MINUTE_UNIVERSE_SOURCE`; when absent, the prior reviewed session
universe remains the default.

Prior-session daily references use the same bounded catalog contract instead
of a fixed 10-symbol batch. The initializer derives each batch from the
reviewed Universe size, the daily dataset's current `max_page_size`, and the
500-row TA query ceiling. It separately enforces the provider-neutral V1
`QueryDefaults.max_in_values=100` filter contract, so a 500-symbol Universe
always uses five 100-symbol batches even when catalog `max_page_size=500`.
Every batch is independently read twice, allows at most five cursor pages, and
has a row budget exactly equal to its requested symbol set. Returned
`(ts_code, trade_date)` identities must exactly match that set; missing, extra,
duplicate, over-budget or non-terminating cursor chains, replay-changing,
catalog-drifting, 413/429, timeout, stale or degraded evidence fails closed.
There is no automatic retry, concurrency, fallback, provider route or database
read, so a transport failure cannot become a request storm or partial session.

The initializer's `suspended=false` value is explicitly provisional: it is not
a claim derived from an identityless suspension dataset. Every actual bar must
still be completed and have positive volume, otherwise the minute Evidence
Gate blocks the entire snapshot. A closed day is a safe no-op; degraded daily
evidence, catalog drift, missing symbols, replay mismatch, conflicting existing
inputs, or an already-started target session fails closed.

### Isolated scale-500 runtime candidate

`minute_scale500_runtime.py` is the AShare-owned selector for a direct,
simulation-only 500-symbol transition. It requires an absolute, regular,
single-link, non-writable frozen Universe artifact with exactly 500 unique
reviewed symbols and an explicit canonical SHA256. The runtime candidate is
fixed to `http://127.0.0.1:18082`, `cn.dataset.rt_min`, and the existing
catalog/query clients; it has no provider, database, retry, concurrency, broker,
or historical-minute fallback.

The scale state root and rollback-30 state root must be disjoint. The selector
can write only the scale root. The rollback root is mounted read-only and is
never used to bootstrap a 500 session. The 09:18 initializer reads fresh
calendar and previous-session daily references through formal TradingDatas and
publishes no state bundle. Post-close minute rows and a prior 30-symbol bundle
are forbidden as opening evidence.

Normally, the first accepted scale observations must be the adjacent 09:35 and
09:40 500/500 bars. A one-time, manual `run --allow-late-start` is the only
exception: it can start only from the runner's exact current completed formal
bar after an independently verified 500/500 production canary. The static
late-start candidate requires its secret-free canary receipt and verifies the
same trading date, exact bar, delayed-paper tier, 500 canonical identities,
same-observation replay, receipt/lineage proof and frozen Universe digest
before it invokes the runner. It never
queries or backfills earlier bars, cannot use mixed/failed observations, and is
not accepted by `initialize` or either recurring systemd unit. The tracked
static `tradingagent-ashare-minute-scale500-late-start.service` is the only
release candidate that carries this flag; its `OnFailure` invokes rollback.
The resulting isolated gate records `partial_session=true`, `late_start=true`,
and the exact bar end; it permanently sets `learning_eligible=false` and
`full_session_complete=false` for that trading date. Each accepted bar still
passes the existing exact Universe coverage,
identity, metadata, receipt/lineage, bounded pagination, and same-observation
double-read gates. These isolated simulated observations have no capital,
execution, training, promotion, or real-trading authority. After both pass,
the normal-path timer continues the 48-slot session. Any missing, partial, mixed-time,
degraded, identity, lineage, fanout, continuity, authority, or persistence
failure records one exact reason in the scale gate, exits non-zero, and invokes
the tracked rollback unit.

The full 500/500 cohort remains mandatory before `delayed-paper` can run. A
separate pure receipt builder may describe a 99%-or-higher partial cohort only
when the exact missing identity set is explicit and no replacement identity is
present. That receipt is deterministic, zero-notional and shadow-only: it has
no candidate, capital, execution, training or promotion authority and cannot
be routed through the runner.

Rollback disables the scale timers, atomically repoints TA `current` to the
preserved immutable 30-symbol release, then restores the 30-symbol timers
without deleting or rewriting either state root. It does not manufacture a
same-day 30-symbol session after the 09:18 initializer has passed; that day
remains fail-closed. The scale timer mirrors the delayed schedule but moves the
final 15:00-bar attempt to 15:19 so TradingDatas has one bounded final
publication interval. `DELAYED_PAPER` consumes the shared readiness limit of
one five-minute cadence plus 30 seconds of jitter (330 seconds), measured both
from bar end to source availability and from bar end to the actual decision;
the later timer does not make stale evidence eligible. The low-latency
execution bound remains 30 seconds and is never granted by this observation
tier.

The tracked files under `Ashare/systemd/` are release candidates only.
Installing or enabling them requires an immutable release, the frozen artifact
readback, `REAL_TRADING_ENABLED=false`, systemd verification, old-state byte
fingerprints, and a tested rollback.

The tracked systemd candidate runs at second 40 after each five-minute boundary
during the two A-share sessions, after the independent TradingDatas collector.
It remains a non-production fixture accumulator:

```text
deploy/systemd/tradingagent-ashare-minute-paper.service
deploy/systemd/tradingagent-ashare-minute-paper.timer
deploy/systemd/tradingagent-ashare-minute-session.service
deploy/systemd/tradingagent-ashare-minute-session.timer
```

Enabling this timer does not enable the observation worker, durable capital,
broker execution, the public frontend or `REAL_TRADING_ENABLED`.

`MinuteRollingFeatureEngine` requires consecutive accepted snapshots. Its
transparent V1 features are close-to-close return, intrabar return, bar range,
volume/amount change and an optional bounded context adjustment. The resulting
output is permanently explicit about its current scientific status:

```text
score_semantics = uncalibrated_deterministic_rank_score
calibrated_probability = null
expected_return_bps = null
probability_model_state = not_calibrated
promotion_eligible = false
execution_authority = false
```

This score only creates an ordering for fixture label collection. It does not
claim forward return, probability calibration, investment advice or a trained
model. A name is candidate-eligible only if its TA-owned universe record is a
mainboard ordinary share, belongs to one of the first three research themes,
is at least 30 calendar days old, and is neither risk-warning nor
delisting-risk. ChiNext/STAR/index or industry aggregate observations remain
`context_only` and cannot become a feature symbol, candidate or order.
An exact provider cohort may retain risk-warning or delisting-risk mainboard
rows for snapshot identity, completeness and rejection auditing; the session
initializer preserves their risk flags and the eligibility gate permanently
excludes them from candidates and orders.

`MinuteFixtureClosedLoop` processes one completed cross-sectional snapshot at a
time:

```text
accepted minute bars
  -> rolling features
  -> uncalibrated ranking
  -> action comparison
  -> pending fixture order
  -> exact next completed bar settlement
  -> fixture cash/positions
  -> reconciliation
  -> Decision Ledger
  -> counterfactual attribution
```

The baseline sleeve uses only the transparent score. Event and flow sleeves
require matching, PIT-bounded auxiliary evidence with an explicit expiry;
missing or expired evidence never falls back to the baseline. Missing evidence
is recorded as a model rejection, while expired input fails the current
shadow-ranking step closed. The
dynamic-position sleeve changes only fixture lot count inside the same
canonical 50k constraints. These four books are independent counterfactual
experiments, not four real accounts and not four capital authorities.

Pending orders are restart-state hashed and may settle only against the exact
next five-minute bar. A skipped/missing bar becomes a formal nonfill; it is
never filled late. Data failure cancels pending new-risk attempts, records
data-reject decisions for every monitored trade symbol and creates no new
position. Manual fixture rejection has a dedicated audit path. A completed
step reconciles each sleeve only when marks exist for every held symbol.

The loop is intentionally process-local and fixture-only. Its restart format
protects test state integrity but is not the append-only SampleJournal,
MarketCapitalLedger, durable outbox, production scheduler or promotion
authority. Real minute handoff still requires the separate TradingDatas
catalog profile, five-day observation gate and a future durable settlement
adapter.

### Read-only real-data canary

`Ashare.minute_canary` is the bridge from the mock-ready contract to a formal
TradingDatas minute handoff. It accepts an external secret-free profile
manifest, a separate TA-owned reference-fact manifest, and the existing secure
TA token file. The dataset ID, catalog version, schema/fields, filters,
identity, timestamp semantics, unit multipliers and bounded page/row budgets
must all be explicit in that profile.

Catalog fields advertised only for filtering do not have to appear in
`default_fields`; every identity and OHLCV field consumed from a row must still
be present there. This preserves provider-neutral catalog semantics without
accepting undeclared execution data.

The Evidence Gate consumes the current TradingDatas lineage shape: a complete,
provider-neutral envelope must expose a non-empty, duplicate-free `providers`
list plus `transport_service`. The legacy singular `provider` key does not
satisfy the minute evidence contract.

It performs exact catalog drift validation, two bounded query reads,
pagination/identity checks and the full minute Evidence Gate, then writes a
0600 observation-only receipt. It cannot create candidates, orders, fills,
capital entries, schedules or trading authority.

Example after a formal non-secret TradingDatas handoff:

```bash
export REAL_TRADING_ENABLED=false
python3 -m Ashare.minute_canary \
  --manifest /absolute/runtime/minute-profile.json \
  --reference-facts /absolute/runtime/minute-reference-facts.json \
  --token-file /run/secrets/tradingagent/tradingdatas-read.token \
  --decision-time 2026-07-28T09:35:25+08:00 \
  --trading-date 2026-07-28 \
  --output /absolute/runtime/minute-canary-receipt.json
```

The manifest and reference facts are runtime evidence, not repository defaults.
TradingDatas formally froze `cn.dataset.rt_min` schema major 2 and began the
ten-mainboard-symbol five-minute continuous canary on 2026-07-28. TA read back
adjacent completed bars through the formal catalog/query API with complete
receipt and lineage metadata. The provider currently exposes the latest bar
about one complete five-minute bar late, so the strict 30-second execution
evidence gate remains closed. This handoff authorizes observation and data
accumulation only; the five-trading-day observation gate, fresh daily limit and
suspension references, and 20-session sample accumulation remain separate.

### Phase-one A-share scope

- Tradable equities: Shanghai/Shenzhen mainboard ordinary shares only.
- Context-only evidence: ChiNext/STAR indices and industry aggregates may
  influence market or sector state, but cannot become order symbols.
- First research directions: AI/semiconductor infrastructure,
  robotics/industrial automation and innovative medicines, with a broad-index
  control sample.
- The broad opportunity radar may monitor up to 500 mainboard names first and
  later the full mainboard universe. Broad monitoring does not imply broad
  holdings: the same 4-6 position, lot, fee, liquidity and risk gates remain
  authoritative.
- ST/risk-warning names, delisting risk, listings younger than 30 days,
  suspension, zero volume and unproven trading state remain upstream universe
  exclusions. The minute gate independently rechecks mainboard identity,
  suspension and positive volume.
- DeepSeek/other LLMs remain news, event and industry-evidence sidecars. They
  cannot place orders, set position size, change a model, bypass risk or write
  the paper account.

### Runtime stop line

Until TA completes five consecutive trade-date observations, validates the
expanded symbol batch, and obtains fresh daily/limit/suspension reference
evidence:

- do not enable a TA production timer;
- do not run this fixture as the current capital authority;
- do not connect a broker or create real orders;
- do not restore the online front;
- keep `REAL_TRADING_ENABLED=false`.
