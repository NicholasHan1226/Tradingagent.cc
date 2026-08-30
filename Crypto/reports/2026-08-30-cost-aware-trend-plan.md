# Cost-aware slow-trend experiment v1

Frozen before this batch's comparison run on 2026-08-30 (Asia/Shanghai).
This is a new diagnostic of already viewed historical data, NOT a clean
holdout or a preregistration of unseen returns. No forward window or K10
definition is changed. No production execution, promotion or risk expansion.

## Question and budget

Do cost-aware buy admission and two-day signal confirmation add net value to
the existing daily 20/60-day SMA trend under unchanged sampled risk rules?
Old G5 intraday threshold/maker experiments are not reopened.

Exactly six variants: baseline_risk, cost_only, confirmation_only, combined,
combined_no_trend, combined_no_vol. The last two remove one signal component,
not account risk. Exactly two cost scenarios: existing 10bps fee + 2bps
slippage per side, and double both. No parameter grid, best-cell selection or
post-result adjustment. Twelve strategy cells; each scenario also shows cash
and original-signal-budget BTC+cash. Cross-scenario trade decisions may differ
because the cost gate sees its scenario's cost; this is not fixed-trade repricing.

## Fixed definitions

- All ten frozen symbols, same original longest continuous daily valuation
  segment. Feature lookback 60 days; input cutoff before 2026-08-30T00:00Z.
- Original signal: SMA20 > SMA60, per-symbol cap 10%, annual volatility cap
  40% using prior 20 returns, original relative rebalance band 25%.
- Cost forecast: pooled one-dimensional ridge regression with intercept,
  x = SMA20/SMA60 - 1; y = execution-open return over five days. Center x/y,
  slope = sum((x-xmean)*(y-ymean))/(sum((x-xmean)^2)+0.01).
  This is an uncalibrated point estimate, not a probability or proven edge.
- Training considers past 60 decision days on the fixed five-day grid anchored
  at 2026-02-01 UTC. Every label's exit open must be STRICTLY BEFORE the
  current 00:00 UTC decision. Entry-to-exit daily valuation dates must be
  contiguous. Minimum 30 asset-windows AND five distinct entry-date clusters;
  cross-asset rows are not independent samples. Insufficient training blocks
  buys but never valid reductions.
- Gate buys/increases only when estimated five-day gross return exceeds
  2 * ((1+fee)*(1+slip)/((1-fee)*(1-slip)) - 1).
- Confirmation: two consecutive positive trend days admit the signal;
  two consecutive nonpositive trend days close it. First negative day can
  retain the admitted target, with current prior-data volatility sizing.
  Missing features close/reset the signal. Risk exits override confirmation.
- no_trend removes trend admission/confirmation only; the forecast still uses
  the same frozen strength feature. no_vol removes volatility signal sizing,
  not risk latches or the 10% per-symbol target cap. These are component
  ablations, not claims that all trend information or all risk is removed.
- All six variants reuse the same existing research risk engine: daily-loss
  3%, three losing exit batches, drawdown 5% => 0.75 target multiplier,
  drawdown 7% => sticky pause without automatic reset. Only sampled daily
  opens/closes/fills are observed; this does not validate 5m risk or a loss cap.
- Cash and BTC+cash are explicit context, not strict ex-post risk matches.
  No extra portfolio leverage, no direct exchange/provider or TD database read.

## Required results

Retain all cells, net return, daily and sampled drawdown, fees, turnover,
trade legs, closed position episodes, pause date/reasons, buy rejections,
cash conservation, per-day equity and input/plan/output hashes. Report
training coverage and coefficient/label-cutoff provenance. Compare each
variant to baseline_risk and report paired removal deltas without causal or
significance claims. Preserve negative/zero-trade results. Do not promote.

## Validation and handoff

Test future perturbation invariance, strict label maturity at decision time,
missing-day/gap rejection, insufficient training, deterministic rerun,
cost stress/admission, confirmation versus risk override, no_vol cap,
cash/positions/fees reconciliation and legacy simulator output parity.
Input reuse is the already verified TD catalog/query copy; source receipts
must match its rows, not merely accompany them. New code lives in an isolated
candidate on #608 head 7e5312be4ad5d753f68ad6ba8d87d50272cd501a.
Default market-lane identity check rejects this non-lane worktree as expected;
isolated path/ancestry checks are structural only, not Controller acceptance.
Keep #606/#608 unchanged and submit a separate stacked draft; no merge/deploy.
