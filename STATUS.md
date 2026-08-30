# TradingAgent current status

Observed at: 2026-08-30T13:47:30+08:00

This replaceable snapshot separates source, installed configuration, runtime and
market receipts. Historical research/results remain in Git and dated reports;
none of these observations grants capital, orders, live trading or promotion.

## Source and release layers

| Layer | Fresh observation | Boundary |
|---|---|---|
| 本地主线 | clean user/Kimi research branch, preserved without switching | resolve with `git rev-parse HEAD origin/main`; not current main |
| GitHub 主线 | use the current remote ref and its matching CI evidence | resolve with `git rev-parse HEAD origin/main`; never pin a self-invalidating main SHA in this row |
| Ordinary server source | clean at `5cd9649` in `/opt/investment/tradingagent` | behind GitHub; not the active release, no three-end-sync claim |
| Immutable current release | `580b453bf724d27931e3493f9bf01a138fa825b8` | code release, not market success |
| Front read API | `127.0.0.1:8787/healthz` returned `ok=true` | minute fixture receipts are a separate read model |
| Repair candidate | rolling continuity and complete A-share release binding under review | not yet a deployment at this timestamp |

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
- The Aug-26 `86fd04c` release lacks `_collect_stable_minute_pair`; current
  `580b453` includes it. Friday's failure is not evidence that the newly
  deployed stable-pair implementation itself failed a natural session.
- All four existing A-share session/paper timers are enabled and active.
  Next initializer: Aug-31 09:18; next first delayed bar: Aug-31 09:42.
  These scheduling facts do not prove Monday's data or consumer success.

Confirmed repairs in this candidate:

1. Rolling mode admits the first validated subset, without the fixed-cohort
   two-opening-bar availability gate.
2. A mismatched observation still fails closed for that read, but does not
   poison future independent slots. Gap recovery must retain missing-slot
   evidence and keep full-session/learning eligibility false.
3. The 30-symbol service explicitly permits gap-marked late starts.
4. All four A-share units join immutable release reconciliation and rollback.
   The scale-paper template allows 240 seconds (180-second read budget plus
   processing headroom), below the five-minute cadence.
5. Deployment must reconcile the installed 480-second temporary override and
   preserve the existing event-aux option when enabling baseline late start.

The fixture accumulator is `non_production_fixture`, not the canonical 50,000
CNY capital-backed execution loop. Coverage is not a resolved outcome or PnL.

## Crypto track

- G5 delayed-paper service completed successfully at 13:47:13; G5 learning
  completed at 13:43:42, both exit 0
  on `580b453`. Earlier same-batch delayed-paper output reported
  `data_incomplete=false`; this does not alone prove a newly resolved label.
- Ten-symbol observation remains separately pinned to `5d33501`, with a
  successful observation in the readback batch.
- Forty-symbol observation remains pinned to `e64e20d`. Its 13:45 batch
  rejected the old Aug-29 19:35Z slot with `query_shape_invalid`, then rejected
  the current Aug-30 05:40Z slot with `watermark_invalid`.
  Systemd exit 0 therefore **does not mean usable data**; `data_incomplete=true`.
  This issue is separate from A-share and does not justify relaxing PIT checks.
- A bounded repair candidate removes 20/45-second shape retry sleeps for a
  historical window only, preserving current-window retry and every watermark
  check. Its timer template is aligned with the already installed +285-second
  override. This is not a claim that upstream receipt timing is fixed.
- Research artifacts and rolling evaluations are not summarized as current
  profitability here; use their dated receipts and the independent Crypto task.

Full forty-symbol coverage and a latest continuous 288-bar segment constrain
runtime maturity and later authority claims; they do not block safe-segment simulation.

## Lessons and next evidence

Do not infer missing receipts from permission-denied/empty directory reads.
Do not infer collector/consumer health from timer state, HTTP 200 or exit 0.
Separate an invalid batch from future recovery, and audit both service templates
and overriding drop-ins. Source fixes need effective-version and next-natural-slot
readback; a weekend fixture test cannot replace that market evidence.

After candidate CI, independent review and authorized release: verify all four
effective A-share bindings, baseline recovery option, scale timeout, unchanged
historical receipt hashes and front health. The next natural session must prove
continued receipt accumulation across valid subsets and explicit gaps.
Authentication, state integrity, real trading and cross-market capital guards
remain closed.

## Copilot and paused scopes

TradingCopilot has no fresh UI/consumer acceptance in this batch. CNFutures and
prediction markets stay paused; no credentials, account state or ledgers were
changed by this audit. No new monitoring task is implied.
