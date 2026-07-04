#!/usr/bin/env python3
"""Read-only opening validation for CN futures 5-minute data."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_SQLITE_DB = Path("/opt/investment/MarketGraphRuntime/read_model/marketdata.sqlite")
CN_TZ = timezone(timedelta(hours=8))


def _now_cn() -> datetime:
    return datetime.now(CN_TZ)


def _parse_now(value: str | None) -> datetime:
    if not value:
        return _now_cn()
    parsed = datetime.fromisoformat(value.replace(" ", "T", 1))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _session_start(now: datetime) -> tuple[str, datetime | None]:
    current = now.time()
    if time(9, 0) <= current <= time(15, 0):
        return "day", datetime.combine(now.date(), time(9, 0), tzinfo=CN_TZ)
    if current >= time(21, 0):
        return "night", datetime.combine(now.date(), time(21, 0), tzinfo=CN_TZ)
    if current <= time(2, 30):
        return "night", datetime.combine(now.date() - timedelta(days=1), time(21, 0), tzinfo=CN_TZ)
    return "closed", None


def _query_session_bars(db_path: Path, start: datetime, now: datetime) -> dict[str, Any]:
    if not db_path.exists():
        return {"error": f"sqlite database not found: {db_path}", "symbol_count": 0, "bar_count": 0}
    start_text = start.strftime("%Y-%m-%d %H:%M:%S")
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(bar_time), MAX(bar_time)
            FROM market_bars_intraday
            WHERE market='Futures'
              AND COALESCE(interval, '') IN ('5min', '5MIN', '5')
              AND provider LIKE '%rt_fut_min%'
              AND bar_time >= ?
              AND bar_time <= ?
            """,
            (start_text, now_text),
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{exc.__class__.__name__}: {exc}", "symbol_count": 0, "bar_count": 0}
    finally:
        if conn is not None:
            conn.close()
    bar_count = int(row[0] or 0) if row else 0
    symbol_count = int(row[1] or 0) if row else 0
    return {
        "bar_count": bar_count,
        "symbol_count": symbol_count,
        "first_bar_time": row[2] if row else None,
        "latest_bar_time": row[3] if row else None,
    }


def validate_opening(
    *,
    sqlite_db: Path = DEFAULT_SQLITE_DB,
    now: datetime | None = None,
    min_symbols: int = 4,
) -> dict[str, Any]:
    current = now or _now_cn()
    if current.tzinfo is None:
        current = current.replace(tzinfo=CN_TZ)
    else:
        current = current.astimezone(CN_TZ)
    session_name, start = _session_start(current)
    result: dict[str, Any] = {
        "market": "cn_futures",
        "report_type": "opening_validation",
        "checked_at": current.isoformat(timespec="seconds"),
        "sqlite_db": str(sqlite_db),
        "session": session_name,
        "session_start": start.isoformat(timespec="seconds") if start else None,
        "min_symbols": max(1, int(min_symbols)),
        "real_trading_enabled": False,
    }
    if start is None:
        return {**result, "status": "warn", "reason": "outside_cn_futures_session"}
    bars = _query_session_bars(sqlite_db, start, current)
    result.update(bars)
    if bars.get("error"):
        result["status"] = "fail"
        result["reason"] = "opening_validation_query_failed"
    elif int(bars.get("bar_count") or 0) <= 0:
        result["status"] = "warn"
        result["reason"] = "opening_session_has_no_5min_bars"
    elif int(bars.get("symbol_count") or 0) < max(1, int(min_symbols)):
        result["status"] = "warn"
        result["reason"] = "opening_session_symbol_coverage_low"
    else:
        result["status"] = "pass"
        result["reason"] = "opening_session_5min_data_ready"
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only CN futures opening 5-minute data validation.")
    parser.add_argument("--sqlite-db", type=Path, default=DEFAULT_SQLITE_DB)
    parser.add_argument("--now", default=None)
    parser.add_argument("--min-symbols", type=int, default=4)
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = validate_opening(sqlite_db=args.sqlite_db, now=_parse_now(args.now), min_symbols=args.min_symbols)
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 2 if report.get("status") == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
