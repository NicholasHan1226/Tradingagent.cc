# CNFutures

国内期货模块当前只做全自动模拟盘。它用于验证策略风格、风控参数、保证金占用、手续费估算和复盘闭环, 为未来受控实盘接口做准备。

## Data Flow

```
SharedSignals(Tushare fut_basic/fut_daily)
  -> market_assets / market_bars_daily, market="Futures"
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
python -m CNFutures.run_simulation --json
```

Cron wrapper:

```bash
shared/wrappers/job_cn_futures_sim.sh
```

The wrapper uses the existing Tradings env loader and writes normal cron logs. It does not write to SharedSignals, MarketGraph, CTP, or any broker account.

## Review

Simulation records are append-only:

- Signal state: `signals/filled/SIM-CNF-*.json`
- Review evidence: `shared/review/data/cn_futures_sim_reviews.jsonl`

Promotion to future real trading is a separate gate. It requires documented broker authorization, account setup, risk limits, callback reconciliation, manual approval, and a fail-closed CTP gateway.
