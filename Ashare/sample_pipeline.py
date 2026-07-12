"""A-share observation and bounded-exploration preparation.

This module sits before strategy thresholds and execution.  It turns every
scored symbol into four style-level counterfactual predictions, while keeping
the one-portfolio execution decision separate.  It never writes an order,
reserves capital, or calls a broker.
"""

from __future__ import annotations

import math
import json
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from Ashare.style_samples import (
    build_style_sample_contract,
    compute_ashare_conservative_costs,
)
from shared.review.forward_labels import build_prediction_snapshot
from shared.review.sample_journal import SampleJournal
from shared.execution.execution_lineage import (
    ASHARE_AUTHORITY_GENERATION,
    ASHARE_CAPITAL_AUTHORITY_ID,
    ASHARE_EXECUTION_LINEAGE_ID,
)


CN_TZ = timezone(timedelta(hours=8))
_DIMENSIONS = ("macro", "event", "fundamental", "capital", "technical", "sentiment")
_STYLE_FEATURE_NAMES = frozenset(
    {
        "breakout_strength",
        "trend_strength",
        "volume_confirmation",
        "pullback_quality",
        "reversal_confirmation",
        "overextension_risk",
        "event_catalyst_score",
        "price_confirmation",
        "realized_volatility",
        "downside_resilience",
        "liquidity_score",
    }
)
EXPLORATION_POLICY_VERSION = "ashare-safe-top-k-epsilon-greedy-v1"
PRIMARY_HORIZON_POLICY_VERSION = "ashare-primary-horizon-v1"
_PRIMARY_HORIZON_BY_STYLE = {
    "trend_breakout_strength_continuation": "1d",
    "pullback_or_short_reversal": "1d",
    "event_catalyst_with_price_confirmation": "1d",
    "defensive_low_volatility_abstain": "close",
}


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _aware_iso(value: Any, *, field: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("%s is required" % field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("%s must be an ISO timestamp" % field) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("%s must include a timezone" % field)
    return parsed.isoformat(timespec="seconds")


def _current_authority_scope(value: Mapping[str, Any] | None) -> dict[str, Any]:
    scope = dict(value or {})
    authority_id = str(
        scope.get("capital_authority_id") or ASHARE_CAPITAL_AUTHORITY_ID
    ).strip()
    generation = scope.get("authority_generation", ASHARE_AUTHORITY_GENERATION)
    lineage_id = str(
        scope.get("execution_lineage_id") or ASHARE_EXECUTION_LINEAGE_ID
    ).strip()
    if authority_id != ASHARE_CAPITAL_AUTHORITY_ID:
        raise ValueError("capital_authority_id must be the current A-share authority")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation != ASHARE_AUTHORITY_GENERATION
    ):
        raise ValueError("authority_generation must be the current A-share generation")
    if lineage_id != ASHARE_EXECUTION_LINEAGE_ID:
        raise ValueError("execution_lineage_id must be the current A-share lineage")
    return {
        "capital_authority_id": authority_id,
        "authority_generation": generation,
        "execution_lineage_id": lineage_id,
    }


def _is_marketgraph_key(value: Any) -> bool:
    key = str(value or "").strip().lower().replace("-", "_")
    return (
        "marketgraph" in key
        or key == "mg"
        or key.startswith("mg_")
        or "_mg_" in key
        or key.endswith("_mg")
    )


def _without_marketgraph(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_marketgraph(nested)
            for key, nested in value.items()
            if not _is_marketgraph_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_without_marketgraph(item) for item in value]
    return deepcopy(value)


def _marketgraph_overlay(score: Mapping[str, Any]) -> dict[str, float]:
    raw_overlay: dict[str, Any] = {}
    for key in ("marketgraph_features", "mg_features"):
        nested = score.get(key)
        if isinstance(nested, Mapping):
            raw_overlay.update(nested)
    for raw_key, raw_value in score.items():
        key = str(raw_key or "").strip().lower().replace("-", "_")
        if key.startswith("mg_") and key[3:] in _STYLE_FEATURE_NAMES:
            raw_overlay[key[3:]] = raw_value
    overlay: dict[str, float] = {}
    for raw_key, raw_value in raw_overlay.items():
        key = str(raw_key or "").strip().lower().replace("-", "_")
        if key not in _STYLE_FEATURE_NAMES:
            continue
        number = _number(raw_value)
        if number is not None:
            overlay[key] = max(0.0, min(1.0, number))
    return dict(sorted(overlay.items()))


def _content_sha_record(record: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(record))
    result.pop("content_sha256", None)
    result["content_sha256"] = _canonical_sha256(result)
    return result


def _number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _positive(value: Any) -> float | None:
    result = _number(value)
    return result if result is not None and result > 0.0 else None


def _compact_trade_date(value: Any) -> str:
    raw = str(value or "").strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10].replace("-", "")
    return raw[:8] if raw else ""


def build_hypothesis_id(
    *,
    trade_date: str,
    symbol: str,
    side: str,
    execution_source: str,
    candidate_pool_layer: str,
    score: float,
) -> str:
    """Build the stable research-hypothesis identity used by the orchestrator."""

    compact = _compact_trade_date(trade_date) or "unknown"
    clean_symbol = str(symbol or "unknown").strip().upper()
    clean_side = str(side or "buy").strip().lower()
    layer = str(candidate_pool_layer or execution_source or "unknown").strip().lower()
    finite_score = _number(score)
    if finite_score is None:
        finite_score = 0.0
    score_bucket = int(max(0.0, min(0.99, finite_score)) * 100)
    return f"ashare-{compact}-{clean_side}-{clean_symbol}-{layer}-s{score_bucket:03d}"


