"""Pure A-share multi-style research and counterfactual sample contract.

This module deliberately has no execution, account, ledger, queue, or file I/O
dependency.  A style may request risk from the one A-share execution account,
but it can never allocate capital or create an order here.
"""

from __future__ import annotations

import math
from collections import Counter
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Tuple

from shared.execution.execution_lineage import (
    ASHARE_AUTHORITY_GENERATION,
    ASHARE_CAPITAL_AUTHORITY_ID,
    ASHARE_EXECUTION_LINEAGE_ID,
)
from shared.execution.execution_reality import (
    ASHARE_EXECUTION_REALITY_VERSION,
    ashare_execution_reality,
)


SCHEMA_VERSION = "ashare-style-samples-v2"
DEFAULT_DECISION_POLICY_VERSION = "ashare-single-execution-account-intent-v3"

STYLE_DEFINITIONS = (
    {
        "style_id": "trend_breakout_strength_continuation",
        "style_version": "2.0.0",
        "lifecycle_status": "challenger",
        "hypothesis_family": "trend_breakout_and_strength_continuation",
        "entry_thesis": "Price strength, trend persistence and volume confirmation align.",
        "exit_thesis": "Exit when trend confirmation fails or the holding horizon expires.",
        "holding_horizon": {"min_trading_days": 1, "max_trading_days": 5},
    },
    {
        "style_id": "pullback_or_short_reversal",
        "style_version": "2.0.0",
        "lifecycle_status": "challenger",
        "hypothesis_family": "pullback_mean_reversion_and_short_reversal_risk",
        "entry_thesis": "A controlled pullback has independent reversal confirmation.",
        "exit_thesis": "Exit if reversal confirmation disappears or the rebound completes.",
        "holding_horizon": {"min_trading_days": 1, "max_trading_days": 3},
    },
    {
        "style_id": "event_catalyst_with_price_confirmation",
        "style_version": "2.0.0",
        "lifecycle_status": "challenger",
        "hypothesis_family": "event_catalyst_conditioned_on_price_confirmation",
        "entry_thesis": "A traceable catalyst is confirmed by observable price behaviour.",
        "exit_thesis": "Exit when the catalyst is invalidated or price confirmation reverses.",
        "holding_horizon": {"min_trading_days": 1, "max_trading_days": 5},
    },
    {
        "style_id": "defensive_low_volatility_abstain",
        "style_version": "2.0.0",
        "lifecycle_status": "baseline",
        "hypothesis_family": "defensive_low_volatility_abstention_baseline",
        "entry_thesis": "Baseline observes whether avoiding risk is preferable to taking it.",
        "exit_thesis": "The baseline remains uninvested and therefore has no trade exit.",
        "holding_horizon": {"min_trading_days": 0, "max_trading_days": 0},
    },
)

VALID_SAMPLE_INTENTS = {"observation", "exploration", "exploitation"}
VALID_LIFECYCLE_STATUSES = {
    "champion",
    "challenger",
    "baseline",
    "paused",
    "deprecated",
}
FORWARD_LABEL_HORIZONS = ["m30", "m60", "close", "next_day", "3d", "5d"]

# ---------------------------------------------------------------------------
# Versioned A-share conservative cost model
# ---------------------------------------------------------------------------

COST_MODEL_VERSION = ASHARE_EXECUTION_REALITY_VERSION
ASHARE_LOT_SIZE = 100
_EXECUTION_REALITY = ashare_execution_reality()
ASHARE_COMMISSION_RATE = _EXECUTION_REALITY.commission_bps / 10_000
ASHARE_MIN_COMMISSION = _EXECUTION_REALITY.min_commission_cny
ASHARE_STAMP_DUTY_RATE = _EXECUTION_REALITY.stamp_duty_sell_bps / 10_000
ASHARE_TRANSFER_FEE_RATE = _EXECUTION_REALITY.transfer_fee_bps / 10_000
ASHARE_SLIPPAGE_BPS = _EXECUTION_REALITY.conservative_label_slippage_bps_per_side


