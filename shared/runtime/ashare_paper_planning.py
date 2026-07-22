"""Deterministic observation-only A-share paper-planning boundary.

The module consumes already-frozen runtime and prospective-history authorities.
It never reads a provider, computes a predictive score, allocates money, or
creates an executable instruction.  Until a separately proven ranking and
minute-evidence authority exists, every result is an immutable abstention.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from shared.data.research_snapshot import ResearchDataSnapshot
from shared.runtime.ashare_observation_history import (
    ASHARE_OBSERVATION_HISTORY_READINESS_SCHEMA_ID,
    PROSPECTIVE_OBSERVATION_HISTORY,
    AshareObservationFeatureReadiness,
    AshareObservationHistoryCoverage,
    AshareObservationHistoryReadiness,
)
from shared.runtime.ashare_runtime_ports import (
    ASHARE_RUNTIME_AUTHORITY_SCHEMA_ID,
    AshareRuntimeAuthorityBundle,
)


ASHARE_PAPER_PLANNING_DECISION_SCHEMA_ID = (
    "tradingagent.ashare.paper-planning-decision.v1"
)
_ARTIFACT_TYPE = "ashare_paper_planning_decision.v1"
_BINDING_TYPE = "ashare_paper_planning_day_binding.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_RE = re.compile(r"^[0-9]{8}$")
_SYMBOL_RE = re.compile(r"^(?:60|00)[0-9]{4}\.(?:SH|SZ)$")
_RUNTIME_BLOCKERS = frozenset(
    {
        "champion_numeric_features_unavailable",
        "minute_execution_evidence_unavailable",
    }
)
_CORE_BLOCKERS = frozenset(
    {
        *_RUNTIME_BLOCKERS,
        "next_trade_session_authority_unavailable",
    }
)
_FEATURE_IDS = frozenset({"momentum_20d", "low_volatility_20d", "adv_20d"})
_DECISION_FIELDS = frozenset(
    {
        "schema_id",
        "decision_id",
        "observation_session",
        "paper_trade_session",
        "decision_as_of",
        "profile_id",
        "catalog_version",
        "schema_major",
        "target_symbol",
        "snapshot_sha256",
        "probe_receipt_sha256",
        "observation_receipt_sha256",
        "observation_membership_sha256",
        "observation_transaction_complete_sha256",
        "history_identity_sha256",
        "history_mode",
        "history_session_count",
        "history_min_required_sessions",
        "status",
        "action",
        "disposition",
        "authority",
        "simulation_only",
        "real_trading_enabled",
        "blockers",
    }
)
_BINDING_FIELDS = frozenset(
    {
        "binding_type",
        "observation_session",
        "paper_trade_session",
        "decision_id",
        "decision_sha256",
        "artifact_sha256",
        "snapshot_sha256",
        "probe_receipt_sha256",
        "observation_receipt_sha256",
        "observation_membership_sha256",
        "observation_transaction_complete_sha256",
        "history_identity_sha256",
        "content_sha256",
    }
)


class AsharePaperPlanningContractError(ValueError):
    """Raised when supplied authorities cannot support an honest abstention."""


class AsharePaperPlanningStoreCorruption(RuntimeError):
    """Raised when persisted paper-planning evidence is not trustworthy."""


class AsharePaperPlanningStoreConflict(RuntimeError):
    """Raised when immutable single-day or CAS semantics reject a write."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_noncanonical_value"
        ) from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_value(value: object) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _require_sha256(value: object, *, field_name: str) -> str:
    if not _valid_sha256(value):
        raise AsharePaperPlanningContractError(
            f"ashare_paper_planning_{field_name}_invalid"
        )
    return str(value)


def _require_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AsharePaperPlanningContractError(
            f"ashare_paper_planning_{field_name}_invalid"
        )
    return value


