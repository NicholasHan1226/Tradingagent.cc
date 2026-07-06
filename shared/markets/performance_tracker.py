#!/usr/bin/env python3
"""Performance evidence store for simulated multi-style market runs."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


TRADINGAGENT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REVIEW_ROOT = TRADINGAGENT_ROOT / "shared" / "review"
AUDIT_SCOPE_KEYS = (
    "exclude_from_dashboard",
    "dashboard_excluded",
    "excluded_from_dashboard",
    "run_context",
    "run_mode",
    "run_source",
    "sample_type",
)


@dataclass(frozen=True)
class StylePerformance:
    style_name: str
    market: str
    date: str
    pnl: float
    win_rate: float
    max_dd: float
    sharpe: float
    trades: int
    avg_hold_hours: float
    exclude_from_dashboard: bool = False
    dashboard_excluded: bool = False
    excluded_from_dashboard: bool = False
    run_context: str = ""
    run_mode: str = ""
    run_source: str = ""
    sample_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["capital_layer"] = "simulated"
        payload["account_type"] = "simulated"
        payload["real_execution"] = False
        return payload


def _review_dir(market: str, review_root: Path | str | None = None) -> Path:
    root = Path(review_root) if review_root is not None else DEFAULT_REVIEW_ROOT
    return root / _normalize_market(market)


def _normalize_market(market: Any) -> str:
    return str(market or "").strip().lower()


def _compact_date(value: Any) -> str:
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else raw


def _today_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _date_from_payload(result: dict[str, Any]) -> str:
    return _compact_date(result.get("date") or result.get("trade_date") or result.get("as_of")) or _today_compact()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, str):
            value = value.strip().rstrip("%")
        number = float(value)
        return default if number != number else number
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _performance_from_result(style_name: str, market: str, result: dict[str, Any]) -> StylePerformance:
    metric = dict(result or {})
    if "style_comparison" in metric:
        rows = metric.get("style_comparison") or []
        for row in rows:
            if isinstance(row, dict) and row.get("style_name") == style_name:
                metric = {**metric, **row}
                break
    return StylePerformance(
        style_name=style_name,
        market=_normalize_market(market),
        date=_date_from_payload(metric),
        pnl=round(_safe_float(metric.get("pnl")), 6),
        win_rate=round(max(0.0, min(1.0, _safe_float(metric.get("win_rate")))), 6),
        max_dd=round(max(0.0, _safe_float(metric.get("max_dd"))), 6),
        sharpe=round(_safe_float(metric.get("sharpe")), 6),
        trades=max(0, _safe_int(metric.get("trades"))),
        avg_hold_hours=round(max(0.0, _safe_float(metric.get("avg_hold_hours"))), 6),
        exclude_from_dashboard=bool(metric.get("exclude_from_dashboard")),
        dashboard_excluded=bool(metric.get("dashboard_excluded")),
        excluded_from_dashboard=bool(metric.get("excluded_from_dashboard")),
        run_context=str(metric.get("run_context") or ""),
        run_mode=str(metric.get("run_mode") or ""),
        run_source=str(metric.get("run_source") or ""),
        sample_type=str(metric.get("sample_type") or ""),
    )


def _performance_key(row: StylePerformance | dict[str, Any]) -> tuple[str, str, str]:
    if isinstance(row, StylePerformance):
        return (_normalize_market(row.market), str(row.style_name or ""), _compact_date(row.date))
    return (
        _normalize_market(row.get("market")),
        str(row.get("style_name") or ""),
        _compact_date(row.get("date")),
    )


def _dedupe_latest(rows: list[StylePerformance]) -> list[StylePerformance]:
    latest_by_key: dict[tuple[str, str, str], StylePerformance] = {}
    order: list[tuple[str, str, str]] = []
    for row in rows:
        key = _performance_key(row)
        if key not in latest_by_key:
            order.append(key)
        latest_by_key[key] = row
    return [latest_by_key[key] for key in order]


def save_run(
    style_name: str,
    market: str,
    result: dict[str, Any],
    *,
    review_root: Path | str | None = None,
) -> StylePerformance:
    """Upsert one daily style performance row to ``style_performance.jsonl``."""

    performance = _performance_from_result(style_name, market, result)
    output_dir = _review_dir(market, review_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "style_performance.jsonl"
    rows = load_history(market, days=36500, review_root=review_root)
    target_key = _performance_key(performance)
    replaced = False
    updated: list[StylePerformance] = []
    for row in rows:
        if _performance_key(row) == target_key:
            if not replaced:
                updated.append(performance)
                replaced = True
            continue
        updated.append(row)
    if not replaced:
        updated.append(performance)
    path.write_text(
        "".join(json.dumps(row.to_dict(), ensure_ascii=False) + "\n" for row in updated),
        encoding="utf-8",
    )
    return performance


def load_history(
    market: str,
    days: int = 90,
    *,
    review_root: Path | str | None = None,
) -> list[StylePerformance]:
    """Load recent performance rows for one market."""

    path = _review_dir(market, review_root) / "style_performance.jsonl"
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))
    rows: list[StylePerformance] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        compact = _compact_date(payload.get("date"))
        try:
            row_date = datetime.strptime(compact, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            row_date = cutoff
        if row_date < cutoff:
            continue
        rows.append(_performance_from_result(str(payload.get("style_name", "")), market, payload))
    return _dedupe_latest(rows)


def compact_history(
    market: str,
    *,
    review_root: Path | str | None = None,
) -> dict[str, Any]:
    """Rewrite a market performance file with one latest row per market/style/date."""

    path = _review_dir(market, review_root) / "style_performance.jsonl"
    rows = load_history(market, days=36500, review_root=review_root)
    before = 0
    try:
        before = len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])
    except FileNotFoundError:
        return {"market": _normalize_market(market), "path": str(path), "before": 0, "after": 0, "removed": 0}
    path.write_text(
        "".join(json.dumps(row.to_dict(), ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    after = len(rows)
    return {
        "market": _normalize_market(market),
        "path": str(path),
        "before": before,
        "after": after,
        "removed": max(0, before - after),
    }


def _load_all_history(days: int, review_root: Path | str | None = None) -> list[StylePerformance]:
    root = Path(review_root) if review_root is not None else DEFAULT_REVIEW_ROOT
    rows: list[StylePerformance] = []
    for path in sorted(root.glob("*/style_performance.jsonl")):
        rows.extend(load_history(path.parent.name, days=days, review_root=root))
    return rows


def _linear_slope(points: list[tuple[int, float]]) -> float:
    if len(points) < 2:
        return 0.0
    xs = [float(item[0]) for item in points]
    ys = [float(item[1]) for item in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom


def detect_trend(
    style_name: str,
    market: str | None = None,
    *,
    days: int = 90,
    review_root: Path | str | None = None,
) -> dict[str, Any]:
    """Return improving/declining/stable based on daily PnL regression."""

    history = load_history(market, days=days, review_root=review_root) if market else _load_all_history(days, review_root)
    by_day: dict[str, float] = defaultdict(float)
    for row in history:
        if row.style_name == style_name:
            by_day[row.date] += row.pnl
    points: list[tuple[int, float]] = []
    for index, date in enumerate(sorted(by_day)):
        points.append((index, by_day[date]))
    slope = _linear_slope(points)
    scale = max(1.0, max((abs(pnl) for pnl in by_day.values()), default=0.0))
    threshold = scale * 0.01
    if slope > threshold:
        trend = "improving"
    elif slope < -threshold:
        trend = "declining"
    else:
        trend = "stable"
    return {
        "style_name": style_name,
        "market": _normalize_market(market) if market else "all",
        "trend": trend,
        "slope": round(slope, 8),
        "sample_days": len(points),
        "daily_pnl": [{"date": date, "pnl": round(pnl, 6)} for date, pnl in sorted(by_day.items())],
    }


def _composite_score(row: dict[str, Any]) -> float:
    max_dd = max(0.0, _safe_float(row.get("max_dd")))
    drawdown_penalty = max(0.0, 1.0 - min(1.0, max_dd))
    score = _safe_float(row.get("sharpe")) * _safe_float(row.get("win_rate")) * drawdown_penalty
    if not math.isfinite(score):
        return 0.0
    return round(score, 8)


def compare_styles(
    market: str,
    *,
    days: int = 90,
    review_root: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Rank styles by Sharpe x win_rate x (1 - max_dd)."""

    history = load_history(market, days=days, review_root=review_root)
    grouped: dict[str, list[StylePerformance]] = defaultdict(list)
    for row in history:
        grouped[row.style_name].append(row)

    rankings: list[dict[str, Any]] = []
    for style_name, rows in grouped.items():
        total_trades = sum(row.trades for row in rows)
        pnl = sum(row.pnl for row in rows)
        denominator = max(1, len(rows))
        metric = {
            "style_name": style_name,
            "market": _normalize_market(market),
            "sample_days": len({row.date for row in rows}),
            "runs": len(rows),
            "trades": total_trades,
            "pnl": round(pnl, 6),
            "win_rate": round(sum(row.win_rate for row in rows) / denominator, 6),
            "max_dd": round(max((row.max_dd for row in rows), default=0.0), 6),
            "sharpe": round(sum(row.sharpe for row in rows) / denominator, 6),
            "avg_hold_hours": round(sum(row.avg_hold_hours for row in rows) / denominator, 6),
        }
        metric["composite_score"] = _composite_score(metric)
        metric["trend"] = detect_trend(style_name, market, days=days, review_root=review_root)["trend"]
        rankings.append(metric)

    rankings.sort(key=lambda item: (item["composite_score"], item["pnl"], item["trades"]), reverse=True)
    for rank, item in enumerate(rankings, start=1):
        item["rank"] = rank
    return rankings


def load_style_weights(
    market: str,
    *,
    review_root: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    """Load the latest evolved style allocation snapshot."""

    path = _review_dir(market, review_root) / "style_weights.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    weights = payload.get("styles") if isinstance(payload, dict) else {}
    return weights if isinstance(weights, dict) else {}


__all__ = [
    "StylePerformance",
    "compare_styles",
    "compact_history",
    "detect_trend",
    "load_history",
    "load_style_weights",
    "save_run",
]