def build_research_hypothesis(
    *,
    trade_date: str,
    symbol: str,
    side: str,
    execution_source: str,
    candidate_pool_layer: str,
    score_snapshot: Mapping[str, Any],
    sample_intent: str,
    capital_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a non-executing hypothesis linked to the six forward horizons."""

    score = _number(score_snapshot.get("combined", score_snapshot.get("score")))
    hypothesis_id = build_hypothesis_id(
        trade_date=trade_date,
        symbol=symbol,
        side=side,
        execution_source=execution_source,
        candidate_pool_layer=candidate_pool_layer,
        score=score if score is not None else 0.0,
    )
    factors = {
        key: round(value, 6)
        for key in _DIMENSIONS + ("combined",)
        if (value := _number(score_snapshot.get(key))) is not None
    }
    # Keep combined first for stable human-facing serialization, followed by
    # the six research dimensions in their canonical order.
    factors = {
        key: factors[key] for key in ("combined",) + _DIMENSIONS if key in factors
    }
    return {
        "hypothesis_id": hypothesis_id,
        "trade_date": _compact_trade_date(trade_date),
        "symbol": str(symbol or "").strip().upper(),
        "side": str(side or "buy").strip().lower(),
        "sample_intent": str(sample_intent or "observation").strip().lower(),
        "factor_snapshot": factors,
        "capital_plan_risk_mode": (capital_plan or {}).get("risk_mode"),
        "expected_validation_horizon": ["m30", "m60", "close", "1d", "3d", "5d"],
        "failure_conditions": [
            "candidate_source_invalid",
            "fill_price_missing",
            "outside_regular_session",
            "forward_return_negative_after_costs",
        ],
    }


def _normalized_date(value: Any) -> tuple[str, str]:
    raw = str(value or "").strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed.strftime("%Y%m%d"), parsed.strftime("%Y-%m-%d")
    raise ValueError("trade_date must be YYYYMMDD or YYYY-MM-DD")


def _bar_source(row: Mapping[str, Any]) -> str:
    return str(row.get("provider") or row.get("source") or "").strip()


def _intraday_reference(
    reader: Any, market: str, symbol: str, date_key: str
) -> dict[str, Any] | None:
    getter = getattr(reader, "get_bars_intraday", None)
    if not callable(getter):
        return None
    try:
        rows = getter(market, symbol, "5m", date_key, date_key)
    except Exception:
        return None
    for raw in reversed(rows or []):
        if not isinstance(raw, Mapping):
            continue
        price = _positive(raw.get("close", raw.get("last_price", raw.get("price"))))
        observed_at = str(
            raw.get("bar_time") or raw.get("trade_time") or raw.get("timestamp") or ""
        ).strip()
        source = _bar_source(raw)
        volume = _positive(raw.get("volume", raw.get("vol", raw.get("bar_volume"))))
        if price is None or not observed_at or not source or volume is None:
            continue
        return {
            "price": price,
            "price_timestamp": observed_at,
            "source": source,
            "volume": volume,
            "frequency": "5m",
            "evidence_class": "verified_intraday_market_data",
            "available_at": raw.get("available_at") or raw.get("published_at"),
            "ingested_at": raw.get("ingested_at") or raw.get("received_at"),
        }
    return None


def _daily_reference(
    reader: Any, market: str, symbol: str, date_key: str
) -> dict[str, Any] | None:
    getter = getattr(reader, "get_bars_daily", None)
    if not callable(getter):
        return None
    try:
        rows = getter(market, symbol, date_key, date_key)
    except Exception:
        return None
    for raw in reversed(rows or []):
        if not isinstance(raw, Mapping):
            continue
        price = _positive(raw.get("close", raw.get("price")))
        trade_date = str(raw.get("trade_date") or raw.get("date") or "").strip()
        source = _bar_source(raw)
        if price is None or not trade_date or not source:
            continue
        compact = trade_date.replace("-", "")
        if compact != date_key:
            continue
        return {
            "price": price,
            "price_timestamp": "%sT15:00:00+08:00"
            % ("%s-%s-%s" % (date_key[:4], date_key[4:6], date_key[6:])),
            "source": source,
            "volume": _number(raw.get("volume", raw.get("vol"))),
            "frequency": "daily",
            "evidence_class": "verified_daily_market_data",
            "available_at": raw.get("available_at") or raw.get("published_at"),
            "ingested_at": raw.get("ingested_at") or raw.get("received_at"),
        }
    return None


def _reference_evidence(
    reader: Any, market: str, symbol: str, date_key: str
) -> dict[str, Any]:
    evidence = _intraday_reference(reader, market, symbol, date_key)
    if evidence is None:
        evidence = _daily_reference(reader, market, symbol, date_key)
    if evidence is not None:
        return {**evidence, "reliable": True, "reason": "verified_market_price"}
    return {
        "price": None,
        "price_timestamp": None,
        "source": None,
        "volume": None,
        "frequency": None,
        "evidence_class": "unverified",
        "reliable": False,
        "reason": "missing_reliable_market_price",
    }


def _score_quality(score: Mapping[str, Any]) -> tuple[bool, str | None]:
    if _number(score.get("combined")) is None:
        return False, "missing_finite_combined_score"
    missing = score.get("missing_evidence_dimensions")
    if isinstance(missing, (list, tuple, set)) and set(
        str(item) for item in missing
    ) >= set(_DIMENSIONS):
        return False, "all_research_dimensions_missing"
    coverage = score.get("evidence_coverage")
    if coverage is not None and (_number(coverage) is None or float(coverage) <= 0.0):
        return False, "research_evidence_coverage_zero"
    return True, None


def _unit(value: Any, default: float = 0.0) -> float:
    number = _number(value)
    if number is None:
        return default
    return max(0.0, min(1.0, number))


def _style_features(score: Mapping[str, Any]) -> dict[str, float]:
    combined = _unit(score.get("combined"), 0.0)
    technical = _unit(score.get("technical"), combined)
    capital = _unit(score.get("capital"), combined)
    event = _unit(score.get("event"), combined)
    fundamental = _unit(score.get("fundamental"), combined)
    sentiment = _unit(score.get("sentiment"), combined)
    turnover = _positive(score.get("turnover_wan"))
    liquidity = min(1.0, math.log10(max(turnover or 1.0, 1.0)) / 5.0)
    return {
        "breakout_strength": technical,
        "trend_strength": (technical + combined) / 2.0,
        "volume_confirmation": capital,
        "pullback_quality": max(0.0, 1.0 - technical),
        "reversal_confirmation": (sentiment + fundamental) / 2.0,
        "overextension_risk": max(0.0, technical - fundamental),
        "event_catalyst_score": event,
        "price_confirmation": technical,
        "realized_volatility": max(0.0, 1.0 - fundamental),
        "downside_resilience": (fundamental + _unit(score.get("macro"), combined))
        / 2.0,
        "liquidity_score": liquidity,
    }


def _prediction_snapshot(
    *,
    symbol: str,
    prediction_at: str,
    reference: Mapping[str, Any],
    style: Mapping[str, Any],
    decision_policy_version: str,
    authority_scope: Mapping[str, Any],
    source_snapshot_sha256: str,
    base_snapshot_sha256: str,
    pair_id: str,
    ablation_group: str,
    feature_snapshot: Mapping[str, Any],
    applied_marketgraph_features: Mapping[str, Any],
    marketgraph_overlay_status: str,
    causal_pair_eligible: bool,
) -> dict[str, Any]:
    prediction = (
        style.get("prediction") if isinstance(style.get("prediction"), Mapping) else {}
    )
    direction = "long" if prediction.get("direction") == "long_bias" else "hold"
    reliable = reference.get("reliable") is True

    # Compute versioned conservative costs from reference price.
    ref_price = reference.get("price")
    costs = None
    cost_rejection = None
    if ref_price is not None:
        try:
            costs = compute_ashare_conservative_costs(float(ref_price))
        except (ValueError, TypeError):
            cost_rejection = "rejected_missing_cost_evidence"
            costs = None
    else:
        cost_rejection = "rejected_missing_cost_evidence"

    marketgraph = deepcopy(style.get("marketgraph") or {})
    marketgraph.update(
        {
            "enabled": ablation_group == "mg_on",
            "ablation_group": ablation_group,
            "pair_id": pair_id,
            "base_snapshot_sha256": base_snapshot_sha256,
            "applied_features": deepcopy(dict(applied_marketgraph_features)),
            "features_physically_excluded": ablation_group == "mg_off",
            "overlay_status": marketgraph_overlay_status,
            "causal_pair_eligible": bool(causal_pair_eligible),
        }
    )
    snapshot_identity = {
        **dict(authority_scope),
        "market": "ashare",
        "symbol": symbol,
        "style_id": style.get("style_id"),
        "style_version": style.get("style_version"),
        "prediction_at": prediction_at,
        "pair_id": pair_id,
        "ablation_group": ablation_group,
    }
    raw = {
        "snapshot_id": "prediction:" + _canonical_sha256(snapshot_identity)[:32],
        "market": "ashare",
        "symbol": symbol,
        "style": style.get("style_id"),
        "style_id": style.get("style_id"),
        "style_version": style.get("style_version"),
        "strategy_version": style.get("style_version"),
        "prediction_at": prediction_at,
        "as_of": prediction_at,
        "point_in_time_as_of": prediction_at,
        "source_event_time": reference.get("price_timestamp"),
        "event_time": reference.get("price_timestamp"),
        "available_at": reference.get("available_at"),
        "ingested_at": reference.get("ingested_at"),
        "retrieved_as_of": prediction_at,
        "point_in_time_lineage": {
            "event_time": reference.get("price_timestamp"),
            "available_at": reference.get("available_at"),
            "ingested_at": reference.get("ingested_at"),
            "retrieved_as_of": prediction_at,
        },
        "source_snapshot_sha256": source_snapshot_sha256,
        "base_snapshot_sha256": base_snapshot_sha256,
        "pair_id": pair_id,
        "feature_snapshot": deepcopy(dict(feature_snapshot)),
        **dict(authority_scope),
        "reference_price": ref_price,
        "direction": direction,
        "rank_score": prediction.get("raw_style_score"),
        "raw_style_score": prediction.get("raw_style_score"),
        "score_semantics": "uncalibrated_rank_score",
        "calibrated_probability": prediction.get("calibrated_probability"),
        "probability_model_state": prediction.get("probability_model_state"),
        "uncalibrated_return_prior": deepcopy(
            style.get("uncalibrated_return_prior") or {}
        ),
        "entry_thesis": style.get("entry_thesis"),
        "exit_thesis": style.get("exit_thesis"),
        "holding_horizon": deepcopy(style.get("holding_horizon") or {}),
        "primary_label_horizon": _PRIMARY_HORIZON_BY_STYLE.get(
            str(style.get("style_id") or ""), "1d"
        ),
        "primary_horizon_policy_version": PRIMARY_HORIZON_POLICY_VERSION,
        "abstain_reason": style.get("abstain_reason"),
        "reject_reason": style.get("reject_reason"),
        "marketgraph": marketgraph,
        "decision_policy_version": decision_policy_version,
        "mature_threshold_passed": bool(
            (style.get("channel_eligibility") or {}).get("exploitation")
        ),
        "execution_gate_passed": False,
        "execution_reject_reason": "not_evaluated_before_execution",
        "sample_intent": "observation",
        "data_quality": {
            "reliable": reliable,
            "source": reference.get("source"),
            "price_timestamp": reference.get("price_timestamp"),
        },
        "real_trading_enabled": False,
        "live_execution_enabled": False,
        "costs": costs,
        "cost_evidence_status": "embedded_conservative"
        if costs is not None
        else cost_rejection,
    }
    return _content_sha_record(build_prediction_snapshot(raw))


def build_candidate_observation(
    *,
    symbol: str,
    trade_date: str,
    mapped_market: str,
    mapped_symbol: str,
    score: Mapping[str, Any],
    reader: Any,
    prediction_at: str | None = None,
    mg_enabled: bool = False,
    style_states: Mapping[str, str] | None = None,
    authority_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build style predictions for one scored symbol before strategy gates."""

    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol is required")
    if not isinstance(score, Mapping):
        raise TypeError("score must be a mapping")
    date_key, date_iso = _normalized_date(trade_date)
    predicted_at = _aware_iso(
        prediction_at or datetime.now(CN_TZ).replace(microsecond=0).isoformat(),
        field="prediction_at",
    )
    current_authority = _current_authority_scope(authority_scope)
    reference = _reference_evidence(
        reader,
        str(mapped_market or "ashare"),
        str(mapped_symbol or normalized_symbol),
        date_key,
    )
    score_ok, score_reason = _score_quality(score)
    qualified = reference.get("reliable") is True and score_ok
    quality_reason = (
        None
        if qualified
        else str(
            reference.get("reason")
            if reference.get("reliable") is not True
            else score_reason
        )
    )
    explicit_base = score.get("base_score")
    explicit_base_present = isinstance(explicit_base, Mapping)
    base_score = _without_marketgraph(explicit_base if explicit_base_present else score)
    if not isinstance(base_score, Mapping):
        raise TypeError("base score must be a mapping")
    base_features = _style_features(base_score)
    explicit_mg_overlay = _marketgraph_overlay(score)
    enhanced_score = score.get("marketgraph_score")
    pairing = score.get("marketgraph_pairing")
    same_snapshot_enhanced = bool(
        isinstance(enhanced_score, Mapping)
        and isinstance(pairing, Mapping)
        and pairing.get("same_scoring_snapshot") is True
        and str(pairing.get("pairing_version") or "") == "six-dimension-mg-pair-v1"
    )
    derived_mg_overlay: dict[str, float] = {}
    if same_snapshot_enhanced:
        enhanced_features = _style_features(enhanced_score)
        derived_mg_overlay = {
            key: value
            for key, value in enhanced_features.items()
            if abs(value - base_features.get(key, value)) > 1e-12
        }
    requested_mg_overlay = explicit_mg_overlay or derived_mg_overlay
    if mg_enabled and requested_mg_overlay and not explicit_base_present:
        mg_overlay: dict[str, float] = {}
        mg_overlay_status = "rejected_missing_explicit_base_score"
    elif mg_enabled and explicit_mg_overlay:
        mg_overlay = requested_mg_overlay
        mg_overlay_status = "applied_from_explicit_base"
    elif mg_enabled and derived_mg_overlay and same_snapshot_enhanced:
        mg_overlay = derived_mg_overlay
        mg_overlay_status = "derived_from_same_scoring_snapshot"
    elif mg_enabled:
        mg_overlay = {}
        mg_overlay_status = (
            "no_marketgraph_score_delta"
            if same_snapshot_enhanced
            else "no_explicit_marketgraph_features"
        )
    else:
        mg_overlay = {}
        mg_overlay_status = "marketgraph_disabled"
    causal_pair_eligible = bool(
        mg_enabled
        and explicit_base_present
        and mg_overlay
        and (bool(explicit_mg_overlay) or same_snapshot_enhanced)
    )
    source_snapshot_sha256 = _canonical_sha256(
        {
            "mapped_market": str(mapped_market or "ashare"),
            "mapped_symbol": str(mapped_symbol or normalized_symbol),
            "score": deepcopy(dict(score)),
            "reference": deepcopy(dict(reference)),
            "prediction_at": predicted_at,
        }
    )
    base_snapshot_payload = {
        **current_authority,
        "market": "ashare",
        "symbol": normalized_symbol,
        "trade_date": date_iso,
        "prediction_at": predicted_at,
        "reference": deepcopy(dict(reference)),
        "base_score": deepcopy(dict(base_score)),
        "base_features": deepcopy(base_features),
    }
    base_snapshot_sha256 = _canonical_sha256(base_snapshot_payload)
    candidate = {
        "symbol": normalized_symbol,
        "trade_date": date_iso,
        "candidate_id": "ashare:%s:%s:%s:%s"
        % (
            current_authority["authority_generation"],
            date_key,
            normalized_symbol,
            predicted_at,
        ),
        **current_authority,
        "as_of": predicted_at,
        "point_in_time_as_of": predicted_at,
        "source_snapshot_sha256": source_snapshot_sha256,
        "base_snapshot_sha256": base_snapshot_sha256,
        "data_quality": {
            "qualified": qualified,
            "reason": quality_reason,
            "source": reference.get("source"),
            "price_timestamp": reference.get("price_timestamp"),
        },
        "features": deepcopy(base_features),
        "marketgraph_features": deepcopy(mg_overlay),
        "requested_marketgraph_features": deepcopy(requested_mg_overlay),
    }
    ablation_groups = ("mg_off", "mg_on") if mg_enabled else ("mg_off",)
    ablation_contracts: dict[str, dict[str, dict[str, Any]]] = {}
    feature_snapshots: dict[str, dict[str, float]] = {}
    for group in ablation_groups:
        features = deepcopy(base_features)
        if group == "mg_on":
            features.update(mg_overlay)
        feature_snapshots[group] = features
        group_candidate = {**candidate, "features": deepcopy(features)}
        ablation_contracts[group] = {
            intent: build_style_sample_contract(
                group_candidate,
                sample_intent=intent,
                mg_enabled=group == "mg_on",
                style_states=style_states,
            )
            for intent in ("observation", "exploration", "exploitation")
        }

    decision_ablation_group = "mg_on" if mg_enabled else "mg_off"
    contracts = ablation_contracts[decision_ablation_group]
    snapshots: list[dict[str, Any]] = []
    for group in ablation_groups:
        observation_contract = ablation_contracts[group]["observation"]
        for style in observation_contract["style_predictions"]:
            style_id = str(style.get("style_id") or "")
            pair_id = (
                "mg-pair:"
                + _canonical_sha256(
                    {
                        **current_authority,
                        "base_snapshot_sha256": base_snapshot_sha256,
                        "style_id": style_id,
                        "style_version": style.get("style_version"),
                    }
                )[:32]
            )
            snapshots.append(
                _prediction_snapshot(
                    symbol=normalized_symbol,
                    prediction_at=predicted_at,
                    reference=reference,
                    style=style,
                    decision_policy_version=str(
                        observation_contract["decision_policy_version"]
                    ),
                    authority_scope=current_authority,
                    source_snapshot_sha256=source_snapshot_sha256,
                    base_snapshot_sha256=base_snapshot_sha256,
                    pair_id=pair_id,
                    ablation_group=group,
                    feature_snapshot=feature_snapshots[group],
                    applied_marketgraph_features=(
                        mg_overlay if group == "mg_on" else {}
                    ),
                    marketgraph_overlay_status=mg_overlay_status,
                    causal_pair_eligible=causal_pair_eligible,
                )
            )
    return {
        "status": "recordable" if qualified else "recordable_data_quality_rejected",
        "market": "ashare",
        "symbol": normalized_symbol,
        "trade_date": date_key,
        "prediction_at": predicted_at,
        "as_of": predicted_at,
        "point_in_time_as_of": predicted_at,
        **current_authority,
        "source_snapshot_sha256": source_snapshot_sha256,
        "base_snapshot_sha256": base_snapshot_sha256,
        "combined_score": _number(score.get("combined")),
        "data_quality": deepcopy(candidate["data_quality"]),
        "reference_price": reference.get("price"),
        "reference_evidence": reference,
        "candidate_snapshot": candidate,
        "sample_contracts": contracts,
        "ablation_contracts": ablation_contracts,
        "decision_ablation_group": decision_ablation_group,
        "mg_ablation_pairing": {
            "paired": mg_enabled,
            "groups": list(ablation_groups),
            "base_snapshot_sha256": base_snapshot_sha256,
            "mg_off_features_physically_excluded": True,
            "marketgraph_overlay_feature_names": sorted(mg_overlay),
            "requested_marketgraph_overlay_feature_names": sorted(requested_mg_overlay),
            "base_score_provenance": (
                "explicit_base_score"
                if explicit_base_present
                else "sanitized_raw_score_without_explicit_mg_keys"
            ),
            "overlay_status": mg_overlay_status,
            "source_pairing_version": (
                str(pairing.get("pairing_version") or "")
                if isinstance(pairing, Mapping)
                else None
            ),
            "same_scoring_snapshot": same_snapshot_enhanced,
            "causal_pair_eligible": causal_pair_eligible,
        },
        "prediction_snapshots": snapshots,
        "real_trading_enabled": False,
        "live_execution_enabled": False,
    }


def _not_selected(reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": "not_selected",
        "reason": reason,
        "selected_count": 0,
        "sample_intent": "exploration",
        "real_trading_enabled": False,
        **extra,
    }


def _linked_prediction_snapshot(
    observation: Mapping[str, Any], primary_style: Any
) -> Mapping[str, Any]:
    style_id = str(primary_style or "").strip()
    decision_group = str(observation.get("decision_ablation_group") or "mg_off")
    snapshots = observation.get("prediction_snapshots")
    if not isinstance(snapshots, Sequence) or isinstance(
        snapshots, (str, bytes, bytearray)
    ):
        raise ValueError("candidate observation has no prediction snapshots")
    decision_group_snapshots = [
        snapshot
        for snapshot in snapshots
        if isinstance(snapshot, Mapping)
        and str((snapshot.get("marketgraph") or {}).get("ablation_group") or "mg_off")
        == decision_group
    ]
    if not style_id:
        if not decision_group_snapshots:
            raise ValueError(
                "candidate observation has no decision-group prediction snapshot"
            )
        return max(
            decision_group_snapshots,
            key=lambda snapshot: (
                _number(snapshot.get("raw_style_score")) or 0.0,
                str(snapshot.get("style_id") or snapshot.get("style") or ""),
            ),
        )
    matches = [
        snapshot
        for snapshot in decision_group_snapshots
        if str(snapshot.get("style_id") or snapshot.get("style") or "") == style_id
    ]
    if len(matches) != 1:
        raise ValueError(
            "candidate observation has no unique primary prediction snapshot"
        )
    return matches[0]


def select_exploration_candidate(
    observations: Sequence[Mapping[str, Any]],
    *,
    normal_candidate_symbols: Sequence[str],
    sample_debt: bool,
    existing_exploration_new_positions: int = 0,
    safety_blockers: Sequence[str] | None = None,
    epsilon: float = 0.20,
    top_k: int = 3,
    selection_seed: str | None = None,
) -> dict[str, Any]:
    """Select one safe candidate with reproducible top-K epsilon-greedy sampling."""

    if not sample_debt:
        return _not_selected("sample_debt_repaid")
    blockers = [str(value) for value in (safety_blockers or []) if str(value)]
    if blockers:
        return _not_selected("safety_gate_blocked", safety_blockers=blockers)
    if int(existing_exploration_new_positions) >= 1:
        return _not_selected("exploration_daily_position_limit_reached")
    epsilon_value = _number(epsilon)
    if epsilon_value is None or not 0.0 <= epsilon_value <= 1.0:
        raise ValueError("epsilon must be between 0 and 1")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise ValueError("top_k must be a positive integer")

    normal = {str(symbol).strip().upper() for symbol in normal_candidate_symbols}
    eligible: list[tuple[float, str, Mapping[str, Any], Mapping[str, Any]]] = []
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        symbol = str(observation.get("symbol") or "").strip().upper()
        if not symbol or symbol in normal:
            continue
        quality = observation.get("data_quality")
        if not isinstance(quality, Mapping) or quality.get("qualified") is not True:
            continue
        contracts = observation.get("sample_contracts")
        exploration = (
            contracts.get("exploration") if isinstance(contracts, Mapping) else None
        )
        intent = (
            exploration.get("portfolio_intent")
            if isinstance(exploration, Mapping)
            else None
        )
        if (
            not isinstance(intent, Mapping)
            or intent.get("action") != "exploration_candidate"
        ):
            continue
        # Relative exploration ranking uses the shared candidate score so a
        # style-specific scale cannot silently dominate cross-candidate order.
        # Style scores remain attribution features after the symbol is chosen.
        rank_score = _number(observation.get("combined_score"))
        if rank_score is None:
            continue
        eligible.append((rank_score, symbol, observation, intent))

    if not eligible:
        return _not_selected("no_data_qualified_exploration_candidate")
    eligible.sort(key=lambda row: (-row[0], row[1]))
    pool = eligible[: min(top_k, len(eligible))]
    seed_material = str(selection_seed or "").strip() or "|".join(
        "%s:%s:%s"
        % (
            row[1],
            row[2].get("prediction_at"),
            row[2].get("base_snapshot_sha256"),
        )
        for row in pool
    )
    seed_sha256 = sha256(seed_material.encode("utf-8")).hexdigest()
    epsilon_draw = int(seed_sha256[:16], 16) / float(16**16)
    random_branch = len(pool) > 1 and epsilon_draw < epsilon_value
    selected_index = (
        int(sha256((seed_material + "|choice").encode("utf-8")).hexdigest()[:16], 16)
        % len(pool)
        if random_branch
        else 0
    )
    rank_score, symbol, selected_observation, intent = pool[selected_index]
    if len(pool) == 1:
        propensity = 1.0
    elif selected_index == 0:
        propensity = (1.0 - epsilon_value) + epsilon_value / len(pool)
    else:
        propensity = epsilon_value / len(pool)
    primary_snapshot = _linked_prediction_snapshot(
        selected_observation, intent.get("primary_style")
    )
    return {
        "status": "selected",
        "reason": "sample_debt_relative_rank_exploration",
        "symbol": symbol,
        "selected_count": 1,
        "eligible_count": len(eligible),
        "relative_rank": selected_index + 1,
        "relative_quantile": round((selected_index + 1.0) / len(eligible), 6),
        "selection_score": round(rank_score, 6),
        "selection_method": "deterministic_top_k_epsilon_greedy",
        "exploration_policy_version": EXPLORATION_POLICY_VERSION,
        "epsilon": epsilon_value,
        "configured_top_k": top_k,
        "eligible_top_k_count": len(pool),
        "eligible_top_k_symbols": [row[1] for row in pool],
        "opportunity_capture_scope": {
            "claim_scope": "scanned_universe_only",
            "full_eligible_universe_recall": None,
            "full_eligible_universe_status": "unavailable_not_proven_complete",
            "scanned_universe_count": len(observations),
            "data_qualified_exploration_count": len(eligible),
            "top_k_count": len(pool),
            "recall_requires_forward_outcomes": True,
        },
        "selection_probability": round(propensity, 12),
        "propensity": round(propensity, 12),
        "selection_seed_sha256": seed_sha256,
        "selection_branch": "epsilon_random" if random_branch else "greedy",
        "absolute_mature_threshold_required": False,
        "sample_intent": "exploration",
        "primary_style": intent.get("primary_style"),
        "supporting_styles": deepcopy(intent.get("supporting_styles") or []),
        "style_scores": deepcopy(intent.get("style_scores") or {}),
        "style_versions": deepcopy(intent.get("style_versions") or {}),
        "decision_policy_version": intent.get("decision_policy_version"),
        "prediction_snapshot_id": primary_snapshot.get("snapshot_id"),
        "capital_authority_id": primary_snapshot.get("capital_authority_id"),
        "authority_generation": primary_snapshot.get("authority_generation"),
        "execution_lineage_id": primary_snapshot.get("execution_lineage_id"),
        "point_in_time_as_of": primary_snapshot.get("point_in_time_as_of"),
        "prediction_source_snapshot_sha256": primary_snapshot.get(
            "source_snapshot_sha256"
        ),
        "real_trading_enabled": False,
    }


def execution_attribution(
    observation: Mapping[str, Any],
    *,
    sample_intent: str,
    selection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return attribution only; execution authority stays with the portfolio."""

    if sample_intent not in {"exploration", "exploitation"}:
        raise ValueError("sample_intent must be exploration or exploitation")
    contracts = observation.get("sample_contracts")
    contract = contracts.get(sample_intent) if isinstance(contracts, Mapping) else None
    intent = contract.get("portfolio_intent") if isinstance(contract, Mapping) else None
    if not isinstance(intent, Mapping):
        raise ValueError("candidate observation has no %s contract" % sample_intent)
    primary_snapshot = _linked_prediction_snapshot(
        observation, intent.get("primary_style")
    )
    has_executable_primary = bool(str(intent.get("primary_style") or "").strip())
    selection_metadata: dict[str, Any] = {}
    if sample_intent == "exploration" and selection is not None:
        if selection.get("status") != "selected":
            raise ValueError("exploration selection must be selected")
        if (
            str(selection.get("symbol") or "").strip().upper()
            != str(observation.get("symbol") or "").strip().upper()
        ):
            raise ValueError("exploration selection symbol does not match observation")
        if selection.get("prediction_snapshot_id") != primary_snapshot.get(
            "snapshot_id"
        ):
            raise ValueError("exploration selection prediction snapshot mismatch")
        selection_metadata = {
            "selection_probability": selection.get("selection_probability"),
            "propensity": selection.get("propensity"),
            "exploration_policy_version": selection.get("exploration_policy_version"),
            "selection_seed_sha256": selection.get("selection_seed_sha256"),
            "selection_method": selection.get("selection_method"),
            "epsilon": selection.get("epsilon"),
            "eligible_top_k_count": selection.get("eligible_top_k_count"),
        }
    return {
        "sample_intent": sample_intent,
        "attribution_status": (
            "execution_style_attributed"
            if has_executable_primary
            else "abstain_no_executable_primary_style"
        ),
        "execution_allowed_by_style_attribution": has_executable_primary,
        "prediction_snapshot_role": (
            "primary_style"
            if has_executable_primary
            else "observation_anchor_not_execution_thesis"
        ),
        "primary_style": intent.get("primary_style"),
        "supporting_styles": deepcopy(intent.get("supporting_styles") or []),
        "style_scores": deepcopy(intent.get("style_scores") or {}),
        "style_versions": deepcopy(intent.get("style_versions") or {}),
        "decision_policy_version": intent.get("decision_policy_version"),
        "style_disagreement": deepcopy(contract.get("style_disagreement") or {}),
        "prediction_snapshot_id": primary_snapshot.get("snapshot_id"),
        "capital_authority_id": primary_snapshot.get("capital_authority_id"),
        "authority_generation": primary_snapshot.get("authority_generation"),
        "execution_lineage_id": primary_snapshot.get("execution_lineage_id"),
        "point_in_time_as_of": primary_snapshot.get("point_in_time_as_of"),
        "source_snapshot_sha256": primary_snapshot.get("source_snapshot_sha256"),
        "base_snapshot_sha256": primary_snapshot.get("base_snapshot_sha256"),
        "pair_id": primary_snapshot.get("pair_id"),
        **selection_metadata,
        "real_trading_enabled": False,
    }


def persist_candidate_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    journal_path: str | Path,
) -> dict[str, Any]:
    """Append every style snapshot before strategy/execution filtering."""

    journal = SampleJournal(journal_path)
    status_counts: Counter[str] = Counter()
    ablation_counts: Counter[str] = Counter()
    data_quality_rejected = 0
    snapshots_to_append: list[Mapping[str, Any]] = []
    for observation in observations:
        snapshots = observation.get("prediction_snapshots")
        if not isinstance(snapshots, Sequence) or isinstance(
            snapshots, (str, bytes, bytearray)
        ):
            continue
        for snapshot in snapshots:
            if not isinstance(snapshot, Mapping):
                continue
            snapshots_to_append.append(snapshot)
            marketgraph = snapshot.get("marketgraph")
            if isinstance(marketgraph, Mapping):
                ablation = str(marketgraph.get("ablation_group") or "unknown")
            else:
                ablation = "unknown"
            ablation_counts[ablation] += 1
            if snapshot.get("forward_label_eligibility") != "eligible":
                data_quality_rejected += 1
    for result in journal.append_predictions(snapshots_to_append):
        status_counts[str(result.get("status") or "unknown")] += 1
    return {
        "status": "recorded",
        "journal_path": str(Path(journal_path)),
        "candidate_observation_count": len(observations),
        "prediction_count": len(snapshots_to_append),
        "appended_count": status_counts["appended"],
        "idempotent_count": status_counts["idempotent"],
        "data_quality_rejected_count": data_quality_rejected,
        "mg_ablation_counts": dict(sorted(ablation_counts.items())),
        "real_trading_enabled": False,
    }


def _actual_filled_quantity(receipt: Mapping[str, Any]) -> int | None:
    value = (
        receipt.get("filled_quantity")
        if "filled_quantity" in receipt
        else receipt.get("filled_qty")
    )
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    quantity = int(value)
    if float(value) != float(quantity) or quantity <= 0:
        return None
    return quantity


def _actual_filled_price(receipt: Mapping[str, Any]) -> float | None:
    for field in ("filled_price", "avg_price"):
        if field in receipt:
            return _positive(receipt.get(field))
    return None


def _actual_execution_costs(receipt: Mapping[str, Any]) -> tuple[float, float] | None:
    fee_marker = next(
        (
            receipt.get(field)
            for field in ("commission", "fee_cny", "fees_cny")
            if field in receipt
        ),
        None,
    )
    if fee_marker is None or "slippage_cny" not in receipt:
        return None
    fee = _number(fee_marker)
    slippage = _number(receipt.get("slippage_cny"))
    if fee is None or slippage is None or fee < 0.0 or slippage < 0.0:
        return None
    return fee, slippage


def _lineage_value(record: Mapping[str, Any], field: str) -> Any:
    order = record.get("order") if isinstance(record.get("order"), Mapping) else {}
    receipt = (
        record.get("receipt") if isinstance(record.get("receipt"), Mapping) else {}
    )
    values = [
        source.get(field) for source in (record, order, receipt) if field in source
    ]
    normalized = [value for value in values if value not in (None, "")]
    if not normalized:
        return None
    first = normalized[0]
    if any(value != first for value in normalized[1:]):
        raise ValueError("%s_conflict" % field)
    return first


def _execution_lineage(
    record: Mapping[str, Any], expected_authority: Mapping[str, Any]
) -> dict[str, Any]:
    authority_id = str(_lineage_value(record, "capital_authority_id") or "").strip()
    generation = _lineage_value(record, "authority_generation")
    lineage_id = str(_lineage_value(record, "execution_lineage_id") or "").strip()
    prediction_snapshot_id = str(
        _lineage_value(record, "prediction_snapshot_id") or ""
    ).strip()
    if not prediction_snapshot_id:
        raise ValueError("prediction_snapshot_id_missing")
    if authority_id != expected_authority["capital_authority_id"]:
        raise ValueError("capital_authority_id_mismatch")
    if generation != expected_authority["authority_generation"]:
        raise ValueError("authority_generation_mismatch")
    if lineage_id != expected_authority["execution_lineage_id"]:
        raise ValueError("execution_lineage_id_mismatch")
    order = record.get("order") if isinstance(record.get("order"), Mapping) else {}
    receipt = (
        record.get("receipt") if isinstance(record.get("receipt"), Mapping) else {}
    )
    as_of = _aware_iso(
        receipt.get("filled_at")
        or receipt.get("point_in_time_as_of")
        or record.get("point_in_time_as_of")
        or order.get("point_in_time_as_of"),
        field="fill point_in_time_as_of",
    )
    prediction_source_sha = str(
        _lineage_value(record, "prediction_source_snapshot_sha256") or ""
    ).strip()
    if prediction_source_sha and (
        len(prediction_source_sha) != 64
        or any(
            character not in "0123456789abcdef"
            for character in prediction_source_sha.lower()
        )
    ):
        raise ValueError("prediction_source_snapshot_sha256_invalid")
    execution_source_claim = str(receipt.get("source_snapshot_sha256") or "").strip()
    if execution_source_claim and (
        len(execution_source_claim) != 64
        or any(
            character not in "0123456789abcdef"
            for character in execution_source_claim.lower()
        )
    ):
        raise ValueError("execution_source_snapshot_sha256_invalid")
    return {
        **dict(expected_authority),
        "prediction_snapshot_id": prediction_snapshot_id,
        "as_of": as_of,
        "point_in_time_as_of": as_of,
        "prediction_source_snapshot_sha256": prediction_source_sha or None,
        "execution_source_claim_sha256": execution_source_claim or None,
        "source_snapshot_sha256": _canonical_sha256(
            {
                "record": deepcopy(dict(record)),
                "receipt": deepcopy(dict(receipt)),
            }
        ),
    }


def _fill_identity(record: Mapping[str, Any], lineage: Mapping[str, Any]) -> str:
    """Build an authority-, generation-, lineage-, and prediction-scoped identity."""
    order = record.get("order") if isinstance(record.get("order"), Mapping) else {}
    receipt = (
        record.get("receipt") if isinstance(record.get("receipt"), Mapping) else {}
    )
    account = str(
        record.get("account")
        or order.get("account")
        or order.get("strategy_name")
        or "ashare_sim"
    ).strip()
    symbol = str(record.get("symbol") or order.get("ts_code") or "").strip().upper()
    side = str(order.get("side") or "").strip().lower()
    trade_id = str(
        receipt.get("trade_id")
        or receipt.get("order_id")
        or order.get("order_id")
        or ""
    ).strip()
    if not account or not symbol or side not in {"buy", "sell"} or not trade_id:
        raise ValueError(
            "fill_identity requires account, symbol, side, and trade_id (or order_id)"
        )
    return "%s|%s|%s|%s|%s|%s|%s|%s" % (
        lineage["capital_authority_id"],
        lineage["authority_generation"],
        lineage["execution_lineage_id"],
        lineage["prediction_snapshot_id"],
        account,
        symbol,
        side,
        trade_id,
    )


def _pairing_key(fill_identity: str) -> str:
    """Extract the exact authority/generation/lineage/prediction/account/symbol key."""
    return fill_identity.rsplit("|", 2)[0]


def _reason_identity(reasons: Sequence[Any]) -> str:
    normalized = "|".join(
        sorted(str(reason).strip() for reason in reasons if str(reason).strip())
    )
    return sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _read_pairing_state(
    events: Sequence[Mapping[str, Any]],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    dict[str, int],
    set[str],
    set[str],
]:
    """Rebuild buy-fill state, cumulative sold quantities, and known identities."""

    buy_fills: dict[str, dict[str, Any]] = {}
    buy_fills_by_pairing: dict[str, list[dict[str, Any]]] = {}
    cum_sold: dict[str, int] = {}
    known_stop_sell_ids: set[str] = set()
    known_round_trip_ids: set[str] = set()

    for event in events:
        eid = str(event.get("journal_event_id") or "")
        rt = str(event.get("record_type") or "")
        fi = str(event.get("fill_identity") or "")

        if rt == "fill" and fi:
            parts = fi.rsplit("|", 2)
            if len(parts) == 3 and parts[1] == "buy":
                buy_fills[fi] = dict(event)
                pk = parts[0]
                buy_fills_by_pairing.setdefault(pk, []).append(dict(event))

        if rt in ("stop", "exit_stop"):
            if fi:
                known_stop_sell_ids.add(fi)
            entry_fi = str(event.get("entry_fill_identity") or "")
            qty = int(event.get("filled_quantity") or 0)
            if entry_fi and qty:
                cum_sold[entry_fi] = cum_sold.get(entry_fi, 0) + qty

        if rt == "completed_round_trip" and eid:
            known_round_trip_ids.add(eid)

    for pk in buy_fills_by_pairing:
        buy_fills_by_pairing[pk].sort(
            key=lambda e: str(e.get("trade_date") or e.get("timestamp") or "")
        )
    return (
        buy_fills,
        buy_fills_by_pairing,
        cum_sold,
        known_stop_sell_ids,
        known_round_trip_ids,
    )


def _build_buy_fill_event(
    date_key: str,
    order_id: str,
    symbol: str,
    sample_intent: str,
    fill_identity: str,
    receipt: Mapping[str, Any],
    order: Mapping[str, Any],
    lineage: Mapping[str, Any],
    prediction_event: Mapping[str, Any],
    filled_quantity: int,
    filled_price: float,
    fee_cny: float,
    slippage_cny: float,
) -> dict[str, Any]:
    requested_quantity = order.get("quantity")
    requested = (
        int(requested_quantity)
        if isinstance(requested_quantity, (int, float))
        and not isinstance(requested_quantity, bool)
        and float(requested_quantity).is_integer()
        and requested_quantity > 0
        else None
    )
    event = {
        "event_id": "ashare_fill:%s:%s" % (date_key, order_id),
        "record_type": "fill",
        "market": "ashare",
        "symbol": symbol,
        "trade_date": date_key,
        "status": str(receipt.get("status") or "").strip().lower(),
        "sample_intent": sample_intent,
        "execution_class": "execution_eligible_simulated",
        "execution_eligible": True,
        "order_id": order_id,
        "fill_identity": fill_identity,
        "filled_quantity": filled_quantity,
        "requested_quantity": requested,
        "unfilled_quantity": (
            max(0, requested - filled_quantity) if requested is not None else None
        ),
        "filled_price": filled_price,
        "fee_cny": round(fee_cny, 4),
        "slippage_cny": round(slippage_cny, 4),
        **dict(lineage),
        "sample_cluster_id": prediction_event.get("sample_cluster_id"),
        "cluster_role": prediction_event.get("cluster_role", "origin"),
        "maturity_weight": prediction_event.get("maturity_weight", 1.0),
        "primary_style": order.get("primary_style"),
        "supporting_styles": deepcopy(order.get("supporting_styles") or []),
        "style_scores": deepcopy(order.get("style_scores") or {}),
        "style_versions": deepcopy(order.get("style_versions") or {}),
        "decision_policy_version": order.get("decision_policy_version"),
        "selection_probability": order.get("selection_probability"),
        "propensity": order.get("propensity"),
        "exploration_policy_version": order.get("exploration_policy_version"),
        "selection_seed_sha256": order.get("selection_seed_sha256"),
        "selection_method": order.get("selection_method"),
        "real_trading_enabled": False,
    }
    return _content_sha_record(event)


def _build_stop_event(
    date_key: str,
    symbol: str,
    sell_fill_identity: str,
    buy_event: Mapping[str, Any],
    alloc_qty: int,
    sell_price: float,
    alloc_fee: float,
    alloc_slip: float,
    exit_reason: str,
    sell_lineage: Mapping[str, Any],
) -> dict[str, Any]:
    event = {
        "event_id": "ashare_stop:%s:%s"
        % (sell_fill_identity, buy_event["fill_identity"]),
        "record_type": "stop",
        "market": "ashare",
        "symbol": symbol,
        "trade_date": date_key,
        "sample_intent": str(buy_event.get("sample_intent") or "exploration"),
        "fill_identity": sell_fill_identity,
        "entry_fill_identity": buy_event["fill_identity"],
        "filled_quantity": alloc_qty,
        "filled_price": sell_price,
        "fee_cny": round(alloc_fee, 4),
        "slippage_cny": round(alloc_slip, 4),
        "exit_reason": exit_reason,
        **{
            key: buy_event.get(key)
            for key in (
                "capital_authority_id",
                "authority_generation",
                "execution_lineage_id",
                "prediction_snapshot_id",
                "sample_cluster_id",
                "cluster_role",
                "maturity_weight",
                "selection_probability",
                "propensity",
                "exploration_policy_version",
                "selection_seed_sha256",
                "selection_method",
            )
        },
        "as_of": sell_lineage.get("as_of"),
        "point_in_time_as_of": sell_lineage.get("point_in_time_as_of"),
        "source_snapshot_sha256": sell_lineage.get("source_snapshot_sha256"),
        "prediction_source_snapshot_sha256": buy_event.get(
            "prediction_source_snapshot_sha256"
        ),
        "primary_style": buy_event.get("primary_style"),
        "supporting_styles": deepcopy(buy_event.get("supporting_styles") or []),
        "style_scores": deepcopy(buy_event.get("style_scores") or {}),
        "style_versions": deepcopy(buy_event.get("style_versions") or {}),
        "decision_policy_version": buy_event.get("decision_policy_version"),
        "real_trading_enabled": False,
    }
    return _content_sha_record(event)


def _build_round_trip_event(
    buy_event: Mapping[str, Any],
    exit_fill_identities: Sequence[str],
    gross_pnl: float,
    total_fee: float,
    total_slip: float,
    net_pnl: float,
    closed_at: str,
) -> dict[str, Any]:
    buy_fi = buy_event["fill_identity"]
    source_snapshot_sha256 = _canonical_sha256(
        {
            "entry_content_sha256": buy_event.get("content_sha256"),
            "exit_fill_identities": list(exit_fill_identities),
            "closed_at": closed_at,
        }
    )
    entry_quantity = int(buy_event.get("filled_quantity") or 0)
    entry_price = float(buy_event.get("filled_price") or 0.0)
    event = {
        "event_id": "ashare_round_trip:%s" % buy_fi,
        "record_type": "completed_round_trip",
        "round_trip_complete": True,
        "completed": True,
        "execution_eligible": True,
        "costs_cover": "round_trip",
        "cost_model_version": "actual_execution_costs_v1",
        "market": "ashare",
        "symbol": buy_event.get("symbol"),
        "trade_date": str(closed_at)[:10].replace("-", ""),
        "closed_at": closed_at,
        "as_of": closed_at,
        "point_in_time_as_of": closed_at,
        "source_snapshot_sha256": source_snapshot_sha256,
        "sample_intent": str(buy_event.get("sample_intent") or "exploration"),
        "entry_fill_identity": buy_fi,
        "exit_fill_identities": list(exit_fill_identities),
        "entry_quantity": entry_quantity,
        "entry_price": entry_price,
        "notional_cny": round(entry_quantity * entry_price, 4),
        "gross_pnl_cny": round(gross_pnl, 4),
        "fee_cny": round(total_fee, 4),
        "slippage_cny": round(total_slip, 4),
        "net_pnl_cny": round(net_pnl, 4),
        **{
            key: buy_event.get(key)
            for key in (
                "capital_authority_id",
                "authority_generation",
                "execution_lineage_id",
                "prediction_snapshot_id",
                "sample_cluster_id",
                "cluster_role",
                "maturity_weight",
                "selection_probability",
                "propensity",
                "exploration_policy_version",
                "selection_seed_sha256",
                "selection_method",
                "prediction_source_snapshot_sha256",
            )
        },
        "primary_style": buy_event.get("primary_style"),
        "supporting_styles": deepcopy(buy_event.get("supporting_styles") or []),
        "style_scores": deepcopy(buy_event.get("style_scores") or {}),
        "style_versions": deepcopy(buy_event.get("style_versions") or {}),
        "decision_policy_version": buy_event.get("decision_policy_version"),
        "real_trading_enabled": False,
    }
    return _content_sha_record(event)


def _build_chain_validation_event(
    sell_fill_identity: str,
    symbol: str,
    date_key: str,
    pairing_key: str,
    reason: str,
    receipt: Mapping[str, Any],
    lineage: Mapping[str, Any],
    filled_quantity: int,
    filled_price: float,
) -> dict[str, Any]:
    event = {
        "event_id": "ashare_chain_validation:%s" % sell_fill_identity,
        "record_type": "chain_validation",
        "market": "ashare",
        "symbol": symbol,
        "trade_date": date_key,
        "sample_classification": "chain_validation",
        "fill_identity": sell_fill_identity,
        "side": "sell",
        "pairing_status": "rejected",
        "reason": reason,
        "pairing_key": pairing_key,
        "filled_quantity": filled_quantity,
        "filled_price": filled_price,
        **dict(lineage),
        "real_trading_enabled": False,
    }
    return _content_sha_record(event)


def persist_simulation_outcomes(
    *,
    journal_path: str | Path,
    trade_date: str,
    records: Sequence[Mapping[str, Any]],
    risk_rejections: Sequence[Mapping[str, Any]],
    authority_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist verified fills and risk rejects, pairing sells with buys for
    stop/exit and completed-round-trip events without mixing sample layers."""

    date_key, _ = _normalized_date(trade_date)
    journal = SampleJournal(journal_path)
    current_authority = _current_authority_scope(authority_scope)

    # ---- Phase 1: read existing journal state for pairing ---------------
    existing_events = journal.read_events()
    prediction_events = {
        str(event.get("snapshot_id") or ""): event
        for event in existing_events
        if event.get("journal_event_type") == "prediction_snapshot"
        and str(event.get("snapshot_id") or "")
    }
    (
        buy_fills,
        buy_fills_by_pairing,
        cum_sold,
        known_stop_sell_ids,
        known_round_trip_ids,
    ) = _read_pairing_state(existing_events)

    # In-flight copies that merge existing state + this batch
    inflight_buys_by_pairing: dict[str, list[dict[str, Any]]] = {
        pk: list(events) for pk, events in buy_fills_by_pairing.items()
    }
    inflight_cum_sold: dict[str, int] = dict(cum_sold)
    inflight_stop_sell_ids: set[str] = set(known_stop_sell_ids)

    # ---- Phase 2: process records, build new events --------------------
    new_samples: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    skipped: list[dict[str, Any]] = []

    # Track buys that become fully closed in this batch so we can emit
    # completed_round_trip events after all stop events are built.
    fully_closed_buy_fis: list[str] = []
    # Collect NEW stop events for round-trip PnL computation.
    new_stop_events: list[dict[str, Any]] = []

    for record in records:
        if not isinstance(record, Mapping):
            continue
        order = record.get("order") if isinstance(record.get("order"), Mapping) else {}
        receipt = (
            record.get("receipt") if isinstance(record.get("receipt"), Mapping) else {}
        )

        receipt_status = str(receipt.get("status") or "").strip().lower()
        if receipt_status not in {"filled", "partial"}:
            continue

        symbol = str(record.get("symbol") or order.get("ts_code") or "").strip().upper()
        order_id = str(order.get("order_id") or receipt.get("order_id") or "").strip()

        if receipt.get("execution_eligible") is not True:
            skipped.append(
                {
                    "symbol": symbol,
                    "order_id": order_id,
                    "reason": "fill_not_execution_eligible",
                }
            )
            continue

        if not symbol or not order_id:
            skipped.append(
                {
                    "symbol": symbol,
                    "order_id": order_id,
                    "reason": "fill_identity_missing",
                }
            )
            continue

        actual_quantity = _actual_filled_quantity(receipt)
        if actual_quantity is None:
            skipped.append(
                {
                    "symbol": symbol,
                    "order_id": order_id,
                    "reason": "actual_filled_quantity_missing",
                }
            )
            continue
        actual_price = _actual_filled_price(receipt)
        if actual_price is None:
            skipped.append(
                {
                    "symbol": symbol,
                    "order_id": order_id,
                    "reason": "actual_filled_price_missing",
                }
            )
            continue
        actual_costs = _actual_execution_costs(receipt)
        if actual_costs is None:
            skipped.append(
                {
                    "symbol": symbol,
                    "order_id": order_id,
                    "reason": "actual_fee_or_slippage_missing",
                }
            )
            continue
        fee_cny, slippage_cny = actual_costs

        try:
            lineage = _execution_lineage(record, current_authority)
        except ValueError as exc:
            skipped.append(
                {
                    "symbol": symbol,
                    "order_id": order_id,
                    "reason": str(exc),
                }
            )
            continue
        prediction_event = prediction_events.get(str(lineage["prediction_snapshot_id"]))
        if prediction_event is None:
            skipped.append(
                {
                    "symbol": symbol,
                    "order_id": order_id,
                    "reason": "prediction_snapshot_not_found",
                }
            )
            continue
        if any(
            prediction_event.get(field) != lineage.get(field)
            for field in (
                "capital_authority_id",
                "authority_generation",
                "execution_lineage_id",
            )
        ):
            skipped.append(
                {
                    "symbol": symbol,
                    "order_id": order_id,
                    "reason": "prediction_lineage_mismatch",
                }
            )
            continue
        try:
            prediction_as_of = datetime.fromisoformat(
                str(
                    prediction_event.get("point_in_time_as_of")
                    or prediction_event.get("prediction_at")
                    or ""
                ).replace("Z", "+00:00")
            )
            fill_as_of = datetime.fromisoformat(
                str(lineage["point_in_time_as_of"]).replace("Z", "+00:00")
            )
            if fill_as_of < prediction_as_of:
                raise ValueError("fill_before_prediction")
        except (TypeError, ValueError):
            skipped.append(
                {
                    "symbol": symbol,
                    "order_id": order_id,
                    "reason": "fill_before_or_incomparable_to_prediction",
                }
            )
            continue

        try:
            fi = _fill_identity(record, lineage)
        except ValueError:
            skipped.append(
                {
                    "symbol": symbol,
                    "order_id": order_id,
                    "reason": "fill_identity_incomplete",
                }
            )
            continue

        side = str(order.get("side") or "buy").strip().lower()

        # -- BUY -----------------------------------------------------------
        if side == "buy":
            sample_intent = str(order.get("sample_intent") or "").strip().lower()
            if sample_intent not in {"exploration", "exploitation"}:
                skipped.append(
                    {
                        "symbol": symbol,
                        "order_id": order_id,
                        "reason": "fill_sample_intent_unclassified",
                    }
                )
                continue
            if sample_intent == "exploration":
                probability = _number(order.get("selection_probability"))
                propensity = _number(order.get("propensity"))
                if (
                    probability is None
                    or propensity is None
                    or not 0.0 < probability <= 1.0
                    or abs(probability - propensity) > 1e-12
                    or str(order.get("exploration_policy_version") or "")
                    != EXPLORATION_POLICY_VERSION
                ):
                    skipped.append(
                        {
                            "symbol": symbol,
                            "order_id": order_id,
                            "reason": "exploration_propensity_missing_or_invalid",
                        }
                    )
                    continue
            event = _build_buy_fill_event(
                date_key,
                order_id,
                symbol,
                sample_intent,
                fi,
                receipt,
                order,
                lineage,
                prediction_event,
                actual_quantity,
                actual_price,
                fee_cny,
                slippage_cny,
            )
            new_samples.append(event)
            counts["%s_fill" % sample_intent] += 1
            pk = _pairing_key(fi)
            inflight_buys_by_pairing.setdefault(pk, []).append(event)
            continue

        # -- SELL ----------------------------------------------------------
        if side == "sell":
            if fi in inflight_stop_sell_ids:
                counts["idempotent_execution_outcome"] += 1
                continue

            pk = _pairing_key(fi)
            open_buys = inflight_buys_by_pairing.get(pk, [])

            if not open_buys:
                new_samples.append(
                    _build_chain_validation_event(
                        fi,
                        symbol,
                        date_key,
                        pk,
                        "no_open_buy_fill_for_exact_lineage",
                        receipt,
                        lineage,
                        actual_quantity,
                        actual_price,
                    )
                )
                counts["pairing_rejection"] += 1
                inflight_stop_sell_ids.add(fi)
                continue

            sell_qty = actual_quantity
            sell_price = actual_price
            sell_fee = fee_cny
            sell_slip = slippage_cny
            exit_reason = str(order.get("exit_reason") or "").strip()

            remaining = sell_qty
            allocated: list[tuple[dict[str, Any], int]] = []

            for buy_event in open_buys:
                if remaining <= 0:
                    break
                buy_fi = buy_event["fill_identity"]
                buy_qty = int(buy_event.get("filled_quantity") or 0)
                already_sold = inflight_cum_sold.get(buy_fi, 0)
                open_qty = max(0, buy_qty - already_sold)
                if open_qty <= 0:
                    continue
                alloc_qty = min(remaining, open_qty)
                allocated.append((buy_event, alloc_qty))
                inflight_cum_sold[buy_fi] = already_sold + alloc_qty
                remaining -= alloc_qty

            if not allocated:
                new_samples.append(
                    _build_chain_validation_event(
                        fi,
                        symbol,
                        date_key,
                        pk,
                        "no_open_buy_fill_for_exact_lineage",
                        receipt,
                        lineage,
                        actual_quantity,
                        actual_price,
                    )
                )
                counts["pairing_rejection"] += 1
                inflight_stop_sell_ids.add(fi)
                continue

            unfinished = 0
            for buy_event, alloc_qty in allocated:
                buy_fi = buy_event["fill_identity"]
                ratio = alloc_qty / sell_qty if sell_qty > 0 else 1.0
                stop_event = _build_stop_event(
                    date_key,
                    symbol,
                    fi,
                    buy_event,
                    alloc_qty,
                    sell_price,
                    sell_fee * ratio,
                    sell_slip * ratio,
                    exit_reason,
                    lineage,
                )
                new_samples.append(stop_event)
                new_stop_events.append(stop_event)
                counts["exit_stop"] += 1

                buy_qty = int(buy_event.get("filled_quantity") or 0)
                total_sold = inflight_cum_sold.get(buy_fi, 0)
                if total_sold >= buy_qty:
                    if buy_fi not in fully_closed_buy_fis:
                        fully_closed_buy_fis.append(buy_fi)
                else:
                    unfinished += 1

            if remaining > 0:
                new_samples.append(
                    _build_chain_validation_event(
                        fi,
                        symbol,
                        date_key,
                        pk,
                        "sell_quantity_exceeds_exact_open_quantity",
                        receipt,
                        lineage,
                        remaining,
                        actual_price,
                    )
                )
                counts["pairing_rejection"] += 1

            counts["unfinished_exit"] += unfinished
            inflight_stop_sell_ids.add(fi)
            continue

        # Unknown side
        skipped.append(
            {"symbol": symbol, "order_id": order_id, "reason": "unknown_side"}
        )

    # ---- Phase 3: build completed_round_trip events ----------------------
    # Reconstruct all stop events for each fully-closed buy:
    # existing stop events + new stop events from this batch.
    all_stop_events: dict[str, list[dict[str, Any]]] = {}
    # Load existing stop events
    for event in existing_events:
        if str(event.get("record_type") or "") in ("stop", "exit_stop"):
            entry_fi = str(event.get("entry_fill_identity") or "")
            if entry_fi:
                all_stop_events.setdefault(entry_fi, []).append(dict(event))
    # Merge new stop events
    for stop in new_stop_events:
        entry_fi = str(stop.get("entry_fill_identity") or "")
        if entry_fi:
            all_stop_events.setdefault(entry_fi, []).append(stop)

    # Buy events indexed for lookup (existing + new buy fills from this batch)
    all_buy_events: dict[str, dict[str, Any]] = dict(buy_fills)
    for sample in new_samples:
        if sample.get("record_type") == "fill" and sample.get("fill_identity"):
            all_buy_events[sample["fill_identity"]] = sample

    for buy_fi in fully_closed_buy_fis:
        rt_event_id = "ashare_round_trip:%s" % buy_fi
        if rt_event_id in known_round_trip_ids:
            continue

        buy_event = all_buy_events.get(buy_fi)
        if buy_event is None:
            continue

        stops = all_stop_events.get(buy_fi, [])
        exit_fis: list[str] = []
        total_gross = 0.0
        total_fee = float(buy_event.get("fee_cny") or 0)
        total_slip = float(buy_event.get("slippage_cny") or 0)
        buy_price = float(buy_event.get("filled_price") or 0)
        last_closed_at = "%s-%s-%sT15:00:00+08:00" % (
            date_key[:4],
            date_key[4:6],
            date_key[6:],
        )

        for stop in stops:
            exit_fi = str(stop.get("fill_identity") or "")
            if exit_fi and exit_fi not in exit_fis:
                exit_fis.append(exit_fi)
            qty = int(stop.get("filled_quantity") or 0)
            price = float(stop.get("filled_price") or 0)
            total_gross += qty * (price - buy_price)
            total_fee += float(stop.get("fee_cny") or 0)
            total_slip += float(stop.get("slippage_cny") or 0)
            stop_as_of = str(
                stop.get("point_in_time_as_of") or stop.get("as_of") or last_closed_at
            )
            if stop_as_of > last_closed_at:
                last_closed_at = stop_as_of

        net_pnl = total_gross - total_fee - total_slip
        rt_event = _build_round_trip_event(
            buy_event,
            exit_fis,
            total_gross,
            total_fee,
            total_slip,
            net_pnl,
            last_closed_at,
        )
        new_samples.append(rt_event)
        counts["completed_round_trip"] += 1
        known_round_trip_ids.add(rt_event_id)

    # ---- Phase 4: risk rejections (unchanged) ---------------------------
    for rejection in risk_rejections:
        if not isinstance(rejection, Mapping):
            continue
        symbol = (
            str(rejection.get("symbol") or rejection.get("ts_code") or "")
            .strip()
            .upper()
        )
        raw_reasons = rejection.get("reasons")
        reasons = (
            [str(reason).strip() for reason in raw_reasons if str(reason).strip()]
            if isinstance(raw_reasons, Sequence)
            and not isinstance(raw_reasons, (str, bytes, bytearray))
            else []
        )
        if not symbol or not reasons:
            skipped.append(
                {"symbol": symbol, "reason": "risk_reject_missing_specific_reason"}
            )
            continue
        rejection_event = {
            "event_id": "ashare_risk_reject:%s:%s:%s"
            % (date_key, symbol, _reason_identity(reasons)),
            "record_type": "risk_reject",
            "market": "ashare",
            "symbol": symbol,
            "style": rejection.get("primary_style"),
            "trade_date": date_key,
            "status": "risk_rejected",
            "sample_intent": str(rejection.get("sample_intent") or "observation"),
            "reject_reason": reasons[0],
            "reject_reasons": reasons,
            "primary_style": rejection.get("primary_style"),
            "supporting_styles": deepcopy(rejection.get("supporting_styles") or []),
            **current_authority,
            "as_of": str(
                rejection.get("as_of")
                or "%s-%s-%sT15:00:00+08:00"
                % (date_key[:4], date_key[4:6], date_key[6:])
            ),
            "point_in_time_as_of": str(
                rejection.get("point_in_time_as_of")
                or rejection.get("as_of")
                or "%s-%s-%sT15:00:00+08:00"
                % (date_key[:4], date_key[4:6], date_key[6:])
            ),
            "source_snapshot_sha256": _canonical_sha256(dict(rejection)),
            "real_trading_enabled": False,
        }
        new_samples.append(_content_sha_record(rejection_event))
        counts["risk_reject"] += 1

    # ---- Phase 5: atomically append all new events ----------------------
    if new_samples:
        journal.append_samples(new_samples, expected_event_count=len(existing_events))

    return {
        "status": "recorded",
        "journal_path": str(Path(journal_path)),
        "exploration_fill_count": counts["exploration_fill"],
        "exploitation_fill_count": counts["exploitation_fill"],
        "exit_stop_count": counts["exit_stop"],
        "completed_round_trip_count": counts["completed_round_trip"],
        "unfinished_exit_count": counts["unfinished_exit"],
        "pairing_rejection_count": counts["pairing_rejection"],
        "idempotent_execution_outcome_count": counts["idempotent_execution_outcome"],
        "risk_reject_count": counts["risk_reject"],
        "skipped_outcome_count": len(skipped),
        "skipped_outcomes": skipped,
        "real_trading_enabled": False,
    }


__all__ = [
    "build_candidate_observation",
    "build_hypothesis_id",
    "build_research_hypothesis",
    "execution_attribution",
    "persist_candidate_observations",
    "persist_simulation_outcomes",
    "select_exploration_candidate",
]