def compute_ashare_conservative_costs(
    reference_price: float,
    *,
    lot_size: int = ASHARE_LOT_SIZE,
) -> dict[str, Any]:
    """Compute versioned conservative round-trip costs for one A-share lot.

    Returns ``round_trip_fee_bps``, ``round_trip_slippage_bps``,
    ``cost_model_version``, and ``cost_basis_notional_cny``.  Raises
    ``ValueError`` when *reference_price* is missing or non-positive.

    Sourced from the current :class:`ExecutionRealityModel`:
    - provisional commission = max(notional * 0.025%, 5 CNY), pending the
      verified broker contract/statement;
    - stamp duty = notional * 0.05% on sells only;
    - transfer fee = notional * 0.001% on each side;
    - conservative counterfactual slippage = 5 bps per side.
    """
    if not isinstance(reference_price, (int, float)):
        raise ValueError("reference_price must be numeric")
    price = float(reference_price)
    if price <= 0 or not math.isfinite(price):
        raise ValueError("reference_price must be positive and finite")
    notional = price * float(lot_size)
    if notional <= 0 or not math.isfinite(notional):
        raise ValueError("notional must be positive and finite")

    reality = ashare_execution_reality()
    buy_fees = reality.calculate_fees("buy", notional)
    sell_fees = reality.calculate_fees("sell", notional)
    buy_commission = float(buy_fees["commission"])
    sell_commission = float(sell_fees["commission"])
    stamp_duty = float(sell_fees["stamp_duty"])
    buy_transfer_fee = float(buy_fees["transfer_fee"])
    sell_transfer_fee = float(sell_fees["transfer_fee"])
    total_fee_cny = float(buy_fees["total"]) + float(sell_fees["total"])
    round_trip_fee_bps = round(total_fee_cny / notional * 10_000, 4)

    buy_slippage_cny = notional * ASHARE_SLIPPAGE_BPS / 10_000
    sell_slippage_cny = notional * ASHARE_SLIPPAGE_BPS / 10_000
    total_slippage_cny = buy_slippage_cny + sell_slippage_cny
    round_trip_slippage_bps = round(total_slippage_cny / notional * 10_000, 4)

    return {
        "round_trip_fee_bps": round_trip_fee_bps,
        "round_trip_slippage_bps": round_trip_slippage_bps,
        "cost_model_version": COST_MODEL_VERSION,
        "cost_basis_notional_cny": round(notional, 4),
        "cost_basis_reference_price": round(price, 4),
        "lot_size": lot_size,
        "buy_commission_cny": round(buy_commission, 4),
        "sell_commission_cny": round(sell_commission, 4),
        "stamp_duty_cny": round(stamp_duty, 4),
        "buy_transfer_fee_cny": round(buy_transfer_fee, 4),
        "sell_transfer_fee_cny": round(sell_transfer_fee, 4),
        "commission_schedule_status": reality.commission_schedule_status,
        "commission_schedule_version": reality.commission_schedule_version,
        "execution_reality_model_version": reality.model_version,
        "buy_slippage_cny": round(buy_slippage_cny, 4),
        "sell_slippage_cny": round(sell_slippage_cny, 4),
    }


def _marketgraph_contract(enabled: bool) -> Dict[str, Any]:
    return {
        "enabled": bool(enabled),
        "ablation_group": "mg_on" if enabled else "mg_off",
        "role": "optional_research_enhancement",
    }