def _parse_instant(value: object, *, field_name: str) -> datetime:
    text = _require_text(value, field_name=field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AsharePaperPlanningContractError(
            f"ashare_paper_planning_{field_name}_invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AsharePaperPlanningContractError(
            f"ashare_paper_planning_{field_name}_invalid"
        )
    return parsed


def _runtime_identity(
    authority: AshareRuntimeAuthorityBundle,
) -> tuple[str, str, int, str, str, str, str, str, str, str]:
    if type(authority) is not AshareRuntimeAuthorityBundle:
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_runtime_authority_type_invalid"
        )
    if authority.schema_id != ASHARE_RUNTIME_AUTHORITY_SCHEMA_ID:
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_runtime_authority_schema_invalid"
        )
    if (
        authority.committed_state_verified is not True
        or authority.observation_eligible is not True
        or authority.ranking_eligible is not False
        or authority.planning_eligible is not False
        or authority.execution_evidence_eligible is not False
        or authority.historical_pit_eligible is not False
        or authority.historical_feature_claims
    ):
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_runtime_authority_scope_invalid"
        )
    if (
        not isinstance(authority.blockers, tuple)
        or len(set(authority.blockers)) != len(authority.blockers)
        or any(
            not isinstance(reason, str) or not reason or reason != reason.strip()
            for reason in authority.blockers
        )
        or not _RUNTIME_BLOCKERS.issubset(authority.blockers)
    ):
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_runtime_blockers_invalid"
        )
    profile_id = _require_text(authority.profile_id, field_name="profile_id")
    catalog_version = _require_text(
        authority.catalog_version,
        field_name="catalog_version",
    )
    if (
        isinstance(authority.schema_major, bool)
        or not isinstance(authority.schema_major, int)
        or authority.schema_major <= 0
    ):
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_schema_major_invalid"
        )
    decision_as_of = _require_text(
        authority.decision_as_of,
        field_name="decision_as_of",
    )
    decision_instant = _parse_instant(decision_as_of, field_name="decision_as_of")
    snapshot_sha256 = _require_sha256(
        authority.snapshot_sha256,
        field_name="snapshot_sha256",
    )
    probe_sha256 = _require_sha256(
        authority.probe_receipt_sha256,
        field_name="probe_receipt_sha256",
    )
    observation_sha256 = _require_sha256(
        authority.observation_receipt_sha256,
        field_name="observation_receipt_sha256",
    )
    membership_sha256 = _require_sha256(
        authority.observation_membership_sha256,
        field_name="observation_membership_sha256",
    )
    transaction_complete_sha256 = _require_sha256(
        authority.observation_transaction_complete_sha256,
        field_name="observation_transaction_complete_sha256",
    )
    snapshot = authority.research_snapshot
    if type(snapshot) is not ResearchDataSnapshot:
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_research_snapshot_invalid"
        )
    if (
        snapshot.profile_id != profile_id
        or snapshot.catalog_version != catalog_version
        or snapshot.decision_as_of != decision_as_of
        or snapshot.snapshot_sha256 != snapshot_sha256
        or snapshot.execution_eligible is not True
        or snapshot.historical_pit_eligible is not False
        or snapshot.blocking_reasons
    ):
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_runtime_snapshot_identity_mismatch"
        )
    expected_snapshot_sha256 = _sha256_value(
        {
            "profile_id": snapshot.profile_id,
            "profile_contract_sha256": snapshot.profile_contract_sha256,
            "catalog_version": snapshot.catalog_version,
            "decision_as_of": snapshot.decision_as_of,
            "datasets": [
                {
                    "dataset_id": item.dataset_id,
                    "role": item.role,
                    "response_sha256": item.response_sha256,
                }
                for item in snapshot.datasets
            ],
            "blocking_reasons": list(snapshot.blocking_reasons),
        }
    )
    if expected_snapshot_sha256 != snapshot_sha256:
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_runtime_snapshot_hash_mismatch"
        )
    daily = [
        item
        for item in snapshot.datasets
        if item.row_event_time_field == "trade_date"
        and item.row_event_time_format == "yyyymmdd"
        and item.row_event_time_semantic == "session"
        and {"ts_code", "trade_date"}.issubset(item.identity_fields)
    ]
    if len(daily) != 1 or daily[0].historical_pit_eligible is not False:
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_daily_snapshot_contract_invalid"
        )
    observation_session = daily[0].max_row_event_value
    if (
        not isinstance(observation_session, str)
        or _SESSION_RE.fullmatch(observation_session) is None
    ):
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_session_date_invalid"
        )
    try:
        session = datetime.strptime(observation_session, "%Y%m%d").date()
    except ValueError as exc:
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_session_date_invalid"
        ) from exc
    if decision_instant.astimezone(ZoneInfo("Asia/Shanghai")).date() != session:
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_session_decision_date_mismatch"
        )
    return (
        profile_id,
        catalog_version,
        authority.schema_major,
        decision_as_of,
        observation_session,
        snapshot_sha256,
        probe_sha256,
        observation_sha256,
        membership_sha256,
        transaction_complete_sha256,
    )


