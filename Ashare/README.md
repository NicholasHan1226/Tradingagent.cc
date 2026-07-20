# AShare fixture 20-day loop

`twenty_day_fixture_loop.py` is an offline, fixture-only minimum vertical
slice for exactly 20 supplied trading dates.  Each date records evidence
eligibility, the mainboard-only universe, candidate/risk result, a simulated
intent receipt or a reason code, reconciliation, and sample/review entries.

Each day carries a structured fixture Evidence Gate: exact TradingDatas routes
`GET /v1/catalog` and `POST /v1/query`, `available` state, non-degraded fresh
and valid quality, lineage/receipt IDs, calendar eligibility/lineage, and a
timezone-aware available time. Any missing, stale, degraded, failed, or mistyped
field blocks candidate selection and orders. No dataset ID is invented. It has
no data client, network access, SQLite access, broker, LLM, scheduler, outbox,
or runtime ledger side effect.

Each `FixtureDay` must explicitly supply a current `mark_prices` mapping for
every open position. Cost basis and mark are separate: reconciliation reports
realized/unrealized PnL, market value, equity, and gross exposure from current
marks. A missing or invalid mark blocks reconciliation and all new simulated
orders for that day; it never silently reuses the entry price.

The loop keeps `ashare-capital-v1` at 50,000 CNY simulated capital and applies
mainboard-only filtering, canonical capital-policy lots/limits, verified fixture
weekday sessions, T+1 sells, versioned execution-reality fees plus conservative
per-side slippage, a no-trade score band, single-name/gross/cash limits, and
explicit no-trade reasons. ChiNext, STAR, and Beijing individual equities are
fully excluded. Only canonical context-only indices and sector/industry
aggregates may enter context; they never enter candidate selection or receipts.
Promotion, risk expansion, LLM influence, and real trading remain disabled.
