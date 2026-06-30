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
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from shared.data.reader import DEFAULT_SHARED_SIGNALS_DB, SharedSignalsReader
except Exception:  # pragma: no cover - benchmark falls back to direct sqlite reads
    DEFAULT_SHARED_SIGNALS_DB = Path("/opt/investment/MarketGraphRuntime/read_model/marketdata.sqlite")
    SharedSignalsReader = None  # type: ignore[assignment]

REVIEW_DIR = Path(__file__).resolve().parent
BENCHMARK_STORE = REVIEW_DIR / "data" / "benchmark_history.json"
LAST_PERIOD_STORE = REVIEW_DIR / "data" / "last_period_return.json"

CSI300_SYMBOLS = ("000300.SH", "399300.SZ", "000300", "399300")
CHINEXT_SYMBOLS = ("399006.SZ", "399006")
BENCHMARK_LOOKBACK_DAYS = 40


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


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_trade_date(value: str) -> date:
    raw = str(value).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()


def _date_key(value: str | date) -> str:
    return value.strftime("%Y%m%d") if isinstance(value, date) else _to_trade_date(value).strftime("%Y%m%d")


def _shared_signals_db_path() -> Path:
    env_value = os.environ.get("SHARED_SIGNALS_DB")
    return Path(env_value).expanduser() if env_value else Path(DEFAULT_SHARED_SIGNALS_DB)


def _sqlite_uri(path: Path) -> str:
    return "file:" + str(path) + "?mode=ro"


def _calc_return(previous_close: float | None, current_close: float | None) -> float:
    if previous_close in (None, 0) or current_close is None:
        return 0.0
    return (float(current_close) - float(previous_close)) / float(previous_close)


def _history_buy_hold_return(date_key: str) -> float:
    history = _read_json(BENCHMARK_STORE)
    if not history:
        return 0.0
    if isinstance(history.get(date_key), dict):
        return float(history[date_key].get("buy_hold_return") or 0.0)
    records = history.get("records")
    if isinstance(records, list):
        for record in records:
            if str(record.get("date")) == date_key:
                return float(record.get("buy_hold_return") or 0.0)
    if str(history.get("date")) == date_key:
        return float(history.get("buy_hold_return") or 0.0)
    return 0.0


def _rows_from_reader(symbol: str, target_date: str) -> list[dict[str, Any]]:
    if SharedSignalsReader is None:
        return []
    start = (_to_trade_date(target_date) - timedelta(days=BENCHMARK_LOOKBACK_DAYS)).strftime("%Y%m%d")
    reader = SharedSignalsReader(_shared_signals_db_path())
    try:
        return reader.get_bars_daily("Ashare", symbol, start=start, end=target_date)
    finally:
        reader.close()


def _rows_from_sqlite(symbol: str, target_date: str) -> list[dict[str, Any]]:
    db_path = _shared_signals_db_path()
    if not db_path.exists():
        return []
    conn = sqlite3.connect(_sqlite_uri(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT trade_date, close FROM market_bars_daily "
            "WHERE LOWER(market)=LOWER(?) AND symbol=? "
            "AND REPLACE(REPLACE(trade_date, '-', ''), '/', '')<=? "
            "AND close IS NOT NULL "
            "ORDER BY REPLACE(REPLACE(trade_date, '-', ''), '/', '') ASC",
            ("Ashare", symbol, target_date),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _select_latest_two(rows: list[dict[str, Any]], target_date: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        trade_date = _date_key(str(row.get("trade_date") or ""))
        close_value = _safe_float(row.get("close"))
        if not trade_date or close_value is None or trade_date > target_date:
            continue
        deduped[trade_date] = {"trade_date": trade_date, "close": close_value}
    ordered = sorted(deduped.values(), key=lambda item: str(item["trade_date"]))
    if not ordered:
        return None, None
    current = ordered[-1]
    previous = ordered[-2] if len(ordered) >= 2 else None
    return current, previous


def _read_index_return(target_date: str, symbols: tuple[str, ...], label: str) -> tuple[float, str]:
    for symbol in symbols:
        try:
            current, previous = _select_latest_two(_rows_from_reader(symbol, target_date), target_date)
        except Exception:
            current, previous = None, None
        if current is not None:
            return _calc_return(_safe_float(previous.get("close")) if previous else None, _safe_float(current.get("close"))), f"sharedsignals_reader:{label}:{symbol}:{current['trade_date']}"
    for symbol in symbols:
        try:
            current, previous = _select_latest_two(_rows_from_sqlite(symbol, target_date), target_date)
        except Exception:
            current, previous = None, None
        if current is not None:
            return _calc_return(_safe_float(previous.get("close")) if previous else None, _safe_float(current.get("close"))), f"sharedsignals_sqlite:{label}:{symbol}:{current['trade_date']}"
    return 0.0, f"sharedsignals_unavailable:{label}"


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
        - 优先通过 SharedSignalsReader 读取 SharedSignals 日线, 若 reader 不可用则
          直接只读打开 marketdata.sqlite 计算最近两个交易日收益.
        - last_period_return 从本地 store 读取, 由本模块在每次复盘后写入.
    """
    date_key = _date_key(date)
    last = _read_json(LAST_PERIOD_STORE)
    csi300_return, csi300_source = _read_index_return(date_key, CSI300_SYMBOLS, "csi300")
    chinext_return, chinext_source = _read_index_return(date_key, CHINEXT_SYMBOLS, "chinext")
    return {
        "date": date_key,
        "csi300_return": float(csi300_return),
        "chinext_return": float(chinext_return),
        "buy_hold_return": _history_buy_hold_return(date_key),
        "last_period_return": float(last.get("return", 0.0)),
        "last_period_kind": last.get("kind", "unknown"),
        "source": f"{csi300_source}|{chinext_source}",
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
