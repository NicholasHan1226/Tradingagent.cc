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
night-session automation skips the style. This style is for simulated
validation of index-direction timing; it does not enable real CFFEX trading.

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
must continue reading those bars through SharedSignals/read-model APIs rather
than adding a separate TradingAgent data download path.

The 5-minute runner rejects stale intraday input by default when the latest bar
is more than 10 minutes old. Configure the threshold with
`--max-intraday-bar-age-minutes` or `CN_FUTURES_MAX_INTRADAY_BAR_AGE_MINUTES`.
It also blocks repeated same-side exposure for the same style and contract on
the same trade date; an opposite-side signal can still create a new simulated
trade.

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

These scores rank simulated styles for further research and feed existing
health/metrics surfaces. They do not create a standalone dashboard, do not grant
live trading permission, and do not automatically promote a style into real
trading.

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
risk. This remains a simulation lane: outputs include
`real_trading_enabled=false`, do not touch CTP/SimNow, and do not promote any
style to real trading.

When `index_intraday_directional` performs well, generated variants keep the
same style family, `IF/IH/IC/IM` product scope, and `no_overnight=true`. The
evolution layer can tune thresholds, lookback windows, and simulated risk
weights, but it cannot turn this lane into a real-trading strategy.

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

On production, point it at the SharedSignals runtime if the sibling directory is
not available:

```bash
python shared/runtime_test/cn_futures_live_check.py \
  --sharedsignals-root /opt/investment/SharedSignals \
  --pretty
```

The report joins:

- SharedSignals Futures 5-minute freshness from `tools/check_cn_futures_5min_freshness.py`
- SharedSignals and TradingAgent cron entries
- latest CNFutures simulation cron log
- append-only review rows in `shared/review/data/cn_futures_sim_reviews.jsonl`
- style comparison and style performance outputs
- simulated evolution plan and style weights
- existing `market_health` and `ops_report` CNFutures surfaces

`pass` means the chain has fresh data and review/style samples. `warn` is
acceptable during weekends, closed sessions, or before the first live sample is
produced. `fail` means a hard wiring problem such as missing cron, unreadable
freshness output, or broken existing health surfaces. The script is read-only
and always reports `real_trading_enabled=false`.

## Real Trading Reserve

`CNFutures/live_gateway.py` is a fail-closed placeholder for future CTP / futures-company integration. It currently:

- reports `real_trading_enabled=false`
- reports `broker_adapter_ready=false`
- rejects every real order request with `SafetyViolation`
- forbids falling back from a rejected real order to simulated execution

Promotion to future real trading is a separate gate. It requires documented broker authorization, account setup, futures-company margin/fee metadata, risk limits, callback reconciliation, manual approval, emergency halt, and a reviewed fail-closed CTP gateway.
