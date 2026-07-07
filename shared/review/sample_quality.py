#!/usr/bin/env python3
"""Classify review samples without rewriting append-only trade ledgers."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")


def _normalize(value: Any) -> str:
    return str(value or "").strip().lower()


def _parse_trade_timestamp(row: dict[str, Any]) -> datetime | None:
    raw = str(row.get("created_at") or row.get("timestamp") or row.get("filled_at") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _is_ashare_regular_session(row: dict[str, Any]) -> bool:
    if row.get("ashare_session_valid") is False:
        return False
    trade_time = _parse_trade_timestamp(row)
    if trade_time is None:
        return True
    try:
        from Ashare.t_plus_1 import is_trading_day

        if not is_trading_day(trade_time.date()):
            return False
    except Exception:
        if trade_time.weekday() >= 5:
            return False
    current = trade_time.time()
    return (time(9, 30) <= current <= time(11, 30)) or (time(13, 0) <= current <= time(14, 57))


def classify_trade_sample(row: dict[str, Any]) -> dict[str, Any]:
    """Return strategy-sample validity for one normalized trade row."""

    market = _normalize(row.get("market") or "ashare")
    capital_layer = _normalize(row.get("capital_layer") or row.get("account_type") or "simulated")
    side = _normalize(row.get("side"))
    execution_source = _normalize(row.get("execution_source"))
    candidate_layer = _normalize(row.get("candidate_pool_layer"))

    if market != "ashare" or capital_layer != "simulated":
        return {
            "strategy_sample_valid": True,
            "sample_classification": "strategy_sample",
            "sample_quality_reason": "non_ashare_or_non_simulated",
        }

    if not _is_ashare_regular_session(row):
        return {
            "strategy_sample_valid": False,
            "sample_classification": "chain_validation",
            "sample_quality_reason": "outside_ashare_regular_session",
        }

    if side == "buy" and execution_source == "ashare_candidate_layer" and candidate_layer == "candidate":
        return {
            "strategy_sample_valid": True,
            "sample_classification": "strategy_sample",
            "sample_quality_reason": "ashare_candidate_layer_buy",
        }

    if side == "sell" and execution_source == "ashare_rebalance_sell":
        return {
            "strategy_sample_valid": True,
            "sample_classification": "strategy_sample",
            "sample_quality_reason": "ashare_rebalance_sell",
        }

    return {
        "strategy_sample_valid": False,
        "sample_classification": "chain_validation",
        "sample_quality_reason": "missing_ashare_candidate_provenance",
    }


def enrich_trade_sample(row: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(row)
    enriched.update(classify_trade_sample(enriched))
    return enriched


def summarize_sample_quality(rows: list[dict[str, Any]]) -> dict[str, Any]:
    enriched = [enrich_trade_sample(row) for row in rows]
    classifications = Counter(str(row.get("sample_classification") or "unknown") for row in enriched)
    reasons = Counter(str(row.get("sample_quality_reason") or "unknown") for row in enriched)
    valid_count = sum(1 for row in enriched if bool(row.get("strategy_sample_valid")))
    validation_count = sum(1 for row in enriched if row.get("sample_classification") == "chain_validation")
    return {
        "total_count": len(enriched),
        "strategy_sample_valid_count": valid_count,
        "validation_sample_count": validation_count,
        "invalid_strategy_sample_count": len(enriched) - valid_count,
        "by_classification": dict(sorted(classifications.items())),
        "by_reason": dict(sorted(reasons.items())),
    }


def strategy_valid_trades(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in (enrich_trade_sample(item) for item in rows) if bool(row.get("strategy_sample_valid"))]


__all__ = [
    "classify_trade_sample",
    "enrich_trade_sample",
    "strategy_valid_trades",
    "summarize_sample_quality",
]
