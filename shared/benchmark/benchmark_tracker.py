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
  after fetching benchmark prices through the SharedSignals API/read model.
  The caller passes the closing values; this module stores and computes returns.

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
    "date",
    "csi300_close",
    "chinext_close",
    "buyhold_value",
    "csi300_return",
    "chinext_return",
    "buyhold_return",
    "source",
    "updated_at",
]

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


def _sort_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return records sorted by trading date."""
    return sorted(records, key=lambda r: r.get("date", ""))


def _get_latest() -> dict[str, Any] | None:
    """Get the most recent benchmark record."""
    records = _sort_records(_read_all())
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


def _to_float(value: Any) -> float | None:
    """Convert CSV values to float, preserving blanks as None."""
    if value in (None, ""):
        return None
    return float(value)


def _calc_return(old: float, new: float) -> float:
    """Calculate return: (new - old) / old. Returns 0.0 if old is 0."""
    if old == 0:
        return 0.0
    return round((new - old) / old, 6)


def _recalculate_returns(records: list[dict[str, Any]], start_idx: int = 0) -> None:
    """Recalculate daily returns from start_idx through the end of the series."""
    if not records:
        return

    start_idx = max(0, start_idx)
    for idx in range(start_idx, len(records)):
        current = records[idx]
        previous = records[idx - 1] if idx > 0 else None

        current_csi300 = _to_float(current.get("csi300_close"))
        current_chinext = _to_float(current.get("chinext_close"))
        current_buyhold = _to_float(current.get("buyhold_value"))

        previous_csi300 = _to_float(previous.get("csi300_close")) if previous else None
        previous_chinext = _to_float(previous.get("chinext_close")) if previous else None
        previous_buyhold = _to_float(previous.get("buyhold_value")) if previous else None

        current["csi300_return"] = (
            _calc_return(previous_csi300, current_csi300)
            if previous_csi300 is not None and current_csi300 is not None
            else 0.0
        )
        current["chinext_return"] = (
            _calc_return(previous_chinext, current_chinext)
            if previous_chinext is not None and current_chinext is not None
            else 0.0
        )
        current["buyhold_return"] = (
            _calc_return(previous_buyhold, current_buyhold)
            if previous_buyhold is not None and current_buyhold is not None
            else 0.0
        )


def update_benchmark(
    date: str,
    csi300_close: float | None = None,
    chinext_close: float | None = None,
    buyhold_value: float | None = None,
    buyhold_holdings: list[dict[str, Any]] | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    """Update or insert benchmark data for a given date."""
    _ensure_csv()

    if csi300_close is not None:
        csi300_close = float(csi300_close)
    if chinext_close is not None:
        chinext_close = float(chinext_close)

    if buyhold_value is None and buyhold_holdings is not None:
        buyhold_value = sum(
            float(h.get("quantity", 0)) * float(h.get("price", 0))
            for h in buyhold_holdings
        )
    if buyhold_value is not None:
        buyhold_value = float(buyhold_value)

    records = _sort_records(_read_all())
    existing_idx = next((i for i, record in enumerate(records) if record["date"] == date), None)
    updated = existing_idx is not None

    if updated:
        record = dict(records[existing_idx])
    else:
        record = {header: "" for header in CSV_HEADERS}
        record["date"] = date

    if csi300_close is not None:
        record["csi300_close"] = csi300_close
    if chinext_close is not None:
        record["chinext_close"] = chinext_close
    if buyhold_value is not None:
        record["buyhold_value"] = buyhold_value
    record["source"] = source
    record["updated_at"] = datetime.now().isoformat()

    if updated:
        records[existing_idx] = record
    else:
        records.append(record)
        records = _sort_records(records)
        existing_idx = next(i for i, item in enumerate(records) if item["date"] == date)

    _recalculate_returns(records, start_idx=existing_idx or 0)

    with open(BENCHMARK_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(records)

    record = records[existing_idx or 0]
    return {
        "date": record["date"],
        "csi300_close": _to_float(record.get("csi300_close")),
        "chinext_close": _to_float(record.get("chinext_close")),
        "buyhold_value": _to_float(record.get("buyhold_value")),
        "csi300_return": float(record.get("csi300_return", 0.0) or 0.0),
        "chinext_return": float(record.get("chinext_return", 0.0) or 0.0),
        "buyhold_return": float(record.get("buyhold_return", 0.0) or 0.0),
        "updated": updated,
    }


def get_benchmark_return(
    start: str,
    end: str,
    benchmark: str = "csi300",
) -> dict[str, Any]:
    """Get benchmark return between two dates (inclusive)."""
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
    """Compare portfolio return against benchmark return for a period."""
    portfolio_return = float(portfolio_return)
    benchmark_return = float(benchmark_return)
    excess = round(portfolio_return - benchmark_return, 6)
    beat = portfolio_return > benchmark_return

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


if __name__ == "__main__":
    r1 = update_benchmark("20260601", csi300_close=3500.0, chinext_close=2100.0,
                          buyhold_value=100000.0, source="smoke_test")
    print("day1:", r1)

    r2 = update_benchmark("20260602", csi300_close=3535.0, chinext_close=2121.0,
                          buyhold_value=101000.0, source="smoke_test")
    print("day2:", r2)

    r3 = update_benchmark("20260603", csi300_close=3510.0, chinext_close=2080.0,
                          buyhold_value=99500.0, source="smoke_test")
    print("day3:", r3)

    ret = get_benchmark_return("20260601", "20260603", benchmark="csi300")
    print("\ncsi300 return:", json.dumps(ret, indent=2))

    portfolio_ret = ret["cumulative_return"] + 0.005
    cmp = compare(portfolio_ret, ret["cumulative_return"], period="cumulative")
    print("\ncomparison:", json.dumps(cmp, indent=2))
