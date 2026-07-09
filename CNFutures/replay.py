#!/usr/bin/env python3
"""Read-only historical 5-minute replay for CNFutures style gates."""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import MARKET
from .adapter import CNFuturesAdapter, READER_MARKET
from .contract_rules import get_contract_rule, is_executable_contract_symbol
from .margin_model import estimate_order_cost
from .signal_engine import generate_style_signal
from .sim_runner import _style_allows_symbol

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "shared" / "review" / "cn_futures" / "replay_latest.json"
DEFAULT_HISTORY = ROOT / "shared" / "review" / "cn_futures" / "replay_history.jsonl"
DEFAULT_SHAREDSIGNALS_API_URL = "http://127.0.0.1:8082"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _read_intraday_bars(reader: Any, symbol: str, date: str) -> list[dict[str, Any]]:
    method = getattr(reader, "get_bars_intraday", None)
    if not callable(method):
        return []
    try:
        rows = method(READER_MARKET, symbol, "5min", date, date)
    except TypeError:
        rows = method(market=READER_MARKET, symbol=symbol, interval="5min", start=date, end=date)
    except Exception:
        return []
    return sorted([dict(row) for row in rows or [] if isinstance(row, dict)], key=lambda row: str(row.get("bar_time") or row.get("time") or ""))


def _symbols_from_realtime_batch(reader: Any, date: str, *, max_symbols: int) -> list[str]:
    method = getattr(reader, "get_realtime_5min_batch", None)
    if not callable(method):
        return []
    try:
        rows = method(READER_MARKET, date, limit=max_symbols * 4)
    except Exception:
        return []
    symbols: list[str] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or row.get("ts_code") or "").strip().upper()
        if not symbol or symbol in seen or not is_executable_contract_symbol(symbol):
            continue
        close = row.get("close") or row.get("price")
        try:
            if float(close) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        symbols.append(symbol)
        seen.add(symbol)
        if len(symbols) >= max_symbols:
            break
    return symbols


