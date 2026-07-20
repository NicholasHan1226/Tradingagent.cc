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
orders for that day; it never silently reuses the entry price.

The loop keeps `ashare-capital-v1` at 50,000 CNY simulated capital and applies
mainboard-only filtering, canonical capital-policy lots/limits, deterministic
fixture weekday/session checks, T+1 state-machine math, versioned
execution-reality fees plus conservative per-side slippage, a no-trade score
band, single-name/gross/cash limits, and explicit no-trade reasons. The
`ashare_regular` fixture decision window is 09:15-11:30 or 13:00-15:00 Shanghai
time. It is a bootstrap, not a real exchange-calendar assertion: until fresh
TradingDatas calendar handoff, all 20 sessions declare
`calendar_authoritative=false`, `real_session_verified=false`, and are neither
training- nor promotion-eligible. ChiNext, STAR, and Beijing individual equities are
fully excluded. Only canonical context-only indices and sector/industry
aggregates may enter context; they never enter candidate selection or receipts.
Promotion, risk expansion, LLM influence, and real trading remain disabled.
