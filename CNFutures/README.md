# CNFutures

国内期货模块当前只做全自动模拟盘。它用于验证策略风格、风控参数、保证金占用、手续费估算和复盘闭环, 为未来受控实盘接口做准备。

## Data Flow

```
SharedSignals(Tushare fut_basic/fut_daily/rt_fut_min)
  -> market_assets / market_bars_daily / market_bars_intraday, market="Futures"
  -> CNFutures adapter, internal market="cn_futures"
  -> multi-style signal generation
  -> simulated fill
  -> signals/filled + shared/review/data
```

MarketGraph 可以读取同一份 SharedSignals 数据做商品、宏观和跨市场研究; CNFutures 只消费研究证据和数据, 不把交易决策回写给 MarketGraph。

## Simulation Boundary

- Only `capital_layer=simulated` is allowed.
- There is no separate CNFutures shadow layer.
- Each style runs as an isolated simulated lane, for example trend, breakout, and mean reversion.
- CTP/SimNow real trading is documentation and interface reserve only. Runtime defaults must stay fail-closed with `real_trading_enabled=false`.

## Styles

Checked-in styles live under `CNFutures/strategies/`:

- `trend`, `breakout`, and `mean_reversion` are commodity-futures simulation lanes for `rb/cu/i/m`.
- `index_intraday_directional` is an intraday long/short direction lane for stock-index futures `IF/IH/IC/IM`.

`index_intraday_directional` uses 5-minute bars to estimate short-horizon
direction from multi-bar momentum, moving-average distance, and volume
confirmation. It can emit simulated `buy`, `sell`, or `hold`, and it stops
opening new signals near the day-session close. The style is day-session only:
bars outside 09:30-11:30 and 13:00-15:00 China time are forced to `hold`, and
night-session automation skips the style. To improve win-rate quality before
sample size is large, the style also requires momentum and moving-average
distance to point in the same direction and defaults to `min_volume_ratio=1.05`;
weak or misaligned moves become `hold` instead of simulated trades. It also
skips the first 15 minutes after a day-session open, pauses after large opening
gaps for 30 minutes, and filters very low realized 5-minute range so the
win-rate lane avoids thin/noisy setups. The second-stage quality filters also
require recent bars to move consistently in the predicted direction, reject
latest-bar intrabar reversals such as spike-and-fade moves, and require the
direction score to be large enough relative to recent range. A further bar
quality layer rejects missing 5-minute bar gaps, latest bars with weak
body-to-range commitment, insufficient consecutive aligned bars, and obvious
late-chase entries after a sharp extension. This style is for simulated
validation of index-direction timing; it does not enable real CFFEX trading.
Confirmed signals also carry simulated review metadata: `scenario_tags` for
session/product/volatility/volume/signal-strength buckets and an `exit_plan`
with prediction horizon, time stop, max hold, stop-loss, take-profit, and
no-overnight intent. These fields support review and evolution only; they do
not route to any real broker.

## Automation Entry

Run one simulation cycle:

```bash
python -m CNFutures.run_simulation --cadence 5min --json
```

Cron wrapper:

```bash
shared/wrappers/job_cn_futures_sim.sh
```

The wrapper uses the existing Tradings env loader and writes normal cron logs. It does not write to SharedSignals, MarketGraph, CTP, or any broker account.

Production cadence is 5 minutes during day and night futures sessions. The
TradingAgent cron runs one minute after SharedSignals CN futures 5-minute
collection, so simulation reads the latest `market_bars_intraday` bar with
`interval="5min"`. Order idempotency includes the latest bar timestamp, not
only the trade date, so separate 5-minute bars can create separate simulated
orders while duplicate reruns of the same bar remain idempotent.

SharedSignals CN futures 5-minute collection defaults to the commodity products
`rb/cu/i/m` plus stock-index futures `IF/IH/IC/IM`. The index-direction style
must continue reading those bars through SharedSignals APIs rather
than adding a separate TradingAgent data download path.

Production readers use `SHAREDSIGNALS_API_URL` first for Futures assets,
daily bars, and 5-minute bars. Direct SQLite read-model access is only a
diagnostic/test path and must be explicitly enabled with
`TRADINGAGENT_ALLOW_SHARED_SIGNALS_SQLITE=1`. `SHARED_SIGNALS_DB` only
selects the diagnostic database after one of those explicit switches is set; it
does not enable fallback by itself.

