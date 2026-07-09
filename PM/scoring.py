#!/usr/bin/env python3
"""Prediction Markets probability-domain scoring.

Production PM tradeable edge must come from ``PM.research_probability`` fed by
the MarketGraph API. This scorer is a local diagnostic adapter and must not be
used to promote SharedSignals inline probability fields into tradeable research
probabilities.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from shared.data.reader import TradingagentDataReader


DEFAULT_WEIGHTS: dict[str, float] = {
    "probability_value": 0.30,
    "liquidity": 0.20,
    "event_clarity": 0.20,
    "time_to_settlement": 0.15,
    "sentiment": 0.15,
}

_DATE_KEYS = ("end_date", "end_time", "close_time", "resolution_time")
_PRICE_KEYS = ("yes_price", "last_price", "price", "implied_probability", "probability")
_MODEL_KEYS = ("model_probability", "model_prob", "fair_probability", "estimated_probability")


def _safe_float(value: Any, default: float = 0.5) -> float:
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _first_present(row: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _parse_date(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for candidate, fmt in (
        (raw[:10], "%Y-%m-%d"),
        (raw[:10], "%Y/%m/%d"),
        (raw[:8], "%Y%m%d"),
    ):
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    return None


def _market_row(reader: Any, market_id: str) -> dict[str, Any]:
    try:
        markets = reader.get_pm_markets(active_only=False)
    except TypeError:
        markets = reader.get_pm_markets()
    except Exception:
        return {}
    for row in markets or []:
        if not isinstance(row, dict):
            continue
        candidate = _first_present(row, ("market_id", "id", "slug", "condition_id"))
        if str(candidate) == market_id:
            return row
    return {}


def _latest_price_row(reader: Any, market_id: str, date: str) -> dict[str, Any]:
    try:
        rows = reader.get_pm_prices(market_id, None, date)
    except Exception:
        return {}
    if not rows:
        return {}
    latest = rows[-1]
    return latest if isinstance(latest, dict) else {}


def _probability(row: dict[str, Any], fallback: dict[str, Any], keys: Iterable[str]) -> float | None:
    value = _first_present(row, keys)
    if value is None:
        value = _first_present(fallback, keys)
    if value is None:
        return None
    return _clamp(_safe_float(value))


def _score_probability_value(market: dict[str, Any], price: dict[str, Any]) -> float:
    market_probability = _probability(price, market, _PRICE_KEYS)
    model_probability = _probability(price, market, _MODEL_KEYS)
    if market_probability is None or model_probability is None:
        return 0.5
    edge = abs(model_probability - market_probability)
    return _clamp(0.5 + edge * 2.0)


def _score_liquidity(market: dict[str, Any], price: dict[str, Any]) -> float:
    explicit = _first_present(price, ("liquidity_score", "depth_score"))
    if explicit is None:
        explicit = _first_present(market, ("liquidity_score", "depth_score"))
    if explicit is not None:
        base = _clamp(_safe_float(explicit))
    else:
        liquidity = _safe_float(_first_present(market, ("liquidity", "liquidity_depth")), 0.0)
        volume = _safe_float(_first_present(market, ("volume", "volume_24h")), 0.0)
        depth = max(liquidity, volume)
        if depth <= 0:
            base = 0.5
        elif depth >= 50000:
            base = 1.0
        elif depth >= 5000:
            base = 0.70 + (depth - 5000) / 45000 * 0.30
        else:
            base = 0.30 + depth / 5000 * 0.40

    spread = _safe_float(_first_present(price, ("bid_ask_spread", "spread")), 0.0)
    if spread <= 0:
        return _clamp(base)
    spread_penalty = _clamp(1.0 - (spread / 0.10), 0.20, 1.0)
    return _clamp(base * spread_penalty)


def _score_event_clarity(market: dict[str, Any]) -> float:
    explicit = _first_present(market, ("event_clarity", "clarity_score", "resolution_clarity"))
    if explicit is not None:
        return _clamp(_safe_float(explicit))

    score = 0.45
    if _first_present(market, ("resolution_source", "outcome_source")):
        score += 0.25
    if _first_present(market, ("title", "question")):
        score += 0.10
    if _first_present(market, ("description", "rules")):
        score += 0.10
    category = str(_first_present(market, ("category", "topic")) or "").lower()
    if any(term in category for term in ("social", "entertainment", "tweet")):
        score -= 0.15
    return _clamp(score)


def _score_time_to_settlement(market: dict[str, Any], date: str) -> float:
    end_date = _parse_date(_first_present(market, _DATE_KEYS))
    current = _parse_date(date)
    if end_date is None or current is None:
        return 0.5
    days = (end_date.date() - current.date()).days
    if days < 0:
        return 0.1
    if days <= 7:
        return 0.85
    if days <= 30:
        return 0.95
    if days <= 90:
        return 0.75
    if days <= 180:
        return 0.45
    return 0.30


def _score_sentiment(market: dict[str, Any], price: dict[str, Any]) -> float:
    value = _first_present(price, ("sentiment_score", "nlp_sentiment"))
    if value is None:
        value = _first_present(market, ("sentiment_score", "nlp_sentiment"))
    if value is None:
        return 0.5
    raw = _safe_float(value)
    if -1.0 <= raw <= 1.0 and raw < 0.0:
        return _clamp((raw + 1.0) / 2.0)
    return _clamp(raw)


def score_market(
    market_id: str,
    date: str,
    data_reader: Any | None = None,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Return PM-specific 0-1 scores without using six-dimension scoring."""

    reader = data_reader or TradingagentDataReader()
    market = _market_row(reader, market_id)
    price = _latest_price_row(reader, market_id, date)
    dimensions = {
        "probability_value": _score_probability_value(market, price),
        "liquidity": _score_liquidity(market, price),
        "event_clarity": _score_event_clarity(market),
        "time_to_settlement": _score_time_to_settlement(market, date),
        "sentiment": _score_sentiment(market, price),
    }

    score_weights = weights or DEFAULT_WEIGHTS
    total_weight = sum(score_weights.get(key, 0.0) for key in dimensions)
    if total_weight <= 0:
        combined = 0.5
    else:
        combined = sum(dimensions[key] * score_weights.get(key, 0.0) for key in dimensions) / total_weight

    return {
        **dimensions,
        "combined": _clamp(combined),
        "market": "pm",
        "sector": str(market.get("category") or "prediction_market"),
        "capital_layer": "shadow",
        "score_model": "pm_probability_v1",
    }


def score_pm_market(market_id: str, date: str, data_reader: Any | None = None) -> dict[str, Any]:
    return score_market(market_id, date, data_reader=data_reader)
