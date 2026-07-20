# AShare fixture 20-day loop

`twenty_day_fixture_loop.py` is an offline, fixture-only minimum vertical
slice for exactly 20 supplied trading dates.  Each date records evidence
eligibility, the mainboard-only universe, candidate/risk result, a simulated
intent receipt or a reason code, reconciliation, and sample/review entries.

It accepts only the TradingDatas route labels `GET /v1/catalog` and
`POST /v1/query` as caller-supplied evidence metadata.  It has no data client,
dataset ID, network access, SQLite access, broker, LLM, scheduler, outbox, or
runtime ledger side effect.  It is therefore not a live-paper scheduler or a
capital authority replacement.

The loop keeps `ashare-capital-v1` at 50,000 CNY simulated capital and applies
mainboard-only filtering, 100-share lots, T+1 sells, fees, a no-trade score
band, single-name/gross/cash limits, and explicit no-trade reasons.  ChiNext,
STAR, and Beijing individual equities remain context-only and never enter the
tradable universe, candidate selection, or receipts.  Promotion, risk
expansion, LLM influence, and real trading remain disabled.
