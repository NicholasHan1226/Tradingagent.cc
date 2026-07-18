"""Pure read-side outcome projection over a frozen SampleJournal event view."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Protocol, Sequence

from shared.models.lifecycle import ValidationPlan
from shared.review.forward_labels import (
    canonical_horizon,
    evidence_envelope_from_record,
    validate_evidence_envelope,
    validate_point_in_time_lineage,
)
from shared.review.sample_journal import SampleJournal


OUTCOME_EVALUATION_SCHEMA_VERSION = "ashare-outcome-evaluation.v1"
_AUTHORITY = {
    "research_only": True,
    "capital_authority": False,
    "position_authority": False,
    "order_authority": False,
    "automatic_promotion_enabled": False,
    "automatic_risk_expansion_enabled": False,
    "live_transition_authorized": False,
    "real_trading_enabled": False,
}
_AUTHORITY_SCOPE_KEYS = {
    "capital_authority_id",
    "authority_generation",
    "execution_lineage_id",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FORWARD_LABEL_BINDING_KEYS = {
    "validation_plan_sha256",
    "trading_session_calendar_sha256",
    "trading_session_calendar_verification_proof_sha256",
}
_UPDATE_IDENTITY_FIELDS = (
    "market",
    "symbol",
    "decision_cluster_id",
    "primary_label_horizon",
    "source_snapshot_sha256",
    "base_snapshot_sha256",
    "pair_id",
)
_VALIDATION_PLAN_PROVENANCE_KEYS = {
    "validation_plan_sha256",
    "artifact_sha256",
    "authority_tier",
    "production_eligible",
    "verification_receipt_sha256",
}
_VALIDATION_PLAN_PROOF_KEYS = {
    "accepted",
    "production_eligible",
    "verifier_id",
    "verifier_version",
    "proof_sha256",
    "validation_plan_sha256",
    "artifact_sha256",
    "verification_receipt_sha256",
}
_MARKET_TRUTH_PROOF_KEYS = {
    "accepted",
    "production_eligible",
    "verifier_id",
    "verifier_version",
    "proof_sha256",
    "reference_evidence_sha256",
    "exit_evidence_sha256",
}


class OutcomeEvaluationError(ValueError):
    """Raised when a read-side outcome input is ambiguous or unsafe."""


class OutcomeMarketTruthVerifier(Protocol):
    """No-default port for binding reference and exit prices to market truth."""

    verifier_id: str
    verifier_version: str

    def verify(
        self,
        *,
        snapshot_id: str,
        horizon: str,
        reference_evidence: Mapping[str, Any],
        exit_evidence: Mapping[str, Any],
        target_at: datetime,
        as_of: datetime,
    ) -> Mapping[str, Any]:
        """Return a detached proof bound to both exact evidence payloads."""


class ValidationPlanProvenanceVerifier(Protocol):
    """No-default port for proving that a plan artifact is externally trusted."""

    verifier_id: str
    verifier_version: str

    def verify(
        self,
        *,
        validation_plan: ValidationPlan,
        provenance: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Return a detached proof bound to the plan and artifact receipt."""


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _aware(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OutcomeEvaluationError("%s_invalid" % field) from exc
    else:
        raise OutcomeEvaluationError("%s_invalid" % field)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OutcomeEvaluationError("%s_timezone_required" % field)
    return parsed.astimezone(timezone.utc)


def _authority_scope(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _AUTHORITY_SCOPE_KEYS:
        raise OutcomeEvaluationError("authority_scope_invalid")
    authority_id = str(value.get("capital_authority_id") or "").strip()
    lineage_id = str(value.get("execution_lineage_id") or "").strip()
    generation = value.get("authority_generation")
    if not authority_id or not lineage_id:
        raise OutcomeEvaluationError("authority_scope_invalid")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
    ):
        raise OutcomeEvaluationError("authority_scope_invalid")
    return {
        "capital_authority_id": authority_id,
        "authority_generation": generation,
        "execution_lineage_id": lineage_id,
    }


def _in_scope(value: Mapping[str, Any], scope: Mapping[str, Any]) -> bool:
    return all(value.get(key) == expected for key, expected in scope.items())


def _finite_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _label_payload(raw: Any, horizon: str) -> dict[str, Any]:
    label = raw if isinstance(raw, Mapping) else {}
    return {
        "horizon": horizon,
        "source_horizon": label.get("horizon"),
        "status": str(label.get("status") or "missing"),
        "reason": label.get("reason"),
        "target_at": label.get("target_at"),
        "evidence_at": label.get("evidence_at"),
        "evidence_source": label.get("evidence_source"),
        "exit_price": _finite_or_none(label.get("exit_price")),
        "exit_evidence_payload": deepcopy(label.get("exit_evidence_payload")),
        "exit_evidence_sha256": label.get("exit_evidence_sha256"),
        "market_return": _finite_or_none(label.get("market_return")),
        "gross_return_after_direction": _finite_or_none(
            label.get("gross_return_after_direction")
        ),
        "fee_bps": _finite_or_none(label.get("fee_bps")),
        "slippage_bps": _finite_or_none(label.get("slippage_bps")),
        "total_cost_bps": _finite_or_none(label.get("total_cost_bps")),
        "net_return_after_costs": _finite_or_none(label.get("net_return_after_costs")),
        "cost_model_version": label.get("cost_model_version"),
        "cost_evidence_event_id": label.get("cost_evidence_event_id"),
        "outcome": label.get("outcome"),
        "point_in_time_lineage": deepcopy(label.get("point_in_time_lineage")),
        **{key: label.get(key) for key in sorted(_FORWARD_LABEL_BINDING_KEYS)},
    }


def _validation_plan_binding(
    validation_plan: Optional[ValidationPlan],
) -> Optional[dict[str, str]]:
    if validation_plan is None:
        return None
    if not isinstance(validation_plan, ValidationPlan):
        raise OutcomeEvaluationError("validation_plan_invalid")
    if validation_plan.market.strip().lower() not in {
        "ashare",
        "a_share",
        "a-share",
        "a股",
        "cn",
        "china",
    }:
        raise OutcomeEvaluationError("validation_plan_market_invalid")
    calendar = validation_plan.trading_session_calendar
    proof = validation_plan.trading_session_calendar_verification
    if (
        calendar is None
        or proof is None
        or proof.accepted is not True
        or proof.calendar_sha256 != calendar.calendar_sha256
        or proof.source_receipt_id != calendar.source_receipt_id
        or proof.source_receipt_sha256 != calendar.source_receipt_sha256
        or proof.frozen_at != validation_plan.frozen_at
        or proof.verified_at > validation_plan.frozen_at
        or calendar.available_at > validation_plan.frozen_at
    ):
        raise OutcomeEvaluationError("validation_plan_authority_binding_invalid")
    return {
        "validation_plan_sha256": validation_plan.sha256(),
        "trading_session_calendar_sha256": calendar.calendar_sha256,
        "trading_session_calendar_verification_proof_sha256": proof.proof_sha256,
    }


def _validation_plan_provenance(
    value: Optional[Mapping[str, Any]],
    *,
    validation_plan: Optional[ValidationPlan],
    validation_plan_binding: Optional[Mapping[str, str]],
    verifier: Optional[ValidationPlanProvenanceVerifier],
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]], bool]:
    if value is None:
        return None, None, False
    if not isinstance(value, Mapping) or set(value) != _VALIDATION_PLAN_PROVENANCE_KEYS:
        raise OutcomeEvaluationError("validation_plan_provenance_invalid")
    normalized = {
        "validation_plan_sha256": str(value.get("validation_plan_sha256") or ""),
        "artifact_sha256": str(value.get("artifact_sha256") or ""),
        "authority_tier": str(value.get("authority_tier") or "").strip(),
        "production_eligible": value.get("production_eligible"),
        "verification_receipt_sha256": str(
            value.get("verification_receipt_sha256") or ""
        ),
    }
    for field in (
        "validation_plan_sha256",
        "artifact_sha256",
        "verification_receipt_sha256",
    ):
        if not _SHA256_RE.fullmatch(normalized[field]):
            raise OutcomeEvaluationError("validation_plan_provenance_invalid")
    expected_plan_sha = (
        validation_plan_binding.get("validation_plan_sha256")
        if isinstance(validation_plan_binding, Mapping)
        else None
    )
    tier = normalized["authority_tier"].casefold()
    claim_is_eligible = bool(
        expected_plan_sha
        and normalized["validation_plan_sha256"] == expected_plan_sha
        and normalized["production_eligible"] is True
        and tier
        and not any(marker in tier for marker in ("fixture", "mock", "local"))
    )
    if not claim_is_eligible or validation_plan is None or verifier is None:
        return normalized, None, False
    verifier_id = str(getattr(verifier, "verifier_id", "") or "").strip()
    verifier_version = str(getattr(verifier, "verifier_version", "") or "").strip()
    if not verifier_id or not verifier_version:
        return normalized, None, False
    try:
        raw_proof = verifier.verify(
            validation_plan=validation_plan,
            provenance=deepcopy(normalized),
        )
    except Exception:
        return normalized, None, False
    if (
        not isinstance(raw_proof, Mapping)
        or set(raw_proof) != _VALIDATION_PLAN_PROOF_KEYS
    ):
        return normalized, None, False
    proof = deepcopy(dict(raw_proof))
    trusted = bool(
        proof.get("accepted") is True
        and proof.get("production_eligible") is True
        and proof.get("verifier_id") == verifier_id
        and proof.get("verifier_version") == verifier_version
        and proof.get("validation_plan_sha256") == normalized["validation_plan_sha256"]
        and proof.get("artifact_sha256") == normalized["artifact_sha256"]
        and proof.get("verification_receipt_sha256")
        == normalized["verification_receipt_sha256"]
        and _SHA256_RE.fullmatch(str(proof.get("proof_sha256") or "")) is not None
    )
    return normalized, proof if trusted else None, trusted