def _normalise_identity(candidate: Mapping[str, Any]) -> Tuple[str, str, str]:
    symbol = str(candidate.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("candidate symbol is required")
    raw_trade_date = str(candidate.get("trade_date") or "").strip()
    for date_format in ("%Y-%m-%d", "%Y%m%d"):
        try:
            trade_date = (
                datetime.strptime(raw_trade_date, date_format).date().isoformat()
            )
            break
        except ValueError:
            continue
    else:
        raise ValueError("trade_date must be YYYY-MM-DD or YYYYMMDD")
    return symbol, trade_date, "ashare:%s:%s" % (trade_date, symbol)


def _risk_budget_request(direction: str) -> Dict[str, Any]:
    return {
        "request_only": True,
        "single_market_portfolio_required": True,
        "allocated_capital_cny": None,
        "requested_risk_fraction": None,
        "requested_notional_fraction": None,
        "requested_risk_tier": "minimal_candidate"
        if direction == "long_bias"
        else "none",
    }


def _feature(candidate: Mapping[str, Any], name: str, default: float = 0.0) -> float:
    features = candidate.get("features")
    source = features if isinstance(features, Mapping) else candidate
    try:
        value = float(source.get(name, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return max(0.0, min(1.0, value))


def _data_quality(candidate: Mapping[str, Any]) -> Tuple[bool, Optional[str]]:
    quality = candidate.get("data_quality")
    if not isinstance(quality, Mapping):
        return False, "candidate_data_quality_unverified"
    if quality.get("qualified") is not True:
        return False, str(quality.get("reason") or "candidate_data_quality_unreliable")
    return True, None


def _style_score_and_trigger(
    style_id: str, candidate: Mapping[str, Any]
) -> Tuple[float, bool, str]:
    liquidity = _feature(candidate, "liquidity_score")
    if style_id == "trend_breakout_strength_continuation":
        score = (
            0.35 * _feature(candidate, "breakout_strength")
            + 0.30 * _feature(candidate, "trend_strength")
            + 0.20 * _feature(candidate, "volume_confirmation")
            + 0.15 * liquidity
        )
        return score, score >= 0.62, "trend_confirmation_below_threshold"
    if style_id == "pullback_or_short_reversal":
        score = (
            0.35 * _feature(candidate, "pullback_quality")
            + 0.35 * _feature(candidate, "reversal_confirmation")
            + 0.15 * (1.0 - _feature(candidate, "overextension_risk"))
            + 0.15 * liquidity
        )
        return score, score >= 0.62, "pullback_reversal_not_confirmed"
    if style_id == "event_catalyst_with_price_confirmation":
        catalyst = _feature(candidate, "event_catalyst_score")
        price_confirmation = _feature(candidate, "price_confirmation")
        score = 0.45 * catalyst + 0.35 * price_confirmation + 0.20 * liquidity
        triggered = catalyst >= 0.55 and price_confirmation >= 0.55 and score >= 0.62
        return score, triggered, "event_or_price_confirmation_missing"

    score = (
        0.45 * (1.0 - _feature(candidate, "realized_volatility"))
        + 0.35 * _feature(candidate, "downside_resilience")
        + 0.20 * liquidity
    )
    return score, False, "defensive_abstain_baseline"


def _uncalibrated_return_prior(direction: str, score: float) -> Dict[str, Any]:
    if direction != "long_bias":
        return {
            "unit": "decimal_return",
            "p10": 0.0,
            "p50": 0.0,
            "p90": 0.0,
            "model_state": "uncalibrated_research_prior",
            "decision_eligible": False,
        }
    median = max(0.0, (score - 0.50) * 0.08)
    return {
        "unit": "decimal_return",
        "p10": round(median - 0.04, 6),
        "p50": round(median, 6),
        "p90": round(median + 0.05, 6),
        "model_state": "uncalibrated_research_prior",
        "decision_eligible": False,
    }


def _evaluate_style(
    definition: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    lifecycle_status: str,
    mg_enabled: bool,
) -> Dict[str, Any]:
    style_id = str(definition["style_id"])
    quality_ok, quality_reason = _data_quality(candidate)
    score, triggered, abstain_reason = _style_score_and_trigger(style_id, candidate)
    paused = lifecycle_status in {"paused", "deprecated"}
    # Lifecycle controls selection, never the counterfactual prediction itself.
    direction = "long_bias" if quality_ok and triggered else "abstain"
    reject_reason = quality_reason
    if quality_ok and paused:
        reject_reason = "style_%s" % lifecycle_status
    if direction == "long_bias":
        abstain_reason = None
    channel_eligibility = {
        "observation": quality_ok,
        "exploration": (
            quality_ok
            and direction == "long_bias"
            and lifecycle_status in {"champion", "challenger"}
        ),
        "exploitation": (
            quality_ok and direction == "long_bias" and lifecycle_status == "champion"
        ),
    }

    return {
        **definition,
        "lifecycle_status": lifecycle_status,
        "prediction": {
            "direction": direction,
            "raw_style_score": round(score, 6),
            "score_semantics": "uncalibrated_heuristic",
            "calibrated_probability": None,
            "probability_model_state": "not_calibrated",
        },
        "uncalibrated_return_prior": _uncalibrated_return_prior(direction, score),
        "risk_budget_request": _risk_budget_request(direction),
        "abstain_reason": abstain_reason,
        "reject_reason": reject_reason,
        "marketgraph": _marketgraph_contract(mg_enabled),
        "channel_eligibility": channel_eligibility,
        "forward_label_request": {
            "request_only": True,
            "eligible": quality_ok,
            "horizons": list(FORWARD_LABEL_HORIZONS),
            "rejection_reason": None if quality_ok else quality_reason,
        },
    }


def _build_disagreement(predictions: list) -> Dict[str, Any]:
    directions = {
        str(row["style_id"]): str(row["prediction"]["direction"]) for row in predictions
    }
    counts = Counter(directions.values())
    scores = [float(row["prediction"]["raw_style_score"]) for row in predictions]
    style_ids = list(directions)
    conflict_pairs = [
        [left, right]
        for index, left in enumerate(style_ids)
        for right in style_ids[index + 1 :]
        if directions[left] != directions[right]
    ]
    return {
        "has_disagreement": len(counts) > 1,
        "direction_vote_counts": dict(sorted(counts.items())),
        "style_directions": directions,
        "conflicting_style_pairs": conflict_pairs,
        "score_range": round(max(scores) - min(scores), 6) if scores else 0.0,
    }


def _build_portfolio_intent(
    predictions: list,
    *,
    sample_intent: str,
    decision_policy_version: str,
    idempotency_key: str,
) -> Dict[str, Any]:
    active = [
        row
        for row in predictions
        if row["channel_eligibility"][sample_intent]
        and row["lifecycle_status"] not in {"paused", "deprecated", "baseline"}
    ]
    if active:
        primary = max(
            active, key=lambda row: float(row["prediction"]["raw_style_score"])
        )
        primary_direction = primary["prediction"]["direction"]
        primary_style = primary["style_id"]
        supporting = [
            row["style_id"]
            for row in active
            if row["style_id"] != primary_style
            and row["prediction"]["direction"] == primary_direction
        ]
    else:
        primary_style = None
        supporting = []
    if sample_intent == "observation":
        action = "observe"
    elif active:
        action = "%s_candidate" % sample_intent
    else:
        action = "abstain"
    return {
        "action": action,
        "sample_intent": sample_intent,
        "primary_style": primary_style,
        "supporting_styles": supporting,
        "eligible_styles": [row["style_id"] for row in active],
        "style_scores": {
            row["style_id"]: row["prediction"]["raw_style_score"] for row in predictions
        },
        "style_versions": {
            row["style_id"]: row["style_version"] for row in predictions
        },
        "decision_policy_version": decision_policy_version,
        "idempotency_key": idempotency_key,
        "creates_order": False,
        "execution_authority": "none_research_only",
    }


def _sample_channel(sample_intent: str) -> Dict[str, Any]:
    return {
        "name": sample_intent,
        "performance_bucket": {
            "observation": "observation_counterfactual",
            "exploration": "exploration_simulated",
            "exploitation": "exploitation_simulated",
        }[sample_intent],
        "may_request_simulated_fill": sample_intent != "observation",
        "fill_authority": "external_single_ashare_account_only",
    }


def _authority_contract(candidate: Mapping[str, Any]) -> Dict[str, Any]:
    authority_id = str(candidate.get("capital_authority_id") or "").strip()
    generation = candidate.get("authority_generation")
    lineage_id = str(candidate.get("execution_lineage_id") or "").strip()
    if authority_id != ASHARE_CAPITAL_AUTHORITY_ID:
        raise ValueError("candidate capital_authority_id is not current")
    if generation != ASHARE_AUTHORITY_GENERATION:
        raise ValueError("candidate authority_generation is not current")
    if lineage_id != ASHARE_EXECUTION_LINEAGE_ID:
        raise ValueError("candidate execution_lineage_id is not current")
    return {
        "model": "single_ashare_execution_account",
        "capital_authority_id": authority_id,
        "authority_generation": generation,
        "execution_lineage_id": lineage_id,
        "execution_account_count": 1,
        "style_ledgers_allowed": False,
        "style_capital_summing_allowed": False,
    }


def _validate_contract_options(
    sample_intent: str, style_states: Optional[Mapping[str, str]]
) -> Dict[str, str]:
    if sample_intent not in VALID_SAMPLE_INTENTS:
        raise ValueError(
            "sample_intent must be observation, exploration, or exploitation"
        )
    states = dict(style_states or {})
    known_styles = {str(row["style_id"]) for row in STYLE_DEFINITIONS}
    unknown_styles = sorted(set(states) - known_styles)
    if unknown_styles:
        raise ValueError("unknown style: %s" % ", ".join(unknown_styles))
    invalid_states = sorted(
        (style_id, state)
        for style_id, state in states.items()
        if state not in VALID_LIFECYCLE_STATUSES
    )
    if invalid_states:
        raise ValueError("invalid lifecycle status for %s: %s" % invalid_states[0])
    return states


def build_style_sample_contract(
    candidate: Mapping[str, Any],
    *,
    sample_intent: str = "observation",
    mg_enabled: bool = False,
    style_states: Optional[Mapping[str, str]] = None,
    decision_policy_version: str = DEFAULT_DECISION_POLICY_VERSION,
) -> Dict[str, Any]:
    """Build one pure research record from one shared candidate."""

    states = _validate_contract_options(sample_intent, style_states)
    symbol, trade_date, idempotency_key = _normalise_identity(candidate)
    capital_authority = _authority_contract(candidate)
    predictions = []
    for definition in STYLE_DEFINITIONS:
        style_id = definition["style_id"]
        predictions.append(
            _evaluate_style(
                definition,
                candidate,
                lifecycle_status=states.get(style_id, definition["lifecycle_status"]),
                mg_enabled=mg_enabled,
            )
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "ashare_multi_style_counterfactual",
        "market": "ashare",
        "symbol": symbol,
        "trade_date": trade_date,
        "idempotency_key": idempotency_key,
        "sample_intent": sample_intent,
        "sample_channel": _sample_channel(sample_intent),
        "decision_policy_version": decision_policy_version,
        "real_trading_enabled": False,
        "candidate_snapshot": {
            "candidate_id": deepcopy(candidate.get("candidate_id")),
            "data_quality": deepcopy(candidate.get("data_quality") or {}),
            "features": deepcopy(candidate.get("features") or {}),
            "marketgraph_ablation_group": "mg_on" if mg_enabled else "mg_off",
        },
        "capital_authority": capital_authority,
        "style_predictions": predictions,
        "style_disagreement": _build_disagreement(predictions),
        "portfolio_intent": _build_portfolio_intent(
            predictions,
            sample_intent=sample_intent,
            decision_policy_version=decision_policy_version,
            idempotency_key=idempotency_key,
        ),
    }


def migrate_v1_prediction_to_v2(v1_prediction: dict[str, Any]) -> dict[str, Any]:
    """Read-only compatibility: wrap a v1 prediction for v2 consumers.

    Old ``probability`` becomes ``legacy_uncalibrated_probability`` with
    ``calibration_eligible=False`` and ``promotion_eligible=False``.
    Old ``expected_return_distribution`` becomes ``uncalibrated_return_prior``
    with ``model_state=legacy_v1_uncalibrated``.
    """
    result = deepcopy(v1_prediction)
    pred = result.get("prediction")
    if isinstance(pred, dict):
        old_prob = pred.pop("probability", None)
        old_score = pred.get("score")
        if "raw_style_score" not in pred:
            pred["raw_style_score"] = old_score if old_score is not None else old_prob
        if "score_semantics" not in pred:
            pred["score_semantics"] = "legacy_uncalibrated_heuristic"
        if "calibrated_probability" not in pred:
            pred["calibrated_probability"] = None
        if "probability_model_state" not in pred:
            pred["probability_model_state"] = "not_calibrated"
        # Keep legacy probability as read-only marker; never promote.
        if old_prob is not None and "legacy_uncalibrated_probability" not in pred:
            pred["legacy_uncalibrated_probability"] = old_prob
        pred.setdefault("calibration_eligible", False)
        pred.setdefault("promotion_eligible", False)

    old_erd = result.pop("expected_return_distribution", None)
    if "uncalibrated_return_prior" not in result:
        if isinstance(old_erd, dict):
            up = deepcopy(old_erd)
            up["model_state"] = "legacy_v1_uncalibrated"
            up["decision_eligible"] = False
            result["uncalibrated_return_prior"] = up
        else:
            result["uncalibrated_return_prior"] = {
                "unit": "decimal_return",
                "p10": 0.0,
                "p50": 0.0,
                "p90": 0.0,
                "model_state": "legacy_v1_uncalibrated",
                "decision_eligible": False,
            }
    return result


__all__ = [
    "ASHARE_LOT_SIZE",
    "COST_MODEL_VERSION",
    "DEFAULT_DECISION_POLICY_VERSION",
    "SCHEMA_VERSION",
    "STYLE_DEFINITIONS",
    "build_style_sample_contract",
    "compute_ashare_conservative_costs",
    "migrate_v1_prediction_to_v2",
]
