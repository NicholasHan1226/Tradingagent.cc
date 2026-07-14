"""Fail-closed, shadow-only sector-flow confirmation contract.

This module deliberately has no decision or execution dependency.  It builds a
paired feature record for later observation and makes non-consumption explicit.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import hmac
import json
import math
import re
from typing import Any, Mapping


FEATURE_NAME = "sector_flow_confirmation"
FEATURE_VERSION = "sector-flow-confirmation-shadow-v1"
PAIRING_VERSION = "sector-flow-confirmation-pair-v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SNAPSHOT_PAYLOAD_FIELDS = (
    "scope",
    "sector_id",
    "sector_name",
    "taxonomy",
    "snapshot_id",
    "net_inflow_cny",
    "rank",
    "event_time",
    "available_at",
)


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _native_nonempty_string(value: Any) -> str | None:
    if type(value) is not str:
        return None
    normalized = value.strip()
    return normalized or None


def _aware_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _decision_identity(
    *,
    base_snapshot_sha256: str,
    decision_as_of: str,
    pair_identity_sha256: str | None,
) -> dict[str, Any]:
    return {
        "identity_version": "sector-flow-decision-identity-v1",
        "base_snapshot_sha256": base_snapshot_sha256,
        "decision_as_of": decision_as_of,
        "pair_identity_sha256": pair_identity_sha256,
    }


def _consumption_receipt(identity: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "consumer": "shadow_observation_only",
        "consumed": False,
        "changed_candidate_membership": False,
        "changed_ranking": False,
        "changed_playbook": False,
        "changed_strategy": False,
        "changed_execution_eligibility": False,
        "execution_gate_bypassed": False,
        "before_identity": deepcopy(dict(identity)),
        "after_identity": deepcopy(dict(identity)),
        "reason": "feature_not_wired_to_decision_consumers",
    }


def _feature_record(
    *,
    enabled: bool,
    base_snapshot_sha256: str,
    decision_as_of: str,
    pair_identity_sha256: str | None,
) -> dict[str, Any]:
    identity = _decision_identity(
        base_snapshot_sha256=base_snapshot_sha256,
        decision_as_of=decision_as_of,
        pair_identity_sha256=pair_identity_sha256,
    )
    return {
        "feature_name": FEATURE_NAME,
        "feature_version": FEATURE_VERSION,
        "enabled": enabled,
        "shadow_only": True,
        "applied": False,
        "base_snapshot_sha256": base_snapshot_sha256,
        "decision_as_of": decision_as_of,
        "pair_identity_sha256": pair_identity_sha256,
        "consumption_receipt": _consumption_receipt(identity),
    }


def _degraded(
    reason: str,
    *,
    base_snapshot_sha256: str,
    decision_as_of: str,
    pair_identity_sha256: str | None,
) -> dict[str, Any]:
    return {
        **_feature_record(
            enabled=True,
            base_snapshot_sha256=base_snapshot_sha256,
            decision_as_of=decision_as_of,
            pair_identity_sha256=pair_identity_sha256,
        ),
        "status": "degraded",
        "reason": reason,
        "confirmation": None,
        "point_in_time_lineage": {"qualified": False, "reason": reason},
    }


def _build_on(
    *,
    sector_id: Any,
    decision_as_of: str,
    sector_snapshot: Mapping[str, Any] | None,
    base_snapshot_sha256: str,
    pair_identity_sha256: str | None,
) -> dict[str, Any]:
    def degraded(reason: str) -> dict[str, Any]:
        return _degraded(
            reason,
            base_snapshot_sha256=base_snapshot_sha256,
            decision_as_of=decision_as_of,
            pair_identity_sha256=pair_identity_sha256,
        )

    if not isinstance(sector_snapshot, Mapping):
        return degraded("missing_sector_flow_snapshot")

    snapshot = deepcopy(dict(sector_snapshot))
    raw_scope = snapshot.get("scope")
    if raw_scope is None:
        return degraded("flow_scope_is_not_sector")
    if type(raw_scope) is not str:
        return degraded("invalid_flow_scope_type")
    scope = _native_nonempty_string(raw_scope)
    if scope is None or scope.lower() != "sector":
        return degraded("flow_scope_is_not_sector")
    if type(sector_id) is not str:
        return degraded("invalid_requested_sector_id_type")
    requested_sector_id = _native_nonempty_string(sector_id)
    if requested_sector_id is None:
        return degraded("missing_requested_sector_id")
    raw_snapshot_sector_id = snapshot.get("sector_id")
    if type(raw_snapshot_sector_id) is not str:
        return degraded("invalid_snapshot_sector_id_type")
    snapshot_sector_id = _native_nonempty_string(raw_snapshot_sector_id)
    if snapshot_sector_id is None:
        return degraded("missing_snapshot_sector_id")
    if snapshot_sector_id != requested_sector_id:
        return degraded("sector_id_mismatch")
    raw_snapshot_id = snapshot.get("snapshot_id")
    if type(raw_snapshot_id) is not str:
        return degraded("invalid_snapshot_id_type")
    snapshot_id = _native_nonempty_string(raw_snapshot_id)
    if snapshot_id is None:
        return degraded("missing_snapshot_id")
    raw_taxonomy = snapshot.get("taxonomy")
    if type(raw_taxonomy) is not str:
        return degraded("invalid_sector_taxonomy_type")
    taxonomy = _native_nonempty_string(raw_taxonomy)
    if taxonomy is None:
        return degraded("missing_sector_taxonomy")

    source_sha = str(snapshot.get("source_snapshot_sha256") or "").strip().lower()
    if not _SHA256_RE.fullmatch(source_sha):
        return degraded("invalid_source_snapshot_sha256")

    decision_time = _aware_datetime(decision_as_of)
    event_time = _aware_datetime(snapshot.get("event_time"))
    available_at = _aware_datetime(snapshot.get("available_at"))
    if decision_time is None:
        return degraded("invalid_decision_as_of")
    if event_time is None:
        return degraded("invalid_sector_flow_event_time")
    if available_at is None:
        return degraded("invalid_sector_flow_available_at")
    if event_time > available_at:
        return degraded("snapshot_available_before_event")
    if available_at > decision_time:
        return degraded("snapshot_available_after_decision")

    raw_net_inflow_cny = snapshot.get("net_inflow_cny")
    if type(raw_net_inflow_cny) not in {int, float}:
        return degraded("invalid_sector_flow_value_type")
    try:
        net_inflow_cny = float(raw_net_inflow_cny)
    except (TypeError, ValueError, OverflowError):
        return degraded("invalid_sector_flow_value")
    if not math.isfinite(net_inflow_cny):
        return degraded("invalid_sector_flow_value")
    raw_rank = snapshot.get("rank")
    if type(raw_rank) is not int or raw_rank < 1:
        return degraded("invalid_sector_flow_rank")
    rank = raw_rank

    canonical_snapshot_payload = {
        field: snapshot.get(field) for field in _SNAPSHOT_PAYLOAD_FIELDS
    }
    computed_source_sha = _canonical_sha256(canonical_snapshot_payload)
    if not hmac.compare_digest(source_sha, computed_source_sha):
        return degraded("source_snapshot_sha256_mismatch")

    confirmation = bool(net_inflow_cny > 0 and rank <= 3)
    return {
        **_feature_record(
            enabled=True,
            base_snapshot_sha256=base_snapshot_sha256,
            decision_as_of=decision_as_of,
            pair_identity_sha256=pair_identity_sha256,
        ),
        "status": "confirmed" if confirmation else "not_confirmed",
        "reason": None,
        "confirmation": confirmation,
        "sector_id": requested_sector_id,
        "sector_name": str(snapshot.get("sector_name") or "").strip() or None,
        "taxonomy": taxonomy,
        "snapshot_id": snapshot_id,
        "net_inflow_cny": net_inflow_cny,
        "rank": rank,
        "source_snapshot_sha256": source_sha,
        "computed_source_snapshot_sha256": computed_source_sha,
        "point_in_time_lineage": {
            "qualified": True,
            "event_time": event_time.isoformat(),
            "available_at": available_at.isoformat(),
            "decision_as_of": decision_time.isoformat(),
            "source_snapshot_sha256": source_sha,
        },
    }


def build_sector_flow_confirmation_pair(
    *,
    base_snapshot_sha256: str,
    sector_id: Any,
    decision_as_of: str,
    sector_snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return paired off/on shadow records bound to one immutable base."""

    base_sha = str(base_snapshot_sha256 or "").strip().lower()
    parsed_decision = _aware_datetime(decision_as_of)
    canonical_decision_as_of = (
        parsed_decision.isoformat() if parsed_decision is not None else str(decision_as_of)
    )
    requested_sector_id = _native_nonempty_string(sector_id)
    snapshot = dict(sector_snapshot) if isinstance(sector_snapshot, Mapping) else None
    snapshot_scope = (
        _native_nonempty_string(snapshot.get("scope")) if snapshot is not None else None
    )
    snapshot_sector_id = (
        _native_nonempty_string(snapshot.get("sector_id"))
        if snapshot is not None
        else None
    )
    snapshot_id = (
        _native_nonempty_string(snapshot.get("snapshot_id"))
        if snapshot is not None
        else None
    )
    taxonomy = (
        _native_nonempty_string(snapshot.get("taxonomy"))
        if snapshot is not None
        else None
    )
    pair_identity_valid = bool(
        requested_sector_id is not None
        and snapshot_scope is not None
        and snapshot_scope.lower() == "sector"
        and snapshot_sector_id == requested_sector_id
        and snapshot_id is not None
        and taxonomy is not None
    )
    pair_identity_sha256 = (
        _canonical_sha256(
            {
                "pairing_version": PAIRING_VERSION,
                "base_snapshot_sha256": base_sha or None,
                "sector_id": requested_sector_id,
                "decision_as_of": canonical_decision_as_of,
            }
        )
        if pair_identity_valid
        else None
    )
    off = {
        **_feature_record(
            enabled=False,
            base_snapshot_sha256=base_sha,
            decision_as_of=canonical_decision_as_of,
            pair_identity_sha256=pair_identity_sha256,
        ),
        "status": "disabled",
        "reason": "feature_switch_off",
        "confirmation": None,
        "point_in_time_lineage": {"qualified": False, "reason": "feature_switch_off"},
    }
    on = (
        _build_on(
            sector_id=sector_id,
            decision_as_of=canonical_decision_as_of,
            sector_snapshot=sector_snapshot,
            base_snapshot_sha256=base_sha,
            pair_identity_sha256=pair_identity_sha256,
        )
        if _SHA256_RE.fullmatch(base_sha)
        else _degraded(
            "invalid_base_snapshot_sha256",
            base_snapshot_sha256=base_sha,
            decision_as_of=canonical_decision_as_of,
            pair_identity_sha256=pair_identity_sha256,
        )
    )
    pairing = {
        "pairing_version": PAIRING_VERSION,
        "same_base_snapshot": bool(_SHA256_RE.fullmatch(base_sha)),
        "base_snapshot_sha256": base_sha or None,
        "off_enabled": False,
        "on_enabled": True,
        "sector_id": requested_sector_id,
        "decision_as_of": canonical_decision_as_of,
        "pair_identity_valid": pair_identity_valid,
        "pair_identity_sha256": pair_identity_sha256,
    }
    pairing["pair_sha256"] = _canonical_sha256(
        {"pairing": pairing, "off": off, "on": on}
    )
    return {"feature_name": FEATURE_NAME, "pairing": pairing, "off": off, "on": on}


__all__ = ["build_sector_flow_confirmation_pair"]
