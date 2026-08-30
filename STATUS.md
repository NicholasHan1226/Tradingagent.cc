# TradingAgent current status

Observed at: 2026-08-30T15:57:00+08:00

This replaceable snapshot separates source, installed configuration, runtime and
market receipts. Historical research/results remain in Git and dated reports;
none of these observations grants capital, orders, live trading or promotion.

## Source and release layers

| Layer | Fresh observation | Boundary |
|---|---|---|
| 本地主线 | clean user/Kimi research branch, preserved without switching | resolve with `git rev-parse HEAD origin/main`; not current main |
| GitHub 主线 | use the current remote ref and its matching CI evidence | resolve with `git rev-parse HEAD origin/main`; never pin a self-invalidating main SHA in this row |
| Ordinary server source | clean at `5cd9649` in `/opt/investment/tradingagent` | behind GitHub; not the active release, no three-end-sync claim |
| Immutable current release | `bb4413864ef0b76452916e87e48fdc72e69a7deb` | code release, not market success |
| Front read API | health returned `ok=true`; snapshot returned parseable JSON; process cwd matches the new immutable release | minute fixture receipts are a separate read model |
| Accepted repair | [PR 605](https://github.com/NicholasHan1226/Tradingagent.cc/pull/605), exact-main tests [33296436665](https://github.com/NicholasHan1226/Tradingagent.cc/actions/runs/33296436665), deployment [33296809251](https://github.com/NicholasHan1226/Tradingagent.cc/actions/runs/33296809251) all complete | existing internal simulation release only; no public route, broker or capital change |

## A-share: actual coverage, not a timer-only claim

The latest natural trading session is **2026-08-28** (today is Sunday).

- Baseline initializer published 30 symbols. Scale initializer published 3,188
  active symbols from 3,193 source identities: two recent listings pending and
  three missing previous closes excluded. These exclusions are local, not a
  whole-universe entry gate.
- Root-authorized readback of
  `/var/lib/tradingagent/ashare-minute-paper-scale500/20260828/coverage-receipts/094500.json`
  proves **1,197 accepted / 3,188 active**, 1,991 missing, three row rejections,
  and 20 budget-exhausted shards. This is a valid partial coverage receipt, not
  zero activity and not 1,197 positions or fills.
- The day's gate retains that 09:45 accepted bar, then records
  `fallback30_selected / minute_same_observation_mismatch`. Later scale
  invocations were no-ops; the baseline remained blocked by
  `minute_auto_initial_bar_missing` after its first read failed.
- The Aug-26 `86fd04c` release lacks `_collect_stable_minute_pair`;
  subsequent `580b453` and current `bb441386` include it. Friday's failure
  is not evidence that the newly
  deployed stable-pair implementation itself failed a natural session.
- All four existing A-share session/paper timers are enabled and active.
  Next initializer: Aug-31 09:18; next first delayed bar: Aug-31 09:42.
  These scheduling facts do not prove Monday's data or consumer success.

Confirmed repairs now deployed:

1. Rolling mode admits the first validated subset, without the fixed-cohort
   two-opening-bar availability gate.
2. A mismatched observation still fails closed for that read, but does not
   poison future independent slots. Gap recovery must retain missing-slot
   evidence and keep full-session/learning eligibility false.
3. The 30-symbol service explicitly permits gap-marked late starts.
4. All four A-share units join immutable release reconciliation and rollback.
   The scale-paper template allows 240 seconds (180-second read budget plus
   processing headroom), below the five-minute cadence.
5. The installed 480-second temporary override was moved to a recoverable
   backup. Effective scale timeout is 240 seconds; baseline ExecStart includes
   both `--event-aux` and `--allow-late-start`.

All four effective A-share WorkingDirectory/PYTHONPATH/ReadOnlyPaths bindings
match `bb441386`; service-user imports pass with bytecode writes disabled.
The Aug-28 coverage receipt SHA256 remains
`88a4a75ec037c06a7630bdafd091292e7f69f3d0879f49e10001773e4646a87c`;
the state-bundle SHA256 remains
`53b1d9e5de51476403fd008f02642edd524e9988be5bb31c24761bd9554e291e`.
No historical gate, receipt or capital ledger was reset.

The fixture accumulator is `non_production_fixture`, not the canonical 50,000
CNY capital-backed execution loop. Coverage is not a resolved outcome or PnL.

The Aug-28 persisted books have **zero fills and zero positions**. Each of the
four independent fixture sleeves retains 3,188 rejection records from the
incident recovery. One accepted bar alone has no rolling features; this is not
a completed trade. The canonical capital-backed composition remains test-only
and network-closed, without a CLI or scheduler; it was not activated by this
batch. Existing delayed-paper fixture simulation continues independently.

Current candidate work, not yet deployed at this timestamp:

- Skip only the unaffordable top stock and consider the next eligible stock;
  preserve the refusal, 100-share lot and 7,500 CNY cap.
- Add actual settled quantity/notional/fees to each sleeve receipt. Existing
  runner/loop/capital tests pass with independent persisted cash/position
  roll-forward and restart verification (62 tests); adjacent checks pass 287.
- Bound retry sleeps and wire requests by the remaining shard/global budget,
  preserving valid shards. Historical performance probes read three independent
  100-stock shards twice: pair durations 21.997, 13.935 and 16.626 seconds;
  catalog 2.927 seconds. These are timing observations, not fresh PIT acceptance
  or proof of next-session full coverage. Do not mechanically divide the budget
  so tightly that an otherwise successful first/replay pair cannot complete.

## TD consumer compatibility

Authenticated 18082 catalog readback reports `v1-10c505ce7d8b1c43`:
`rt_min` major 2 has 1,199,368 rows; `broker_recommend` major 2 has 544 rows.
`rt_min_daily` remains major 2, zero rows, `unobserved`, with
`active_config_receipt_mismatch`. Its producer release/receipt remains a
dataset-local dependency and does not block `rt_min` or TA deployment.

At 15:55:22 CST, accepted-main code in an isolated server directory consumed
one August broker recommendation for `600519.SH`, with two matching traversals,
month precision, no audit rejection and receipt
`receipt:69ade3f976d7c133d9eda6953b7bec2fd6067889271dfbd15616209c09af89f1`.
Training/execution/live authority remained false. Single-dataset profile loading
uses the existing exact catalog-row adapter, not the complete event-ensemble
gate. Earlier empty, stale and out-of-mainboard queries were refused, not
promoted into valid input. Schema-compatible source is not deployed runtime:
the active TA release still precedes the recent read-only adapters.

## Crypto track

- All five G5 effective release bindings now match `bb441386`.
  G5 delayed-paper service completed successfully at 14:27:07 after its
  14:25:58 timer start on the new release. The 06:20Z-window receipt says
  `completed`, `data_incomplete=false`, `execution_eligible=false` and
  `real_trading_enabled=false`. The learning service also completed its
  new-release timer cycle at 14:33:44 with exit 0; this is not a profitability
  or model-promotion claim.
- Ten-symbol observation remains separately pinned to `5d33501`, with a
  successful observation in the readback batch.
- Forty-symbol observation was separately rebound from `e64e20d` to
  `bb441386`. The old release's 13:45 batch
  rejected the old Aug-29 19:35Z slot with `query_shape_invalid`, then rejected
  the current Aug-30 05:40Z slot with `watermark_invalid`.
  Systemd exit 0 therefore **does not mean usable data**; `data_incomplete=true`.
  This issue is separate from A-share and does not justify relaxing PIT checks.
- The deployed bounded repair removes 20/45-second shape retry sleeps for a
  historical window only, preserving current-window retry and every watermark
  check. Its installed timer template is aligned with the existing
  +285-second override. The first resumed invocation (14:25:07–14:25:28)
  rejected the 06:20Z current window with `watermark_invalid`; the historical
  shape rejection no longer incurred its old 65-second wait. The normal
  14:29:48 invocation exhausted its 300-second budget: `backlog_pending`,
  `budget_deferred=true`, `requested_window_consumed=false`, one processed
  historical rejection and `data_incomplete_reason=query_shape_invalid`.
  The 06:25Z requested window was not accepted. The next timer invocation
  began at 14:34:48; long current-window collection is an unresolved P0,
  not evidence that the whole TA chain is healthy.
  This is not a claim that upstream receipt timing is fixed.
- Research artifacts and rolling evaluations are not summarized as current
  profitability here; use their dated receipts and the independent Crypto task.

Full forty-symbol coverage and a latest continuous 288-bar segment constrain
runtime maturity and later authority claims; they do not block safe-segment simulation.

Fresh 15:57 inspection finds G5 and forty-symbol services executing on the
same `bb441386` release; in-progress runs are not acceptance. The new candidate
persists validated bars before optional spread collection and uses existing
pending recovery across invocations. Budget exhaustion remains visible;
historical receipts, original cutoffs and checkpoint integrity are unchanged.

## Lessons and next evidence

Do not infer missing receipts from permission-denied/empty directory reads.
Do not infer collector/consumer health from timer state, HTTP 200 or exit 0.
Separate an invalid batch from future recovery, and audit both service templates
and overriding drop-ins. Source fixes need effective-version and next-natural-slot
readback; a weekend fixture test cannot replace that market evidence.
Documentation checks must protect scope and provenance, not whitelist obsolete
change numbers or require a successful runtime outcome regardless of new facts.
The live-main-row prohibition remains; dated release links and failed/pending
service evidence elsewhere are legitimate.

Apply gates to the consuming scope: one stock's affordability, one optional
dataset, or one auxiliary leg must not discard independent valid work. Persist
verified primary evidence before optional work, and retain an uninterrupted
recovery locator. Budget fairness needs real latency evidence, not only fast
fixture tests. Never convert coverage counts into fill counts.

Candidate CI passed 5,993 Python tests, one skip, 266 subtests and 361 frontend
tests; exact merged-main CI and packaged-source/checksum verification also passed.
Local affected regression passed 261 tests, with 89 architecture/effective-release/
deployment checks in the final combination. Independent review covered the
runtime deltas; the main assistant reviewed the deployment transaction tests.

Original service/helper/drop-in copies and the retired timeout override are in
the root-only `/var/tmp/ta-continuity-20260830.UmOzs6` backup. The old immutable
`580b453` remains available. All ten previously enabled/active timers were
restored; no timer was newly enabled, and TD was not restarted. Rollback may
switch verified code/configuration but must preserve new append-only facts.

The next natural A-share session (Aug-31) must prove continued receipt
accumulation across valid subsets and explicit gaps; weekend checks cannot
replace that evidence. The forty-symbol current-window budget remains unresolved;
measure bar/catalog/spread/transport phase costs before changing query strategy,
and do not widen PIT cutoffs, fabricate data or block independent consumers.
Authentication, state integrity, real trading and cross-market capital guards
remain closed.

## Copilot and paused scopes

TradingCopilot has no fresh UI/consumer acceptance in this batch. CNFutures and
prediction markets stay paused; no credentials, account state or ledgers were
changed by this audit. No new monitoring task is implied.
