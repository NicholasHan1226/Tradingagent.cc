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
    raw = str(
        row.get("trade_timestamp_bj")
        or row.get("timestamp_bj")
        or row.get("created_at")
        or row.get("timestamp")
        or row.get("filled_at")
        or ""
    ).strip()
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
    return (time(9, 30) <= current <= time(11, 30)) or (
        time(13, 0) <= current <= time(14, 57)
    )


def _fill_price_source_class(row: dict[str, Any]) -> str:
    evidence = (
        row.get("fill_evidence") if isinstance(row.get("fill_evidence"), dict) else {}
    )
    return _normalize(
        row.get("fill_price_source_class")
        or evidence.get("fill_price_source_class")
        or row.get("price_source_class")
    )


def _has_market_data_fill_price(row: dict[str, Any]) -> bool:
    source_class = _fill_price_source_class(row)
    if source_class == "market_data":
        return True
    evidence = (
        row.get("fill_evidence") if isinstance(row.get("fill_evidence"), dict) else {}
    )
    source = _normalize(
        row.get("fill_price_source")
        or evidence.get("fill_price_source")
        or row.get("price_source")
    )
    if not source or source in {
        "signal_card.price",
        "unknown",
        "requested_order_price",
    }:
        return False
    return any(
        marker in source
        for marker in (
            "market_snapshot",
            ".ask_price",
            ".bid_price",
            ".last_price",
            ".close",
            ".latest_price",
        )
    )


def _has_strategy_fill_price(row: dict[str, Any]) -> bool:
    if _has_market_data_fill_price(row):
        return True
    source_class = _fill_price_source_class(row)
    evidence = (
        row.get("fill_evidence") if isinstance(row.get("fill_evidence"), dict) else {}
    )
    source = _normalize(
        row.get("fill_price_source")
        or evidence.get("fill_price_source")
        or row.get("price_source")
    )
    if source_class != "signal_card_price" and source != "signal_card.price":
        return False
    try:
        return float(row.get("filled_price") or row.get("avg_price") or 0.0) > 0
    except (TypeError, ValueError):
        return False


def _evolution_evidence_check(row: dict[str, Any]) -> tuple[bool, str]:
    """Require a time-and-liquidity-backed market quote before learning from a fill.

    Returns (eligible, rejection_reason). rejection_reason is non-empty only
    when eligible is False, describing the first failing condition encountered.
    """
    evidence = (
        row.get("fill_evidence") if isinstance(row.get("fill_evidence"), dict) else {}
    )
    execution_class = evidence.get("execution_evidence_class")
    if not execution_class:
        return False, "missing_execution_evidence_class"
    if execution_class != "verified_5min_market_data":
        return False, "execution_evidence_class_not_verified"
    if not _has_market_data_fill_price(row):
        return False, "no_market_data_fill_price"
    bar_time = row.get("bar_time") or evidence.get("bar_time")
    bar_volume = row.get("bar_volume") or evidence.get("bar_volume")
    if not bar_time:
        return False, "missing_bar_time"
    try:
        vol = float(bar_volume or 0)
    except (TypeError, ValueError):
        return False, "bar_volume_not_numeric"
    if vol <= 0:
        return False, "bar_volume_zero_or_negative"
    trade_time = _parse_trade_timestamp(row)
    if trade_time is None:
        return False, "missing_trade_timestamp"
    try:
        parsed_bar_time = datetime.fromisoformat(str(bar_time).replace("Z", "+00:00"))
    except ValueError:
        return False, "bar_time_unparseable"
    if parsed_bar_time.tzinfo is None:
        parsed_bar_time = parsed_bar_time.replace(tzinfo=CN_TZ)
    else:
        parsed_bar_time = parsed_bar_time.astimezone(CN_TZ)
    age_seconds = (trade_time - parsed_bar_time).total_seconds()
    if age_seconds < -300:
        return False, "bar_time_too_future"
    if age_seconds > 900:
        return False, "bar_time_too_stale"
    return True, "verified_market_data_execution"


