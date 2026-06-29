#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Benchmark tracking for the review system.

Provides 3 of the 3 comparison axes:
  - actual vs expected goals   (handled in daily/weekly/monthly_review via goals.yaml)
  - actual vs benchmark (CSI300/Chinext/buy-hold)   <-- this module
  - actual vs last period      (this module stores last_period_return)

All values are returns (decimal, e.g. 0.012 = 1.2%).
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REVIEW_DIR = Path(__file__).resolve().parent
BENCHMARK_STORE = REVIEW_DIR / "data" / "benchmark_history.json"
LAST_PERIOD_STORE = REVIEW_DIR / "data" / "last_period_return.json"


# ---- helpers ----------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_dirs() -> None:
    BENCHMARK_STORE.parent.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    _ensure_dirs()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe_len(seq: Any) -> int:
    try:
        return len(seq)
    except TypeError:
        return 0


# ---- public API -------------------------------------------------------------

def get_benchmark(date: str) -> dict[str, Any]:
    """Return benchmark returns for a given date.

    Args:
        date: trading date, YYYYMMDD string.

    Returns:
        {
          "date": "20260629",
          "csi300_return": float,        # 沪深300 当日收益( decimal )
          "chinext_return": float,       # 创业板指
          "buy_hold_return": float,      # 买入持有( 等权持仓, 不调仓 )累计
          "last_period_return": float,   # 上一周期( 昨日/上周/上月 )组合收益
          "source": str,
          "as_of": iso8601,
        }

    Notes:
        - 实际生产中 csi300/chinext 应从 MarketGraph / Tushare 拉取; 此处先给
          结构与回退值(0.0), 由 daily_runner 在调用前注入真实行情.
        - last_period_return 从本地 store 读取, 由本模块在每次复盘后写入.
    """
    last = _read_json(LAST_PERIOD_STORE)
    return {
        "date": date,
        "csi300_return": 0.0,
        "chinext_return": 0.0,
        "buy_hold_return": 0.0,
        "last_period_return": float(last.get("return", 0.0)),
        "last_period_kind": last.get("kind", "unknown"),
        "source": "placeholder_inject_real_via_daily_runner",
        "as_of": _now_iso(),
    }


def record_last_period(return_value: float, kind: str = "daily") -> None:
    """Persist the last-period portfolio return so the next review can compare.

    Args:
        return_value: portfolio return for the just-finished period (decimal).
        kind: "daily" | "weekly" | "monthly".
    """
    _write_json(
        LAST_PERIOD_STORE,
        {
            "return": float(return_value),
            "kind": kind,
            "recorded_at": _now_iso(),
        },
    )


def compare_to_benchmark(
    portfolio_return: float,
    benchmark_return: float,
    portfolio_returns_series: list[float] | None = None,
    benchmark_returns_series: list[float] | None = None,
) -> dict[str, Any]:
    """Compare portfolio vs benchmark.

    Args:
        portfolio_return: portfolio return for the period (decimal or cumulative).
        benchmark_return: benchmark return for the same period.
        portfolio_returns_series: optional daily returns of the portfolio
            (used to compute sharpe / max_drawdown / beta).
        benchmark_returns_series: optional daily returns of the benchmark
            (must align in length with portfolio_returns_series).

    Returns:
        {
          "alpha": float,            # 超额收益 = portfolio - benchmark
          "beta": float,             # 对基准的敏感度( 需序列, 否则 None )
          "sharpe": float,           # 夏普( 需序列, 否则 None )
          "max_drawdown": float,     # 最大回撤( 需序列, 否则 None )
          "beat_benchmark": bool,    # portfolio > benchmark
          "excess_return": float,    # 同 alpha, 显式命名
        }
    """
    alpha = float(portfolio_return) - float(benchmark_return)
    beat = float(portfolio_return) > float(benchmark_return)

    beta: float | None = None
    sharpe: float | None = None
    max_dd: float | None = None

    if (
        portfolio_returns_series
        and benchmark_returns_series
        and _safe_len(portfolio_returns_series) == _safe_len(benchmark_returns_series)
        and _safe_len(portfolio_returns_series) >= 2
    ):
        beta = _beta(portfolio_returns_series, benchmark_returns_series)
        sharpe = _sharpe(portfolio_returns_series)
        max_dd = _max_drawdown(portfolio_returns_series)

    return {
        "alpha": round(alpha, 6),
        "beta": round(beta, 6) if beta is not None else None,
        "sharpe": round(sharpe, 6) if sharpe is not None else None,
        "max_drawdown": round(max_dd, 6) if max_dd is not None else None,
        "beat_benchmark": beat,
        "excess_return": round(alpha, 6),
    }


# ---- statistics -------------------------------------------------------------

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _var(xs: list[float], ddof: int = 1) -> float:
    if len(xs) <= ddof:
        return 0.0
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - ddof)


def _std(xs: list[float], ddof: int = 1) -> float:
    return math.sqrt(_var(xs, ddof))


def _beta(port: list[float], bench: list[float]) -> float:
    """CAPM beta = cov(port, bench) / var(bench)."""
    if len(port) < 2 or len(bench) < 2:
        return 0.0
    mp, mb = _mean(port), _mean(bench)
    vb = _var(bench)
    if vb == 0.0:
        return 0.0
    cov = sum((p - mp) * (b - mb) for p, b in zip(port, bench)) / (len(port) - 1)
    return cov / vb


def _sharpe(daily_returns: list[float], risk_free_daily: float = 0.0) -> float:
    """Annualised Sharpe from daily returns (assumes 252 trading days)."""
    if len(daily_returns) < 2:
        return 0.0
    ex = [r - risk_free_daily for r in daily_returns]
    sd = _std(ex)
    if sd == 0.0:
        return 0.0
    return (_mean(ex) / sd) * math.sqrt(252)


def _max_drawdown(daily_returns: list[float]) -> float:
    """Max drawdown from a daily returns series (positive number = drawdown)."""
    if not daily_returns:
        return 0.0
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in daily_returns:
        equity *= (1.0 + r)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd


# ---- self-test --------------------------------------------------------------

if __name__ == "__main__":
    # smoke: compare a winning portfolio vs flat benchmark
    port_series = [0.01, -0.005, 0.012, 0.003, -0.002, 0.008]
    bench_series = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    port_cum = 1.0
    for r in port_series:
        port_cum *= (1 + r)
    port_ret = port_cum - 1.0
    cmp = compare_to_benchmark(port_ret, 0.0, port_series, bench_series)
    print(json.dumps(cmp, ensure_ascii=False, indent=2))
    record_last_period(port_ret, "daily")
    print("benchmark get_benchmark:", get_benchmark("20260629"))