def _prediction_by_snapshot(
    events: Sequence[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    index: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        if not (
            event.get("journal_event_type") == "prediction_snapshot"
            or event.get("record_type") == "prediction"
        ):
            continue
        snapshot_id = str(event.get("snapshot_id") or "").strip()
        if snapshot_id:
            index[snapshot_id].append(event)
    return index


def _update_matches_prediction(
    update: Mapping[str, Any], prediction: Mapping[str, Any]
) -> bool:
    if any(update.get(key) != prediction.get(key) for key in _AUTHORITY_SCOPE_KEYS):
        return False
    for field in _UPDATE_IDENTITY_FIELDS:
        expected = str(prediction.get(field) or "").strip()
        actual = str(update.get(field) or "").strip()
        if expected and actual != expected:
            return False
    return isinstance(update.get("labels"), Mapping)


def _project_bound_label_updates(
    events: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    predictions = _prediction_by_snapshot(events)
    projection_input: list[Mapping[str, Any]] = []
    invalid_snapshots: set[str] = set()
    latest_valid: dict[str, tuple[datetime, int, Mapping[str, Any]]] = {}
    for sequence, event in enumerate(events):
        if event.get("journal_event_type") != "forward_label_update":
            if (
                event.get("journal_event_type") == "prediction_snapshot"
                or event.get("record_type") == "prediction"
            ):
                stripped = deepcopy(dict(event))
                stripped.pop("labels", None)
                stripped.pop("labels_as_of", None)
                stripped.pop("forward_label_authority_binding", None)
                projection_input.append(stripped)
            else:
                projection_input.append(event)
            continue
        snapshot_id = str(event.get("snapshot_id") or "").strip()
        candidates = predictions.get(snapshot_id, [])
        try:
            labels_as_of = _aware(event.get("labels_as_of"), "labels_as_of")
        except OutcomeEvaluationError:
            labels_as_of = None
        if (
            not snapshot_id
            or len(candidates) != 1
            or labels_as_of is None
            or not _update_matches_prediction(event, candidates[0])
        ):
            if snapshot_id:
                invalid_snapshots.add(snapshot_id)
            continue
        projection_input.append(event)
        current = latest_valid.get(snapshot_id)
        candidate_key = (labels_as_of, sequence)
        if current is None or candidate_key >= (current[0], current[1]):
            latest_valid[snapshot_id] = (labels_as_of, sequence, event)

    projected = SampleJournal.project_sample_records(projection_input)
    for row in projected:
        if not (
            row.get("journal_event_type") == "prediction_snapshot"
            or row.get("record_type") == "prediction"
        ):
            continue
        latest = latest_valid.get(str(row.get("snapshot_id") or "").strip())
        if latest is not None:
            row["forward_label_authority_binding"] = deepcopy(
                latest[2].get("forward_label_authority_binding")
            )
    return projected, invalid_snapshots, set(latest_valid)


def _decision_key(value: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(value.get("decision_cluster_id") or "").strip(),
        str(value.get("symbol") or "").strip().upper(),
    )


def _verified_trade_date(
    event: Mapping[str, Any],
    *,
    prediction_at: datetime,
    validation_plan: Optional[ValidationPlan],
) -> tuple[Optional[str], list[str]]:
    """Bind the reported date to the Shanghai decision date and frozen calendar."""

    exchange_tz = timezone(timedelta(hours=8))
    local_date = prediction_at.astimezone(exchange_tz).date()
    expected = local_date.strftime("%Y%m%d")
    reported = str(event.get("trade_date") or "").strip()
    exclusions: list[str] = []
    if reported != expected:
        exclusions.append("trade_date_prediction_time_mismatch")
    sessions = (
        tuple(validation_plan.trading_session_calendar.sessions)
        if validation_plan is not None
        and validation_plan.trading_session_calendar is not None
        else ()
    )
    if not sessions or local_date not in sessions:
        exclusions.append("trade_date_calendar_authority_mismatch")
    return (expected if not exclusions else None), exclusions


def _primary_label(event: Mapping[str, Any], horizon: str) -> dict[str, Any]:
    labels = event.get("labels")
    if not isinstance(labels, Mapping):
        return _label_payload(None, horizon)
    try:
        canonical_primary = canonical_horizon(horizon)
    except ValueError as exc:
        raise OutcomeEvaluationError("primary_label_horizon_invalid") from exc
    matches = []
    for raw_horizon, raw_label in labels.items():
        try:
            canonical = canonical_horizon(raw_horizon)
        except ValueError:
            continue
        if canonical == canonical_primary:
            matches.append(raw_label)
    if len(matches) > 1:
        raise OutcomeEvaluationError("ambiguous_primary_label_aliases")
    return _label_payload(matches[0] if matches else None, canonical_primary)


def _prediction_evidence_exclusions(
    event: Mapping[str, Any],
    *,
    prediction_at: datetime,
) -> list[str]:
    exclusions: list[str] = []
    try:
        pit = validate_point_in_time_lineage(event)
    except (TypeError, ValueError):
        pit = {"status": "invalid", "complete": False}
    if pit.get("status") != "valid" or pit.get("complete") is not True:
        exclusions.append("point_in_time_lineage_not_verified")

    try:
        envelope = evidence_envelope_from_record(event)
        envelope_validation = validate_evidence_envelope(
            envelope,
            boundary=prediction_at,
            require_receipts=True,
        )
    except (TypeError, ValueError):
        envelope_validation = {"status": "invalid", "complete": False}
    if (
        envelope_validation.get("status") != "valid"
        or envelope_validation.get("complete") is not True
    ):
        exclusions.append("source_evidence_envelope_not_verified")

    point_in_time_as_of = event.get("point_in_time_as_of")
    if point_in_time_as_of in (None, ""):
        exclusions.append("point_in_time_as_of_missing")
    else:
        try:
            data_as_of = _aware(point_in_time_as_of, "point_in_time_as_of")
        except OutcomeEvaluationError:
            exclusions.append("point_in_time_as_of_invalid")
        else:
            if data_as_of > prediction_at:
                exclusions.append("point_in_time_as_of_after_prediction")

    source_sha = str(event.get("source_snapshot_sha256") or "").strip().lower()
    if not _SHA256_RE.fullmatch(source_sha):
        exclusions.append("source_snapshot_sha256_not_verified")

    data_quality = event.get("data_quality")
    if not isinstance(data_quality, Mapping):
        exclusions.append("data_quality_evidence_missing")
    else:
        source = str(data_quality.get("source") or "").strip()
        if not source:
            exclusions.append("data_quality_source_missing")
        elif any(marker in source.casefold() for marker in ("fixture", "mock")):
            exclusions.append("fixture_source_excluded")
        if not (
            data_quality.get("reliable") is True
            or data_quality.get("qualified") is True
        ):
            exclusions.append("data_quality_not_qualified")
    source_class = str(event.get("source_class") or "").strip().casefold()
    if any(marker in source_class for marker in ("fixture", "mock")):
        exclusions.append("fixture_source_excluded")
    return exclusions


def _evidence_payload_exclusions(
    payload: Any,
    claimed_sha256: Any,
    *,
    role: str,
    expected_price: Optional[float],
    expected_source: str,
    expected_event_at: Optional[datetime],
    boundary: datetime,
) -> list[str]:
    """Recompute one price-evidence envelope and bind it to the row claims."""

    reason = "%s_evidence_payload_not_verified" % role
    if not isinstance(payload, Mapping):
        return [reason]
    claimed = str(claimed_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(claimed) or claimed != canonical_sha256(payload):
        return [reason]
    price = _finite_or_none(payload.get("price"))
    source = str(payload.get("source") or "").strip()
    if (
        expected_price is None
        or price is None
        or price <= 0.0
        or not math.isclose(price, expected_price, rel_tol=0.0, abs_tol=1e-12)
        or not source
        or source != expected_source
        or payload.get("reliable") is not True
        or any(marker in source.casefold() for marker in ("fixture", "mock"))
    ):
        return [reason]
    try:
        validation = validate_evidence_envelope(
            evidence_envelope_from_record(payload),
            boundary=boundary,
            require_receipts=True,
        )
        timestamps = validation.get("canonical_timestamps")
        event_at = _aware(
            timestamps.get("event_time") if isinstance(timestamps, Mapping) else None,
            "%s_evidence_event_time" % role,
        )
    except (OutcomeEvaluationError, TypeError, ValueError):
        return [reason]
    if (
        validation.get("status") != "valid"
        or validation.get("complete") is not True
        or event_at > boundary
        or (expected_event_at is not None and event_at != expected_event_at)
    ):
        return [reason]
    return []


def _market_truth_verification(
    *,
    verifier: Optional[OutcomeMarketTruthVerifier],
    snapshot_id: str,
    horizon: str,
    reference_evidence: Any,
    exit_evidence: Any,
    target_at: datetime,
    as_of: datetime,
) -> tuple[Optional[dict[str, Any]], bool]:
    """Require one caller-supplied detached authority; there is no fallback."""

    if (
        verifier is None
        or not isinstance(reference_evidence, Mapping)
        or not isinstance(exit_evidence, Mapping)
    ):
        return None, False
    verifier_id = str(getattr(verifier, "verifier_id", "") or "").strip()
    verifier_version = str(getattr(verifier, "verifier_version", "") or "").strip()
    if not verifier_id or not verifier_version:
        return None, False
    reference_sha = canonical_sha256(reference_evidence)
    exit_sha = canonical_sha256(exit_evidence)
    try:
        raw = verifier.verify(
            snapshot_id=snapshot_id,
            horizon=horizon,
            reference_evidence=deepcopy(dict(reference_evidence)),
            exit_evidence=deepcopy(dict(exit_evidence)),
            target_at=target_at,
            as_of=as_of,
        )
    except Exception:
        return None, False
    if not isinstance(raw, Mapping) or set(raw) != _MARKET_TRUTH_PROOF_KEYS:
        return None, False
    proof = deepcopy(dict(raw))
    trusted = bool(
        proof.get("accepted") is True
        and proof.get("production_eligible") is True
        and proof.get("verifier_id") == verifier_id
        and proof.get("verifier_version") == verifier_version
        and proof.get("reference_evidence_sha256") == reference_sha
        and proof.get("exit_evidence_sha256") == exit_sha
        and _SHA256_RE.fullmatch(str(proof.get("proof_sha256") or "")) is not None
    )
    return proof if trusted else None, trusted


def _label_exclusions(
    event: Mapping[str, Any],
    label: Mapping[str, Any],
    *,
    prediction_at: datetime,
    cutoff: datetime,
    validation_plan: Optional[ValidationPlan],
    validation_plan_binding: Optional[Mapping[str, str]],
) -> list[str]:
    exclusions: list[str] = []
    market_return = _finite_or_none(label.get("market_return"))
    gross = _finite_or_none(label.get("gross_return_after_direction"))
    fee_bps = _finite_or_none(label.get("fee_bps"))
    slippage_bps = _finite_or_none(label.get("slippage_bps"))
    total_cost_bps = _finite_or_none(label.get("total_cost_bps"))
    net = _finite_or_none(label.get("net_return_after_costs"))
    if label.get("status") != "ready" or net is None:
        return ["primary_label_not_ready"]
    if not label.get("target_at") or not label.get("evidence_at"):
        return ["label_temporal_evidence_incomplete"]
    try:
        target_at = _aware(label["target_at"], "label_target_at")
        evidence_at = _aware(label["evidence_at"], "label_evidence_at")
    except OutcomeEvaluationError:
        return ["label_temporal_evidence_invalid"]
    if target_at < prediction_at:
        exclusions.append("label_target_precedes_prediction")
    if target_at > cutoff or evidence_at > cutoff:
        exclusions.append("label_evidence_after_as_of")
    if evidence_at < target_at:
        exclusions.append("label_evidence_precedes_target")

    if validation_plan is not None and label.get("horizon") in {
        "close",
        "1d",
        "3d",
        "5d",
    }:
        calendar = validation_plan.trading_session_calendar
        exchange_tz = timezone(timedelta(hours=8))
        prediction_local = prediction_at.astimezone(exchange_tz)
        prediction_date = prediction_local.date()
        sessions = tuple(calendar.sessions) if calendar is not None else ()
        future_sessions = [session for session in sessions if session > prediction_date]
        same_day_close = datetime(
            prediction_date.year,
            prediction_date.month,
            prediction_date.day,
            15,
            tzinfo=exchange_tz,
        )
        close_session = (
            prediction_date
            if prediction_date in sessions and prediction_local < same_day_close
            else future_sessions[0]
            if future_sessions
            else None
        )
        index_by_horizon = {"1d": 0, "3d": 2, "5d": 4}
        if label.get("horizon") == "close":
            target_session = close_session
        else:
            index = index_by_horizon[str(label.get("horizon"))]
            target_session = (
                future_sessions[index] if len(future_sessions) > index else None
            )
        expected_target = (
            datetime(
                target_session.year,
                target_session.month,
                target_session.day,
                15,
                tzinfo=exchange_tz,
            ).astimezone(timezone.utc)
            if target_session is not None
            else None
        )
        if expected_target is None or target_at != expected_target:
            exclusions.append("label_target_calendar_authority_mismatch")
        if evidence_at != target_at:
            exclusions.append("label_session_evidence_time_mismatch")

    evidence_source = str(label.get("evidence_source") or "").strip()
    exit_price = _finite_or_none(label.get("exit_price"))
    cost_evidence_id = str(label.get("cost_evidence_event_id") or "").strip()
    if (
        label.get("reason") != "verified_exit_evidence"
        or not evidence_source
        or any(marker in evidence_source.casefold() for marker in ("fixture", "mock"))
        or exit_price is None
        or exit_price <= 0.0
        or market_return is None
        or gross is None
        or not cost_evidence_id
    ):
        exclusions.append("label_exit_evidence_incomplete")
    else:
        normalized_direction = str(event.get("direction") or "long").strip().lower()
        if normalized_direction in {"long", "buy", "bullish", "up", "1", "+1"}:
            multiplier = 1
        elif normalized_direction in {
            "short",
            "sell",
            "bearish",
            "down",
            "-1",
        }:
            multiplier = -1
        elif normalized_direction in {"hold", "flat", "neutral", "abstain", "0"}:
            multiplier = 0
        else:
            multiplier = None
        if multiplier is None:
            exclusions.append("label_direction_invalid")
        elif not math.isclose(
            gross,
            market_return * multiplier,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            exclusions.append("label_direction_return_mismatch")

    labels_as_of_raw = event.get("labels_as_of")
    lineage = label.get("point_in_time_lineage")
    label_pit_valid = False
    if labels_as_of_raw not in (None, "") and isinstance(lineage, Mapping):
        try:
            labels_as_of = _aware(labels_as_of_raw, "labels_as_of")
            pit = validate_point_in_time_lineage(
                {
                    "point_in_time_lineage": lineage,
                    "labels_as_of": labels_as_of.isoformat(),
                }
            )
            nested_validation = lineage.get("evidence_envelope_validation")
            raw_envelope = (
                nested_validation.get("evidence_envelope")
                if isinstance(nested_validation, Mapping)
                else None
            )
            if not isinstance(raw_envelope, Mapping):
                raise OutcomeEvaluationError("label_evidence_envelope_missing")
            envelope = validate_evidence_envelope(
                raw_envelope,
                boundary=labels_as_of,
                require_receipts=True,
            )
            timestamps = pit.get("timestamps")
            envelope_timestamps = envelope.get("canonical_timestamps")
            pit_event_at = _aware(
                timestamps.get("event_time")
                if isinstance(timestamps, Mapping)
                else None,
                "label_pit_event_time",
            )
            envelope_event_at = _aware(
                (
                    envelope_timestamps.get("event_time")
                    if isinstance(envelope_timestamps, Mapping)
                    else None
                ),
                "label_envelope_event_time",
            )
            label_pit_valid = bool(
                pit.get("status") == "valid"
                and pit.get("complete") is True
                and envelope.get("status") == "valid"
                and envelope.get("complete") is True
                and pit_event_at == evidence_at
                and envelope_event_at == evidence_at
            )
        except (OutcomeEvaluationError, TypeError, ValueError):
            label_pit_valid = False
    if not label_pit_valid:
        exclusions.append("label_point_in_time_lineage_not_verified")

    reference_price = _finite_or_none(event.get("reference_price"))
    data_quality = event.get("data_quality")
    reference_source = (
        str(data_quality.get("source") or "").strip()
        if isinstance(data_quality, Mapping)
        else ""
    )
    try:
        reference_event_at = _aware(
            event.get("event_time"), "reference_evidence_event_time"
        )
    except OutcomeEvaluationError:
        reference_event_at = None
    exclusions.extend(
        _evidence_payload_exclusions(
            event.get("reference_evidence_payload"),
            event.get("reference_evidence_sha256"),
            role="reference",
            expected_price=reference_price,
            expected_source=reference_source,
            expected_event_at=reference_event_at,
            boundary=prediction_at,
        )
    )
    try:
        exit_boundary = _aware(event.get("labels_as_of"), "labels_as_of")
    except OutcomeEvaluationError:
        exit_boundary = cutoff
    exclusions.extend(
        _evidence_payload_exclusions(
            label.get("exit_evidence_payload"),
            label.get("exit_evidence_sha256"),
            role="exit",
            expected_price=exit_price,
            expected_source=evidence_source,
            expected_event_at=evidence_at,
            boundary=min(exit_boundary, cutoff),
        )
    )
    if (
        reference_price is None
        or reference_price <= 0.0
        or exit_price is None
        or market_return is None
        or not math.isclose(
            market_return,
            (exit_price - reference_price) / reference_price,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    ):
        exclusions.append("label_market_return_price_mismatch")

    root_binding = event.get("forward_label_authority_binding")
    label_binding = {key: label.get(key) for key in _FORWARD_LABEL_BINDING_KEYS}
    if (
        validation_plan_binding is None
        or not isinstance(root_binding, Mapping)
        or set(root_binding) != _FORWARD_LABEL_BINDING_KEYS
        or dict(root_binding) != dict(validation_plan_binding)
        or label_binding != dict(validation_plan_binding)
        or any(
            not _SHA256_RE.fullmatch(str(value or ""))
            for value in label_binding.values()
        )
    ):
        exclusions.append("label_validation_plan_authority_not_verified")

    try:
        source_horizon = canonical_horizon(label.get("source_horizon"))
    except ValueError:
        source_horizon = None
    if source_horizon != label.get("horizon"):
        exclusions.append("label_horizon_binding_invalid")

    cost_version = str(label.get("cost_model_version") or "").strip()
    if (
        gross is None
        or fee_bps is None
        or slippage_bps is None
        or fee_bps < 0.0
        or slippage_bps < 0.0
        or total_cost_bps is None
        or total_cost_bps < 0.0
        or not cost_version
    ):
        exclusions.append("label_cost_evidence_incomplete")
    else:
        expected_total = fee_bps + slippage_bps
        expected_net = gross - expected_total / 10_000.0
        if not math.isclose(
            total_cost_bps,
            expected_total,
            rel_tol=0.0,
            abs_tol=1e-12,
        ) or not math.isclose(net, expected_net, rel_tol=0.0, abs_tol=1e-12):
            exclusions.append("label_cost_arithmetic_mismatch")

    expected_outcome = "win" if net > 0.0 else "loss" if net < 0.0 else "flat"
    if label.get("outcome") != expected_outcome:
        exclusions.append("label_outcome_mismatch")

    costs = event.get("costs")
    if not isinstance(costs, Mapping):
        exclusions.append("prediction_cost_contract_missing")
    else:
        embedded_version = str(costs.get("cost_model_version") or "").strip()
        embedded_fee = _finite_or_none(costs.get("round_trip_fee_bps"))
        embedded_slippage = _finite_or_none(costs.get("round_trip_slippage_bps"))
        if (
            embedded_version != cost_version
            or embedded_fee is None
            or embedded_slippage is None
            or fee_bps is None
            or slippage_bps is None
            or not math.isclose(embedded_fee, fee_bps, rel_tol=0.0, abs_tol=1e-12)
            or not math.isclose(
                embedded_slippage,
                slippage_bps,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            exclusions.append("prediction_label_cost_contract_mismatch")
    return exclusions


def build_outcome_evaluation(
    events: Sequence[Mapping[str, Any]],
    *,
    as_of: Any,
    authority_scope: Mapping[str, Any],
    validation_plan: Optional[ValidationPlan] = None,
    validation_plan_provenance: Optional[Mapping[str, Any]] = None,
    validation_plan_provenance_verifier: Optional[
        ValidationPlanProvenanceVerifier
    ] = None,
    market_truth_verifier: Optional[OutcomeMarketTruthVerifier] = None,
) -> dict[str, Any]:
    """Join predictions, labels and decision dispositions without writing facts."""

    if isinstance(events, (str, bytes, bytearray)):
        raise OutcomeEvaluationError("events_must_be_sequence")
    copied = deepcopy(list(events))
    if any(not isinstance(event, Mapping) for event in copied):
        raise OutcomeEvaluationError("event_must_be_mapping")
    # Preserve the immutable source hash while only reusing the journal's
    # canonical merge semantics after an update is bound to one exact prediction.
    projected, invalid_update_snapshots, valid_update_snapshots = (
        _project_bound_label_updates(copied)
    )
    scope = _authority_scope(authority_scope)
    cutoff = _aware(as_of, "as_of")
    source_events_sha256 = canonical_sha256(copied)
    trusted_plan_binding = _validation_plan_binding(validation_plan)
    normalized_plan_provenance, plan_provenance_proof, trusted_plan_provenance = (
        _validation_plan_provenance(
            validation_plan_provenance,
            validation_plan=validation_plan,
            validation_plan_binding=trusted_plan_binding,
            verifier=validation_plan_provenance_verifier,
        )
    )

    decisions: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for event in projected:
        if event.get(
            "audit_event_type"
        ) == "decision_exposure_disposition" and _in_scope(event, scope):
            payload = event.get("decision_exposure")
            if isinstance(payload, Mapping):
                decisions[_decision_key(payload)].append(payload)

    seen_snapshots: set[str] = set()
    outcomes: list[dict[str, Any]] = []
    excluded_authority = 0
    for event in projected:
        if not (
            event.get("journal_event_type") == "prediction_snapshot"
            or event.get("record_type") == "prediction"
        ):
            continue
        if not _in_scope(event, scope):
            excluded_authority += 1
            continue
        snapshot_id = str(event.get("snapshot_id") or "").strip()
        if not snapshot_id:
            raise OutcomeEvaluationError("snapshot_id_required")
        if snapshot_id in seen_snapshots:
            raise OutcomeEvaluationError("duplicate_prediction_identity")
        seen_snapshots.add(snapshot_id)
        prediction_at = _aware(event.get("prediction_at"), "prediction_at")
        if prediction_at > cutoff:
            raise OutcomeEvaluationError("prediction_after_as_of")
        symbol = str(event.get("symbol") or "").strip().upper()
        cluster_id = str(event.get("decision_cluster_id") or "").strip()
        if not symbol or not cluster_id:
            raise OutcomeEvaluationError("prediction_identity_incomplete")

        horizon = str(event.get("primary_label_horizon") or "").strip()
        if not horizon:
            raise OutcomeEvaluationError("primary_label_horizon_required")
        label = _primary_label(event, horizon)
        related_decisions = decisions.get((cluster_id, symbol), [])
        exclusions = _prediction_evidence_exclusions(
            event,
            prediction_at=prediction_at,
        )
        if snapshot_id not in valid_update_snapshots:
            exclusions.append("forward_label_update_required")
        if (
            snapshot_id in invalid_update_snapshots
            and snapshot_id not in valid_update_snapshots
        ):
            exclusions.append("forward_label_update_authority_or_identity_invalid")
        if not trusted_plan_provenance:
            exclusions.append("validation_plan_provenance_not_verified")
        if (
            validation_plan is None
            or validation_plan.frozen_at.astimezone(timezone.utc) > prediction_at
        ):
            exclusions.append("validation_plan_frozen_after_prediction")
        verified_trade_date, trade_date_exclusions = _verified_trade_date(
            event,
            prediction_at=prediction_at,
            validation_plan=validation_plan,
        )
        exclusions.extend(trade_date_exclusions)
        decision_id: Optional[str] = None
        action: Optional[str] = None
        disposition = "observation_only"
        disposition_reason: Optional[str] = None
        if len(related_decisions) == 1:
            decision = related_decisions[0]
            decision_id = str(decision.get("decision_id") or "").strip() or None
            action = str(decision.get("action") or "").strip() or None
            disposition = str(decision.get("disposition") or "").strip()
            disposition_reason = (
                decision.get("rejection_reason")
                or decision.get("nonfill_reason")
                or None
            )
            if (
                not decision_id
                or not action
                or disposition
                not in {
                    "paper_filled",
                    "paper_not_filled",
                    "rejected",
                    "shadow_only",
                    "observation_only",
                }
            ):
                exclusions.append("decision_evidence_incomplete_or_invalid")
            else:
                try:
                    decision_time = _aware(
                        decision.get("decision_time"), "decision_time"
                    )
                except OutcomeEvaluationError:
                    exclusions.append("decision_time_invalid")
                else:
                    if decision_time < prediction_at or decision_time > cutoff:
                        exclusions.append("decision_time_outside_valid_window")
                expected_identity = {
                    "model_id": event.get("model_id"),
                    "model_version": event.get("model_version")
                    or event.get("strategy_version"),
                }
                for field, expected_value in expected_identity.items():
                    expected = str(expected_value or "").strip()
                    actual = str(decision.get(field) or "").strip()
                    if expected and actual != expected:
                        exclusions.append("decision_prediction_identity_mismatch")
                        break
        elif len(related_decisions) > 1:
            exclusions.append("ambiguous_decision_evidence")

        exclusions.extend(
            _label_exclusions(
                event,
                label,
                prediction_at=prediction_at,
                cutoff=cutoff,
                validation_plan=validation_plan,
                validation_plan_binding=trusted_plan_binding,
            )
        )
        market_truth_verification: Optional[dict[str, Any]] = None
        try:
            market_truth_target = _aware(label.get("target_at"), "label_target_at")
        except OutcomeEvaluationError:
            market_truth_target = None
        if market_truth_target is not None:
            market_truth_verification, market_truth_verified = (
                _market_truth_verification(
                    verifier=market_truth_verifier,
                    snapshot_id=snapshot_id,
                    horizon=label["horizon"],
                    reference_evidence=event.get("reference_evidence_payload"),
                    exit_evidence=label.get("exit_evidence_payload"),
                    target_at=market_truth_target,
                    as_of=cutoff,
                )
            )
        else:
            market_truth_verified = False
        if not market_truth_verified:
            exclusions.append("market_truth_authority_not_verified")
        labels_as_of = event.get("labels_as_of")
        if labels_as_of and _aware(labels_as_of, "labels_as_of") > cutoff:
            exclusions.append("label_projection_after_as_of")
        maturity_weight = _finite_or_none(event.get("maturity_weight"))
        if maturity_weight is None or maturity_weight <= 0.0:
            exclusions.append("maturity_weight_missing_or_nonpositive")

        marketgraph = event.get("marketgraph")
        ablation_group = (
            str(marketgraph.get("ablation_group") or "unknown")
            if isinstance(marketgraph, Mapping)
            else "unknown"
        )
        row = {
            "outcome_id": "outcome:"
            + canonical_sha256(
                {
                    "snapshot_id": snapshot_id,
                    "as_of": cutoff.isoformat(),
                    "horizon": label["horizon"],
                    "authority_scope": scope,
                }
            )[:32],
            "snapshot_id": snapshot_id,
            "decision_id": decision_id,
            "decision_cluster_id": cluster_id,
            "symbol": symbol,
            "decision_at": prediction_at.isoformat(),
            "trade_date": verified_trade_date,
            "reported_trade_date": str(event.get("trade_date") or ""),
            "style": event.get("style") or event.get("style_id"),
            "model_id": event.get("model_id"),
            "model_version": event.get("model_version")
            or event.get("strategy_version"),
            "action": action,
            "ablation_group": ablation_group,
            "pair_id": event.get("pair_id"),
            "base_snapshot_sha256": event.get("base_snapshot_sha256"),
            "source_snapshot_sha256": event.get("source_snapshot_sha256"),
            "sample_intent": event.get("sample_intent"),
            "source_class": event.get("source_class"),
            "maturity_weight": maturity_weight,
            "propensity": _finite_or_none(event.get("propensity")),
            "selection_probability": _finite_or_none(
                event.get("selection_probability")
            ),
            "calibration_role": event.get("calibration_role"),
            "calibrated_probability": _finite_or_none(
                event.get("calibrated_probability")
            ),
            "probability_model_state": event.get("probability_model_state"),
            "disposition": disposition,
            "disposition_reason": disposition_reason,
            "primary_horizon": label["horizon"],
            "point_in_time_as_of": event.get("point_in_time_as_of"),
            "data_quality_sha256": (
                canonical_sha256(event["data_quality"])
                if isinstance(event.get("data_quality"), Mapping)
                else None
            ),
            "cost_contract_sha256": (
                canonical_sha256(event["costs"])
                if isinstance(event.get("costs"), Mapping)
                else None
            ),
            "label": label,
            "market_truth_verification": market_truth_verification,
            "path_outcome": {
                "status": "unavailable_without_verified_path",
                "mae_return": None,
                "mfe_return": None,
            },
            "exclusion_reasons": sorted(set(exclusions)),
            "eligible_for_statistical_learning": not exclusions,
            "eligible_for_promotion": False,
        }
        outcomes.append(row)

    outcomes.sort(
        key=lambda row: (row["decision_at"], row["symbol"], row["snapshot_id"])
    )
    report: dict[str, Any] = {
        "record_type": "ashare_outcome_evaluation",
        "schema_version": OUTCOME_EVALUATION_SCHEMA_VERSION,
        "as_of": cutoff.isoformat(),
        "source_events_sha256": source_events_sha256,
        "validation_plan_binding": deepcopy(trusted_plan_binding),
        "validation_plan_provenance": deepcopy(normalized_plan_provenance),
        "validation_plan_provenance_verification": deepcopy(plan_provenance_proof),
        "authority_scope": scope,
        "excluded_authority_event_count": excluded_authority,
        "ignored_invalid_forward_label_update_snapshot_count": len(
            invalid_update_snapshots.intersection(valid_update_snapshots)
        ),
        "outcome_count": len(outcomes),
        "outcomes": outcomes,
        "authority": deepcopy(_AUTHORITY),
    }
    report["report_sha256"] = canonical_sha256(report)
    _verify_outcome_evaluation_structure(report)
    return report


def verify_outcome_evaluation_against_source(
    value: Any,
    *,
    events: Sequence[Mapping[str, Any]],
    expected_as_of: str,
    expected_authority_scope: Mapping[str, Any],
    validation_plan: Optional[ValidationPlan] = None,
    validation_plan_provenance: Optional[Mapping[str, Any]] = None,
    validation_plan_provenance_verifier: Optional[
        ValidationPlanProvenanceVerifier
    ] = None,
    market_truth_verifier: Optional[OutcomeMarketTruthVerifier] = None,
) -> bool:
    """Rebuild one report from immutable events and require exact equality."""

    _verify_outcome_evaluation_structure(value)
    if isinstance(events, (str, bytes, bytearray)):
        raise OutcomeEvaluationError("events_must_be_sequence")
    normalized_as_of = _aware(expected_as_of, "expected_as_of").isoformat()
    if value.get("as_of") != normalized_as_of:
        raise OutcomeEvaluationError("outcome_expected_as_of_mismatch")
    normalized_scope = _authority_scope(expected_authority_scope)
    if value.get("authority_scope") != normalized_scope:
        raise OutcomeEvaluationError("outcome_expected_authority_scope_mismatch")
    expected = build_outcome_evaluation(
        events,
        as_of=normalized_as_of,
        authority_scope=normalized_scope,
        validation_plan=validation_plan,
        validation_plan_provenance=validation_plan_provenance,
        validation_plan_provenance_verifier=validation_plan_provenance_verifier,
        market_truth_verifier=market_truth_verifier,
    )
    if dict(value) != expected:
        raise OutcomeEvaluationError("outcome_report_does_not_match_source_events")
    return True


def _verify_outcome_evaluation_structure(value: Any) -> bool:
    if not isinstance(value, Mapping):
        raise OutcomeEvaluationError("outcome_report_invalid")
    if value.get("schema_version") != OUTCOME_EVALUATION_SCHEMA_VERSION:
        raise OutcomeEvaluationError("outcome_report_schema_invalid")
    if value.get("authority") != _AUTHORITY:
        raise OutcomeEvaluationError("outcome_report_authority_invalid")
    outcomes = value.get("outcomes")
    if not isinstance(outcomes, list) or value.get("outcome_count") != len(outcomes):
        raise OutcomeEvaluationError("outcome_collection_invalid")
    _authority_scope(value.get("authority_scope"))
    source_sha = str(value.get("source_events_sha256") or "").strip().lower()
    if len(source_sha) != 64 or any(
        character not in "0123456789abcdef" for character in source_sha
    ):
        raise OutcomeEvaluationError("source_events_sha256_invalid")
    binding = value.get("validation_plan_binding")
    if binding is not None and (
        not isinstance(binding, Mapping)
        or set(binding) != _FORWARD_LABEL_BINDING_KEYS
        or any(not _SHA256_RE.fullmatch(str(item or "")) for item in binding.values())
    ):
        raise OutcomeEvaluationError("validation_plan_binding_invalid")
    provenance = value.get("validation_plan_provenance")
    if provenance is not None and (
        not isinstance(provenance, Mapping)
        or set(provenance) != _VALIDATION_PLAN_PROVENANCE_KEYS
    ):
        raise OutcomeEvaluationError("validation_plan_provenance_invalid")
    provenance_proof = value.get("validation_plan_provenance_verification")
    if provenance_proof is not None and (
        not isinstance(provenance_proof, Mapping)
        or set(provenance_proof) != _VALIDATION_PLAN_PROOF_KEYS
        or not isinstance(provenance, Mapping)
    ):
        raise OutcomeEvaluationError("validation_plan_provenance_proof_invalid")
    outcome_ids: set[str] = set()
    for row in outcomes:
        if not isinstance(row, Mapping):
            raise OutcomeEvaluationError("outcome_row_invalid")
        outcome_id = str(row.get("outcome_id") or "").strip()
        if not outcome_id or outcome_id in outcome_ids:
            raise OutcomeEvaluationError("outcome_id_invalid_or_duplicate")
        outcome_ids.add(outcome_id)
        reasons = row.get("exclusion_reasons")
        if (
            not isinstance(reasons, list)
            or any(not isinstance(reason, str) for reason in reasons)
            or reasons != sorted(set(reasons))
            or row.get("eligible_for_statistical_learning") != (not reasons)
            or row.get("eligible_for_promotion") is not False
        ):
            raise OutcomeEvaluationError("outcome_eligibility_invalid")
        if row.get("path_outcome") != {
            "status": "unavailable_without_verified_path",
            "mae_return": None,
            "mfe_return": None,
        }:
            raise OutcomeEvaluationError("path_outcome_invalid")
        label = row.get("label")
        if not isinstance(label, Mapping):
            raise OutcomeEvaluationError("outcome_label_invalid")
        if not reasons and (
            label.get("status") not in {"ready", "labeled"}
            or _finite_or_none(label.get("net_return_after_costs")) is None
        ):
            raise OutcomeEvaluationError("eligible_outcome_label_invalid")
        market_truth = row.get("market_truth_verification")
        if not reasons and (
            not isinstance(market_truth, Mapping)
            or set(market_truth) != _MARKET_TRUTH_PROOF_KEYS
            or not isinstance(provenance_proof, Mapping)
            or not re.fullmatch(r"\d{8}", str(row.get("trade_date") or ""))
        ):
            raise OutcomeEvaluationError("eligible_outcome_authority_invalid")
    supplied = value.get("report_sha256")
    unsigned = deepcopy(dict(value))
    unsigned.pop("report_sha256", None)
    if supplied != canonical_sha256(unsigned):
        raise OutcomeEvaluationError("outcome_report_sha256_mismatch")
    return True


def verify_outcome_evaluation(
    value: Any,
    *,
    events: Sequence[Mapping[str, Any]],
    expected_as_of: str,
    expected_authority_scope: Mapping[str, Any],
    validation_plan: Optional[ValidationPlan] = None,
    validation_plan_provenance: Optional[Mapping[str, Any]] = None,
    validation_plan_provenance_verifier: Optional[
        ValidationPlanProvenanceVerifier
    ] = None,
    market_truth_verifier: Optional[OutcomeMarketTruthVerifier] = None,
) -> bool:
    """Public verification always binds the report to exact source events."""

    return verify_outcome_evaluation_against_source(
        value,
        events=events,
        expected_as_of=expected_as_of,
        expected_authority_scope=expected_authority_scope,
        validation_plan=validation_plan,
        validation_plan_provenance=validation_plan_provenance,
        validation_plan_provenance_verifier=validation_plan_provenance_verifier,
        market_truth_verifier=market_truth_verifier,
    )


def eligible_unambiguous_outcome_rows(
    outcome_report: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], int, int]:
    """Return one eligible row per decision cluster without post-hoc selection."""

    _verify_outcome_evaluation_structure(outcome_report)
    by_cluster: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in outcome_report["outcomes"]:
        if row.get("eligible_for_statistical_learning") is not True:
            continue
        cluster = str(row.get("decision_cluster_id") or "").strip()
        if cluster:
            by_cluster[cluster].append(row)
    ambiguous = sum(1 for rows in by_cluster.values() if len(rows) != 1)
    selected = [
        deepcopy(dict(rows[0])) for rows in by_cluster.values() if len(rows) == 1
    ]
    selected.sort(
        key=lambda row: (
            str(row.get("trade_date") or ""),
            str(row.get("decision_cluster_id") or ""),
        )
    )
    return selected, len(by_cluster), ambiguous


def outcome_rows_as_sample_records(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project the verified cohort into the existing deterministic KPI schema."""

    records: list[dict[str, Any]] = []
    seen_clusters: set[str] = set()
    for row in rows:
        if row.get("eligible_for_statistical_learning") is not True:
            raise OutcomeEvaluationError("ineligible_outcome_in_science_cohort")
        cluster = str(row.get("decision_cluster_id") or "").strip()
        label = row.get("label")
        horizon = str(row.get("primary_horizon") or "").strip()
        if (
            not cluster
            or cluster in seen_clusters
            or not isinstance(label, Mapping)
            or not horizon
        ):
            raise OutcomeEvaluationError("ambiguous_outcome_science_cohort")
        seen_clusters.add(cluster)
        records.append(
            {
                "record_type": "prediction",
                "journal_event_type": "prediction_snapshot",
                "snapshot_id": row.get("snapshot_id"),
                "decision_cluster_id": cluster,
                "symbol": row.get("symbol"),
                "style": row.get("style"),
                "prediction_at": row.get("decision_at"),
                "trade_date": row.get("trade_date"),
                "primary_label_horizon": horizon,
                "labels": {horizon: deepcopy(dict(label))},
                "maturity_weight": row.get("maturity_weight"),
                "propensity": row.get("propensity"),
                "selection_probability": row.get("selection_probability"),
                "calibration_role": row.get("calibration_role"),
                "calibrated_probability": row.get("calibrated_probability"),
                "probability_model_state": row.get("probability_model_state"),
                "sample_intent": row.get("sample_intent"),
                "source_class": row.get("source_class"),
            }
        )
    return records


__all__ = [
    "OUTCOME_EVALUATION_SCHEMA_VERSION",
    "OutcomeEvaluationError",
    "OutcomeMarketTruthVerifier",
    "ValidationPlanProvenanceVerifier",
    "build_outcome_evaluation",
    "canonical_sha256",
    "eligible_unambiguous_outcome_rows",
    "outcome_rows_as_sample_records",
    "verify_outcome_evaluation",
    "verify_outcome_evaluation_against_source",
]
