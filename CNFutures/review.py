#!/usr/bin/env python3
"""Append-only review records for CN futures simulation runs."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REVIEW_PATH = (
    Path(__file__).resolve().parents[1]
    / "shared"
    / "review"
    / "data"
    / "cn_futures_sim_reviews.jsonl"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _record_pnl(record: dict[str, Any]) -> float | None:
    for container_key in ("performance", "pnl", "result"):
        container = record.get(container_key)
        if isinstance(container, dict):
            for key in ("realized_pnl", "net_pnl", "pnl"):
                if key in container:
                    return _safe_float(container.get(key))
    for key in ("realized_pnl", "net_pnl", "pnl"):
        if key in record:
            return _safe_float(record.get(key))
    return None


def score_records(records: list[dict[str, Any]], *, min_sample_trades: int = 20) -> dict[str, Any]:
    """Score simulated CN futures styles from append-only review records.

    Open-only simulation fills do not prove profitability. When realized PnL is
    unavailable or sample size is small, the score is explicitly marked as
    sample_insufficient instead of implying a tradable edge.
    """

    styles: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "trade_count": 0,
            "filled_count": 0,
            "fee": 0.0,
            "margin_required": 0.0,
            "notional": 0.0,
            "realized_pnl": 0.0,
            "pnl_sample_count": 0,
            "wins": 0,
            "losses": 0,
            "max_drawdown": None,
            "score": 0.0,
            "status": "sample_insufficient",
            "sample_warning": "",
        }
    )
    equity_curves: dict[str, list[float]] = defaultdict(list)
    for record in records:
        if not isinstance(record, dict):
            continue
        style = str(record.get("style") or "unknown")
        receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
        raw = receipt.get("raw_response") if isinstance(receipt.get("raw_response"), dict) else {}
        metrics = styles[style]
        metrics["trade_count"] += 1
        if str(receipt.get("status", "")).lower() == "filled":
            metrics["filled_count"] += 1
        metrics["fee"] += _safe_float(receipt.get("fee"))
        metrics["margin_required"] += _safe_float(raw.get("margin_required"))
        metrics["notional"] += _safe_float(raw.get("notional"))
        pnl = _record_pnl(record)
        if pnl is not None:
            metrics["pnl_sample_count"] += 1
            metrics["realized_pnl"] += pnl
            if pnl > 0:
                metrics["wins"] += 1
            elif pnl < 0:
                metrics["losses"] += 1
            previous = equity_curves[style][-1] if equity_curves[style] else 0.0
            equity_curves[style].append(previous + pnl)

    for style, metrics in styles.items():
        trade_count = int(metrics["trade_count"])
        pnl_sample_count = int(metrics["pnl_sample_count"])
        decisive = int(metrics["wins"]) + int(metrics["losses"])
        metrics["win_rate"] = (metrics["wins"] / decisive) if decisive else None
        curve = equity_curves.get(style, [])
        if curve:
            peak = curve[0]
            max_drawdown = 0.0
            for value in curve:
                peak = max(peak, value)
                max_drawdown = min(max_drawdown, value - peak)
            metrics["max_drawdown"] = abs(max_drawdown)
        if trade_count < min_sample_trades or pnl_sample_count < min_sample_trades:
            metrics["status"] = "sample_insufficient"
            metrics["sample_warning"] = (
                f"requires at least {min_sample_trades} realized PnL samples; "
                f"has trades={trade_count}, pnl_samples={pnl_sample_count}"
            )
            metrics["score"] = 0.0
            continue
        win_rate = _safe_float(metrics.get("win_rate"), 0.0)
        realized_pnl = _safe_float(metrics.get("realized_pnl"))
        fee = _safe_float(metrics.get("fee"))
        drawdown = _safe_float(metrics.get("max_drawdown"))
        risk_penalty = drawdown + fee
        metrics["score"] = round(realized_pnl + (win_rate * 100.0) - risk_penalty, 4)
        metrics["status"] = "eligible_for_candidate_pool" if metrics["score"] > 0 else "underperforming"
        metrics["sample_warning"] = ""
    return {
        "min_sample_trades": min_sample_trades,
        "style_scores": {style: dict(metrics) for style, metrics in styles.items()},
    }


def summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    styles: dict[str, dict[str, Any]] = defaultdict(lambda: {"filled_count": 0, "fee": 0.0, "margin_required": 0.0})
    for record in records:
        style = str(record.get("style") or "unknown")
        receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
        raw = receipt.get("raw_response") if isinstance(receipt, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        if str(receipt.get("status", "")).lower() == "filled":
            styles[style]["filled_count"] += 1
        styles[style]["fee"] += float(receipt.get("fee") or 0.0)
        styles[style]["margin_required"] += float(raw.get("margin_required") or 0.0)
    return {
        "filled_count": sum(item["filled_count"] for item in styles.values()),
        "styles": {style: dict(values) for style, values in styles.items()},
    }


def append_review(
    *,
    date: str,
    market: str,
    records: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    path: Path | None = None,
) -> dict[str, Any]:
    """Append one run review and return the persisted payload."""

    summary = summarize_records(records)
    score_summary = score_records(records)
    payload: dict[str, Any] = {
        "date": date,
        "market": market,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "state": "degraded" if errors else "ok",
        "record_count": len(records),
        "error_count": len(errors),
        "errors": errors,
        "generated_at": _now_iso(),
        **summary,
        "score_summary": score_summary,
    }
    target = path or DEFAULT_REVIEW_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return payload


__all__ = ["DEFAULT_REVIEW_PATH", "append_review", "score_records", "summarize_records"]
