#!/usr/bin/env python3
"""Post-session forward labeling and win-rate calibration for CN futures sims."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import MARKET
from .adapter import CNFuturesAdapter, READER_MARKET
from .contract_rules import normalize_product
from .review import DEFAULT_REVIEW_PATH, dynamic_threshold_candidates, summarize_forward_outcomes


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIGNALS_DIR = ROOT / "signals"
DEFAULT_REVIEW_ROOT = ROOT / "shared" / "review"
DEFAULT_LABELS_PATH = DEFAULT_REVIEW_ROOT / "cn_futures" / "forward_labels.jsonl"
DEFAULT_REPORT_JSON = DEFAULT_REVIEW_ROOT / "cn_futures" / "win_rate_calibration_report.json"
DEFAULT_REPORT_MD = DEFAULT_REVIEW_ROOT / "cn_futures" / "win_rate_calibration_report.md"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_dt(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace(" ", "T", 1).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


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


def _latest_review_hold_summary(review_path: Path = DEFAULT_REVIEW_PATH) -> dict[str, Any]:
    rows = _read_jsonl(review_path)
    if not rows:
        return {}
    latest = rows[-1]
    summary = latest.get("hold_reason_summary")
    return summary if isinstance(summary, dict) else {}


def _signal_cards(signals_dir: Path, *, date: str = "", max_cards: int = 500) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for state in ("filled", "partial"):
        for path in sorted((signals_dir / state).glob("SIM-CNF-*.json")):
            card = _read_json(path)
            if not card:
                continue
            if str(card.get("market") or "").lower() != MARKET:
                continue
            if date and str(card.get("valid_until") or card.get("trade_date") or "") != date:
                continue
            card["_state"] = state
            card["_path"] = str(path)
            cards.append(card)
    return cards[-max(1, int(max_cards)):]


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


def _exit_plan(card: dict[str, Any]) -> dict[str, Any]:
    signal = card.get("signal") if isinstance(card.get("signal"), dict) else {}
    plan = signal.get("exit_plan") if isinstance(signal.get("exit_plan"), dict) else {}
    horizon = max(1, _safe_int(plan.get("prediction_horizon_bars") or signal.get("prediction_horizon_bars"), 3))
    time_stop_bars = max(1, _safe_int(plan.get("time_stop_bars"), horizon))
    return {
        "prediction_horizon_bars": horizon,
        "time_stop_bars": time_stop_bars,
        "stop_loss_pct": max(0.0, _safe_float(plan.get("stop_loss_pct"), 0.004)),
        "take_profit_pct": max(0.0, _safe_float(plan.get("take_profit_pct"), 0.006)),
    }


def _scenario_tags(card: dict[str, Any]) -> dict[str, Any]:
    signal = card.get("signal") if isinstance(card.get("signal"), dict) else {}
    tags = signal.get("scenario_tags") if isinstance(signal.get("scenario_tags"), dict) else {}
    symbol = str(card.get("symbol") or card.get("ts_code") or "")
    try:
        product = normalize_product(symbol)
    except ValueError:
        product = "unknown"
    return {
        "product": tags.get("product", product),
        "session": tags.get("session", "unknown"),
        "time_bucket": tags.get("time_bucket", "unknown"),
        "direction": tags.get("direction") or card.get("side") or card.get("direction") or "unknown",
        "volatility_bucket": tags.get("volatility_bucket", "unknown"),
        "volume_bucket": tags.get("volume_bucket", "unknown"),
        "signal_strength_bucket": tags.get("signal_strength_bucket", "unknown"),
    }


def label_signal_card(card: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any]:
    order_id = str(card.get("order_id") or "")
    symbol = str(card.get("symbol") or card.get("ts_code") or "")
    style = str(card.get("strategy_name") or card.get("style") or "")
    side = str(card.get("side") or card.get("direction") or "").lower().strip()
    direction = 1 if side == "buy" else (-1 if side == "sell" else 0)
    entry_price = _safe_float(card.get("filled_price") or card.get("price") or card.get("trigger_price"), 0.0)
    entry_time = _parse_dt(card.get("bar_time") or card.get("filled_at") or card.get("fill_time"))
    plan = _exit_plan(card)
    horizon = max(1, _safe_int(plan.get("prediction_horizon_bars"), 3))
    scenario = _scenario_tags(card)
    base = {
        "order_id": order_id,
        "style": style,
        "symbol": symbol,
        "bar_time": card.get("bar_time", ""),
        "scenario_tags": scenario,
        "exit_plan": plan,
        "capital_layer": "simulated",
        "real_trading_enabled": False,
    }
    if direction == 0 or entry_price <= 0 or entry_time is None:
        return {**base, "forward_outcome": {"status": "unscored", "reason": "invalid_entry", "prediction_horizon_bars": horizon}}
    future_rows = []
    for row in bars:
        row_time = _parse_dt(row.get("bar_time") or row.get("time"))
        if row_time is not None and row_time > entry_time:
            future_rows.append(row)
    if not future_rows:
        return {
            **base,
            "forward_outcome": {
                "status": "pending_future_bars",
                "entry_price": entry_price,
                "direction": side,
                "prediction_horizon_bars": horizon,
            },
        }
    closes = [_safe_float(row.get("close"), 0.0) for row in future_rows[:horizon]]
    closes = [value for value in closes if value > 0]
    if not closes:
        return {**base, "forward_outcome": {"status": "unscored", "reason": "missing_future_close", "prediction_horizon_bars": horizon}}
    returns = [direction * ((close / entry_price) - 1.0) for close in closes]
    horizon_return = returns[-1]
    time_stop_index = min(len(returns), max(1, _safe_int(plan.get("time_stop_bars"), horizon))) - 1
    time_stop_return = returns[time_stop_index]
    max_favorable = max(returns)
    max_adverse = min(returns)
    take_profit = _safe_float(plan.get("take_profit_pct"), 0.0)
    stop_loss = _safe_float(plan.get("stop_loss_pct"), 0.0)
    return {
        **base,
        "forward_outcome": {
            "status": "labeled",
            "entry_price": entry_price,
            "direction": side,
            "prediction_horizon_bars": horizon,
            "future_bar_count": len(future_rows[:horizon]),
            "horizon_return_pct": round(horizon_return, 8),
            "time_stop_return_pct": round(time_stop_return, 8),
            "max_favorable_excursion_pct": round(max_favorable, 8),
            "max_adverse_excursion_pct": round(max_adverse, 8),
            "direction_correct": horizon_return > 0,
            "time_stop_positive": time_stop_return > 0,
            "take_profit_hit": bool(take_profit and max_favorable >= take_profit),
            "stop_loss_hit": bool(stop_loss and abs(max_adverse) >= stop_loss),
        },
    }


def _merge_labels(path: Path, labels: list[dict[str, Any]]) -> None:
    existing = {str(row.get("order_id")): row for row in _read_jsonl(path) if row.get("order_id")}
    for row in labels:
        if row.get("order_id"):
            existing[str(row["order_id"])] = row
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in sorted(existing.values(), key=lambda item: str(item.get("order_id") or "")):
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _recommendations(summary: dict[str, Any], candidates: list[dict[str, Any]]) -> list[str]:
    recs = []
    styles = summary.get("styles") if isinstance(summary.get("styles"), dict) else {}
    total_labeled = sum(int(row.get("labeled") or 0) for row in styles.values() if isinstance(row, dict))
    if total_labeled < 20:
        recs.append("前向标签少于20笔，继续观察，不调整风格权重。")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        action = candidate.get("action")
        style = candidate.get("style_name")
        if action == "raise_threshold":
            recs.append(f"{style}: 前向胜率低于50%，只建议模拟提高阈值，不自动上线。")
        elif action == "test_lower_threshold_variant":
            recs.append(f"{style}: 胜率较高但 hold 压力大，可小幅测试更低阈值变体。")
    return recs or ["保持当前模拟参数，继续收集样本。"]


def build_calibration_report(
    *,
    date: str,
    reader: Any | None = None,
    signals_dir: Path = DEFAULT_SIGNALS_DIR,
    review_path: Path = DEFAULT_REVIEW_PATH,
    labels_path: Path = DEFAULT_LABELS_PATH,
    max_cards: int = 500,
    write_labels: bool = True,
) -> dict[str, Any]:
    adapter = CNFuturesAdapter(reader=reader) if reader is not None else CNFuturesAdapter()
    active_reader = reader or adapter.reader
    cards = _signal_cards(signals_dir, date=date, max_cards=max_cards)
    bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
    labels: list[dict[str, Any]] = []
    for card in cards:
        symbol = str(card.get("symbol") or card.get("ts_code") or "")
        if symbol not in bars_by_symbol:
            bars_by_symbol[symbol] = _read_intraday_bars(active_reader, symbol, date)
        labels.append(label_signal_card(card, bars_by_symbol.get(symbol, [])))
    if write_labels:
        _merge_labels(labels_path, labels)
    records = [
        {
            "style": row.get("style"),
            "symbol": row.get("symbol"),
            "bar_time": row.get("bar_time"),
            "scenario_tags": row.get("scenario_tags"),
            "forward_outcome": row.get("forward_outcome"),
        }
        for row in labels
    ]
    summary = summarize_forward_outcomes(records)
    hold_summary = _latest_review_hold_summary(review_path)
    candidates = dynamic_threshold_candidates(summary, hold_summary)
    labeled_count = sum(1 for row in labels if (row.get("forward_outcome") or {}).get("status") == "labeled")
    pending_count = sum(1 for row in labels if (row.get("forward_outcome") or {}).get("status") == "pending_future_bars")
    report = {
        "market": MARKET,
        "report_type": "cn_futures_win_rate_calibration",
        "date": date,
        "generated_at": _now_iso(),
        "signal_card_count": len(cards),
        "label_count": len(labels),
        "labeled_count": labeled_count,
        "pending_count": pending_count,
        "forward_label_summary": summary,
        "dynamic_threshold_candidates": candidates,
        "recommendations": _recommendations(summary, candidates),
        "labels_path": str(labels_path),
        "capital_layer": "simulated",
        "real_trading_enabled": False,
    }
    return report


def _write_report_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_report_md(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# CNFutures Win-Rate Calibration",
        "",
        f"- Date: {report.get('date')}",
        f"- Labeled / Pending: {report.get('labeled_count')} / {report.get('pending_count')}",
        f"- Signal cards reviewed: {report.get('signal_card_count')}",
        f"- Real trading enabled: {str(report.get('real_trading_enabled')).lower()}",
        "",
        "## Recommendations",
    ]
    for item in report.get("recommendations", []):
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Dynamic Threshold Candidates")
    candidates = report.get("dynamic_threshold_candidates") if isinstance(report.get("dynamic_threshold_candidates"), list) else []
    if not candidates:
        lines.append("- None")
    for item in candidates:
        if isinstance(item, dict):
            lines.append(f"- {item.get('style_name')}: {item.get('action')} ({item.get('reason')})")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post-session CN futures forward labeling and win-rate calibration.")
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--signals-dir", type=Path, default=DEFAULT_SIGNALS_DIR)
    parser.add_argument("--review-path", type=Path, default=DEFAULT_REVIEW_PATH)
    parser.add_argument("--labels-path", type=Path, default=DEFAULT_LABELS_PATH)
    parser.add_argument("--write-json", type=Path, default=DEFAULT_REPORT_JSON)
    parser.add_argument("--write-md", type=Path, default=DEFAULT_REPORT_MD)
    parser.add_argument("--max-cards", type=int, default=500)
    parser.add_argument("--no-write-labels", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_calibration_report(
        date=str(args.date),
        signals_dir=args.signals_dir,
        review_path=args.review_path,
        labels_path=args.labels_path,
        max_cards=args.max_cards,
        write_labels=not args.no_write_labels,
    )
    if args.write_json:
        _write_report_json(args.write_json, report)
    if args.write_md:
        _write_report_md(args.write_md, report)
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
