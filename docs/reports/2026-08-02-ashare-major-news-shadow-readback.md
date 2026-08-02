# A-share major-news shadow readback — 2026-08-02

## Outcome

`cn.dataset.major_news` completed a formal, authenticated TradingDatas V1
current-observation read in a detached TradingAgent candidate. The source was
accepted with one row, one page and an identical same-observation replay. The
four-source sidecar result was nevertheless `blocked`: `cn.dataset.anns_d`,
`cn.dataset.moneyflow` and `cn.dataset.moneyflow_ths` were each stale at the
weekend decision time. This is a correct data-quality outcome, not a fallback
or a partial execution release.

## Candidate and isolation

- Code candidate: merged TradingAgent commit `318efe7e0b7b749535ffc644942c40e6e06ac7ef`
  (PR #182); it was used only from a detached server candidate and never
  switched TradingAgent `current`.
- Candidate output root:
  `/var/lib/tradingagent/trading-copilot-event-shadow/20260802-318efe7-r2/`.
  The root is `tradingagent:tradingagent`, mode `0700`; manifest and receipt
  are regular `0600` files.
- Server release evidence:
  `/opt/investment/release-evidence/tradingagent/20260802T131044Z-ta-major-news-shadow-318efe7-r2/`.
  Its focused test, preflight and runtime-summary artifacts are owned by
  `marketgraph`; an earlier root-owned evidence attempt was retained as failed
  evidence and was not used for this result.
- The only consumer calls were authenticated `GET /v1/catalog` and
  `POST /v1/query` to `http://127.0.0.1:18082`, through the existing
  TA-scoped token file. No token, hash, or event body is recorded here.

## Contract result

The formal catalog version was `v1-e23dc83446ca082f`.

| Dataset | Result | Reason / proof |
| --- | --- | --- |
| `cn.dataset.major_news` | accepted | 1 row; 1 page; replay equal; receipt and complete lineage verified |
| `cn.dataset.anns_d` | rejected | `ashare_evidence_metadata_not_ready` |
| `cn.dataset.moneyflow` | rejected | `ashare_moneyflow_metadata_not_ready` |
| `cn.dataset.moneyflow_ths` | rejected | `ashare_moneyflow_metadata_not_ready` |

`major_news` is an append-only current-observation profile whose public query
does not accept `as_of`. The consumer therefore omits that optional request
member only for this profile; event time and envelope `observed_at` remain the
availability authorities. It is not historical PIT evidence.

The receipt records `zero_notional_cny=0` and false for candidate, training,
capital, order, execution, risk, position, LLM-network, promotion, timer,
release-switch and scale500 authority. No systemd unit or timer was installed
or changed. The active TradingAgent `current` remains the existing A-share
release and its normal minute-paper timer was left unchanged.

## Next gate

Re-run the same bounded sidecar only when the announcement and both moneyflow
datasets have fresh, valid, non-degraded formal metadata. A successful
four-source receipt still remains a zero-notional shadow observation and
requires separate approval before any candidate, learning or execution use.
