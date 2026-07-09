#!/usr/bin/env python3
"""Read-only forward validation labels for A-share server-local simulated fills."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from shared.data.reader import TradingagentDataReader
from shared.execution import local_sim_ledger
from shared.review.sample_quality import classify_trade_sample

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "shared" / "review" / "ashare" / "forward_validation_latest.json"
DEFAULT_HISTORY = ROOT / "shared" / "review" / "ashare" / "forward_validation.jsonl"
CN_TZ = timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed else default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _parse_dt(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace(" ", "T", 1).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _compact_date(value: Any) -> str:
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw[:10] if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _trade_time(row: dict[str, Any]) -> datetime | None:
    return _parse_dt(row.get("trade_timestamp_bj") or row.get("timestamp_bj") or row.get("created_at"))


def _read_intraday(reader: Any, symbol: str, date: str) -> list[dict[str, Any]]:
    method = getattr(reader, "get_bars_intraday", None)
    if not callable(method):
        return []
    try:
        rows = method("ashare", symbol, "5m", date, date)
    except TypeError:
        rows = method(market="ashare", symbol=symbol, interval="5m", start=date, end=date)
    except Exception:
        return []
    return sorted([dict(row) for row in rows or [] if isinstance(row, dict)], key=lambda item: str(item.get("bar_time") or item.get("time") or ""))


def _read_daily(reader: Any, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    method = getattr(reader, "get_bars_daily", None)
    if not callable(method):
        return []
    try:
        rows = method("ashare", symbol, start, end)
    except TypeError:
        rows = method(market="ashare", symbol=symbol, start=start, end=end)
    except Exception:
        return []
    return sorted([dict(row) for row in rows or [] if isinstance(row, dict)], key=lambda item: str(item.get("trade_date") or item.get("date") or ""))


def _price_at_or_after(rows: list[dict[str, Any]], trade_time: datetime, minutes: int) -> tuple[float, str]:
    target = trade_time + timedelta(minutes=minutes)
    for row in rows:
        row_time = _parse_dt(row.get("bar_time") or row.get("time"))
        if row_time is None or row_time < target:
            continue
        price = _safe_float(row.get("close") or row.get("price"), 0.0)
        if price > 0:
            return price, row_time.isoformat(timespec="seconds")
    return 0.0, ""


def _pct(price: float, entry: float, side: str) -> float | None:
    if price <= 0 or entry <= 0:
        return None
    direction = -1.0 if side == "sell" else 1.0
    return round(direction * ((price / entry) - 1.0), 8)


def label_trade(trade: dict[str, Any], *, reader: Any | None = None) -> dict[str, Any]:
    active_reader = reader or TradingagentDataReader()
    symbol = str(trade.get("ts_code") or trade.get("symbol") or "").strip().upper()
    side = str(trade.get("side") or "buy").lower().strip()
    trade_date = _compact_date(trade.get("trade_date") or trade.get("created_at"))
    trade_time = _trade_time(trade)
    entry = _safe_float(trade.get("filled_price") or trade.get("price"), 0.0)
    base = {
        "trade_id": trade.get("trade_id"),
        "order_id": trade.get("order_id"),
        "symbol": symbol,
        "trade_date": trade_date,
        "entry_price": entry,
        "side": side,
        "strategy_sample_valid": bool(classify_trade_sample(trade).get("strategy_sample_valid")),
    }
    if not base["strategy_sample_valid"]:
        return {**base, "status": "skipped", "reason": "not_strategy_sample"}
    if not symbol or not trade_date or trade_time is None or entry <= 0:
        return {**base, "status": "unscored", "reason": "invalid_entry"}
    intraday = _read_intraday(active_reader, symbol, trade_date)
    labels: dict[str, Any] = {}
    for minutes, key in ((30, "m30"), (60, "m60")):
        price, at = _price_at_or_after(intraday, trade_time, minutes)
        labels[key] = {"status": "labeled" if price > 0 else "pending", "price": price or None, "at": at, "return_pct": _pct(price, entry, side)}
    start_dt = datetime.strptime(trade_date, "%Y%m%d")
    end = (start_dt + timedelta(days=7)).strftime("%Y%m%d")
    daily = _read_daily(active_reader, symbol, trade_date, end)
    same_day = next((row for row in daily if _compact_date(row.get("trade_date") or row.get("date")) == trade_date), {})
    close_price = _safe_float(same_day.get("close"), 0.0) if isinstance(same_day, dict) else 0.0
    labels["close"] = {"status": "labeled" if close_price > 0 else "pending", "price": close_price or None, "return_pct": _pct(close_price, entry, side)}
    next_day = next((row for row in daily if _compact_date(row.get("trade_date") or row.get("date")) > trade_date), {})
    if isinstance(next_day, dict) and next_day:
        labels["next_day"] = {
            "status": "labeled",
            "trade_date": _compact_date(next_day.get("trade_date") or next_day.get("date")),
            "open_return_pct": _pct(_safe_float(next_day.get("open"), 0.0), entry, side),
            "high_return_pct": _pct(_safe_float(next_day.get("high"), 0.0), entry, side),
            "close_return_pct": _pct(_safe_float(next_day.get("close"), 0.0), entry, side),
        }
    else:
        labels["next_day"] = {"status": "pending"}
    return {**base, "status": "labeled", "labels": labels}


def build_forward_validation_report(
    *,
    date: str = "",
    reader: Any | None = None,
    local_trades_path: Path = local_sim_ledger.LOCAL_SIM_TRADES,
    output: Path | None = DEFAULT_OUTPUT,
    history: Path | None = DEFAULT_HISTORY,
) -> dict[str, Any]:
    rows = _read_jsonl(local_trades_path)
    if date:
        compact = _compact_date(date)
        rows = [row for row in rows if _compact_date(row.get("trade_date") or row.get("created_at")) == compact]
    labels = [label_trade(row, reader=reader) for row in rows]
    report = {
        "market": "ashare",
        "report_type": "ashare_forward_validation",
        "date": _compact_date(date) if date else "",
        "generated_at": _now_iso(),
        "trade_count": len(rows),
        "strategy_label_count": sum(1 for row in labels if row.get("status") == "labeled"),
        "pending_count": sum(1 for row in labels if any(isinstance(item, dict) and item.get("status") == "pending" for item in (row.get("labels") or {}).values())),
        "labels": labels,
        "read_only": True,
        "real_trading_enabled": False,
    }
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if history:
        history.parent.mkdir(parents=True, exist_ok=True)
        with history.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build read-only A-share forward validation labels.")
    parser.add_argument("--date", default="")
    parser.add_argument("--local-trades-path", type=Path, default=local_sim_ledger.LOCAL_SIM_TRADES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    report = build_forward_validation_report(
        date=args.date,
        local_trades_path=args.local_trades_path,
        output=None if args.no_write else args.output,
        history=None if args.no_write else args.history,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
