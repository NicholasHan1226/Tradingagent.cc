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

## Review

Simulation records are append-only:

- Signal state: `signals/filled/SIM-CNF-*.json`
- Review evidence: `shared/review/data/cn_futures_sim_reviews.jsonl`

The review payload includes `score_summary` by style:

- `trade_count` / `filled_count`
- `fee`
- `margin_required`
- `notional`
- `realized_pnl` and `win_rate` only when realized PnL samples are available
- `max_drawdown` only when a realized PnL curve exists
- `status`, where small or open-only samples are marked `sample_insufficient`

These scores rank simulated styles for further research. They do not grant live trading permission and do not automatically promote a style into real trading.

## Real Trading Reserve

`CNFutures/live_gateway.py` is a fail-closed placeholder for future CTP / futures-company integration. It currently:

- reports `real_trading_enabled=false`
- reports `broker_adapter_ready=false`
- rejects every real order request with `SafetyViolation`
- forbids falling back from a rejected real order to simulated execution

Promotion to future real trading is a separate gate. It requires documented broker authorization, account setup, futures-company margin/fee metadata, risk limits, callback reconciliation, manual approval, emergency halt, and a reviewed fail-closed CTP gateway.