def _symbols_from_realtime_api(date: str, *, max_symbols: int) -> list[str]:
    base_url = os.environ.get("SHAREDSIGNALS_API_URL", DEFAULT_SHAREDSIGNALS_API_URL).strip().rstrip("/")
    url = f"{base_url}/realtime_5min?{urllib.parse.urlencode({'market': READER_MARKET, 'date': date})}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return []
    rows = payload.get("data") if isinstance(payload, dict) else payload
    symbols: list[str] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or row.get("ts_code") or "").strip().upper()
        if not symbol or symbol in seen or not is_executable_contract_symbol(symbol):
            continue
        try:
            if float(row.get("close") or row.get("price") or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        symbols.append(symbol)
        seen.add(symbol)
        if len(symbols) >= max_symbols:
            break
    return symbols


def _styles(adapter: CNFuturesAdapter, styles: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    if styles is not None:
        return [dict(style) for style in styles if isinstance(style, dict)]
    config = adapter.get_strategy_config()
    raw = config.get("styles") or config.get("strategies") if isinstance(config, dict) else []
    if isinstance(raw, dict):
        return [dict(value) for value in raw.values() if isinstance(value, dict)]
    return [dict(style) for style in raw or [] if isinstance(style, dict)]


def _bar_time(value: Any) -> str:
    return str(value or "").strip()


def _is_lunch_boundary_bar(value: Any) -> bool:
    raw = _bar_time(value)
    return raw.endswith(" 11:30:00") or raw.endswith("T11:30:00") or raw.endswith(" 11:30") or raw.endswith("T11:30")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _execution_annotation(*, symbol: str, style: dict[str, Any], action: str, price: Any, bar_time: Any) -> dict[str, Any]:
    if action not in {"buy", "sell"}:
        return {"execution_eligible": False, "execution_reason": "not_actionable"}
    if not _style_allows_symbol(style, symbol):
        return {"execution_eligible": False, "execution_reason": "product_not_allowed"}
    if _is_lunch_boundary_bar(bar_time):
        return {"execution_eligible": False, "execution_reason": "session_boundary_not_executable"}
    parsed_price = _safe_float(price, 0.0)
    if parsed_price <= 0:
        return {"execution_eligible": False, "execution_reason": "invalid_price"}
    try:
        get_contract_rule(symbol)
        cost = estimate_order_cost(symbol=symbol, side=action, quantity=1, price=parsed_price)
    except Exception as exc:  # noqa: BLE001
        return {"execution_eligible": False, "execution_reason": f"contract_rule_unavailable:{exc.__class__.__name__}"}
    capital = _safe_float(style.get("capital"), 200_000.0)
    margin_cap = capital * min(max(_safe_float(style.get("max_margin_usage"), 0.20), 0.01), 0.80)
    eligible = cost.margin_required <= margin_cap
    return {
        "execution_eligible": bool(eligible),
        "execution_reason": "execution_eligible" if eligible else "margin_cap_exceeded",
        "projected_margin_required": round(cost.margin_required, 6),
        "margin_cap": round(margin_cap, 6),
    }


def build_replay_report(
    *,
    date: str,
    reader: Any | None = None,
    symbols: list[str] | None = None,
    styles: list[dict[str, Any]] | None = None,
    min_bars: int = 6,
    max_symbols: int = 20,
    output: Path | None = DEFAULT_OUTPUT,
    history: Path | None = DEFAULT_HISTORY,
) -> dict[str, Any]:
    os.environ.setdefault("SHAREDSIGNALS_API_URL", DEFAULT_SHAREDSIGNALS_API_URL)
    adapter = CNFuturesAdapter(reader=reader) if reader is not None else CNFuturesAdapter()
    active_reader = reader or adapter.reader
    if symbols is not None:
        selected_symbols = list(symbols)
    else:
        get_intraday_universe = getattr(adapter, "get_intraday_universe", None)
        selected_symbols = list(get_intraday_universe(date) if callable(get_intraday_universe) else adapter.get_universe(date))
        if not selected_symbols:
            selected_symbols = _symbols_from_realtime_batch(active_reader, date, max_symbols=max_symbols)
        if not selected_symbols:
            selected_symbols = _symbols_from_realtime_api(date, max_symbols=max_symbols)
    selected_symbols = selected_symbols[: max(1, max_symbols)]
    selected_styles = _styles(adapter, styles)
    style_summary: dict[str, dict[str, Any]] = {}
    examples: list[dict[str, Any]] = []
    total_windows = 0
    for style in selected_styles:
        style_name = str(style.get("name") or style.get("style_name") or "unknown")
        action_counts: Counter[str] = Counter()
        reason_counts: Counter[str] = Counter()
        non_executable_counts: Counter[str] = Counter()
        symbol_counts: Counter[str] = Counter()
        for symbol in selected_symbols:
            if not _style_allows_symbol(style, symbol):
                non_executable_counts["product_not_allowed"] += 1
                continue
            bars = _read_intraday_bars(active_reader, symbol, date)
            if len(bars) < min_bars:
                reason_counts["insufficient_bars"] += 1
                continue
            for end in range(max(2, min_bars), len(bars) + 1):
                signal = generate_style_signal(symbol, bars[:end], style)
                action = str(signal.get("action") or signal.get("side") or "hold").lower().strip() or "hold"
                reason = str(signal.get("reason") or "unknown")
                action_counts[action] += 1
                reason_counts[reason] += 1
                symbol_counts[symbol] += 1
                total_windows += 1
                if action in {"buy", "sell"} and len(examples) < 20:
                    bar_time = bars[end - 1].get("bar_time") or bars[end - 1].get("time")
                    annotation = _execution_annotation(symbol=symbol, style=style, action=action, price=signal.get("price"), bar_time=bar_time)
                    if not annotation.get("execution_eligible"):
                        non_executable_counts[str(annotation.get("execution_reason") or "not_executable")] += 1
                    examples.append(
                        {
                            "style": style_name,
                            "symbol": symbol,
                            "action": action,
                            "reason": reason,
                            "bar_time": bar_time,
                            "price": signal.get("price"),
                            "confidence": signal.get("confidence"),
                            **annotation,
                        }
                    )
        style_summary[style_name] = {
            "action_counts": dict(action_counts),
            "top_reasons": dict(reason_counts.most_common(10)),
            "non_executable_reasons": dict(non_executable_counts.most_common(10)),
            "symbols_seen": len(symbol_counts),
            "window_count": sum(action_counts.values()),
        }
    report = {
        "market": MARKET,
        "report_type": "cn_futures_5min_replay",
        "date": date,
        "generated_at": _now_iso(),
        "read_only": True,
        "real_trading_enabled": False,
        "symbol_count": len(selected_symbols),
        "style_count": len(selected_styles),
        "window_count": total_windows,
        "style_summary": style_summary,
        "actionable_examples": examples,
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
    parser = argparse.ArgumentParser(description="Run read-only CNFutures 5-minute replay.")
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--symbol", action="append", dest="symbols")
    parser.add_argument("--min-bars", type=int, default=6)
    parser.add_argument("--max-symbols", type=int, default=20)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    report = build_replay_report(
        date=str(args.date),
        symbols=args.symbols,
        min_bars=max(2, _safe_int(args.min_bars, 6)),
        max_symbols=max(1, _safe_int(args.max_symbols, 20)),
        output=None if args.no_write else args.output,
        history=None if args.no_write else args.history,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
