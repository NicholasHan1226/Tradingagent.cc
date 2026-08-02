# TradingCopilot event-evidence v2 readback — 2026-08-02

## Outcome

Candidate commit `18141b336c3fe3c30c720ed4d08da61b0026bf49` completed one
isolated, authenticated, read-only TradingDatas V1 event readback as the
`tradingagent` service user.  The candidate is not the active TradingAgent
release: `/opt/investment/current` remains `2b7b52b...`.

The change accepts TradingDatas's reviewed `research_report` public identity
`[trade_date, title, url]` alongside the pre-existing author-bearing identity
variant.  It does not relax any envelope, receipt, lineage, freshness, or
event-time check.

## Evidence

- Candidate code/venv: `/opt/investment/tradingagent-candidates/ashare-events-v2-18141b3`
  and `/opt/investment/tradingagent-venvs/ashare-events-v2-18141b3`.
- UID 987 preflight verified executable interpreter, traversable candidate
  code, readable token-file path, and imports before making the read.
- The result root is
  `/var/lib/tradingagent/trading-copilot-canary/20260802-events-v2-18141b3-r3/`,
  owned by `tradingagent:tradingagent`, mode `0700`; its result files are
  `0600`.
- The read used only the existing token-file and the fixed `GET /v1/catalog`
  plus `POST /v1/query` consumer path.  No token value or event body was
  written to this report.

## Result

- `cn.dataset.research_report`: one accepted event for the bounded two-symbol
  observation.  This proves the v2 catalog identity compatibility path.
- `cn.dataset.anns_d`, `cn.dataset.cctv_news`, `cn.dataset.irm_qa_sh`, and
  `cn.dataset.irm_qa_sz`: explicitly blocked with
  `ashare_evidence_metadata_not_ready`.
- No sentiment label, event fallback, candidate, training sample, capital
  event, order, broker action, model call, timer, or public route was created.

The older formal projection report remains correct for its earlier candidate
and data observation.  This report supersedes it only for the tested
`research_report` catalog-identity compatibility result.

