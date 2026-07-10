#!/usr/bin/env python3
"""Read-only A-share research evidence for market phases and styles."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from shared.markets.sim_capital import default_sim_capital

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REVIEW_DIR = ROOT / "shared" / "review" / "ashare"
STYLE_DIR = ROOT / "Ashare" / "styles"
STYLE_WEIGHTS_PATH = REVIEW_DIR / "style_weights.json"
SIGNALS_DIR = ROOT / "signals"
NO_TRADE_LOG = ROOT / "shared" / "logs" / "ashare_no_trade_explanations.jsonl"
REVERSE_REPO_CODE = "204001"
CN_TZ = timezone(timedelta(hours=8))
DEFAULT_STYLE_CAPITAL = default_sim_capital("ashare")


def _now_cn() -> datetime:
    return datetime.now(CN_TZ)


def _trade_date(value: str | None = None) -> str:
    if value:
        raw = value.strip().replace("-", "")
        if len(raw) >= 8 and raw[:8].isdigit():
            return raw[:8]
    return _now_cn().strftime("%Y%m%d")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _first_present(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def _row_payload(row: dict[str, Any]) -> dict[str, Any]:
    data = row.get("data")
    if isinstance(data, dict):
        merged = dict(data)
        for key in ("market", "symbol", "ts_code", "provider", "source_file"):
            if key in row and key not in merged:
                merged[key] = row[key]
        return merged
    return row


def _hhmm(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "T" in raw:
        raw = raw.split("T", 1)[1]
    if " " in raw:
        raw = raw.split(" ", 1)[1]
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 4:
        return f"{digits[:2]}:{digits[2:4]}"
    return raw[:5]


def _bar_time(bar: dict[str, Any]) -> str:
    return _hhmm(_first_present(bar, "bar_time", "trade_time", "time", "timestamp"))


def _price(row: dict[str, Any], *keys: str) -> float:
    return _safe_float(_first_present(row, *keys), 0.0)


def _normalize_yield(value: Any) -> float | None:
    parsed = _safe_float(value, 0.0)
    if parsed <= 0:
        return None
    return parsed / 100.0 if parsed > 1 else parsed


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _vwap(bars: list[dict[str, Any]]) -> float:
    amount = 0.0
    volume = 0.0
    for bar in bars:
        vol = _safe_float(_first_present(bar, "volume", "vol"), 0.0)
        close = _price(bar, "close", "price", "last_price")
        if vol > 0 and close > 0:
            amount += vol * close
            volume += vol
    return amount / volume if volume > 0 else 0.0


def _volume_ratio(window: list[dict[str, Any]], all_bars: list[dict[str, Any]]) -> float:
    window_volume = sum(_safe_float(_first_present(bar, "volume", "vol"), 0.0) for bar in window)
    prior = [bar for bar in all_bars if _bar_time(bar) < "14:40"]
    if not prior:
        return 0.0
    avg_prior = sum(_safe_float(_first_present(bar, "volume", "vol"), 0.0) for bar in prior) / max(1, len(prior))
    return window_volume / avg_prior if avg_prior > 0 else 0.0


def _next_trading_day_value(trade_date: str) -> str:
    try:
        from Ashare.t_plus_1 import next_trading_day

        return next_trading_day(trade_date).strftime("%Y%m%d")
    except Exception:
        current = datetime.strptime(trade_date, "%Y%m%d").date() + timedelta(days=1)
        while current.weekday() >= 5:
            current += timedelta(days=1)
        return current.strftime("%Y%m%d")


def _read_daily_bar(reader: Any, symbol: str, trade_date: str) -> dict[str, Any] | None:
    if reader is None:
        return None
    candidates = [symbol]
    if symbol == REVERSE_REPO_CODE:
        candidates = [f"{REVERSE_REPO_CODE}.SH", REVERSE_REPO_CODE]
    for candidate in candidates:
        for market in ("Ashare", "ashare", "cn"):
            try:
                rows = reader.get_bars_daily(market, candidate, trade_date, trade_date)
            except Exception:
                rows = []
            for row in rows or []:
                if isinstance(row, dict):
                    return _row_payload(row)
    return None


def resolve_reverse_repo_yield(reader: Any, trade_date: str) -> tuple[float, str]:
    row = _read_daily_bar(reader, REVERSE_REPO_CODE, trade_date)
    if row:
        for key in ("annualized_yield", "yield", "close", "price", "last_price"):
            yld = _normalize_yield(row.get(key))
            if yld is not None:
                return yld, f"daily_bar:{key}"
    fallback = _normalize_yield(os.environ.get("ASHARE_REVERSE_REPO_ANNUALIZED_YIELD")) or 0.018
    return fallback, "env_or_default"


def estimate_reverse_repo_accrual(
    idle_cash: float,
    *,
    annualized_yield: float | None = None,
    yield_source: str = "manual_or_env",
    days: int = 1,
) -> dict[str, Any]:
    yld = annualized_yield
    if yld is None:
        yld = _safe_float(os.environ.get("ASHARE_REVERSE_REPO_ANNUALIZED_YIELD"), 0.018)
    lots = int(max(0.0, idle_cash) // 1000)
    amount = float(lots * 1000)
    interest = round(amount * max(0.0, yld) * max(1, days) / 365.0, 4)
    return {
        "code": REVERSE_REPO_CODE,
        "action": "lend" if lots > 0 else "skip",
        "idle_cash": round(float(idle_cash), 2),
        "amount": amount,
        "lots": lots,
        "annualized_yield": round(float(yld), 6),
        "yield_source": yield_source,
        "days": max(1, days),
        "estimated_interest": interest,
        "booked_to_pnl": False,
        "evidence_only": True,
    }


def closing_momentum_evidence(
    bars_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    trade_date: str | None = None,
    reader: Any = None,
    top_n: int = 20,
) -> dict[str, Any]:
    label_date = _next_trading_day_value(trade_date) if trade_date else None
    candidates: list[dict[str, Any]] = []
    symbols_with_bars = 0
    for symbol, bars in sorted(bars_by_symbol.items()):
        clean = [bar for bar in bars if isinstance(bar, dict)]
        if not clean:
            continue
        symbols_with_bars += 1
        window = [bar for bar in clean if "14:40" <= _bar_time(bar) <= "14:56"]
        if len(window) < 2:
            continue
        first_price = _price(window[0], "open", "close", "price")
        last_price = _price(window[-1], "close", "price", "last_price")
        high = max((_price(bar, "high", "close", "price") for bar in clean), default=0.0)
        vwap = _vwap(window)
        tail_momentum = (last_price - first_price) / first_price if first_price > 0 and last_price > 0 else 0.0
        vwap_deviation = (last_price - vwap) / vwap if vwap > 0 and last_price > 0 else 0.0
        close_to_high = last_price / high if high > 0 and last_price > 0 else 0.0
        volume_ratio = _volume_ratio(window, clean)
        if tail_momentum >= 0.003 and vwap_deviation >= 0.0 and close_to_high >= 0.985:
            candidates.append(
                {
                    "symbol": symbol,
                    "tail_momentum": round(tail_momentum, 6),
                    "vwap_deviation": round(vwap_deviation, 6),
                    "close_to_high": round(close_to_high, 6),
                    "volume_ratio": round(volume_ratio, 4),
                    "last_price": round(last_price, 4),
                    "bar_count": len(window),
                    "next_trading_day": label_date,
                    "next_day_open_return": None,
                    "next_day_high_return": None,
                    "next_day_close_return": None,
                    "label_state": "pending_next_day_bar" if label_date else "no_trade_date",
                }
            )
    candidates.sort(key=lambda item: (item["tail_momentum"], item["volume_ratio"], item["close_to_high"]), reverse=True)
    if label_date and reader is not None:
        for candidate in candidates:
            bar = _read_daily_bar(reader, str(candidate["symbol"]), label_date)
            last_price = _safe_float(candidate.get("last_price"), 0.0)
            if not bar or last_price <= 0:
                continue
            open_price = _price(bar, "open", "open_price")
            high_price = _price(bar, "high", "high_price")
            close_price = _price(bar, "close", "price", "last_price")
            if open_price > 0:
                candidate["next_day_open_return"] = round((open_price - last_price) / last_price, 6)
            if high_price > 0:
                candidate["next_day_high_return"] = round((high_price - last_price) / last_price, 6)
            if close_price > 0:
                candidate["next_day_close_return"] = round((close_price - last_price) / last_price, 6)
            if any(candidate.get(key) is not None for key in ("next_day_open_return", "next_day_high_return", "next_day_close_return")):
                candidate["label_state"] = "labeled"
    state = "ready" if candidates else ("no_candidates" if symbols_with_bars else "no_5min_data")
    return {
        "state": state,
        "symbols_checked": len(bars_by_symbol),
        "symbols_with_bars": symbols_with_bars,
        "candidate_count": len(candidates),
        "candidates": candidates[: max(1, top_n)],
        "research_only": True,
    }


def opening_auction_evidence(
    bars_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    current_time: str = "09:20",
    top_n: int = 20,
) -> dict[str, Any]:
    phase = "cancel_allowed" if "09:15" <= current_time < "09:20" else "no_cancel" if "09:20" <= current_time <= "09:25" else "outside"
    anomalies: list[dict[str, Any]] = []
    symbols_with_bars = 0
    proxy_symbols_with_bars = 0
    for symbol, bars in sorted(bars_by_symbol.items()):
        clean = [bar for bar in bars if isinstance(bar, dict)]
        window = [bar for bar in clean if "09:15" <= _bar_time(bar) <= "09:25"]
        data_mode = "auction"
        if not window:
            window = [bar for bar in clean if "09:30" <= _bar_time(bar) <= "09:35"]
            data_mode = "first_5m_proxy"
            if not window:
                continue
            proxy_symbols_with_bars += 1
        else:
            symbols_with_bars += 1
        open_price = _price(window[0], "open", "close", "price")
        prev_close = _price(window[0], "pre_close", "prev_close", "previous_close")
        gap_pct = (open_price - prev_close) / prev_close if prev_close > 0 and open_price > 0 else 0.0
        baseline = sum(_safe_float(_first_present(bar, "volume", "vol"), 0.0) for bar in clean) / max(1, len(clean))
        first_volume = _safe_float(_first_present(window[0], "volume", "vol"), 0.0)
        volume_ratio = first_volume / baseline if baseline > 0 else 0.0
        if abs(gap_pct) >= 0.02 or volume_ratio >= 3.0:
            anomalies.append(
                {
                    "symbol": symbol,
                    "phase": phase,
                    "data_mode": data_mode,
                    "gap_pct": round(gap_pct, 6),
                    "volume_ratio": round(volume_ratio, 4),
                    "opening_price": round(open_price, 4),
                    "previous_close": round(prev_close, 4),
                    "post_open_5m_return": None,
                    "post_open_30m_return": None,
                }
            )
    anomalies.sort(key=lambda item: (abs(item["gap_pct"]), item["volume_ratio"]), reverse=True)
    observed_symbols = symbols_with_bars + proxy_symbols_with_bars
    state = "ready" if anomalies else ("no_anomalies" if observed_symbols else "no_auction_data")
    data_mode = "auction" if symbols_with_bars else "first_5m_proxy" if proxy_symbols_with_bars else "none"
    return {
        "state": state,
        "phase": phase,
        "data_mode": data_mode,
        "symbols_checked": len(bars_by_symbol),
        "symbols_with_bars": symbols_with_bars,
        "proxy_symbols_with_bars": proxy_symbols_with_bars,
        "anomaly_count": len(anomalies),
        "anomalies": anomalies[: max(1, top_n)],
        "research_only": True,
    }


def style_evidence(
    *,
    trade_date: str,
    total_capital: float = DEFAULT_STYLE_CAPITAL,
    styles_dir: Path = STYLE_DIR,
    style_weights_path: Path = STYLE_WEIGHTS_PATH,
    signals_dir: Path = SIGNALS_DIR,
    no_trade_log: Path = NO_TRADE_LOG,
) -> dict[str, Any]:
    styles: dict[str, dict[str, Any]] = {}
    for path in sorted(styles_dir.glob("*.json")):
        payload = _read_json(path)
        name = str(payload.get("name") or path.stem)
        styles[name] = {
            "style": name,
            "configured_status": str(payload.get("status") or "active"),
            "weight": _safe_float(payload.get("weight"), 1.0),
            "position_pct": _safe_float(payload.get("position_pct"), 0.0),
            "signal_counts": {},
            "blocked_reasons": {},
            "state": "no_signals",
        }

    runtime_payload = _read_json(style_weights_path)
    runtime_styles = runtime_payload.get("styles") if isinstance(runtime_payload.get("styles"), dict) else {}
    if isinstance(runtime_styles, dict):
        for name, payload in runtime_styles.items():
            if not isinstance(payload, dict):
                continue
            style_name = str(name)
            if style_name not in styles:
                styles[style_name] = {
                    "style": style_name,
                    "configured_status": "runtime_only",
                    "weight": 0.0,
                    "position_pct": 0.0,
                    "signal_counts": {},
                    "blocked_reasons": {},
                    "state": "no_signals",
                }
            runtime_status = str(payload.get("status") or styles[style_name]["configured_status"])
            styles[style_name]["runtime_status"] = runtime_status
            styles[style_name]["configured_status"] = runtime_status
            styles[style_name]["weight"] = _safe_float(payload.get("weight"), _safe_float(styles[style_name].get("weight"), 1.0))

    for state in ("pending", "claimed", "running", "filled", "failed", "expired", "cancelled", "partial"):
        for path in (signals_dir / state).glob("*.json"):
            payload = _read_json(path)
            if str(payload.get("market") or "").lower() not in {"ashare", "a_share", "a-share", ""}:
                continue
            raw_date = str(payload.get("trade_date") or payload.get("valid_until") or payload.get("timestamp") or "")
            if trade_date and raw_date.replace("-", "")[:8] != trade_date:
                continue
            style_name = str(payload.get("style") or payload.get("strategy_name") or "unclassified")
            if style_name not in styles:
                styles[style_name] = {
                    "style": style_name,
                    "configured_status": "runtime_only",
                    "weight": 0.0,
                    "position_pct": 0.0,
                    "signal_counts": {},
                    "blocked_reasons": {},
                    "state": "no_signals",
                }
            counts = styles[style_name]["signal_counts"]
            counts[state] = int(counts.get(state, 0)) + 1

    for row in _read_jsonl(no_trade_log)[-200:]:
        raw_date = str(row.get("date") or row.get("trade_date") or row.get("generated_at") or "")
        if trade_date and raw_date.replace("-", "")[:8] != trade_date:
            continue
        explanation = row.get("no_trade_explanation") if isinstance(row.get("no_trade_explanation"), dict) else row
        category = str(explanation.get("category") or "unknown")
        for style_name in styles:
            blocked = styles[style_name]["blocked_reasons"]
            blocked[category] = int(blocked.get(category, 0)) + 1

    active = degraded = paused = 0
    for item in styles.values():
        counts = item["signal_counts"]
        configured_status = item["configured_status"]
        if configured_status in {"paused", "deprecated"}:
            item["state"] = configured_status
            paused += 1
        elif int(counts.get("filled", 0)) > 0 or int(counts.get("partial", 0)) > 0:
            item["state"] = "active_sample"
            active += 1
        elif counts:
            item["state"] = "signal_no_fill"
            degraded += 1
        elif item["blocked_reasons"]:
            item["state"] = "blocked_or_filtered"
            degraded += 1
        else:
            item["state"] = "waiting_for_sample"
    budgetable = [
        item
        for item in styles.values()
        if item["configured_status"] not in {"paused", "deprecated"} and _safe_float(item.get("weight"), 0.0) > 0
    ]
    total_weight = sum(_safe_float(item.get("weight"), 0.0) for item in budgetable)
    budget_by_style: dict[str, float] = {}
    if total_weight > 0:
        running_budget = 0.0
        for index, item in enumerate(sorted(budgetable, key=lambda row: str(row["style"]))):
            style_name = str(item["style"])
            if index == len(budgetable) - 1:
                virtual_budget = round(max(0.0, total_capital) - running_budget, 2)
            else:
                virtual_budget = round(max(0.0, total_capital) * _safe_float(item.get("weight"), 0.0) / total_weight, 2)
                running_budget += virtual_budget
            budget_by_style[style_name] = virtual_budget
    allocated_capital = 0.0
    for item in styles.values():
        weight = _safe_float(item.get("weight"), 0.0)
        virtual_budget = budget_by_style.get(str(item["style"]), 0.0)
        budget_weight = virtual_budget / max(0.0, total_capital) if total_capital > 0 else 0.0
        position_pct = _safe_float(item.get("position_pct"), 0.0)
        item["budget_weight"] = round(budget_weight, 6)
        item["virtual_budget"] = virtual_budget
        item["max_single_position"] = round(virtual_budget * position_pct, 2) if position_pct > 0 else virtual_budget
        allocated_capital += virtual_budget
    unallocated_capital = round(max(0.0, total_capital) - allocated_capital, 2)
    if abs(unallocated_capital) < 0.005:
        unallocated_capital = 0.0
    return {
        "state": "ready",
        "trade_date": trade_date,
        "summary": {
            "styles": len(styles),
            "active_sample": active,
            "degraded": degraded,
            "paused": paused,
            "virtual_capital": round(max(0.0, total_capital), 2),
            "allocated_capital": round(allocated_capital, 2),
            "unallocated_capital": unallocated_capital,
        },
        "styles": sorted(styles.values(), key=lambda item: item["style"]),
    }


def _load_bars_from_reader(reader: Any, symbols: list[str], trade_date: str) -> dict[str, list[dict[str, Any]]]:
    bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        try:
            rows = reader.get_bars_intraday("Ashare", symbol, "5m", trade_date, trade_date)
        except Exception:
            rows = []
        bars_by_symbol[symbol] = [_row_payload(row) for row in rows if isinstance(row, dict)]
    return bars_by_symbol


def _recent_intraday_symbols(reader: Any, trade_date: str, max_symbols: int) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    get_tushare = getattr(reader, "get_tushare", None)
    if not callable(get_tushare):
        return symbols
    for api_name in ("rt_min", "stk_mins"):
        try:
            rows = get_tushare(api_name, start_date=trade_date, end_date=trade_date)
        except Exception:
            rows = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            payload = _row_payload(row)
            symbol = str(payload.get("symbol") or payload.get("ts_code") or "").strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(symbol)
            if len(symbols) >= max_symbols:
                return symbols
    return symbols


def _default_symbols(reader: Any, max_symbols: int, trade_date: str | None = None) -> list[str]:
    if trade_date:
        recent = _recent_intraday_symbols(reader, trade_date, max_symbols)
        if recent:
            return recent
    try:
        assets = reader.get_assets("Ashare")
    except Exception:
        assets = []
    symbols: list[str] = []
    for row in assets:
        symbol = str(row.get("symbol") or row.get("ts_code") or "").strip().upper()
        if symbol:
            symbols.append(symbol)
        if len(symbols) >= max_symbols:
            break
    return symbols


def build_research_evidence(
    *,
    trade_date: str | None = None,
    bars_by_symbol: dict[str, list[dict[str, Any]]] | None = None,
    idle_cash: float = 0.0,
    annualized_yield: float | None = None,
    total_capital: float = DEFAULT_STYLE_CAPITAL,
    max_symbols: int = 200,
    reader: Any = None,
) -> dict[str, Any]:
    date_value = _trade_date(trade_date)
    if bars_by_symbol is None:
        if reader is None:
            from shared.data.reader import TradingagentDataReader

            reader = TradingagentDataReader()
        symbols = _default_symbols(reader, max_symbols, date_value)
        bars_by_symbol = _load_bars_from_reader(reader, symbols, date_value)
    yield_source = "manual"
    if annualized_yield is None:
        annualized_yield, yield_source = resolve_reverse_repo_yield(reader, date_value)
    return {
        "report_type": "ashare_research_evidence",
        "market": "ashare",
        "trade_date": date_value,
        "generated_at": _now_cn().isoformat(timespec="seconds"),
        "read_only": True,
        "real_trading_enabled": False,
        "closing_momentum": closing_momentum_evidence(bars_by_symbol, trade_date=date_value, reader=reader),
        "opening_auction": opening_auction_evidence(bars_by_symbol),
        "reverse_repo": estimate_reverse_repo_accrual(idle_cash, annualized_yield=annualized_yield, yield_source=yield_source),
        "style_evidence": style_evidence(trade_date=date_value, total_capital=total_capital),
    }


def write_research_evidence(report: dict[str, Any], review_dir: Path = REVIEW_DIR) -> dict[str, Any]:
    review_dir.mkdir(parents=True, exist_ok=True)
    latest = review_dir / "research_evidence_latest.json"
    history = review_dir / "research_evidence.jsonl"
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    return {"latest": str(latest), "history": str(history)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build read-only A-share research evidence.")
    parser.add_argument("--date", default="")
    parser.add_argument("--idle-cash", type=float, default=0.0)
    parser.add_argument("--annualized-yield", type=float, default=None)
    parser.add_argument("--total-capital", type=float, default=DEFAULT_STYLE_CAPITAL)
    parser.add_argument("--max-symbols", type=int, default=200)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    report = build_research_evidence(
        trade_date=args.date or None,
        idle_cash=args.idle_cash,
        annualized_yield=args.annualized_yield,
        total_capital=max(0.0, args.total_capital),
        max_symbols=max(1, args.max_symbols),
    )
    if args.write:
        report["output"] = write_research_evidence(report)
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_research_evidence",
    "closing_momentum_evidence",
    "estimate_reverse_repo_accrual",
    "opening_auction_evidence",
    "resolve_reverse_repo_yield",
    "style_evidence",
    "write_research_evidence",
]
