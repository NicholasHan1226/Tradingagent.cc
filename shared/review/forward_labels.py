#!/usr/bin/env python3
"""Pure prediction-snapshot and forward-label helpers.

The observation path is intentionally independent from execution eligibility:
strategy thresholds may explain why a candidate was not traded, but only
unreliable price evidence makes a forward label ineligible.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json
from typing import Any, Mapping, Optional, Sequence


CANONICAL_HORIZONS = ("m30", "m60", "close", "1d", "3d", "5d")
HORIZON_ALIASES = {"next-day": "1d", "next_day": "1d"}
PRIMARY_HORIZON_POLICY_VERSION = "ashare-primary-horizon-v1"
SAMPLE_SCIENCE_CONTRACT_VERSION = "ashare-sample-science-v1"
DEFAULT_PRIMARY_LABEL_HORIZON = "1d"
PIT_TIMESTAMP_FIELDS = ("event_time", "available_at", "ingested_at", "retrieved_as_of")
MAX_EVIDENCE_LAG = {
    "m30": timedelta(minutes=15),
    "m60": timedelta(minutes=15),
    "close": timedelta(minutes=30),
    "1d": timedelta(hours=18),
    "3d": timedelta(hours=18),
    "5d": timedelta(hours=18),
}

_LIVE_BOOLEAN_FIELDS = (
    "real_trading_enabled",
    "live_execution_enabled",
    "real_money_enabled",
    "live_broker_enabled",
    "direct_execution_enabled",
    "is_live",
)


def canonical_horizon(value: Any) -> str:
    """Return the canonical horizon name, accepting the next-day alias."""

    normalized = str(value or "").strip().lower()
    normalized = HORIZON_ALIASES.get(normalized, normalized)
    if normalized not in CANONICAL_HORIZONS:
        raise ValueError("unsupported forward-label horizon: %s" % value)
    return normalized


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def validate_point_in_time_lineage(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an explicit point-in-time availability chain.

    A source hash or generic ``as_of`` marker is not enough to prove a
    point-in-time join.  Scientific evidence requires all four timestamps and
    the ordering ``event <= available <= ingested <= retrieved``.  When a
    prediction/label boundary is supplied, retrieval must not be later than
    that boundary.  This validator is diagnostic only: an incomplete chain
    does not suppress observation or forward-label collection.
    """

    if not isinstance(record, Mapping):
        raise TypeError("point-in-time record must be a mapping")
    nested = record.get("point_in_time_lineage")
    if not isinstance(nested, Mapping):
        nested = record.get("pit_lineage")
    source = nested if isinstance(nested, Mapping) else record
    if isinstance(source.get("timestamps"), Mapping):
        source = source["timestamps"]
    raw_values = {field: source.get(field) for field in PIT_TIMESTAMP_FIELDS}
    missing = [field for field, value in raw_values.items() if value in (None, "")]
    if missing:
        return {
            "status": "missing_timestamps",
            "complete": False,
            "missing_fields": missing,
            "timestamps": {field: None for field in PIT_TIMESTAMP_FIELDS},
        }

    parsed: dict[str, datetime] = {}
    invalid: list[str] = []
    for field, value in raw_values.items():
        timestamp = _parse_datetime(value)
        if (
            timestamp is None
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            invalid.append(field)
        else:
            parsed[field] = timestamp
    if invalid:
        return {
            "status": "invalid_or_timezone_naive_timestamps",
            "complete": False,
            "missing_fields": [],
            "invalid_fields": invalid,
            "timestamps": {
                field: _iso(parsed[field]) if field in parsed else None
                for field in PIT_TIMESTAMP_FIELDS
            },
        }

    ordered = [parsed[field] for field in PIT_TIMESTAMP_FIELDS]
    if any(later < earlier for earlier, later in zip(ordered, ordered[1:])):
        return {
            "status": "invalid_timestamp_order",
            "complete": False,
            "missing_fields": [],
            "timestamps": {
                field: _iso(parsed[field]) for field in PIT_TIMESTAMP_FIELDS
            },
        }

    boundary_raw = (
        record.get("prediction_at")
        or record.get("labels_as_of")
        or record.get("decision_as_of")
    )
    if boundary_raw not in (None, ""):
        boundary = _parse_datetime(boundary_raw)
        if boundary is None or boundary.tzinfo is None or boundary.utcoffset() is None:
            return {
                "status": "invalid_or_timezone_naive_boundary",
                "complete": False,
                "missing_fields": [],
                "timestamps": {
                    field: _iso(parsed[field]) for field in PIT_TIMESTAMP_FIELDS
                },
            }
        if parsed["retrieved_as_of"] > boundary:
            return {
                "status": "retrieved_after_decision_boundary",
                "complete": False,
                "missing_fields": [],
                "timestamps": {
                    field: _iso(parsed[field]) for field in PIT_TIMESTAMP_FIELDS
                },
                "decision_boundary": _iso(boundary),
            }

    return {
        "status": "valid",
        "complete": True,
        "missing_fields": [],
        "timestamps": {field: _iso(parsed[field]) for field in PIT_TIMESTAMP_FIELDS},
    }


def _iso(value: datetime) -> str:
    return value.isoformat()


def _as_positive_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _prediction_data_quality(record: Mapping[str, Any]) -> tuple[bool, str]:
    prediction_at = _parse_datetime(record.get("prediction_at"))
    if prediction_at is None:
        return False, "missing_prediction_timestamp"
    if _as_positive_float(record.get("reference_price")) is None:
        return False, "missing_reference_price"

    quality = record.get("data_quality")
    if not isinstance(quality, Mapping):
        return False, "missing_data_quality_evidence"
    if quality.get("reliable") is not True:
        return False, "unreliable_reference_data"
    if not str(quality.get("source") or "").strip():
        return False, "missing_reference_source"
    price_at = _parse_datetime(quality.get("price_timestamp"))
    if price_at is None:
        return False, "missing_reference_timestamp"
    try:
        if price_at > prediction_at:
            return False, "reference_price_after_prediction"
    except TypeError:
        return False, "reference_timestamp_timezone_mismatch"
    return True, "verified_reference_data"


def _stable_snapshot_id(record: Mapping[str, Any]) -> str:
    identity = {
        "market": record.get("market"),
        "symbol": record.get("symbol"),
        "style": record.get("style") or record.get("style_id"),
        "strategy_version": record.get("strategy_version")
        or record.get("style_version"),
        "prediction_at": record.get("prediction_at"),
    }
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return "prediction:" + sha256(encoded).hexdigest()[:24]


def _stable_label_update_id(
    snapshot_id: str,
    labels_as_of: str,
    cost_model_version: Optional[str],
    cost_evidence_id: Optional[str] = None,
) -> str:
    """Build a stable idempotency fingerprint that includes the cost version.

    Old 0-cost labels must never silently collide with versioned labels.
    When *cost_evidence_id* is provided (e.g. an actual execution event id),
    different evidence sources produce different fingerprints, so a cost
    revision does not silently collide.
    """
    identity = {
        "snapshot_id": snapshot_id,
        "labels_as_of": labels_as_of,
        "cost_model_version": cost_model_version or "no_cost_evidence",
        "cost_evidence_id": cost_evidence_id or "",
    }
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return "forward_label_update:" + sha256(encoded).hexdigest()[:32]


def build_prediction_snapshot(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a candidate into an immutable observation/counterfactual snapshot.

    A low score, incomplete research thesis, or execution rejection remains
    useful metadata and never suppresses the snapshot.  Missing or unreliable
    market data is recorded as label ineligibility instead of dropping it.
    """

    if not isinstance(candidate, Mapping):
        raise TypeError("candidate must be a mapping")
    snapshot = deepcopy(dict(candidate))
    reliable, reason = _prediction_data_quality(snapshot)

    snapshot["snapshot_id"] = str(
        snapshot.get("snapshot_id") or _stable_snapshot_id(snapshot)
    )
    snapshot["snapshot_status"] = "recorded"
    snapshot["sample_layer"] = "observation_counterfactual"
    snapshot["forward_label_eligibility"] = (
        "eligible" if reliable else "rejected_data_quality"
    )
    snapshot["forward_label_rejection_reason"] = None if reliable else reason
    snapshot["reference_evidence_status"] = reason
    snapshot["primary_label_horizon"] = canonical_horizon(
        snapshot.get("primary_label_horizon") or DEFAULT_PRIMARY_LABEL_HORIZON
    )
    snapshot["primary_horizon_policy_version"] = str(
        snapshot.get("primary_horizon_policy_version") or PRIMARY_HORIZON_POLICY_VERSION
    )
    snapshot["sample_science_contract_version"] = str(
        snapshot.get("sample_science_contract_version")
        or SAMPLE_SCIENCE_CONTRACT_VERSION
    )
    snapshot["point_in_time_lineage_validation"] = validate_point_in_time_lineage(
        snapshot
    )
    snapshot["capital_layer"] = "simulated"
    snapshot["account_type"] = "simulated"
    for field in _LIVE_BOOLEAN_FIELDS:
        snapshot[field] = False
    return snapshot


def _default_horizon_targets(prediction_at: datetime) -> dict[str, datetime]:
    close_at = prediction_at.replace(hour=15, minute=0, second=0, microsecond=0)
    if close_at <= prediction_at:
        close_at += timedelta(days=1)
    return {
        "m30": prediction_at + timedelta(minutes=30),
        "m60": prediction_at + timedelta(minutes=60),
        "close": close_at,
        "1d": prediction_at + timedelta(days=1),
        "3d": prediction_at + timedelta(days=3),
        "5d": prediction_at + timedelta(days=5),
    }


def _normalized_targets(
    prediction_at: datetime, horizon_targets: Optional[Mapping[str, Any]]
) -> dict[str, datetime]:
    targets = _default_horizon_targets(prediction_at)
    if horizon_targets is not None:
        for raw_name, raw_target in horizon_targets.items():
            name = canonical_horizon(raw_name)
            parsed = _parse_datetime(raw_target)
            if parsed is None:
                raise ValueError("invalid target timestamp for horizon %s" % raw_name)
            try:
                if parsed < prediction_at:
                    raise ValueError(
                        "target timestamp cannot precede prediction_at: %s" % raw_name
                    )
            except TypeError:
                raise ValueError(
                    "horizon targets and prediction_at must use compatible timezones"
                )
            targets[name] = parsed
    return targets


def _direction_multiplier(value: Any) -> int:
    normalized = str(value or "long").strip().lower()
    if normalized in {"long", "buy", "bullish", "up", "1", "+1"}:
        return 1
    if normalized in {"short", "sell", "bearish", "down", "-1"}:
        return -1
    if normalized in {"hold", "flat", "neutral", "abstain", "0"}:
        return 0
    raise ValueError("unsupported prediction direction: %s" % value)


def _cost_bps(
    snapshot: Mapping[str, Any], costs: Optional[Mapping[str, Any]]
) -> tuple[float, float, Optional[str], Optional[str]]:
    """Extract versioned cost evidence.

    Returns ``(fee_bps, slippage_bps, cost_model_version, cost_evidence_id)``.
    Raises ``ValueError`` when no versioned cost evidence is present,
    so that a caller can fail closed or label the sample
    ``rejected_missing_cost_evidence``.
    """
    merged: dict[str, Any] = {}
    embedded = snapshot.get("costs")
    if isinstance(embedded, Mapping):
        merged.update(embedded)
    if isinstance(costs, Mapping):
        merged.update(costs)

    cost_model_version = str(merged.get("cost_model_version") or "").strip() or None
    cost_evidence_id = str(merged.get("cost_evidence_event_id") or "").strip() or None
    if not cost_model_version:
        raise ValueError("missing_cost_model_version")

    def number(name: str) -> float:
        try:
            return max(float(merged.get(name) or 0.0), 0.0)
        except (TypeError, ValueError):
            raise ValueError("%s must be numeric" % name)

    if "round_trip_fee_bps" in merged:
        fee = number("round_trip_fee_bps")
    elif "entry_fee_bps" in merged or "exit_fee_bps" in merged:
        fee = number("entry_fee_bps") + number("exit_fee_bps")
    else:
        fee = number("fee_bps")

    if "round_trip_slippage_bps" in merged:
        slippage = number("round_trip_slippage_bps")
    elif "entry_slippage_bps" in merged or "exit_slippage_bps" in merged:
        slippage = number("entry_slippage_bps") + number("exit_slippage_bps")
    else:
        slippage = number("slippage_bps")
    return fee, slippage, cost_model_version, cost_evidence_id


def _point_timestamp(point: Mapping[str, Any]) -> Optional[datetime]:
    return _parse_datetime(
        point.get("timestamp") or point.get("observed_at") or point.get("bar_time")
    )


def _point_quality(point: Mapping[str, Any]) -> tuple[bool, str]:
    if point.get("reliable") is not True:
        return False, "unreliable_exit_evidence"
    if _as_positive_float(point.get("price")) is None:
        return False, "invalid_exit_price"
    if not str(point.get("source") or "").strip():
        return False, "missing_exit_source"
    if _point_timestamp(point) is None:
        return False, "missing_exit_timestamp"
    return True, "verified_exit_evidence"


def _point_supports_horizon(point: Mapping[str, Any], horizon: str) -> bool:
    raw = point.get("eligible_horizons")
    if raw is None:
        return True
    if not isinstance(raw, (list, tuple, set)):
        return False
    supported: set[str] = set()
    for value in raw:
        try:
            supported.add(canonical_horizon(value))
        except ValueError:
            continue
    return horizon in supported


def _empty_label(
    name: str, target: datetime, status: str, reason: str
) -> dict[str, Any]:
    return {
        "horizon": name,
        "target_at": _iso(target),
        "status": status,
        "reason": reason,
        "evidence_at": None,
        "evidence_source": None,
        "exit_price": None,
        "market_return": None,
        "gross_return_after_direction": None,
        "fee_bps": None,
        "slippage_bps": None,
        "total_cost_bps": None,
        "cost_model_version": None,
        "cost_evidence_event_id": None,
        "net_return_after_costs": None,
        "outcome": None,
        "point_in_time_lineage": {
            "status": "unavailable_no_exit_evidence",
            "complete": False,
        },
    }


def materialize_forward_labels(
    snapshot: Mapping[str, Any],
    price_points: Sequence[Mapping[str, Any]],
    *,
    as_of: Any,
    horizon_targets: Optional[Mapping[str, Any]] = None,
    costs: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Materialize only labels due at ``as_of`` without looking into the future.

    Requires versioned cost evidence.  Fails closed with
    ``rejected_missing_cost_evidence`` when no cost model version is present.
    """

    if not isinstance(snapshot, Mapping):
        raise TypeError("snapshot must be a mapping")
    prediction_at = _parse_datetime(snapshot.get("prediction_at"))
    current_as_of = _parse_datetime(as_of)
    if prediction_at is None:
        raise ValueError("snapshot prediction_at is required")
    if current_as_of is None:
        raise ValueError("as_of must be an ISO timestamp or datetime")
    try:
        if current_as_of < prediction_at:
            raise ValueError("as_of cannot be before prediction_at")
    except TypeError:
        raise ValueError("as_of and prediction_at must use compatible timezones")

    targets = _normalized_targets(prediction_at, horizon_targets)
    entry_price = _as_positive_float(snapshot.get("reference_price"))
    multiplier = _direction_multiplier(snapshot.get("direction"))

    # Extract cost evidence; fail closed when no versioned cost model.
    try:
        fee_bps, slippage_bps, cost_model_version, cost_evidence_id = _cost_bps(
            snapshot, costs
        )
    except ValueError:
        fee_bps = 0.0
        slippage_bps = 0.0
        cost_model_version = None
        cost_evidence_id = None

    points = deepcopy(list(price_points))
    result = deepcopy(dict(snapshot))
    labels: dict[str, dict[str, Any]] = {}

    for name in CANONICAL_HORIZONS:
        target = targets[name]
        try:
            due = current_as_of >= target
        except TypeError:
            raise ValueError("horizon targets and as_of must use compatible timezones")
        if not due:
            labels[name] = _empty_label(
                name, target, "pending_not_due", "horizon_not_due_as_of"
            )
            continue

        if (
            snapshot.get("forward_label_eligibility") != "eligible"
            or entry_price is None
        ):
            labels[name] = _empty_label(
                name,
                target,
                "rejected_data_quality",
                str(
                    snapshot.get("forward_label_rejection_reason")
                    or "invalid_reference_evidence"
                ),
            )
            continue

        # Reject labels when no versioned cost evidence exists.
        if cost_model_version is None:
            labels[name] = _empty_label(
                name,
                target,
                "rejected_missing_cost_evidence",
                "no_versioned_cost_evidence",
            )
            continue

        eligible_window: list[tuple[datetime, Mapping[str, Any]]] = []
        latest_eligible = target + MAX_EVIDENCE_LAG[name]
        for point in points:
            if not isinstance(point, Mapping):
                continue
            if not _point_supports_horizon(point, name):
                continue
            point_at = _point_timestamp(point)
            if point_at is None:
                continue
            try:
                if target <= point_at <= current_as_of and point_at <= latest_eligible:
                    eligible_window.append((point_at, point))
            except TypeError:
                continue
        eligible_window.sort(key=lambda item: item[0])

        selected: Optional[tuple[datetime, Mapping[str, Any]]] = None
        first_quality_reason: Optional[str] = None
        for point_at, point in eligible_window:
            valid, quality_reason = _point_quality(point)
            if valid:
                selected = (point_at, point)
                break
            if first_quality_reason is None:
                first_quality_reason = quality_reason

        if selected is None:
            status = (
                "rejected_data_quality" if eligible_window else "missing_exit_evidence"
            )
            reason = first_quality_reason or "no_exit_evidence_as_of"
            labels[name] = _empty_label(name, target, status, reason)
            continue

        point_at, point = selected
        exit_price = float(point["price"])
        market_return = (exit_price - entry_price) / entry_price
        gross_return = market_return * multiplier
        applied_fee_bps = fee_bps if multiplier else 0.0
        applied_slippage_bps = slippage_bps if multiplier else 0.0
        total_cost_bps = applied_fee_bps + applied_slippage_bps
        net_return = gross_return - (total_cost_bps / 10_000.0)
        point_lineage = validate_point_in_time_lineage(
            {
                "event_time": point.get("event_time") or _iso(point_at),
                "available_at": point.get("available_at"),
                "ingested_at": point.get("ingested_at"),
                "retrieved_as_of": point.get("retrieved_as_of"),
                "labels_as_of": _iso(current_as_of),
            }
        )
        labels[name] = {
            "horizon": name,
            "target_at": _iso(target),
            "status": "ready",
            "reason": "verified_exit_evidence",
            "evidence_at": _iso(point_at),
            "evidence_source": str(point.get("source")),
            "exit_price": exit_price,
            "market_return": market_return,
            "gross_return_after_direction": gross_return,
            "fee_bps": applied_fee_bps,
            "slippage_bps": applied_slippage_bps,
            "total_cost_bps": total_cost_bps,
            "cost_model_version": cost_model_version,
            "cost_evidence_event_id": cost_evidence_id,
            "net_return_after_costs": net_return,
            "outcome": "win"
            if net_return > 0
            else "loss"
            if net_return < 0
            else "flat",
            "point_in_time_lineage": point_lineage,
        }

    result["labels_as_of"] = _iso(current_as_of)
    result["labels"] = labels
    result["label_aliases"] = dict(HORIZON_ALIASES)
    result["real_trading_enabled"] = False
    result["live_execution_enabled"] = False
    return result


__all__ = [
    "CANONICAL_HORIZONS",
    "HORIZON_ALIASES",
    "PRIMARY_HORIZON_POLICY_VERSION",
    "SAMPLE_SCIENCE_CONTRACT_VERSION",
    "DEFAULT_PRIMARY_LABEL_HORIZON",
    "PIT_TIMESTAMP_FIELDS",
    "build_prediction_snapshot",
    "canonical_horizon",
    "materialize_forward_labels",
    "validate_point_in_time_lineage",
    "_stable_label_update_id",
]