def classify_trade_sample(row: dict[str, Any]) -> dict[str, Any]:
    """Return strategy-sample validity for one normalized trade row."""

    market = _normalize(row.get("market") or "ashare")
    capital_layer = _normalize(
        row.get("capital_layer") or row.get("account_type") or "simulated"
    )
    side = _normalize(row.get("side"))
    execution_source = _normalize(row.get("execution_source"))
    candidate_layer = _normalize(row.get("candidate_pool_layer"))
    sample_intent = _normalize(row.get("sample_intent"))

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

    valid_buy_layer = (
        candidate_layer == "candidate" and sample_intent in {"", "exploitation"}
    ) or (candidate_layer == "exploration" and sample_intent == "exploration")
    if (
        side == "buy"
        and execution_source == "ashare_candidate_layer"
        and valid_buy_layer
    ):
        if not _has_strategy_fill_price(row):
            return {
                "strategy_sample_valid": False,
                "sample_classification": "chain_validation",
                "sample_quality_reason": "missing_fill_price_provenance",
            }
        return {
            "strategy_sample_valid": True,
            "sample_classification": "strategy_sample",
            "sample_quality_reason": (
                "ashare_exploration_layer_buy"
                if candidate_layer == "exploration"
                else "ashare_candidate_layer_buy"
            ),
        }

    if side == "sell" and execution_source == "ashare_rebalance_sell":
        if not _has_strategy_fill_price(row):
            return {
                "strategy_sample_valid": False,
                "sample_classification": "chain_validation",
                "sample_quality_reason": "missing_fill_price_provenance",
            }
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
    strategy_valid = bool(enriched.get("strategy_sample_valid"))
    evidence_ok, evidence_reason = _evolution_evidence_check(enriched)
    eligible = strategy_valid and evidence_ok
    enriched["evolution_sample_eligible"] = eligible
    if eligible:
        enriched["evolution_sample_reason"] = "verified_market_data_execution"
    elif not strategy_valid:
        enriched["evolution_sample_reason"] = "not_strategy_sample"
    else:
        enriched["evolution_sample_reason"] = evidence_reason
    return enriched


def summarize_sample_quality(rows: list[dict[str, Any]]) -> dict[str, Any]:
    enriched = [enrich_trade_sample(row) for row in rows]
    classifications = Counter(
        str(row.get("sample_classification") or "unknown") for row in enriched
    )
    reasons = Counter(
        str(row.get("sample_quality_reason") or "unknown") for row in enriched
    )
    valid_count = sum(1 for row in enriched if bool(row.get("strategy_sample_valid")))
    evolution_eligible_count = sum(
        1 for row in enriched if bool(row.get("evolution_sample_eligible"))
    )

    # Rejection reason counters: only count strategy-valid trades that fail evolution eligibility
    evolution_rejection_counter: Counter[str] = Counter()
    for row in enriched:
        if bool(row.get("strategy_sample_valid")) and not bool(
            row.get("evolution_sample_eligible")
        ):
            reason = str(row.get("evolution_sample_reason") or "unknown")
            evolution_rejection_counter[reason] += 1
    evolution_rejection_reasons = dict(sorted(evolution_rejection_counter.items()))

    validation_count = sum(
        1 for row in enriched if row.get("sample_classification") == "chain_validation"
    )
    return {
        "total_count": len(enriched),
        "strategy_sample_valid_count": valid_count,
        "evolution_sample_eligible_count": evolution_eligible_count,
        "validation_sample_count": validation_count,
        "invalid_strategy_sample_count": len(enriched) - valid_count,
        "by_classification": dict(sorted(classifications.items())),
        "by_reason": dict(sorted(reasons.items())),
        "evolution_rejection_reasons": evolution_rejection_reasons,
    }


def strategy_valid_trades(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in (enrich_trade_sample(item) for item in rows)
        if bool(row.get("strategy_sample_valid"))
    ]


def evolution_eligible_trades(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in (enrich_trade_sample(item) for item in rows)
        if bool(row.get("evolution_sample_eligible"))
    ]


__all__ = [
    "classify_trade_sample",
    "enrich_trade_sample",
    "strategy_valid_trades",
    "evolution_eligible_trades",
    "summarize_sample_quality",
]
