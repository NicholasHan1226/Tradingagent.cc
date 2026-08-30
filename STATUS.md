# TradingAgent current status

Observed at: 2026-08-30T16:29:02+08:00

This replaceable snapshot separates source, configuration, runtime and market
receipts. It grants no capital, orders, live trading or promotion.

## Source and release layers

| Layer | Fresh observation | Boundary |
|---|---|---|
| 本地主线 | user's research checkout preserved; delivery uses an isolated worktree | resolve with `git rev-parse HEAD origin/main`; checkout is not necessarily main |
| GitHub 主线 | use the current remote ref and matching CI | resolve with `git rev-parse HEAD origin/main`; no self-invalidating main SHA in this row |
| Ordinary server source | clean at `5cd9649` in `/opt/investment/tradingagent` | not active release; no three-end-sync claim |
| Immutable current release | `7eb0e6249475eb6e521494ac86af4ec160d81558` | `.deployed-sha`, current symlink and front process match |
| Front read API | `/healthz` returned `ok=true`; `/api/trading-agent/snapshot` returned JSON | separate from minute fixture receipts |
| Accepted repair | [613](https://github.com/NicholasHan1226/Tradingagent.cc/pull/613), merge `7eb0e6249475eb6e521494ac86af4ec160d81558` | existing internal sim-only services; no public route, broker, capital or TD deployment change |

Candidate CI [33300656970](https://github.com/NicholasHan1226/Tradingagent.cc/actions/runs/33300656970)
passed 6,122 Python tests, one skip, 266 subtests and 361 frontend tests.
Independent reviewers approved the exact candidate. Exact merged-main CI
[33301063331](https://github.com/NicholasHan1226/Tradingagent.cc/actions/runs/33301063331)
passed the same full suite. Independently downloaded release archive checksum
and `.source-sha` match. Deployment
[33301483147](https://github.com/NicholasHan1226/Tradingagent.cc/actions/runs/33301483147)
completed, and direct server readback confirms the actual cutover, not a skipped
workflow. Effective configuration bindings of all nine helper-managed services
plus the separately rebound forty-symbol observer point to the new release.
Inactive A-share services are not natural-session runtime proof. The front process is verified on that SHA;
service-user imports of five affected modules also resolve to it.

## A-share: coverage and actual fills

The latest natural trading session is **2026-08-28**; today is Sunday.

- Scale initializer published **3,188 active / 3,193 source identities**:
  two recent listings pending, three missing prior closes excluded by stock.
  New listings do not reset already-eligible stocks' clocks.
- Persisted 09:45 coverage proves **1,197 accepted / 3,188 active**, 1,991 missing,
  three row rejections and 20 budget-exhausted shards. Coverage is not fills.
- Persisted state and service-user restore agree: four independent fixture
  books have **zero fills and zero positions**, each with 50,000 CNY cash and
  3,188 rejection records. They are counterfactual books, not four capital
  authorities. One accepted bar does not provide consecutive rolling features.
- Friday's old gate stopped after that bar on `minute_same_observation_mismatch`.
  Already-deployed continuity code admits the first valid rolling subset and
  permits future independent slots; Friday did not test the new release.
- Four existing A-share timers are enabled and active. Next initializers:
  Aug-31 09:18; first delayed-bar attempts: 09:42. Scheduling is not market proof.

The deployed batch adds per-stock affordability continuation,
remaining-budget bounds and actual `settled_quantity`, `settled_notional_cny`,
`settled_fee_cny` receipt fields. It preserves valid shards without increasing
the 180-second load budget, four workers, 100-share lot, 7,500 CNY single-stock
cap or 45,000 CNY gross cap. Historical double-read timing probes measured
21.997, 13.935 and 16.626 seconds for three independent 100-stock shards, plus a
2.927-second catalog read; these are not live PIT or future full-coverage proof.

The accumulator remains `non_production_fixture`.
`compose_capital_backed_paper_runtime` is still test-only and network-closed;
quote/calendar/Champion authority and its production entry remain unwired.
That separate integration debt must not stop valid fixture samples.

Aug-28 files were not reset. Coverage SHA256:
`88a4a75ec037c06a7630bdafd091292e7f69f3d0879f49e10001773e4646a87c`;
state-bundle SHA256:
`53b1d9e5de51476403fd008f02642edd524e9988be5bb31c24761bd9554e291e`.

## TD consumer compatibility

At 16:26:16 CST, authenticated 18082 catalog readback confirms `rt_min` major 2
and `broker_recommend` major 2 remain `success`, non-degraded, with receipts.
`rt_min_daily` is now **major 3**, but remains `unobserved`, degraded, without
receipt/data-through, and reports `active_config_receipt_mismatch`. The new
catalog advertises major 3; usable cumulative-minute data is not yet proven. This
dataset-local TD producer dependency is not an A-share minute outage or a TA
deployment prerequisite. Earlier major-2/zero-row observations are historical.

At 16:25:31 CST, the actual new immutable release, under the service user, consumed one
August broker recommendation for `600519.SH`, with matching double traversal,
month precision, zero audit rejections and receipt
`receipt:69ade3f976d7c133d9eda6953b7bec2fd6067889271dfbd15616209c09af89f1`.
The observed catalog version was `v1-69d7ac518ef3b1aa`; unrelated version drift
did not suppress the unchanged valid broker contract. Execution/training/live
flags were false. The existing single-dataset catalog
row adapter does not require the unrelated complete event ensemble. Earlier
empty, stale and out-of-mainboard reads were rejected, not counted as success.
This is bounded `observed` evidence, not stable consumption or historical PIT.

## Crypto track

G5 delayed-paper service and forty-symbol observation are now bound to
`7eb0e62`; ten-symbol observation stays separately pinned to `5d33501`.
G5's first natural new-release invocation ran 16:25:55–16:27:35 and completed
the 08:20Z window with `status=completed`, `data_incomplete=false`,
`requested_window_consumed=true`, execution/live false. This is one successful
window, not continuous-health or profitability proof. Forty-symbol's first
new-release invocation ran 16:24:47–16:29:00 and also completed the 08:20Z
window: `data_incomplete=false`, `requested_window_consumed=true`,
`budget_deferred=false`, execution/live false. Its head advanced to 2,101 events
and 1,278 accepted observations, retaining 557 data rejects and 266 gaps.
The 159,446-byte primary bars sidecar was saved at 16:27:20, before optional
work and final completion; the original 08:24:30Z cutoff was retained.
Earlier 07:55Z receipts had G5 incomplete and forty-symbol watermark rejection.
Service exit 0 alone is not data acceptance.

The deployed repair saves validated bars before optional spread work and
preserves an uninterrupted pending locator across invocation/crash recovery.
Budget deferral remains visible; invalid bars, changed profiles, wrong cutoffs,
missing proof and corrupt state stay rejected. This does not repair upstream
watermarks, create a fill or prove continuous current coverage.

Full forty-symbol coverage and a latest continuous 288-bar segment constrain
runtime maturity and later authority claims; they do not block safe-segment simulation.
Research drafts 606/608/610/612 remain isolated and are not included in this
runtime release, natural-window acceptance or current profitability claims.

## Lessons and next evidence

- Scope gates to the dependent stock, dataset, window or auxiliary leg. Do not
  apply fixed-cohort experiment requirements to rolling subset simulation.
- Persist verified primary evidence before optional work; preserve its original
  recovery locator and proof even if interrupted again.
- Budget fairness needs real first/replay latency, not only fast fixtures.
  Missing data and no-trade reasons remain visible.
- Separate coverage, candidates, pending orders, actual fills and reconciliation.
  Timer state, HTTP 200, CI and process exit alone do not prove business success.
- Day-5/day-10 are automatic evidence/report checkpoints, not manual admission gates.

Root-only rollback backup: `/var/tmp/ta-sim-release-20260830.742c1b`.
Rollback restores verified code/configuration and prior timer state, never old
ledger contents. All ten originally active/enabled timers were restored at
16:24:47 after a 77-second pause; in-flight learning finished naturally, no
writer was killed, and TD was untouched. Effective A-share baseline/scale
timeouts remain 90/240 seconds; forty-symbol remains 360 seconds. Next: further
independent natural Crypto windows, then Aug-31 rolling subsets through
candidate/fill-or-reject/reconcile. No Monday-market success is claimed.

## Copilot and paused scopes

TradingCopilot has no fresh UI/consumer acceptance in this batch. CNFutures and
prediction markets stay paused. No credentials, accounts, capital ledgers,
research runtime or new monitoring task was enabled by this delivery.
