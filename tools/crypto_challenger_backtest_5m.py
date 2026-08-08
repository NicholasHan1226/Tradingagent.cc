#!/usr/bin/env python3
"""Crypto challenger backtest on the TD 5m read model (official G5 universe).

Reads 5m OHLCV exported from the server TD crypto SQLite read model
(provider_dataset_rows, crypto.spot.binance.*.5m; open_time_ms + OHLCV columns).
Research-only, read-only inputs; no trading, no network in the backtest body.
Set DATA to your exported CSV before running.

Reference: SFI 2025 "Catching Crypto Trends" (multi-timeframe Donchian +
volatility scaling) and AQR TSMOM; baseline replicated from the released G5
frozen-momentum champion (1h regime + 15m decision, 0.1% taker + 2bps slip).
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
import pandas as pd

DATA = "/tmp/crypto_bt/data/all.csv"
MIN_REGIME_RETURN = 0.0
MIN_DECISION_RETURN = 0.001
TAKE_PROFIT_RETURN = 0.03
STOP_LOSS_RETURN = -0.02
MOMENTUM_EXIT_RETURN = -0.001
MAX_HOLD_BARS = 288
TAKER_FEE = 0.001
SLIPPAGE = 0.0002
BASE_POSITION_FRACTION = 0.10
TF_TO_5M = {"1h": 12, "4h": 48, "1d": 288}


@dataclass
class Config:
    name: str
    donchian: dict[str, int] = field(default_factory=dict)
    tsm_days: int = 0
    vol_window_bars: int = 288
    target_annual_vol: float = 0.30
    vol_scale: bool = True
    max_pos: float = 1.0
    fee_mult: float = 1.0


def load_data() -> dict[str, pd.DataFrame]:
    blocks: dict[str, list[list]] = {}
    cur = None
    for line in open(DATA):
        line = line.strip()
        if line.startswith("###"):
            cur = line.strip("#").strip()
            blocks[cur] = []
        elif cur and line and not line.startswith("done"):
            blocks[cur].append(line.split(","))
    out = {}
    for s, rows in blocks.items():
        df = pd.DataFrame(
            rows,
            columns=["open_time_ms", "open", "high", "low", "close", "volume"],
        ).astype(
            {
                "open_time_ms": np.int64,
                "open": float,
                "high": float,
                "low": float,
                "close": float,
                "volume": float,
            }
        )
        df = df.sort_values("open_time_ms").reset_index(drop=True)
        out[s.upper()] = df
    return out


def resample(df: pd.DataFrame, n_bars: int) -> pd.DataFrame:
    df = df.copy()
    df["grp"] = np.arange(len(df)) // n_bars
    return (
        df.groupby("grp")
        .agg(
            open_time_ms=("open_time_ms", "first"),
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .reset_index(drop=True)
    )


def donchian_signal(df_coarse: pd.DataFrame, lookback: int) -> np.ndarray:
    roll_max = df_coarse["close"].shift(1).rolling(lookback).max()
    return (df_coarse["close"] > roll_max).to_numpy(dtype=float)


def tsm_signal(df_coarse: pd.DataFrame, lookback: int) -> np.ndarray:
    ret = df_coarse["close"].pct_change(lookback)
    return (ret > 0).to_numpy(dtype=float)


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
        return {"error": "empty_window", "name": cfg.name}
    open_p = df["open"].to_numpy()
    close_p = df["close"].to_numpy()
    low_p = df["low"].to_numpy()
    high_p = df["high"].to_numpy()
    t = df["open_time_ms"].to_numpy()
    ret_close = np.zeros(n)
    ret_close[1:] = close_p[1:] / close_p[:-1] - 1.0
    cost_per_unit = (TAKER_FEE + SLIPPAGE) * cfg.fee_mult

    if cfg.name == "bh":
        target = np.full(n, BASE_POSITION_FRACTION)
    elif cfg.name == "g5":
        entry_ok = np.zeros(n, dtype=bool)
        for i in range(12, n):
            regime_ret = close_p[i] / open_p[i - 11] - 1.0
            decision_ret = close_p[i] / open_p[i - 2] - 1.0
            entry_ok[i] = (regime_ret >= MIN_REGIME_RETURN) and (
                decision_ret >= MIN_DECISION_RETURN
            )
        exit_ok = np.zeros(n, dtype=bool)
        for i in range(12, n):
            regime_ret = close_p[i] / open_p[i - 11] - 1.0
            decision_ret = close_p[i] / open_p[i - 2] - 1.0
            exit_ok[i] = (regime_ret < 0) and (decision_ret <= MOMENTUM_EXIT_RETURN)
        state = np.zeros(n, dtype=int)
        entry_i = -1
        for i in range(1, n):
            if state[i - 1] == 1:
                held = i - 1 - entry_i
                if exit_ok[i - 1] or held >= MAX_HOLD_BARS:
                    state[i] = 0
                else:
                    sl_price = open_p[entry_i] * (1 + STOP_LOSS_RETURN)
                    tp_price = open_p[entry_i] * (1 + TAKE_PROFIT_RETURN)
                    if low_p[i - 1] <= sl_price or high_p[i - 1] >= tp_price:
                        state[i] = 0
                    else:
                        state[i] = 1
            else:
                if entry_ok[i - 1]:
                    state[i] = 1
                    entry_i = i
                else:
                    state[i] = 0
        target = state.astype(float) * BASE_POSITION_FRACTION
    else:
        coarse = {k: resample(df, TF_TO_5M[k]) for k in cfg.donchian}
        sigs = {}
        for tf, lb in cfg.donchian.items():
            if cfg.tsm_days > 0:
                sigs[tf] = tsm_signal(coarse[tf], cfg.tsm_days)
            else:
                sigs[tf] = donchian_signal(coarse[tf], lb)
        per = {k: TF_TO_5M[k] for k in cfg.donchian}
        raw = np.zeros(n)
        for tf, s in sigs.items():
            for ci, v in enumerate(s):
                if v:
                    start = (ci + 1) * per[tf]
                    end = min(start + per[tf], n)
                    raw[start:end] = 1.0
        if cfg.vol_scale:
            ret5 = pd.Series(close_p).pct_change()
            rv = ret5.rolling(cfg.vol_window_bars).std().to_numpy()
            ann = np.sqrt(365 * 24 * 12)
            scale = np.where(
                np.isfinite(rv) & (rv > 0),
                np.minimum(cfg.target_annual_vol / (rv * ann), 3.0),
                0.0,
            )
            target = np.minimum(raw * BASE_POSITION_FRACTION * scale, cfg.max_pos)
        else:
            target = np.minimum(raw * BASE_POSITION_FRACTION, cfg.max_pos)

    pos = 0.0
    equity = 1.0
    eq_curve = np.ones(n)
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
            trades.append(
                {"ret": float(ret), "bars": i - entry_i}
            )
            entry_price = None
        pos = new_pos
        eq_curve[i] = equity
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
    gross = float(np.prod([1 + x["ret"] for x in trades]) - 1) if trades else 0.0
    return {
        "name": cfg.name,
        "n_bars": n,
        "net_return": round(equity - 1.0, 6),
        "gross_geometric": round(gross, 6),
        "fees_capital_frac": round(fees, 8),
        "sharpe_ann": round(sharpe, 3),
        "max_drawdown": round(max_dd, 6),
        "n_trades": len(trades),
        "win_rate": round(wins / len(trades), 4) if trades else 0.0,
        "avg_hold_bars": round(float(np.mean([x["bars"] for x in trades])), 1) if trades else 0.0,
    }


def make_configs(fee_mult=1.0):
    return [
        Config(name="g5", fee_mult=fee_mult),
        Config(name="bh", fee_mult=fee_mult),
        Config(name="donchian_1h", donchian={"1h": 96}, vol_scale=False, fee_mult=fee_mult),
        Config(name="donchian_4h", donchian={"4h": 20}, vol_scale=False, fee_mult=fee_mult),
        Config(name="donchian_1d", donchian={"1d": 20}, vol_scale=False, fee_mult=fee_mult),
        Config(name="multi_tf_any", donchian={"1h": 96, "4h": 20, "1d": 20}, vol_scale=False, fee_mult=fee_mult),
        Config(name="multi_tf_vol", donchian={"1h": 96, "4h": 20, "1d": 20}, vol_scale=True, fee_mult=fee_mult),
        Config(name="tsm_1d_vol", donchian={"1d": 20}, tsm_days=20, vol_scale=True, fee_mult=fee_mult),
    ]


def summarize(results, symbols, label):
    print(f"\n[{label}]")
    print(f"{'strategy':18s} {'net':>9s} {'gross':>9s} {'fees':>9s} {'trades':>7s} {'win':>6s} {'sharpe':>8s} {'dd':>8s}")
    names = [c.name for c in make_configs()]
    for name in names:
        nets = [results[s][name]["net_return"] for s in symbols]
        gross = [results[s][name]["gross_geometric"] for s in symbols]
        fees = sum(results[s][name]["fees_capital_frac"] for s in symbols)
        trades = sum(results[s][name]["n_trades"] for s in symbols)
        wins = sum(int(results[s][name]["win_rate"] * results[s][name]["n_trades"]) for s in symbols)
        sh = [results[s][name]["sharpe_ann"] for s in symbols]
        dd = [results[s][name]["max_drawdown"] for s in symbols]
        wr = wins / trades if trades else 0.0
        print(
            f"{name:18s} {sum(nets):+9.4f} {sum(gross):+9.4f} {fees:9.4f} {trades:7d} {wr:6.3f} "
            f"{float(np.mean(sh)):+8.3f} {float(np.mean(dd)):8.4f}"
        )


def main():
    data = load_data()
    official = ["BTCUSDT", "ETHUSDT"]
    full_start = int(data["BTCUSDT"]["open_time_ms"].iloc[60 * 24 * 12])
    full_end = int(data["BTCUSDT"]["open_time_ms"].iloc[-1])

    # 1. Fair universe: official BTC/ETH
    results = {s: {} for s in official}
    for s in official:
        for cfg in make_configs():
            results[s][cfg.name] = run_backtest(
                data[s], cfg, start_ms=full_start, end_ms=full_end
            )
    summarize(results, official, "FULL WINDOW official universe BTC/ETH (warmup 60d -> 08-08)")

    # 2. 3-fold walk-forward on official universe
    span = full_end - full_start
    fold = span // 3
    fold_reports = {}
    for f in range(3):
        f0 = full_start + f * fold
        f1 = f0 + fold
        fr = {s: {} for s in official}
        for s in official:
            for cfg in make_configs():
                fr[s][cfg.name] = run_backtest(data[s], cfg, start_ms=f0, end_ms=f1)
        fold_reports[f + 1] = fr
        summarize(fr, official, f"FOLD {f+1}")

    # 3. Fee sensitivity on multi_tf_vol + g5 (official universe, full window)
    print("\n[FEE SENSITIVITY] official universe")
    for mult, label in [(0.0, "zero fee"), (0.5, "half fee"), (1.0, "current"), (2.0, "double fee")]:
        rr = {s: {} for s in official}
        for s in official:
            for cfg in make_configs(fee_mult=mult):
                rr[s][cfg.name] = run_backtest(data[s], cfg, start_ms=full_start, end_ms=full_end)
        g5 = sum(rr[s]["g5"]["net_return"] for s in official)
        mvol = sum(rr[s]["multi_tf_vol"]["net_return"] for s in official)
        tsm = sum(rr[s]["tsm_1d_vol"]["net_return"] for s in official)
        print(f"  {label:14s} g5={g5:+.4f}  multi_tf_vol={mvol:+.4f}  tsm_1d_vol={tsm:+.4f}")

    # 4. Validation window vs MODEL_EXPERIMENTS (08-01 -> 08-08, official)
    aug1 = pd.Timestamp("2026-08-01", tz="UTC").value // 10**6
    aug8 = pd.Timestamp("2026-08-08 13:50", tz="UTC").value // 10**6
    vr = {s: {} for s in official}
    for s in official:
        for cfg in make_configs():
            vr[s][cfg.name] = run_backtest(data[s], cfg, start_ms=aug1, end_ms=aug8)
    summarize(vr, official, "G5 VALIDATION WINDOW 08-01 -> 08-08 (cf MODEL_EXPERIMENTS)")

    with open("/tmp/crypto_bt/results_v3.json", "w") as f:
        json.dump(
            {
                "full": results,
                "folds": fold_reports,
                "validation": vr,
                "configs": [c.name for c in make_configs()],
            },
            f,
            indent=1,
        )


if __name__ == "__main__":
    main()