def _history_identity(
    history: AshareObservationHistoryReadiness,
) -> tuple[str, str, int, int]:
    if type(history) is not AshareObservationHistoryReadiness:
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_history_type_invalid"
        )
    if (
        history.schema_id != ASHARE_OBSERVATION_HISTORY_READINESS_SCHEMA_ID
        or history.history_mode != PROSPECTIVE_OBSERVATION_HISTORY
    ):
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_history_schema_invalid"
        )
    history_sha256 = _require_sha256(
        history.history_identity_sha256,
        field_name="history_identity_sha256",
    )
    if (
        isinstance(history.session_count, bool)
        or not isinstance(history.session_count, int)
        or history.session_count < 0
        or isinstance(history.min_required_sessions, bool)
        or not isinstance(history.min_required_sessions, int)
        or history.min_required_sessions < 21
    ):
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_history_count_invalid"
        )
    if type(history.coverage) is not AshareObservationHistoryCoverage:
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_history_coverage_invalid"
        )
    coverage = history.coverage
    if (
        not isinstance(coverage.target_symbol, str)
        or _SYMBOL_RE.fullmatch(coverage.target_symbol) is None
        or coverage.expected_session_count != history.session_count
        or isinstance(coverage.complete_session_count, bool)
        or not isinstance(coverage.complete_session_count, int)
        or not 0 <= coverage.complete_session_count <= history.session_count
        or isinstance(coverage.coverage_ratio, bool)
        or not isinstance(coverage.coverage_ratio, (int, float))
        or coverage.coverage_ratio
        != (
            coverage.complete_session_count / history.session_count
            if history.session_count
            else 0.0
        )
        or any(
            not isinstance(values, tuple)
            for values in (
                coverage.incomplete_sessions,
                coverage.missing_sessions,
                coverage.duplicate_row_sessions,
                coverage.invalid_value_sessions,
            )
        )
    ):
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_history_coverage_invalid"
        )
    if (
        not isinstance(history.blockers, tuple)
        or len(set(history.blockers)) != len(history.blockers)
        or any(
            not isinstance(reason, str) or not reason or reason != reason.strip()
            for reason in history.blockers
        )
    ):
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_history_blockers_invalid"
        )
    expected_eligible = not history.blockers
    if history.prospective_history_eligible is not expected_eligible:
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_history_eligibility_mismatch"
        )
    if history.session_count < history.min_required_sessions and (
        "insufficient_prospective_sessions" not in history.blockers
    ):
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_history_insufficient_blocker_missing"
        )
    if history.prospective_history_eligible and (
        history.session_count < history.min_required_sessions
        or coverage.complete_session_count != history.session_count
        or coverage.incomplete_sessions
    ):
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_history_eligibility_mismatch"
        )
    if (
        not isinstance(history.feature_readiness, tuple)
        or len(history.feature_readiness) != len(_FEATURE_IDS)
        or {item.feature_id for item in history.feature_readiness} != _FEATURE_IDS
    ):
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_feature_readiness_invalid"
        )
    for feature in history.feature_readiness:
        if type(feature) is not AshareObservationFeatureReadiness or (
            feature.history_mode != history.history_mode
            or feature.required_sessions != history.min_required_sessions
            or feature.observed_sessions != coverage.complete_session_count
            or feature.eligible is not history.prospective_history_eligible
            or feature.blockers != history.blockers
        ):
            raise AsharePaperPlanningContractError(
                "ashare_paper_planning_feature_readiness_invalid"
            )
    return (
        coverage.target_symbol,
        history_sha256,
        history.session_count,
        history.min_required_sessions,
    )


@dataclass(frozen=True)
class AshareDailyPlanningDecision:
    """Frozen non-authority result for one A-share observation session."""

    decision_id: str
    observation_session: str
    paper_trade_session: None
    decision_as_of: str
    profile_id: str
    catalog_version: str
    schema_major: int
    target_symbol: str
    snapshot_sha256: str
    probe_receipt_sha256: str
    observation_receipt_sha256: str
    observation_membership_sha256: str
    observation_transaction_complete_sha256: str
    history_identity_sha256: str
    history_mode: str
    history_session_count: int
    history_min_required_sessions: int
    status: str
    action: str
    disposition: str
    authority: str
    simulation_only: bool
    real_trading_enabled: bool
    blockers: tuple[str, ...]
    decision_sha256: str
    schema_id: str = ASHARE_PAPER_PLANNING_DECISION_SCHEMA_ID

    def canonical_bytes(self) -> bytes:
        """Return the exact content-addressed bytes (hash field excluded)."""

        return _canonical_json(_decision_payload(self)).encode("utf-8")


