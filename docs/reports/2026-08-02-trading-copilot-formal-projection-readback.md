# TradingCopilot formal projection readback — 2026-08-02

## Outcome

The isolated desktop TradingCopilot projection worker completed successfully at
candidate commit `fcc2ba1c46d4988e5ace30d63acff8ab35bfa98f`. The run consumed the
formal TradingDatas catalog/query surface through the existing TradingAgent
read-only token and wrote only to:

`/var/lib/tradingagent/trading-copilot-canary/20260802-v8-final-fcc2ba1-r1/`

This is an isolated evidence readback. It did not switch
`/opt/investment/current`, install or enable a service/timer, publish a public
route, connect a broker, create an order, alter capital, train a model, or
promote a model.

## Verified coverage

- Security master: 30/30 configured symbols, with 30 industries and 30 areas.
  Source receipt: `receipt:fae5885702c6bd31fff58614148f7c73b6e550bb9e102ec7afd61ba58837485e`.
- Eligible minute projection inputs: 2/30 symbols only — `000333.SZ` and
  `002294.SZ`. The other 28 symbols had no row admitted by the frozen minute
  manifest and evidence gate; no projection was fabricated for them.
- Each accepted symbol has one `1D` historical-display point. `5D`, `1M`,
  `6M`, `YTD`, and `1Y` are empty, and the forecast is `null`.
- The minute input is marked `stale` with `freshness_sla_exceeded`. The
  `historical_display` evidence use permits truthful display of this old
  receipt only; it is not historical point-in-time training data and is never
  delayed-paper or execution eligible.
- Accepted events: 0. `anns_d`, `cctv_news`, `irm_qa_sh`, `irm_qa_sz`, and
  `research_report` all returned `ashare_evidence_metadata_not_ready`. Their
  precise reasons are persisted in `worker-result.json`; no sentiment label or
  event card was invented.

## Integrity and authority checks

- Worker result: `pass`, `symbolCount=2`.
- Both projection bytes match their detached receipt `projectionSha256`.
- Output directories are mode `0700`; all evidence and projection files are
  mode `0600`, owned by `tradingagent:tradingagent`.
- Worker authority receipt is false for broker, capital, orders, promotion,
  training, and real trading.
- No `trading-copilot` systemd unit is installed. Production TradingAgent
  remains `/opt/investment/current -> 2b7b52b...`.

## Delivery boundary

The code path and fail-closed behavior are accepted. Formal market/event
coverage remains an upstream data fact: this readback proves 2/30 minute
coverage and 0 accepted events, not 30/30 live coverage. TradingCopilot can
consume better receipts when TradingDatas supplies them without receiving any
Quant Core candidate, capital, order, sample, or promotion authority.
