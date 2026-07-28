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
- no more than 30 seconds between bar end and availability;
- positive valid OHLC prices, nonnegative amount, positive volume, no
  suspension, and no duplicate/conflicting `(symbol, bar_end)` identity;
- envelope state `ready`, `degraded=false`, freshness `fresh/stale=false`,
  quality `valid`, complete provider-neutral lineage, receipt and timestamps.

Rejected data creates `MinuteEvidenceAuditRecord` only. It is never feature-,
candidate-, or execution-eligible.

### Small-account fixture simulation

The operating policy reads the canonical `ashare-capital-v1` projection:

- 50,000 CNY initial capital;
- 15% single-name cap, 90% stock-gross hard cap and 100-share buy lots;
- 10 monitored symbols initially, 60 only after real batch-minute parity;
- up to six actively used positions while the canonical authority retains its
  eight-position safety capacity;
- cash as a formal state and canonical 1,000/2,000 CNY no-trade/economic-order
  thresholds.

`MinuteExecutionPair` forbids same-bar execution. A decision made after bar
`t` may use only the next valid bar `t+1`; the lunch transition is
11:30-to-13:05 and the unsupported closing auction is not used.

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
Until TradingDatas formally freezes `cn.dataset.rt_min`, its catalog contract,
and a fresh five-minute receipt, this command remains unconfigured and must
fail closed. A passing canary authorizes observation only; the five-trading-day
observation gate and 20-session sample accumulation remain separate.

### Phase-one A-share scope

- Tradable equities: Shanghai/Shenzhen mainboard ordinary shares only.
- Context-only evidence: ChiNext/STAR indices and industry aggregates may
  influence market or sector state, but cannot become order symbols.
- First research directions: AI/semiconductor infrastructure,
  robotics/industrial automation and innovative medicines, with a broad-index
  control sample.
- ST/risk-warning names, delisting risk, listings younger than 30 days,
  suspension, zero volume and unproven trading state remain upstream universe
  exclusions. The minute gate independently rechecks mainboard identity,
  suspension and positive volume.
- DeepSeek/other LLMs remain news, event and industry-evidence sidecars. They
  cannot place orders, set position size, change a model, bypass risk or write
  the paper account.

### Runtime stop line

Until TradingDatas supplies a formal five-minute catalog/query handoff and TA
passes a 10-symbol canary plus five consecutive trade-date observations:

- do not enable a TA production timer;
- do not run this fixture as the current capital authority;
- do not connect a broker or create real orders;
- do not restore the online front;
- keep `REAL_TRADING_ENABLED=false`.
