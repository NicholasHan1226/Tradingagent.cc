#!/usr/bin/env python3
"""Benchmark tracker: track CSI300 / ChiNext / buy-hold for performance comparison.

This is the **data layer** for benchmark tracking. It:
  1. Stores daily benchmark prices (CSI300, ChiNext, buy-hold portfolio).
  2. Computes period returns between any two dates.
  3. Compares portfolio return against benchmark return for a given period.

The **analysis layer** (alpha/beta/sharpe/max-drawdown) lives in
review/benchmark.py. This module provides the raw return data that
the review module consumes.

Data injection:
  In production, `update_benchmark(date)` is called by the daily runner
  after fetching real index data from MarketGraph/Tushare. The caller
  passes the closing values; this module stores and computes returns.

Benchmark codes:
  - CSI300:  000300.SH  (沪深300)
  - ChiNext: 399006.SZ  (创业板指)
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

LEDGER_DIR = Path(__file__).resolve().parent.parent / "logs"
BENCHMARK_CSV = LEDGER_DIR / "benchmark_daily.csv"

CSV_HEADERS = [
    "date",              # YYYYMMDD
    "csi300_close",      # CSI300 closing index value
    "chinext_close",     # ChiNext closing index value
    "buyhold_value",     # buy-and-hold portfolio value (starting at 1.0)
    "csi300_return",     # daily return (decimal)
    "chinext_return",    # daily return (decimal)
    "buyhold_return",    # daily return (decimal)
    "source",            # data source annotation
    "updated_at",        # ISO timestamp of this record
]

# Initial buy-hold portfolio value (normalized to 1.0 on first day)
BUYHOLD_INITIAL = 1.0


def _ensure_csv() -> None:
    """Create CSV with headers if it does not exist."""
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    if not BENCHMARK_CSV.exists():
        with open(BENCHMARK_CSV, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_HEADERS)
            writer.writeheader()


def _read_all() -> list[dict[str, Any]]:
    """Read all benchmark records."""
    if not BENCHMARK_CSV.exists():
        return []
    with open(BENCHMARK_CSV, "r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader)


def _get_latest() -> dict[str, Any] | None:
    """Get the most recent benchmark record."""
    records = _read_all()
    if not records:
        return None
    return records[-1]


def _get_by_date(date: str) -> dict[str, Any] | None:
    """Get benchmark record for a specific date."""
    records = _read_all()
    for r in records:
        if r["date"] == date:
            return r
    return None


def _calc_return(old: float, new: float) -> float:
    """Calculate return: (new - old) / old. Returns 0.0 if old is 0."""
    if old == 0:
        return 0.0
    return round((new - old) / old, 6)


def update_benchmark(
    date: str,
    csi300_close: float | None = None,
    chinext_close: float | None = None,
    buyhold_value: float | None = None,
    buyhold_holdings: list[dict[str, Any]] | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    """Update or insert benchmark data for a given date.

    For buy-hold: either pass buyhold_value directly (pre-calculated), or
    pass buyhold_holdings (list of {ts_code, quantity, price}) and the
    value will be computed as sum(quantity * price).

    If a record for this date already exists, it is updated (in-place by
    rewriting the CSV). Daily returns are calculated relative to the
    previous trading day's close.

    Args:
        date: trading date as YYYYMMDD string.
        csi300_close: CSI300 closing index value.
        chinext_close: ChiNext closing index value.
        buyhold_value: pre-calculated buy-hold portfolio value.
        buyhold_holdings: list of {ts_code, quantity, price} for buy-hold calc.
        source: data source annotation (e.g. "tushare", "marketgraph").

    Returns:
        dict with: date, csi300_close, chinext_close, buyhold_value,
        csi300_return, chinext_return, buyhold_return, updated.
    """
    _ensure_csv()

    if csi300_close is not None:
        csi300_close = float(csi300_close)
    if chinext_close is not None:
        chinext_close = float(chinext_close)

    # Calculate buy-hold value if holdings provided
    if buyhold_value is None and buyhold_holdings is not None:
        buyhold_value = sum(
            float(h.get("quantity", 0)) * float(h.get("price", 0))
            for h in buyhold_holdings
        )
    if buyhold_value is not None:
        buyhold_value = float(buyhold_value)

    # Get previous day's values for return calculation
    latest = _get_latest()
    prev_csi300 = float(latest["csi300_close"]) if latest and latest.get("csi300_close") else None
    prev_chinext = float(latest["chinext_close"]) if latest and latest.get("chinext_close") else None
    prev_buyhold = float(latest["buyhold_value"]) if latest and latest.get("buyhold_value") else None

    # If no previous record, initialize buy-hold to initial value
    if latest is None and buyhold_value is not None:
        # First entry: normalize buy-hold to initial value
        # (the actual value is stored, return is 0 for first day)
        pass

    # Calculate daily returns
    csi300_return = _calc_return(prev_csi300, csi300_close) if (prev_csi300 and csi300_close) else 0.0
    chinext_return = _calc_return(prev_chinext, chinext_close) if (prev_chinext and chinext_close) else 0.0

    if buyhold_value is not None:
        if prev_buyhold and prev_buyhold > 0:
            buyhold_return = _calc_return(prev_buyhold, buyhold_value)
        else:
            # First day: no return
            buyhold_return = 0.0
    else:
        buyhold_return = 0.0

    # Build the record
    record = {
        "date": date,
        "csi300_close": csi300_close if csi300_close is not None else "",
        "chinext_close": chinext_close if chinext_close is not None else "",
        "buyhold_value": buyhold_value if buyhold_value is not None else "",
        "csi300_return": csi300_return,
        "chinext_return": chinext_return,
        "buyhold_return": buyhold_return,
        "source": source,
        "updated_at": datetime.now().isoformat(),
    }

    # Check if record for this date already exists
    records = _read_all()
    existing_idx = None
    for i, r in enumerate(records):
        if r["date"] == date:
            existing_idx = i
            break

    if existing_idx is not None:
        # Update existing record
        records[existing_idx] = record
        with open(BENCHMARK_CSV, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_HEADERS)
            writer.writeheader()
            writer.writerows(records)
        updated = True
    else:
        # Append new record
        with open(BENCHMARK_CSV, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_HEADERS)
            writer.writerow(record)
        updated = False

    return {
        "date": date,
        "csi300_close": csi300_close,
        "chinext_close": chinext_close,
        "buyhold_value": buyhold_value,
        "csi300_return": csi300_return,
        "chinext_return": chinext_return,
        "buyhold_return": buyhold_return,
        "updated": updated,
    }


def get_benchmark_return(
    start: str,
    end: str,
    benchmark: str = "csi300",
) -> dict[str, Any]:
    """Get benchmark return between two dates (inclusive).

    Uses the "close" values on start and end dates to compute the
    cumulative return for the period.

    Args:
        start: start date (YYYYMMDD).
        end: end date (YYYYMMDD).
        benchmark: "csi300" | "chinext" | "buyhold".

    Returns:
        dict with: benchmark, start, end, start_value, end_value,
        cumulative_return, daily_returns (list of daily returns in range).
        Returns start_value/end_value as None if data is missing.
    """
    if benchmark not in ("csi300", "chinext", "buyhold"):
        raise ValueError(f"benchmark must be 'csi300', 'chinext', or 'buyhold', got '{benchmark}'")

    close_field = {
        "csi300": "csi300_close",
        "chinext": "chinext_close",
        "buyhold": "buyhold_value",
    }[benchmark]
    return_field = {
        "csi300": "csi300_return",
        "chinext": "chinext_return",
        "buyhold": "buyhold_return",
    }[benchmark]

    records = _read_all()
    # Filter to date range
    in_range = [r for r in records if start <= r["date"] <= end]
    in_range.sort(key=lambda r: r["date"])

    if not in_range:
        return {
            "benchmark": benchmark,
            "start": start,
            "end": end,
            "start_value": None,
            "end_value": None,
            "cumulative_return": 0.0,
            "daily_returns": [],
            "message": "No benchmark data in date range",
        }

    start_record = in_range[0]
    end_record = in_range[-1]

    start_val = float(start_record[close_field]) if start_record.get(close_field) else None
    end_val = float(end_record[close_field]) if end_record.get(close_field) else None

    if start_val and end_val and start_val > 0:
        cumulative_return = round((end_val - start_val) / start_val, 6)
    else:
        cumulative_return = 0.0

    daily_returns = [
        float(r[return_field]) for r in in_range
        if r.get(return_field) not in (None, "")
    ]

    return {
        "benchmark": benchmark,
        "start": start,
        "end": end,
        "start_value": start_val,
        "end_value": end_val,
        "cumulative_return": cumulative_return,
        "daily_returns": daily_returns,
        "trading_days": len(in_range),
    }


def compare(
    portfolio_return: float,
    benchmark_return: float,
    period: str = "daily",
) -> dict[str, Any]:
    """Compare portfolio return against benchmark return for a period.

    This is a lightweight comparison (excess return + beat flag).
    For advanced statistics (alpha/beta/sharpe/max-drawdown), use
    review/benchmark.py:compare_to_benchmark().

    Args:
        portfolio_return: portfolio return for the period (decimal, e.g. 0.012).
        benchmark_return: benchmark return for the same period (decimal).
        period: "daily" | "weekly" | "monthly" | "cumulative".

    Returns:
        dict with:
            "portfolio_return": float,
            "benchmark_return": float,
            "excess_return": float,      # portfolio - benchmark
            "beat_benchmark": bool,      # portfolio > benchmark
            "period": str,
            "annualized_excess": float,  # excess annualized (252/52/12 depending on period)
    """
    portfolio_return = float(portfolio_return)
    benchmark_return = float(benchmark_return)
    excess = round(portfolio_return - benchmark_return, 6)
    beat = portfolio_return > benchmark_return

    # Annualization factor
    ann_factor = {
        "daily": 252,
        "weekly": 52,
        "monthly": 12,
        "cumulative": 1,
    }.get(period, 1)

    if ann_factor > 1 and excess != 0:
        annualized_excess = round(((1 + excess) ** ann_factor - 1), 6)
    else:
        annualized_excess = excess

    return {
        "portfolio_return": round(portfolio_return, 6),
        "benchmark_return": round(benchmark_return, 6),
        "excess_return": excess,
        "beat_benchmark": beat,
        "period": period,
        "annualized_excess": annualized_excess,
    }


# ---- self-test --------------------------------------------------------------

if __name__ == "__main__":
    # Smoke test: update benchmarks for 3 days, then query and compare
    r1 = update_benchmark("20260601", csi300_close=3500.0, chinext_close=2100.0,
                          buyhold_value=100000.0, source="smoke_test")
    print("day1:", r1)

    r2 = update_benchmark("20260602", csi300_close=3535.0, chinext_close=2121.0,
                          buyhold_value=101000.0, source="smoke_test")
    print("day2:", r2)

    r3 = update_benchmark("20260603", csi300_close=3510.0, chinext_close=2080.0,
                          buyhold_value=99500.0, source="smoke_test")
    print("day3:", r3)

    # Get benchmark return for CSI300 over the period
    ret = get_benchmark_return("20260601", "20260603", benchmark="csi300")
    print("\ncsi300 return:", json.dumps(ret, indent=2))

    # Compare portfolio vs benchmark
    portfolio_ret = ret["cumulative_return"] + 0.005  # pretend we beat by 0.5%
    cmp = compare(portfolio_ret, ret["cumulative_return"], period="cumulative")
    print("\ncomparison:", json.dumps(cmp, indent=2))