Intraday simulation candidates must be executable contracts from the latest
current-day 5-minute bar batch in SharedSignals. Generic product symbols such
as `CU.SHF` and expired, stale, or earlier-batch contracts are not valid
simulation candidates even if they exist in the futures asset table or
historical daily bars.

Pre-open validation uses the same executable-contract filter as the runtime
adapter. It reports raw symbol count, executable symbol count, covered products,
5-minute read-model reachability, and runtime style state. A daily bar for a
generic product symbol is not enough to pass readiness.

The 5-minute runner rejects stale intraday input by default when the latest bar
is more than 10 minutes old. Configure the threshold with
`--max-intraday-bar-age-minutes` or `CN_FUTURES_MAX_INTRADAY_BAR_AGE_MINUTES`.
It also blocks repeated same-side exposure for the same style and contract on
the same trade date; an opposite-side signal can still create a new simulated
trade.

Set `CN_FUTURES_SIM_DISABLED=1` to pause the simulated runner without editing
cron. The command exits successfully with `state="paused"` and
`real_trading_enabled=false`; observation/report jobs can continue so operators
still see the paused state.

## Execution Realism

The simulated executor is still paper-only, but it no longer assumes ideal
fills:

- execution price is adjusted by configurable slippage bps and rounded to the
  contract tick size
- if a 5-minute bar or order carries best bid/ask fields, buys use ask and
  sells use bid, with available quote size capping simulated fills
- simulated fill receipts preserve best bid/ask, quote size, last trade date,
  and expiry date when SharedSignals provides them
- static daily price-limit bounds reject clearly invalid simulated prices
- 5-minute bar volume limits maximum fill quantity through
  `volume_participation`; oversized orders become `partial`
- partial receipts are stored in `signals/partial`
- margin, notional, and fees are recomputed from actual simulated fill price
  and filled quantity
- opposite-side fills for the same style and contract estimate round-trip
  realized PnL, so review scoring can start accumulating win-rate samples
- force-flatten closes use the existing position cost basis and simulated fill
  price to record realized PnL; `score_records` exposes `pnl_attribution` so
  operators can distinguish no closed PnL, sample insufficiency, and actual
  realized gains/losses
- simulated positions are snapshotted under
  `signals/positions/cn_futures_sim_positions.json`
- new opening orders are blocked when a style would exceed its configured
  margin-usage cap
- `no_overnight` styles create a simulated flatten order near the configured
  day-session close window
- lunch break is treated as market closed for 5-minute simulation, so stale
  bars during 11:30-13:00 do not create degraded samples
- styles can block new orders inside a configurable rollover window before and
  after the contract month starts
- explicit `last_trade_date` / `expiry_date` metadata triggers an expiry guard
  before simulated execution
- if no style is eligible for night trading, first-sample validation reports a
  pass/observation state rather than an execution failure

This is closer to real trading than ideal fills, but it still does not model
live multi-level order book queue priority, exact exchange limit-state matching,
exchange-grade forced liquidation, or precise exchange delivery calendars when
SharedSignals does not provide that metadata.

## Review

Simulation records are append-only:

- Signal state: `signals/filled/SIM-CNF-*.json`
- Review evidence: `shared/review/data/cn_futures_sim_reviews.jsonl`
- Dashboard-compatible style comparison: `shared/review/cn_futures/style_comparison.json`
- Style performance history: `shared/review/cn_futures/style_performance.jsonl`

The review payload includes `score_summary` by style:

- `trade_count` / `filled_count`
- `fee`
- `margin_required`
- `notional`
- `realized_pnl` and `win_rate` only when realized PnL samples are available
- `max_drawdown` only when a realized PnL curve exists
- `status`, where small or open-only samples are marked `sample_insufficient`
- `error_summary` and `style_health`, so stale data, repeated same-side exposure,
  and other simulated gates can be reviewed by style without opening logs
- `hold_count` and `hold_reason_summary`, so styles that correctly refuse weak
  setups can be reviewed by reason, style, symbol, and session without treating
  every no-trade cycle as missing data
- Closed-session 5-minute cron runs return `market_closed` without appending an
  empty review row, so the latest actionable review keeps the last in-session
  hold/fill evidence instead of being overwritten after the close.
- `forward_label_summary`, which counts pending or labeled forward outcomes by
  style and scenario. Live 5-minute runs start as `pending_future_bars`;
  historical/replay rows with future bars can be labeled with direction
  correctness, time-stop result, take-profit hits, stop-loss hits, MFE, and MAE.
- `dynamic_threshold_candidates`, simulated-only suggestions for raising or
  testing lower thresholds based on forward labels and hold pressure.

