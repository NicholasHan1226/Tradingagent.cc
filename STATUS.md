# TradingAgent current status

Observed at: 2026-08-23T12:58:00+08:00

This file is a replaceable current summary. It separates source, release, runtime,
market evidence, and authority. Historical chronology remains in Git history and
dated `docs/reports/`; it is never a substitute for a fresh readback.

## Source and release layers

| Layer | Current observation | Claim boundary |
|---|---|---|
| 本地主线 | Kimi/user A-share lane, intentionally not acting as canonical main | resolve with `git rev-parse HEAD origin/main`; preserve and do not reset or repurpose |
| GitHub 主线 | branch CI passed and exact-main validation ran green for the latest research/docs merges at the observation time | resolve with `git rev-parse HEAD origin/main`; accepted and packaged source only |
| Ordinary server source | `1d58efe`, behind GitHub and containing untracked operational files | not synchronized; do not clean or fast-forward over unknown files |
| Effective release | immutable release `f74bd1999b576b0bdc44fe1a816479cf9cc8eb28` | deployed code layer, cut over 2026-08-23 with green exact-main and front health; the forty-symbol observer service was rebound to it by drop-in after cutover |
| Runtime authority | all observed A-share/Crypto receipts reported real trading, execution, capital, production promotion, and automatic risk expansion disabled | simulation/read-only only |

An effective immutable release can be valid while the ordinary source checkout is
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

Data plane (verified this batch against the production read-model store,
read-only):

- the six crypto collection timers were resumed and subsequently observed
  collecting with success receipts and zero lock contention;
- spot 5m bars are complete for all 40 datasets (the known 2,560-bar hole was
  closed by backfill); premium-index daily dumps are complete for all 40
  symbols over the 198-day acceptance horizon;
- open interest: the 198-day official daily-dump backfill is accepted; 422
  partially-ingested days (1–287 surviving slots) were repaired through the
  idempotent append-only re-collection path with zero failures. `2026-08-12`
  is a permanent provider-side hole across all 40 symbols — Binance's own
  daily zips for that day contain 285/288 rows, which the complete-grid store
  contract correctly rejects. A residual single-slot ARBUSDT seam at
  `2026-07-04/05` is a grid-phase boundary artifact of the provider's
  unfrozen phase convention, not missing data;
- pre-existing duplicate-timestamp rows (same bucket under different payload
  versions) remain in some OI datasets; read-only research consumers dedupe
  deterministically per slot. No rows were rewritten or erased.

Research plane (sealed `research_only` / `not_promotion_evidence` /
historical-backfill-without-PIT; archived under `Crypto/reports/`):

- the momentum entry event study on current main measured the frozen champion
  entry over ~204 days × 40 symbols (~109k non-overlapping samples in the
  largest cell): every threshold × horizon cell is net-negative with
  |t(net)| ≥ 3.9 under round-trip taker costs, and per-trip net loss equals
  the ~0.24% cost line. Shadow-only conclusion: no parameter change is
  justified; frequency/threshold tuning cannot rescue this signal family;
- the exit-cost counterfactual on the same frozen champion simulated full
  round trips at path level over the identical window (non-overlapping strides,
  stop-loss checked before take-profit inside each bar, 6,208–7,423 trips per
  cell): mean gross return is ≈ 0 in every threshold cell (best +0.008%),
  momentum-reversal exits dominate at 94–96% of trips while take-profit is hit
  only 3–5%, mean favorable excursion never approaches the +3% target, and even
  the maker-exit upper bound (assumes touch equals fill) leaves every cell
  net-negative at about −0.139% to −0.167% per trip versus −0.239% to −0.267%
  under taker exits — the maker delta is pure fee arithmetic (+0.0998%). This
  closes execution-cost reduction as a lever: the per-trip loss is structural,
  not an artifact of exit fees or slippage;
- the 2026-08-18 funding/basis carry research modules and reports landed on
  current main as archived assets.

Runtime plane:

- the delayed-exit shadow reversal threshold now matches the champion exit
  rule on current main;
- the forty-symbol observer is recovered and producing its first healthy
  evidence. Root cause of the earlier zero-success state was the settle-clock
  bug fixed on current main (the lane computed its cutoff with the ten-symbol
  +55s boundary while its collector finishes later, so honest receipts failed
  the watermark gate); the fix reached production through the 2026-08-23
  release cutover plus an observer drop-in rebind, and the next natural cycle
  produced a successful observation with all 40 spot sources fresh through the
  just-closed bar plus a spread sidecar, correct forty-only identity, and all
  authority flags false. The identity isolation readback (inherited ten-prefix
  events stop at 2026-08-22T11:20:55Z) remains verified;
- the G5 delayed-paper service completed successfully with
  `data_incomplete=false` in its latest applicable readback; this batch did
  not re-observe it, and service completion alone does not prove a new
  resolved label, fee-after baseline comparison, or shadow recommendation;
- the 40-symbol timer remains disabled. No provider call or runtime activation
  is inferred from source presence.

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

1. Keep the recovered forty-symbol observer under natural readback across
   several consecutive cycles without changing timer or authority, then feed
   its receipts into the first rolling evaluation entry per point 4.
2. On the next A-share market window, prove the first safe-subset simulation
   decision/outcome without waiting for exact500 or every rolling symbol.
3. The only remaining crypto research lever is a different signal family: the
   2026-08-23 event study exhausted threshold/horizon scanning under taker
   costs, and the exit-cost counterfactual proved even the maker-exit upper
   bound stays net-negative — so no further work should scan thresholds,
   horizons, or execution variants of the current momentum entry; evaluate a
   new signal hypothesis instead (the archived funding/basis carry modules are
   one candidate starting point).
4. For both markets, bind the next resolved outcome to fees/slippage, baseline,
   factor/strategy version, deterministic replay, exclusions, and a shadow-only
   recommendation.
5. Replace this file after the next material readback. Do not append incident logs
   or copy these SHAs, counts, timer states, or maturity claims into durable
   architecture and policy documents.