def _decision_payload(decision: AshareDailyPlanningDecision) -> dict[str, Any]:
    return {
        "action": decision.action,
        "authority": decision.authority,
        "blockers": list(decision.blockers),
        "catalog_version": decision.catalog_version,
        "decision_as_of": decision.decision_as_of,
        "decision_id": decision.decision_id,
        "disposition": decision.disposition,
        "history_identity_sha256": decision.history_identity_sha256,
        "history_min_required_sessions": decision.history_min_required_sessions,
        "history_mode": decision.history_mode,
        "history_session_count": decision.history_session_count,
        "observation_receipt_sha256": decision.observation_receipt_sha256,
        "observation_membership_sha256": decision.observation_membership_sha256,
        "observation_transaction_complete_sha256": (
            decision.observation_transaction_complete_sha256
        ),
        "probe_receipt_sha256": decision.probe_receipt_sha256,
        "profile_id": decision.profile_id,
        "real_trading_enabled": decision.real_trading_enabled,
        "schema_id": decision.schema_id,
        "schema_major": decision.schema_major,
        "observation_session": decision.observation_session,
        "paper_trade_session": decision.paper_trade_session,
        "simulation_only": decision.simulation_only,
        "snapshot_sha256": decision.snapshot_sha256,
        "status": decision.status,
        "target_symbol": decision.target_symbol,
    }


def _validate_decision(decision: AshareDailyPlanningDecision) -> None:
    if type(decision) is not AshareDailyPlanningDecision:
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_decision_type_invalid"
        )
    payload = _decision_payload(decision)
    if set(payload) != _DECISION_FIELDS:
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_decision_fields_invalid"
        )
    source_identity = {
        "schema_id": decision.schema_id,
        "observation_session": decision.observation_session,
        "paper_trade_session": decision.paper_trade_session,
        "profile_id": decision.profile_id,
        "catalog_version": decision.catalog_version,
        "schema_major": decision.schema_major,
        "target_symbol": decision.target_symbol,
        "snapshot_sha256": decision.snapshot_sha256,
        "probe_receipt_sha256": decision.probe_receipt_sha256,
        "observation_receipt_sha256": decision.observation_receipt_sha256,
        "observation_membership_sha256": decision.observation_membership_sha256,
        "observation_transaction_complete_sha256": (
            decision.observation_transaction_complete_sha256
        ),
        "history_identity_sha256": decision.history_identity_sha256,
    }
    if decision.decision_id != _sha256_value(source_identity):
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_decision_id_invalid"
        )
    if decision.decision_sha256 != _sha256_value(payload):
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_decision_sha256_invalid"
        )
    if (
        decision.schema_id != ASHARE_PAPER_PLANNING_DECISION_SCHEMA_ID
        or _SESSION_RE.fullmatch(decision.observation_session) is None
        or decision.paper_trade_session is not None
        or decision.status != "completed_with_blocks"
        or decision.action != "abstain"
        or decision.disposition != "observation_only"
        or decision.authority != "non_authority"
        or decision.simulation_only is not True
        or decision.real_trading_enabled is not False
        or decision.blockers != tuple(sorted(set(decision.blockers)))
        or not _CORE_BLOCKERS.issubset(decision.blockers)
    ):
        raise AsharePaperPlanningContractError(
            "ashare_paper_planning_decision_semantics_invalid"
        )


def build_ashare_daily_planning_decision(
    *,
    runtime_authority: AshareRuntimeAuthorityBundle,
    history_readiness: AshareObservationHistoryReadiness,
) -> AshareDailyPlanningDecision:
    """Bind both authorities and publish only an observation abstention."""

    (
        profile_id,
        catalog_version,
        schema_major,
        decision_as_of,
        observation_session,
        snapshot_sha256,
        probe_sha256,
        observation_sha256,
        membership_sha256,
        transaction_complete_sha256,
    ) = _runtime_identity(runtime_authority)
    (
        target_symbol,
        history_sha256,
        history_session_count,
        min_required_sessions,
    ) = _history_identity(history_readiness)
    blockers = tuple(
        sorted(
            set(runtime_authority.blockers)
            | set(history_readiness.blockers)
            | _CORE_BLOCKERS
        )
    )
    identity_payload = {
        "schema_id": ASHARE_PAPER_PLANNING_DECISION_SCHEMA_ID,
        "observation_session": observation_session,
        "paper_trade_session": None,
        "profile_id": profile_id,
        "catalog_version": catalog_version,
        "schema_major": schema_major,
        "target_symbol": target_symbol,
        "snapshot_sha256": snapshot_sha256,
        "probe_receipt_sha256": probe_sha256,
        "observation_receipt_sha256": observation_sha256,
        "observation_membership_sha256": membership_sha256,
        "observation_transaction_complete_sha256": transaction_complete_sha256,
        "history_identity_sha256": history_sha256,
    }
    decision_id = _sha256_value(identity_payload)
    provisional = AshareDailyPlanningDecision(
        decision_id=decision_id,
        observation_session=observation_session,
        paper_trade_session=None,
        decision_as_of=decision_as_of,
        profile_id=profile_id,
        catalog_version=catalog_version,
        schema_major=schema_major,
        target_symbol=target_symbol,
        snapshot_sha256=snapshot_sha256,
        probe_receipt_sha256=probe_sha256,
        observation_receipt_sha256=observation_sha256,
        observation_membership_sha256=membership_sha256,
        observation_transaction_complete_sha256=transaction_complete_sha256,
        history_identity_sha256=history_sha256,
        history_mode=PROSPECTIVE_OBSERVATION_HISTORY,
        history_session_count=history_session_count,
        history_min_required_sessions=min_required_sessions,
        status="completed_with_blocks",
        action="abstain",
        disposition="observation_only",
        authority="non_authority",
        simulation_only=True,
        real_trading_enabled=False,
        blockers=blockers,
        decision_sha256="",
    )
    decision = AshareDailyPlanningDecision(
        **{
            **provisional.__dict__,
            "decision_sha256": _sha256_value(_decision_payload(provisional)),
        }
    )
    _validate_decision(decision)
    return decision


