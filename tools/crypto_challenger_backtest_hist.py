#!/usr/bin/env python3
"""Crypto challenger long-history validation on Binance daily/4h OHLCV.

Extends the 5m-window results with 2024+ daily and 2025+ 4h data fetched from
the public Binance klines endpoint (one-time research export). Same cost
assumptions (0.1% taker + 2bps slip, 10% base position fraction).
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
import pandas as pd

TAKER_FEE = 0.001
SLIPPAGE = 0.0002
BASE_POSITION_FRACTION = 0.10


@dataclass
class Config:
    name: str
    mode: str = "tsm"          # tsm | donchian
    lookback: int = 20          # bars
    vol_window_bars: int = 20   # ex-ante vol window
    target_annual_vol: float = 0.30
    vol_scale: bool = True
    max_pos: float = 1.0
    fee_mult: float = 1.0
    bars_per_year: float = 365.0


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df.sort_values("open_time_ms").reset_index(drop=True)
    return df


def run_backtest(
    df: pd.DataFrame,
    cfg: Config,
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> dict:
    if start_ms is not None:
        df = df[df["open_time_ms"] >= start_ms].reset_index(drop=True)
    if end_ms is not None:
        df = df[df["open_time_ms"] <= end_ms].reset_index(drop=True)
    n = len(df)
    if n == 0:
        return {"name": cfg.name, "error": "empty_window"}
    open_p = df["open"].to_numpy()
    close_p = df["close"].to_numpy()
    t = df["open_time_ms"].to_numpy()
    ret_close = np.zeros(n)
    ret_close[1:] = close_p[1:] / close_p[:-1] - 1.0
    cost_per_unit = (TAKER_FEE + SLIPPAGE) * cfg.fee_mult

    # PIT signal on closed bar i-1 -> position for bar i
    target = np.zeros(n)
    if cfg.mode == "tsm":
        rets = pd.Series(close_p).pct_change(cfg.lookback).to_numpy()
        sig = np.zeros(n, dtype=float)
        sig[cfg.lookback:] = (rets[cfg.lookback:] > 0).astype(float)
    else:  # donchian breakout
        roll_max = pd.Series(close_p).shift(1).rolling(cfg.lookback).max()
        sig = np.zeros(n, dtype=float)
        sig[cfg.lookback:] = (pd.Series(close_p) > roll_max).astype(float).to_numpy()[cfg.lookback:]
    if cfg.vol_scale:
        rv = pd.Series(close_p).pct_change().rolling(cfg.vol_window_bars).std().to_numpy()
        ann = np.sqrt(cfg.bars_per_year)
        scale = np.where(
            np.isfinite(rv) & (rv > 0),
            np.minimum(cfg.target_annual_vol / (rv * ann), 3.0),
            0.0,
        )
        target[1:] = sig[:-1] * BASE_POSITION_FRACTION * scale[:-1]
    else:
        target[1:] = sig[:-1] * BASE_POSITION_FRACTION
    target = np.minimum(target, cfg.max_pos)

    pos = 0.0
    equity = 1.0
    eq_curve = np.ones(n)
    pos_series = np.zeros(n)
    fees = 0.0
    trades = []
    entry_price = None
    entry_i = -1
    for i in range(1, n):
        if pos > 0:
            equity *= 1.0 + pos * ret_close[i]
        new_pos = target[i]
        delta = abs(new_pos - pos)
        if delta > 0:
            fee = delta * cost_per_unit
            fees += fee
            equity *= 1.0 - fee
        if new_pos > 0 and pos == 0:
            entry_price = open_p[i]
            entry_i = i
        elif new_pos == 0 and pos > 0:
            ret = open_p[i] / entry_price - 1.0
            trades.append({"ret": float(ret), "bars": i - entry_i})
            entry_price = None
        pos = new_pos
        eq_curve[i] = equity
        pos_series[i] = pos
    if pos > 0 and entry_price is not None:
        ret = close_p[-1] / entry_price - 1.0
        trades.append({"ret": float(ret), "bars": n - entry_i, "open": True})
        equity *= 1.0 + pos * ret_close[-1]

    eq = eq_curve
    peak = np.maximum.accumulate(eq)
    max_dd = float(np.max((peak - eq) / peak))
    eq_s = pd.Series(eq, index=pd.to_datetime(t, unit="ms", utc=True))
    daily = eq_s.resample("1D").last().dropna()
    daily_rets = daily.pct_change().dropna()
    sharpe = (
        float(daily_rets.mean() / daily_rets.std() * np.sqrt(365))
        if len(daily_rets) >= 2 and daily_rets.std() > 0
        else 0.0
    )
    wins = sum(1 for x in trades if x["ret"] > 0)
    gross_hold = 1.0
    for i in range(1, n):
        if pos_series[i] > 0:
            gross_hold *= 1.0 + pos_series[i] * ret_close[i]
    gross = gross_hold - 1.0
    return {
        "name": cfg.name,
        "n_bars": n,
        "net_return": round(equity - 1.0, 6),
        "gross_hold": round(gross, 6),
        "fees_capital_frac": round(fees, 8),
        "sharpe_ann": round(sharpe, 3),
        "max_drawdown": round(max_dd, 6),
        "n_trades": len(trades),
        "win_rate": round(wins / len(trades), 4) if trades else 0.0,
        "avg_hold_bars": round(float(np.mean([x["bars"] for x in trades])), 1) if trades else 0.0,
    }


def summarize(results, symbols, label, names):
    print(f"\n[{label}]")
    print(f"{'strategy':22s} {'net':>9s} {'gross':>9s} {'fees':>9s} {'trades':>6s} {'win':>6s} {'sharpe':>8s} {'dd':>8s}")
    for name in names:
        nets = [results[s][name]["net_return"] for s in symbols]
        gross = [results[s][name]["gross_hold"] for s in symbols]
        fees = sum(results[s][name]["fees_capital_frac"] for s in symbols)
        trades = sum(results[s][name]["n_trades"] for s in symbols)
        wins = sum(int(results[s][name]["win_rate"] * results[s][name]["n_trades"]) for s in symbols)
        sh = [results[s][name]["sharpe_ann"] for s in symbols]
        dd = [results[s][name]["max_drawdown"] for s in symbols]
        wr = wins / trades if trades else 0.0
        print(
            f"{name:22s} {sum(nets):+9.4f} {sum(gross):+9.4f} {fees:9.4f} {trades:6d} {wr:6.3f} "
            f"{float(np.mean(sh)):+8.3f} {float(np.mean(dd)):8.4f}"
        )


def main():
    symbols = ["BTCUSDT", "ETHUSDT"]
    # --- daily data 2024-01-01 -> now ---
    daily_cfgs = [
        Config(name="tsm_20d", mode="tsm", lookback=20, vol_scale=False),
        Config(name="tsm_20d_vol", mode="tsm", lookback=20, vol_scale=True),
        Config(name="tsm_90d_vol", mode="tsm", lookback=90, vol_scale=True),
        Config(name="donchian_20d", mode="donchian", lookback=20, vol_scale=False),
        Config(name="donchian_55d", mode="donchian", lookback=55, vol_scale=False),
        Config(name="donchian_20d_vol", mode="donchian", lookback=20, vol_scale=True),
        Config(name="donchian_55d_vol", mode="donchian", lookback=55, vol_scale=True),
    ]
    daily = {s: load_csv(f"/tmp/crypto_bt/data/{s.lower()}_1d.csv") for s in symbols}
    start = pd.Timestamp("2024-06-01", tz="UTC").value // 10**6  # 5mo warmup
    results_d = {s: {} for s in symbols}
    for s in symbols:
        for cfg in daily_cfgs:
            results_d[s][cfg.name] = run_backtest(daily[s], cfg, start_ms=start)
    summarize(results_d, symbols, "DAILY 2024-06-01 -> 08-08 (2.2y)", [c.name for c in daily_cfgs])

    # walk-forward on daily: 2024-06-01..2025-06-01 (train-ish) vs 2025-06-01.. (test)
    mid = pd.Timestamp("2025-06-01", tz="UTC").value // 10**6
    print("\n--- daily walk-forward: first 12mo vs second 12mo+ ---")
    for label, (ms, me) in {"first_12mo": (start, mid), "second_12mo": (mid, None)}.items():
        rr = {s: {} for s in symbols}
        for s in symbols:
            for cfg in daily_cfgs:
                rr[s][cfg.name] = run_backtest(daily[s], cfg, start_ms=ms, end_ms=me)
        summarize(rr, symbols, label, [c.name for c in daily_cfgs])

    # --- 4h data 2025-01-01 -> now ---
    h4_cfgs = [
        Config(name="tsm_4h_120", mode="tsm", lookback=120, vol_scale=False, bars_per_year=2190),
        Config(name="tsm_4h_120_vol", mode="tsm", lookback=120, vol_scale=True, bars_per_year=2190),
        Config(name="donchian_4h_20", mode="donchian", lookback=20, vol_scale=False, bars_per_year=2190),
        Config(name="donchian_4h_20_vol", mode="donchian", lookback=20, vol_scale=True, bars_per_year=2190),
        Config(name="donchian_4h_80", mode="donchian", lookback=80, vol_scale=False, bars_per_year=2190),
        Config(name="donchian_4h_80_vol", mode="donchian", lookback=80, vol_scale=True, bars_per_year=2190),
    ]
    h4 = {s: load_csv(f"/tmp/crypto_bt/data/{s.lower()}_4h.csv") for s in symbols}
    start4 = pd.Timestamp("2025-02-01", tz="UTC").value // 10**6
    results_h = {s: {} for s in symbols}
    for s in symbols:
        for cfg in h4_cfgs:
            results_h[s][cfg.name] = run_backtest(h4[s], cfg, start_ms=start4)
    summarize(results_h, symbols, "4H 2025-02-01 -> 08-08 (1.5y)", [c.name for c in h4_cfgs])

    with open("/tmp/crypto_bt/results_hist.json", "w") as f:
        json.dump({"daily": results_d, "h4": results_h}, f, indent=1)


if __name__ == "__main__":
    main()