These scores rank simulated styles for further research and feed existing
health/metrics surfaces. They do not create a standalone dashboard, do not grant
live trading permission, and do not automatically promote a style into real
trading.

## Observation Report

Use the read-only observation report as the dashboard-ready summary of the
5-minute futures loop:

```bash
python -m CNFutures.observation_report --pretty
```

On production, point it at the SharedSignals runtime:

```bash
SHAREDSIGNALS_API_URL=http://127.0.0.1:8082 \
python -m CNFutures.observation_report --pretty
```

The report summarizes:

- current phase: waiting for 5-minute data, waiting for simulated samples,
  waiting for style review, ready to observe, or blocked
- dashboard-ready `schema_version`, `dashboard`, and `next_validation` fields
  so UI code does not need to parse nested check details
- data freshness, latest bar time, symbol count, and current/next session
- latest simulated review counts, fills, errors, and error summary
- hold counts and top hold reason, so the dashboard can distinguish "no
  opportunity" from data or execution gaps
- forward-labeled and forward-pending sample counts, plus dynamic threshold
  candidate count
- 5-minute sample evidence such as `cadence`, `latest_bar_time`, and
  `real_trading_enabled=false` when available
- ranked style rows, runtime style weights, generated variants, and alerts

It is read-only by default. `--write-json <path>` may be used by a dashboard or
cron wrapper to publish a snapshot, but the report itself does not create
signals, change weights, or touch broker adapters.

Optional wrapper:

```bash
shared/wrappers/job_cn_futures_observation_report.sh
```

The production cron refreshes this report after each CNFutures simulated cycle
during day/night futures sessions. It is read-only and writes
`shared/review/cn_futures/observation_report.json` for dashboard consumption;
it does not create signals, mutate style weights, or touch execution queues.

## Win-Rate Calibration

Post-session calibration is intentionally lightweight:

```bash
python -m CNFutures.calibration --pretty
```

Optional wrapper:

```bash
shared/wrappers/job_cn_futures_calibration_report.sh
```

The calibration task reads CNFutures simulated filled/partial signal cards and
SharedSignals Futures 5-minute bars, then writes:

- `shared/review/cn_futures/forward_labels.jsonl`
- `shared/review/cn_futures/win_rate_calibration_report.json`
- `shared/review/cn_futures/win_rate_calibration_report.md`

It does not rewrite append-only simulation reviews, does not change checked-in
strategy JSON, does not create orders, and keeps `real_trading_enabled=false`.
If not enough future 5-minute bars exist yet, labels remain
`pending_future_bars` and the report recommends observation instead of
parameter changes.

Opening validation is also read-only:

```bash
python -m CNFutures.opening_validator --pretty
```

It checks whether the current day/night session has started receiving Futures
5-minute bars and whether symbol coverage is above the minimum threshold.
Optional wrapper:

```bash
shared/wrappers/job_cn_futures_opening_validation.sh
```

Its output explicitly reports `data_source="SharedSignals read_model"` and
`read_only=true`; it does not collect futures data from TradingAgent.

Two additional read-only checks are available for opening-day operations:

```bash
python -m CNFutures.opening_validator --pre-open --pretty
python -m CNFutures.opening_validator --first-sample --pretty
```

Wrappers:

```bash
shared/wrappers/job_cn_futures_pre_open_validation.sh
shared/wrappers/job_cn_futures_first_sample_alert.sh
```

`--pre-open` checks whether SharedSignals daily Futures bars are present before
the next day, afternoon, or night session. `--first-sample` waits for the first
5-minute window after a session opens, then alerts if SharedSignals has no
usable 5-minute Futures bars or if TradingAgent has not produced the first
simulated sample. Both modes are read-only, keep `real_trading_enabled=false`,
and do not write to SharedSignals, MarketGraph, CTP, SimNow, or broker queues.

Minimum next-session acceptance checklist:

1. Before open, verify the production read model exists and SharedSignals
   reports Futures daily or latest 5-minute coverage for the target session.
2. At 08:55, 12:55, or 20:55, run the pre-open validator and confirm
   `read_only=true` and `real_trading_enabled=false`.
3. At 09:05, 13:05, 21:05, or 00:35, run the opening validator and confirm
   Futures 5-minute bars are present for the current session.
4. At 09:10, 13:10, 21:10, or 00:40, run the first-sample validator and check
   top-level alerts, not only the `opening_30m_review` block.