def _decode_decision(
    raw: object, *, expected_sha256: str
) -> AshareDailyPlanningDecision:
    if not isinstance(raw, Mapping) or set(raw) != _DECISION_FIELDS:
        raise AsharePaperPlanningStoreCorruption(
            "ashare_paper_planning_store_artifact_fields_invalid"
        )
    try:
        blockers = raw.get("blockers")
        if not isinstance(blockers, list):
            raise AsharePaperPlanningContractError("blockers_invalid")
        decision = AshareDailyPlanningDecision(
            decision_id=raw.get("decision_id"),
            observation_session=raw.get("observation_session"),
            paper_trade_session=raw.get("paper_trade_session"),
            decision_as_of=raw.get("decision_as_of"),
            profile_id=raw.get("profile_id"),
            catalog_version=raw.get("catalog_version"),
            schema_major=raw.get("schema_major"),
            target_symbol=raw.get("target_symbol"),
            snapshot_sha256=raw.get("snapshot_sha256"),
            probe_receipt_sha256=raw.get("probe_receipt_sha256"),
            observation_receipt_sha256=raw.get("observation_receipt_sha256"),
            observation_membership_sha256=raw.get(
                "observation_membership_sha256"
            ),
            observation_transaction_complete_sha256=raw.get(
                "observation_transaction_complete_sha256"
            ),
            history_identity_sha256=raw.get("history_identity_sha256"),
            history_mode=raw.get("history_mode"),
            history_session_count=raw.get("history_session_count"),
            history_min_required_sessions=raw.get("history_min_required_sessions"),
            status=raw.get("status"),
            action=raw.get("action"),
            disposition=raw.get("disposition"),
            authority=raw.get("authority"),
            simulation_only=raw.get("simulation_only"),
            real_trading_enabled=raw.get("real_trading_enabled"),
            blockers=tuple(blockers),
            decision_sha256=expected_sha256,
            schema_id=raw.get("schema_id"),
        )
        _validate_decision(decision)
    except (AsharePaperPlanningContractError, TypeError) as exc:
        raise AsharePaperPlanningStoreCorruption(
            "ashare_paper_planning_store_artifact_invalid"
        ) from exc
    return decision


def _binding_payload(decision: AshareDailyPlanningDecision) -> dict[str, Any]:
    unsigned: dict[str, Any] = {
        "artifact_sha256": decision.decision_sha256,
        "binding_type": _BINDING_TYPE,
        "decision_id": decision.decision_id,
        "decision_sha256": decision.decision_sha256,
        "history_identity_sha256": decision.history_identity_sha256,
        "observation_receipt_sha256": decision.observation_receipt_sha256,
        "observation_membership_sha256": decision.observation_membership_sha256,
        "observation_transaction_complete_sha256": (
            decision.observation_transaction_complete_sha256
        ),
        "probe_receipt_sha256": decision.probe_receipt_sha256,
        "observation_session": decision.observation_session,
        "paper_trade_session": decision.paper_trade_session,
        "snapshot_sha256": decision.snapshot_sha256,
    }
    unsigned["content_sha256"] = _sha256_value(unsigned)
    return unsigned


