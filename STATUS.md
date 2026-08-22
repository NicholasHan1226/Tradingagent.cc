# TradingAgent current status

Observed at: 2026-08-22T18:32:09+08:00

This file is a replaceable current summary. It separates source, release, runtime,
market evidence, and authority. Historical chronology remains in Git history and
dated `docs/reports/`; it is never a substitute for a fresh readback.

## Source and release layers

| Layer | Current observation | Claim boundary |
|---|---|---|
| 本地主线 | Kimi/user A-share lane, intentionally not acting as canonical main | resolve with `git rev-parse HEAD origin/main`; preserve and do not reset or repurpose |
| GitHub 主线 | current-main CI and automated deployment workflow passed at the observation time | resolve with `git rev-parse HEAD origin/main`; accepted and packaged source only |
| Ordinary server source | `1d58efe`, behind GitHub and containing untracked operational files | not synchronized; do not clean or fast-forward over unknown files |
| Effective release | immutable release `9768907e741913541034f76357088d95febde057` | deployed code layer |
| Runtime authority | all observed A-share/Crypto receipts reported real trading, execution, capital, production promotion, and automatic risk expansion disabled | simulation/read-only only |

The source observation used release `9768907` before this documentation candidate;
the merge commit created by the documentation change is intentionally not predicted
inside a tracked status file. An effective immutable release can be valid while the ordinary source checkout is
dirty or behind. These are separate layers; neither state is described as “all
three ends synchronized.”

## A-share track

Latest applicable natural market evidence is from 2026-08-21:

- the 30-symbol session initializer completed successfully with 30 active
  partitions, no pending listings, and all trading/capital authority false, but
  catalog-version drift left `rolling_eligible=false`;
- the rolling scale session completed successfully with 3,186 eligible symbols,
  five previous-close exclusions, two newly listed pending symbols, and
  `rolling_eligible=true`;
- the scale paper unit completed as a safe `noop`, selecting the preserved
  rollback-30 state because of `minute_scale500_unclassified_urlerror`;
- the scale session and paper timers are enabled/waiting; the legacy 30-symbol
  paper timer is disabled. Timer state proves scheduling only.

Therefore the broad rolling cohort is available for a named coverage claim, but
the latest paper cycle did not create a new simulation fill/outcome. The exact
next product evidence is one natural 2026-08-24 closed-bar cycle that consumes a
safe eligible subset, records a receipt-bound simulated decision or explicit
abstention, and later resolves its fixed-horizon outcome after declared costs. The
3,186-symbol count is not a global gate and the five/two local exclusions do not
block the safe subset.

## Crypto track

- the isolated 40-symbol observer completed once under the effective release;
  each symbol contributed a bounded real-receipt bar segment;
- spread sampling accepted 28 symbols and rejected 12 with the capability-local
  `crypto_spread_watermark_invalid` reason, so bars remain usable while the spread
  feature is degraded for the rejected symbols;
- the same cycle's append-only store used the forty-symbol event contract, but the
  public runtime receipt/event identity still inherited ten-symbol names. This is
  an auditability defect, not a reason to erase the valid bars. It requires a
  current-main forward fix and a new natural readback;
- the G5 delayed-paper service completed successfully with
  `data_incomplete=false`; service completion alone does not prove a new resolved
  label, fee-after baseline comparison, or shadow recommendation;
- the 40-symbol timer remains disabled. No provider call or runtime activation is
  inferred from source presence.

Crypto may use any complete gap-bounded segment for deterministic delayed-paper or
factor/strategy evaluation. Labels never cross a gap. Full 40-symbol coverage and
a latest continuous 288-bar segment constrain coverage/runtime maturity and later
promotion/risk claims; they do not block safe-segment simulation.

## Copilot and paused scopes

- TradingCopilot remains a transitional A-share-only observation and manual
  takeover surface. No fresh Copilot runtime/consumer readback was taken in this
  observation batch, so no current health claim is made and no Copilot field blocks
  A-share or Crypto TA.
- CNFutures and prediction markets remain paused. U.S. equities and A-share
  options remain future isolated scopes.

## Factor/Strategy MVP evidence

For each active market, MVP-1 requires one real receipt-bound resolved outcome,
declared fees/slippage, one existing factor or strategy, a simple baseline,
deterministic artifact, and a shadow-only retain/downweight/disable/parameter
recommendation. This observation proves runtime/data plumbing but does not yet
prove a new MVP-1 outcome for either market. Pending labels, service/timer health,
coverage counts, and generated projections are not called learning.

## Next acceptance points

1. Forward-fix the 40-symbol public identity on current main, then read back one
   natural event without changing timer or authority.
2. On the next A-share market window, prove the first safe-subset simulation
   decision/outcome without waiting for exact500 or every rolling symbol.
3. For both markets, bind the next resolved outcome to fees/slippage, baseline,
   factor/strategy version, deterministic replay, exclusions, and a shadow-only
   recommendation.
4. Replace this file after the next material readback. Do not append incident logs
   or copy these SHAs, counts, timer states, or maturity claims into durable
   architecture and policy documents.