5. By 09:30, 13:30, 21:30, or 01:00, confirm one of two outcomes: a simulated
   sample/review/receipt exists, or the report gives a clear hold/no-trade
   reason. Missing data, missing review, and missing receipt must stay visible
   as warnings.

## Simulated Evolution

CNFutures has a simulated-only style governor:

```bash
python -m CNFutures.evolution --pretty
```

Preview without writing runtime overlays:

```bash
python -m CNFutures.evolution --dry-run --pretty
```

The governor reads `style_performance.jsonl`, `style_comparison.json`, and the
checked-in JSON files under `CNFutures/strategies/`. It writes only runtime
simulation overlays under `shared/review/cn_futures/`:

- `style_weights.json`: active/paused status and simulated risk weights
- `evolution_plan.json`: latest decision record
- `evolution_log.jsonl`: append-only decision history
- `generated_styles/*.json`: small candidate variants for further simulation

The checked-in strategy files stay read-only during evolution. The adapter loads
runtime generated styles and weight overlays for future simulated runs, so poor
or blocked styles can be paused and improving styles can receive more simulated
risk. When a style has improving positive samples, the governor can create up
to three variants per cycle (`precision`, `fast`, `smooth`) under the objective
`win_rate_first_risk_adjusted`. This raises iteration speed by testing a small
parameter family in parallel while keeping the base strategy file unchanged.
This remains a simulation lane: outputs include
`real_trading_enabled=false`, do not touch CTP/SimNow, and do not promote any
style to real trading.

When `index_intraday_directional` performs well, generated variants keep the
same style family, `IF/IH/IC/IM` product scope, `no_overnight=true`,
`day_session_only=true`, `trend_alignment_required=true`, and volume
confirmation. The evolution layer can tune thresholds, lookback windows,
volume filters, simulated risk weights, and simulated-only threshold
candidates, but it cannot turn this lane into a real-trading strategy.

Production cron runs the governor every 30 minutes during CN futures day and
night sessions. This slower cadence is intentional: 5-minute simulation keeps
collecting samples, while evolution waits for enough evidence before changing
simulated style weights.

## Live Chain Validation

Use the read-only live-chain check before judging whether the 5-minute futures
loop is ready for observation:

```bash
python shared/runtime_test/cn_futures_live_check.py --pretty
```

On production, point it at the SharedSignals API; do not read a sibling
SharedSignals runtime directory:

```bash
SHAREDSIGNALS_API_URL=http://127.0.0.1:8082 \
python shared/runtime_test/cn_futures_live_check.py --pretty
```

The report joins:

- SharedSignals Futures 5-minute freshness from `tools/check_cn_futures_5min_freshness.py`
- SharedSignals and TradingAgent cron entries
- latest CNFutures simulation cron log
- append-only review rows in `shared/review/data/cn_futures_sim_reviews.jsonl`
- style comparison and style performance outputs
- simulated evolution plan and style weights
- existing `market_health` and `ops_report` CNFutures surfaces

It also emits `observation_phase` and `alerts` for the dashboard. `pass` means
the chain has fresh data and review/style samples. `warn` is
acceptable during weekends, closed sessions, or before the first live sample is
produced. `fail` means a hard wiring problem such as missing cron, unreadable
freshness output, or broken existing health surfaces. The script is read-only
and always reports `real_trading_enabled=false`. `next_validation` tells the
next session workflow whether to wait for the next session, validate the
current session, or continue accumulating win-rate samples.

## Real Trading Reserve

`CNFutures/live_gateway.py` is a fail-closed placeholder for future CTP / futures-company integration. It currently:

- reports `real_trading_enabled=false`
- reports `broker_adapter_ready=false`
- rejects every real order request with `SafetyViolation`
- forbids falling back from a rejected real order to simulated execution

Promotion to future real trading is a separate gate. It requires documented broker authorization, account setup, futures-company margin/fee metadata, risk limits, callback reconciliation, manual approval, emergency halt, and a reviewed fail-closed CTP gateway.

Minimum future live-trading handoff conditions:

- at least one full live-sim observation window with fresh SharedSignals
  Futures 5-minute bars and no hard live-chain failures
- sufficient realized PnL samples in simulated review output; open-only fills
  remain `sample_insufficient`
- risk limits documented per style, product, margin usage, daily loss, and
  emergency halt
- broker adapter, order callback, position reconciliation, receipt checksum,
  and manual approval token verified in a non-production account
- Nicholas explicitly approves moving a named style from simulation review into
  a separate real-trading queue