def _decode_binding(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != _BINDING_FIELDS:
        raise AsharePaperPlanningStoreCorruption(
            "ashare_paper_planning_store_binding_fields_invalid"
        )
    payload = dict(raw)
    content_sha256 = payload.pop("content_sha256")
    if (
        raw.get("binding_type") != _BINDING_TYPE
        or content_sha256 != _sha256_value(payload)
        or not isinstance(raw.get("observation_session"), str)
        or _SESSION_RE.fullmatch(str(raw.get("observation_session"))) is None
        or raw.get("paper_trade_session") is not None
        or any(
            not _valid_sha256(raw.get(field_name))
            for field_name in (
                "decision_id",
                "decision_sha256",
                "artifact_sha256",
                "snapshot_sha256",
                "probe_receipt_sha256",
                "observation_receipt_sha256",
                "observation_membership_sha256",
                "observation_transaction_complete_sha256",
                "history_identity_sha256",
            )
        )
        or raw.get("artifact_sha256") != raw.get("decision_sha256")
    ):
        raise AsharePaperPlanningStoreCorruption(
            "ashare_paper_planning_store_binding_invalid"
        )
    return dict(raw)


def _assert_safe_path(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise AsharePaperPlanningStoreCorruption(
                "ashare_paper_planning_store_path_unreadable"
            ) from exc
        if stat.S_ISLNK(mode):
            raise AsharePaperPlanningStoreCorruption(
                "ashare_paper_planning_store_symlink_forbidden"
            )
        if current != absolute and not stat.S_ISDIR(mode):
            raise AsharePaperPlanningStoreCorruption(
                "ashare_paper_planning_store_parent_not_directory"
            )


def _no_follow_flag() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _directory_flag() -> int:
    return getattr(os, "O_DIRECTORY", 0)


def _same_file_identity(fd: int, path: Path, *, kind: str) -> os.stat_result:
    try:
        fd_stat = os.fstat(fd)
        path_stat = path.lstat()
    except OSError as exc:
        raise AsharePaperPlanningStoreCorruption(
            f"ashare_paper_planning_store_{kind}_identity_unavailable"
        ) from exc
    if not stat.S_ISREG(fd_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise AsharePaperPlanningStoreCorruption(
            f"ashare_paper_planning_store_{kind}_not_regular"
        )
    if fd_stat.st_nlink != 1 or path_stat.st_nlink != 1:
        raise AsharePaperPlanningStoreCorruption(
            f"ashare_paper_planning_store_{kind}_hardlink_forbidden"
        )
    if (
        stat.S_IMODE(fd_stat.st_mode) != 0o600
        or stat.S_IMODE(path_stat.st_mode) != 0o600
    ):
        raise AsharePaperPlanningStoreCorruption(
            f"ashare_paper_planning_store_{kind}_mode_invalid"
        )
    if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
        raise AsharePaperPlanningStoreCorruption(
            f"ashare_paper_planning_store_{kind}_identity_changed"
        )
    return fd_stat


def _fsync_directory(path: Path) -> None:
    _assert_safe_path(path)
    try:
        fd = os.open(
            os.fspath(path),
            os.O_RDONLY | _directory_flag() | _no_follow_flag(),
        )
    except OSError as exc:
        raise AsharePaperPlanningStoreCorruption(
            "ashare_paper_planning_store_directory_sync_failed"
        ) from exc
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise AsharePaperPlanningStoreCorruption(
                "ashare_paper_planning_store_root_not_directory"
            )
        os.fsync(fd)
    except OSError as exc:
        raise AsharePaperPlanningStoreCorruption(
            "ashare_paper_planning_store_directory_sync_failed"
        ) from exc
    finally:
        os.close(fd)


class FileAsharePaperPlanningStore:
    """Immutable content-addressed store with one binding per session day."""

    def __init__(self, root: Path | str) -> None:
        if not isinstance(root, (str, os.PathLike)) or not os.fspath(root):
            raise ValueError("ashare paper planning store root must be explicit")
        raw = Path(os.fspath(root))
        if not raw.is_absolute():
            raise ValueError("ashare paper planning store root must be absolute")
        if ".." in raw.parts:
            raise ValueError("ashare paper planning store root traversal forbidden")
        self.root = Path(os.path.abspath(os.fspath(raw)))
        _assert_safe_path(self.root)

    def _prepare_root(self) -> None:
        _assert_safe_path(self.root)
        existed = self.root.exists()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AsharePaperPlanningStoreCorruption(
                "ashare_paper_planning_store_root_unavailable"
            ) from exc
        _assert_safe_path(self.root)
        if not stat.S_ISDIR(self.root.lstat().st_mode):
            raise AsharePaperPlanningStoreCorruption(
                "ashare_paper_planning_store_root_not_directory"
            )
        if not existed:
            _fsync_directory(self.root.parent)
        _fsync_directory(self.root)

    def _artifact_path(self, decision_sha256: str) -> Path:
        if not _valid_sha256(decision_sha256):
            raise AsharePaperPlanningStoreCorruption(
                "ashare_paper_planning_store_decision_sha256_invalid"
            )
        return self.root / f"artifact-{decision_sha256}.json"

    def _binding_path(self, observation_session: str) -> Path:
        if (
            not isinstance(observation_session, str)
            or _SESSION_RE.fullmatch(observation_session) is None
        ):
            raise AsharePaperPlanningStoreCorruption(
                "ashare_paper_planning_store_observation_session_invalid"
            )
        return self.root / f"observation-{observation_session}.json"

    def _lock_path(self, observation_session: str) -> Path:
        if (
            not isinstance(observation_session, str)
            or _SESSION_RE.fullmatch(observation_session) is None
        ):
            raise AsharePaperPlanningStoreCorruption(
                "ashare_paper_planning_store_observation_session_invalid"
            )
        return self.root / f".observation-{observation_session}.lock"

    @contextmanager
    def _locked(
        self,
        observation_session: str,
        *,
        exclusive: bool,
    ) -> Iterator[None]:
        self._prepare_root()
        path = self._lock_path(observation_session)
        _assert_safe_path(path)
        try:
            fd = os.open(
                os.fspath(path),
                os.O_RDWR | os.O_CREAT | _no_follow_flag(),
                0o600,
            )
        except OSError as exc:
            raise AsharePaperPlanningStoreCorruption(
                "ashare_paper_planning_store_lock_unavailable"
            ) from exc
        try:
            _same_file_identity(fd, path, kind="lock")
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            _same_file_identity(fd, path, kind="lock")
            yield
            _same_file_identity(fd, path, kind="lock")
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _read_json(self, path: Path, *, kind: str) -> dict[str, Any]:
        _assert_safe_path(path)
        try:
            fd = os.open(os.fspath(path), os.O_RDONLY | _no_follow_flag())
        except OSError as exc:
            raise AsharePaperPlanningStoreCorruption(
                f"ashare_paper_planning_store_{kind}_unavailable"
            ) from exc
        try:
            before = _same_file_identity(fd, path, kind=kind)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = _same_file_identity(fd, path, kind=kind)
            if (
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise AsharePaperPlanningStoreCorruption(
                    f"ashare_paper_planning_store_{kind}_changed_during_read"
                )
        finally:
            os.close(fd)

        def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise AsharePaperPlanningStoreCorruption(
                        f"ashare_paper_planning_store_{kind}_duplicate_key"
                    )
                result[key] = value
            return result

        try:
            text = b"".join(chunks).decode("utf-8")
            raw = json.loads(text, object_pairs_hook=reject_duplicates)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AsharePaperPlanningStoreCorruption(
                f"ashare_paper_planning_store_{kind}_malformed"
            ) from exc
        if not isinstance(raw, dict) or _canonical_json(raw) != text:
            raise AsharePaperPlanningStoreCorruption(
                f"ashare_paper_planning_store_{kind}_not_canonical"
            )
        return raw

    def _atomic_create(self, path: Path, payload: Mapping[str, Any]) -> None:
        _assert_safe_path(path)
        encoded = _canonical_json(payload).encode("utf-8")
        temporary = self.root / f".tmp-{uuid.uuid4().hex}.json"
        _assert_safe_path(temporary)
        fd: int | None = None
        try:
            fd = os.open(
                os.fspath(temporary),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
                0o600,
            )
            _same_file_identity(fd, temporary, kind="temporary")
            offset = 0
            while offset < len(encoded):
                written = os.write(fd, encoded[offset:])
                if written <= 0:
                    raise AsharePaperPlanningStoreCorruption(
                        "ashare_paper_planning_store_short_write"
                    )
                offset += written
            os.fsync(fd)
            _same_file_identity(fd, temporary, kind="temporary")
            os.close(fd)
            fd = None
            os.link(temporary, path, follow_symlinks=False)
            os.unlink(temporary)
            _fsync_directory(self.root)
            final_fd = os.open(os.fspath(path), os.O_RDONLY | _no_follow_flag())
            try:
                _same_file_identity(final_fd, path, kind="published")
                os.fsync(final_fd)
            finally:
                os.close(final_fd)
        except AsharePaperPlanningStoreCorruption:
            if fd is not None:
                os.close(fd)
            temporary.unlink(missing_ok=True)
            if self.root.exists():
                _fsync_directory(self.root)
            raise
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            temporary.unlink(missing_ok=True)
            if self.root.exists():
                _fsync_directory(self.root)
            raise AsharePaperPlanningStoreCorruption(
                "ashare_paper_planning_store_atomic_write_failed"
            ) from exc

    def _read_artifact(self, decision_sha256: str) -> AshareDailyPlanningDecision:
        path = self._artifact_path(decision_sha256)
        raw = self._read_json(path, kind="artifact")
        encoded = _canonical_json(raw).encode("utf-8")
        if _sha256_bytes(encoded) != decision_sha256:
            raise AsharePaperPlanningStoreCorruption(
                "ashare_paper_planning_store_artifact_hash_mismatch"
            )
        return _decode_decision(raw, expected_sha256=decision_sha256)

    def compare_and_swap(
        self,
        *,
        decision: AshareDailyPlanningDecision,
        expected_decision_sha256: str | None,
    ) -> None:
        _validate_decision(decision)
        if expected_decision_sha256 is not None and not _valid_sha256(
            expected_decision_sha256
        ):
            raise AsharePaperPlanningStoreCorruption(
                "ashare_paper_planning_store_expected_sha256_invalid"
            )
        binding_path = self._binding_path(decision.observation_session)
        with self._locked(decision.observation_session, exclusive=True):
            if binding_path.exists() or binding_path.is_symlink():
                binding = _decode_binding(self._read_json(binding_path, kind="binding"))
                current_sha = str(binding["decision_sha256"])
                if current_sha == decision.decision_sha256:
                    if expected_decision_sha256 not in (None, current_sha):
                        raise AsharePaperPlanningStoreConflict(
                            "ashare_paper_planning_store_compare_and_swap_failed"
                        )
                    recovered = self._read_artifact(current_sha)
                    if recovered != decision or binding != _binding_payload(decision):
                        raise AsharePaperPlanningStoreCorruption(
                            "ashare_paper_planning_store_idempotent_replay_mismatch"
                        )
                    return
                if expected_decision_sha256 != current_sha:
                    raise AsharePaperPlanningStoreConflict(
                        "ashare_paper_planning_store_compare_and_swap_failed"
                    )
                raise AsharePaperPlanningStoreConflict(
                    "ashare_paper_planning_store_immutable_day_conflict"
                )
            if expected_decision_sha256 is not None:
                raise AsharePaperPlanningStoreConflict(
                    "ashare_paper_planning_store_compare_and_swap_failed"
                )
            artifact_path = self._artifact_path(decision.decision_sha256)
            if artifact_path.exists() or artifact_path.is_symlink():
                recovered = self._read_artifact(decision.decision_sha256)
                if recovered != decision:
                    raise AsharePaperPlanningStoreCorruption(
                        "ashare_paper_planning_store_orphan_artifact_mismatch"
                    )
            else:
                self._atomic_create(artifact_path, _decision_payload(decision))
                if self._read_artifact(decision.decision_sha256) != decision:
                    raise AsharePaperPlanningStoreCorruption(
                        "ashare_paper_planning_store_published_artifact_mismatch"
                    )
            binding = _binding_payload(decision)
            self._atomic_create(binding_path, binding)
            if (
                _decode_binding(self._read_json(binding_path, kind="binding"))
                != binding
            ):
                raise AsharePaperPlanningStoreCorruption(
                    "ashare_paper_planning_store_published_binding_mismatch"
                )

    def load(
        self,
        *,
        observation_session: str,
    ) -> AshareDailyPlanningDecision | None:
        if (
            not isinstance(observation_session, str)
            or _SESSION_RE.fullmatch(observation_session) is None
        ):
            raise AsharePaperPlanningStoreCorruption(
                "ashare_paper_planning_store_observation_session_invalid"
            )
        _assert_safe_path(self.root)
        if not self.root.exists():
            return None
        binding_path = self._binding_path(observation_session)
        if not binding_path.exists() and not binding_path.is_symlink():
            return None
        with self._locked(observation_session, exclusive=False):
            binding = _decode_binding(self._read_json(binding_path, kind="binding"))
            decision = self._read_artifact(str(binding["decision_sha256"]))
            if binding != _binding_payload(decision):
                raise AsharePaperPlanningStoreCorruption(
                    "ashare_paper_planning_store_binding_artifact_mismatch"
                )
            return decision


__all__ = [
    "ASHARE_PAPER_PLANNING_DECISION_SCHEMA_ID",
    "AshareDailyPlanningDecision",
    "AsharePaperPlanningContractError",
    "AsharePaperPlanningStoreConflict",
    "AsharePaperPlanningStoreCorruption",
    "FileAsharePaperPlanningStore",
    "build_ashare_daily_planning_decision",
]
