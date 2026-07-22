"""Pure readiness projection for prospectively accumulated A-share observations.

This module consumes only frozen :class:`AshareRuntimeAuthorityBundle` values.
It performs no I/O, opens no transport, and creates no ranking, probability,
plan, order, capital, or execution authority.  The accumulated history is
explicitly a ``prospective_observation_history``: it never rewrites a source
snapshot's ``historical_pit_eligible=False`` claim.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from shared.data.research_snapshot import (
    ResearchDataSnapshot,
    ResearchDatasetSnapshot,
)
from shared.runtime.ashare_observation_ledger import (
    OBSERVED_REASON_CODE,
    AshareObservationLedgerContractError,
    AshareObservationMembershipArtifact,
    observation_membership_source_symbols,
)
from shared.runtime.ashare_runtime_ports import AshareRuntimeAuthorityBundle


PROSPECTIVE_OBSERVATION_HISTORY = "prospective_observation_history"
ASHARE_OBSERVATION_HISTORY_READINESS_SCHEMA_ID = (
    "tradingagent.ashare.prospective-observation-history-readiness.v1"
)

_FEATURE_IDS = ("momentum_20d", "low_volatility_20d", "adv_20d")
_SYMBOL_RE = re.compile(r"^(?:60|00)[0-9]{4}\.(?:SH|SZ)$")
_TRADE_DATE_RE = re.compile(r"^[0-9]{8}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BLOCKER_ORDER = (
    "observation_authority_invalid",
    "observation_authority_ineligible",
    "observation_authority_scope_invalid",
    "research_snapshot_missing",
    "research_snapshot_contract_invalid",
    "historical_pit_claim_forbidden",
    "source_identity_incomplete",
    "authority_identity_mismatch",
    "authority_contract_mismatch",
    "membership_artifact_missing",
    "membership_artifact_invalid",
    "membership_artifact_identity_mismatch",
    "duplicate_membership_identity",
    "membership_target_not_observed",
    "daily_dataset_contract_invalid",
    "decision_as_of_invalid",
    "decision_as_of_not_strictly_increasing",
    "daily_trade_date_invalid",
    "daily_trade_date_not_strictly_increasing",
    "duplicate_snapshot_identity",
    "duplicate_receipt_identity",
    "duplicate_session",
    "trading_session_continuity_authority_unavailable",
    "corporate_action_adjustment_authority_unavailable",
    "insufficient_prospective_sessions",
    "incomplete_symbol_history",
)


@dataclass(frozen=True)
class AshareObservationFeatureReadiness:
    """Readiness of one numeric feature from prospective observations only."""

    feature_id: str
    history_mode: str
    required_sessions: int
    observed_sessions: int
    eligible: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class AshareObservationHistoryCoverage:
    """Per-symbol coverage across unique, structurally identified sessions."""

    target_symbol: str
    expected_session_count: int
    complete_session_count: int
    coverage_ratio: float
    incomplete_sessions: tuple[str, ...]
    missing_sessions: tuple[str, ...]
    duplicate_row_sessions: tuple[str, ...]
    invalid_value_sessions: tuple[str, ...]


@dataclass(frozen=True)
class AshareObservationHistoryReadiness:
    """Fail-closed readiness projection without predictive or trade authority."""

    history_mode: str
    session_count: int
    min_required_sessions: int
    history_identity_sha256: str | None
    prospective_history_eligible: bool
    feature_readiness: tuple[AshareObservationFeatureReadiness, ...]
    coverage: AshareObservationHistoryCoverage
    blockers: tuple[str, ...]
    schema_id: str = ASHARE_OBSERVATION_HISTORY_READINESS_SCHEMA_ID


def _append(blockers: list[str], reason: str) -> None:
    if reason not in blockers:
        blockers.append(reason)


def _ordered_blockers(blockers: list[str]) -> tuple[str, ...]:
    present = set(blockers)
    ordered = [reason for reason in _BLOCKER_ORDER if reason in present]
    ordered.extend(sorted(present.difference(_BLOCKER_ORDER)))
    return tuple(ordered)


def _sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _aware_utc(value: object) -> datetime | None:
    if not _nonempty(value):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _positive_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _daily_dataset(
    snapshot: ResearchDataSnapshot,
    *,
    blockers: list[str],
) -> ResearchDatasetSnapshot | None:
    matches = [
        item
        for item in snapshot.datasets
        if isinstance(item, ResearchDatasetSnapshot)
        and item.row_event_time_field == "trade_date"
        and item.row_event_time_format == "yyyymmdd"
        and item.row_event_time_semantic == "session"
        and {"ts_code", "trade_date"}.issubset(item.identity_fields)
    ]
    if len(matches) != 1:
        _append(blockers, "daily_dataset_contract_invalid")
        return None
    daily = matches[0]
    if (
        not _nonempty(daily.dataset_id)
        or daily.role != "required_execution"
        or daily.catalog_version != snapshot.catalog_version
        or daily.evidence_action != "accept"
        or daily.eligible is not True
        or daily.source_proof_complete is not True
        or daily.observation_mode != "current_observation"
        or daily.historical_pit_eligible is not False
        or daily.next_cursor is not None
        or not _nonempty(daily.receipt_id)
        or not all(
            _sha256(value)
            for value in (
                daily.lineage_sha256,
                daily.source_proof_sha256,
                daily.identity_sha256,
                daily.row_observation_sha256,
                daily.pagination_trace_sha256,
                daily.pagination_semantic_sha256,
                daily.page_request_set_sha256,
                daily.page_response_set_sha256,
                daily.cursor_chain_sha256,
                daily.response_sha256,
            )
        )
    ):
        _append(blockers, "source_identity_incomplete")
    return daily


def _bundle_contract(
    bundle: AshareRuntimeAuthorityBundle,
    *,
    blockers: list[str],
) -> tuple[ResearchDataSnapshot | None, datetime | None]:
    if (
        bundle.committed_state_verified is not True
        or bundle.observation_eligible is not True
    ):
        _append(blockers, "observation_authority_ineligible")
    if (
        bundle.ranking_eligible is not False
        or bundle.planning_eligible is not False
        or bundle.execution_evidence_eligible is not False
    ):
        _append(blockers, "observation_authority_scope_invalid")
    snapshot = bundle.research_snapshot
    if snapshot is None:
        _append(blockers, "research_snapshot_missing")
        return None, _aware_utc(bundle.decision_as_of)
    if not isinstance(snapshot, ResearchDataSnapshot):
        _append(blockers, "research_snapshot_contract_invalid")
        return None, _aware_utc(bundle.decision_as_of)
    if (
        bundle.historical_pit_eligible is not False
        or bundle.historical_feature_claims
        or snapshot.historical_pit_eligible is not False
        or any(item.historical_pit_eligible is not False for item in snapshot.datasets)
    ):
        _append(blockers, "historical_pit_claim_forbidden")
    if (
        not _nonempty(bundle.profile_id)
        or not _nonempty(bundle.catalog_version)
        or isinstance(bundle.schema_major, bool)
        or not isinstance(bundle.schema_major, int)
        or bundle.schema_major <= 0
        or not _sha256(bundle.snapshot_sha256)
        or not _sha256(bundle.probe_receipt_sha256)
        or not _sha256(bundle.observation_receipt_sha256)
        or not _sha256(bundle.observation_membership_sha256)
        or not _sha256(bundle.observation_transaction_complete_sha256)
        or not _sha256(snapshot.profile_contract_sha256)
        or not _sha256(snapshot.snapshot_sha256)
    ):
        _append(blockers, "source_identity_incomplete")
    if (
        bundle.profile_id != snapshot.profile_id
        or bundle.catalog_version != snapshot.catalog_version
        or bundle.decision_as_of != snapshot.decision_as_of
        or bundle.snapshot_sha256 != snapshot.snapshot_sha256
    ):
        _append(blockers, "authority_identity_mismatch")
    decision = _aware_utc(bundle.decision_as_of)
    snapshot_decision = _aware_utc(snapshot.decision_as_of)
    if decision is None or snapshot_decision is None:
        _append(blockers, "decision_as_of_invalid")
    elif decision != snapshot_decision:
        _append(blockers, "authority_identity_mismatch")
    return snapshot, decision


def build_ashare_observation_history_readiness(
    bundles: Sequence[AshareRuntimeAuthorityBundle],
    *,
    membership_artifacts: Sequence[AshareObservationMembershipArtifact],
    target_symbol: str,
    min_required_sessions: int = 21,
) -> AshareObservationHistoryReadiness:
    """Evaluate raw inputs for 20-day features from forward observations.

    The input order is authoritative collection order.  It is never sorted or
    repaired: timestamps and sessions must already be strictly increasing.
    Twenty-session return features require 21 closing observations.  Until a
    frozen trading-session continuity proof and corporate-action adjustment
    proof are supplied, raw history can be identified but feature readiness
    remains fail-closed.
    Malformed evidence returns stable blockers; invalid caller configuration
    raises ``ValueError``.
    """

    if (
        not isinstance(target_symbol, str)
        or _SYMBOL_RE.fullmatch(target_symbol) is None
    ):
        raise ValueError("target_symbol_must_be_canonical")
    if (
        isinstance(min_required_sessions, bool)
        or not isinstance(min_required_sessions, int)
        or min_required_sessions < 21
    ):
        raise ValueError("min_required_sessions_must_be_integer_at_least_21")
    if isinstance(bundles, (str, bytes)) or not isinstance(bundles, Sequence):
        raise ValueError("bundles_must_be_a_sequence")
    if isinstance(membership_artifacts, (str, bytes)) or not isinstance(
        membership_artifacts, Sequence
    ):
        raise ValueError("membership_artifacts_must_be_a_sequence")

    blockers: list[str] = []
    if len(membership_artifacts) != len(bundles):
        _append(blockers, "membership_artifact_missing")
    baseline_contract: tuple[str, str, int] | None = None
    previous_decision: datetime | None = None
    previous_session: str | None = None
    seen_sessions: set[str] = set()
    seen_snapshots: set[str] = set()
    seen_receipts: set[str] = set()
    seen_memberships: set[str] = set()
    complete_sessions: list[str] = []
    missing_sessions: list[str] = []
    duplicate_row_sessions: list[str] = []
    invalid_value_sessions: list[str] = []
    history_sources: list[dict[str, object]] = []

    for bundle_index, bundle in enumerate(bundles):
        membership = (
            membership_artifacts[bundle_index]
            if bundle_index < len(membership_artifacts)
            else None
        )
        if type(bundle) is not AshareRuntimeAuthorityBundle:
            _append(blockers, "observation_authority_invalid")
            continue
        snapshot, decision = _bundle_contract(bundle, blockers=blockers)
        contract = (bundle.profile_id, bundle.catalog_version, bundle.schema_major)
        if baseline_contract is None and all(
            isinstance(value, (str, int)) for value in contract
        ):
            baseline_contract = contract  # type: ignore[assignment]
        elif baseline_contract is not None and contract != baseline_contract:
            _append(blockers, "authority_contract_mismatch")

        if decision is not None:
            if previous_decision is not None and decision <= previous_decision:
                _append(blockers, "decision_as_of_not_strictly_increasing")
            previous_decision = decision
        if snapshot is None:
            continue

        if _sha256(bundle.snapshot_sha256):
            snapshot_identity = str(bundle.snapshot_sha256)
            if snapshot_identity in seen_snapshots:
                _append(blockers, "duplicate_snapshot_identity")
            seen_snapshots.add(snapshot_identity)
        for receipt in (
            bundle.probe_receipt_sha256,
            bundle.observation_receipt_sha256,
            bundle.observation_transaction_complete_sha256,
        ):
            if _sha256(receipt):
                receipt_identity = str(receipt)
                if receipt_identity in seen_receipts:
                    _append(blockers, "duplicate_receipt_identity")
                seen_receipts.add(receipt_identity)

        daily = _daily_dataset(snapshot, blockers=blockers)
        if daily is None:
            continue
        if _nonempty(daily.receipt_id):
            receipt_identity = str(daily.receipt_id)
            if receipt_identity in seen_receipts:
                _append(blockers, "duplicate_receipt_identity")
            seen_receipts.add(receipt_identity)

        try:
            rows = daily.decoded_rows()
        except (TypeError, ValueError):
            _append(blockers, "daily_dataset_contract_invalid")
            continue
        if daily.row_count != len(rows) or not rows:
            _append(blockers, "daily_dataset_contract_invalid")
            continue
        row_sessions = {
            row.get("trade_date")
            for row in rows
            if isinstance(row, dict)
            and _TRADE_DATE_RE.fullmatch(str(row.get("trade_date")))
        }
        if (
            len(row_sessions) != 1
            or any(not isinstance(row, dict) for row in rows)
            or any(
                not isinstance(row.get("trade_date"), str)
                or _TRADE_DATE_RE.fullmatch(row["trade_date"]) is None
                for row in rows
            )
        ):
            _append(blockers, "daily_trade_date_invalid")
            continue
        session = str(next(iter(row_sessions)))
        if daily.max_row_event_value != session:
            _append(blockers, "daily_trade_date_invalid")
            continue
        if previous_session is not None and session <= previous_session:
            _append(blockers, "daily_trade_date_not_strictly_increasing")
        previous_session = session
        if session in seen_sessions:
            _append(blockers, "duplicate_session")
            continue
        seen_sessions.add(session)

        membership_valid = True
        if type(membership) is not AshareObservationMembershipArtifact:
            _append(blockers, "membership_artifact_missing")
            membership_valid = False
        else:
            try:
                membership.__post_init__()
            except AshareObservationLedgerContractError:
                _append(blockers, "membership_artifact_invalid")
                membership_valid = False

        try:
            source_symbols = observation_membership_source_symbols(
                snapshot,
                observation_session=session,
            )
        except AshareObservationLedgerContractError:
            _append(blockers, "membership_artifact_identity_mismatch")
            source_symbols = ()
        if membership_valid and membership is not None:
            membership_symbols = tuple(item.symbol for item in membership.records)
            if (
                membership.observation_session != session
                or membership.decision_as_of != bundle.decision_as_of
                or membership.profile_id != bundle.profile_id
                or membership.profile_contract_sha256
                != snapshot.profile_contract_sha256
                or membership.catalog_version != bundle.catalog_version
                or membership.snapshot_sha256 != bundle.snapshot_sha256
                or membership.probe_receipt_sha256 != bundle.probe_receipt_sha256
                or membership.observation_receipt_sha256
                != bundle.observation_receipt_sha256
                or membership.content_sha256
                != bundle.observation_membership_sha256
                or membership_symbols != source_symbols
            ):
                _append(blockers, "membership_artifact_identity_mismatch")
                membership_valid = False
            if membership.content_sha256 in seen_memberships:
                _append(blockers, "duplicate_membership_identity")
                membership_valid = False
            seen_memberships.add(membership.content_sha256)

        target_observed = False
        if membership_valid and membership is not None:
            target_records = [
                item for item in membership.records if item.symbol == target_symbol
            ]
            target_observed = len(target_records) == 1 and (
                target_records[0].disposition == "observed"
                and target_records[0].reason_code == OBSERVED_REASON_CODE
            )
            if not target_observed:
                _append(blockers, "membership_target_not_observed")
            else:
                history_sources.append(
                    {
                        "sequence": len(history_sources),
                        "profile_id": bundle.profile_id,
                        "catalog_version": bundle.catalog_version,
                        "schema_major": bundle.schema_major,
                        "decision_as_of": bundle.decision_as_of,
                        "snapshot_sha256": bundle.snapshot_sha256,
                        "probe_receipt_sha256": bundle.probe_receipt_sha256,
                        "observation_receipt_sha256": (
                            bundle.observation_receipt_sha256
                        ),
                        "authority_membership_sha256": (
                            bundle.observation_membership_sha256
                        ),
                        "observation_transaction_complete_sha256": (
                            bundle.observation_transaction_complete_sha256
                        ),
                        "daily_dataset_id": daily.dataset_id,
                        "daily_receipt_id": daily.receipt_id,
                        "daily_response_sha256": daily.response_sha256,
                        "daily_lineage_sha256": daily.lineage_sha256,
                        "daily_session": session,
                        "membership_schema_id": membership.schema_id,
                        "membership_content_sha256": membership.content_sha256,
                        "membership_universe_sha256": membership.universe_sha256,
                    }
                )

        symbol_rows = [row for row in rows if row.get("ts_code") == target_symbol]
        if not symbol_rows:
            missing_sessions.append(session)
        elif len(symbol_rows) != 1:
            duplicate_row_sessions.append(session)
        elif not _positive_number(symbol_rows[0].get("close")) or not _positive_number(
            symbol_rows[0].get("amount")
        ):
            invalid_value_sessions.append(session)
        elif not target_observed:
            missing_sessions.append(session)
        else:
            complete_sessions.append(session)

    session_count = len(seen_sessions)
    _append(blockers, "trading_session_continuity_authority_unavailable")
    _append(blockers, "corporate_action_adjustment_authority_unavailable")
    incomplete_sessions = sorted(
        set(missing_sessions + duplicate_row_sessions + invalid_value_sessions)
    )
    if session_count < min_required_sessions:
        _append(blockers, "insufficient_prospective_sessions")
    if incomplete_sessions:
        _append(blockers, "incomplete_symbol_history")
    ordered_blockers = _ordered_blockers(blockers)
    structural_blockers = set(ordered_blockers).difference(
        {
            "trading_session_continuity_authority_unavailable",
            "corporate_action_adjustment_authority_unavailable",
            "insufficient_prospective_sessions",
            "incomplete_symbol_history",
        }
    )
    history_identity_sha256 = (
        None
        if structural_blockers
        else _canonical_sha256(
            {
                "schema_id": ASHARE_OBSERVATION_HISTORY_READINESS_SCHEMA_ID,
                "history_mode": PROSPECTIVE_OBSERVATION_HISTORY,
                "target_symbol": target_symbol,
                "min_required_sessions": min_required_sessions,
                "sources": history_sources,
            }
        )
    )
    coverage = AshareObservationHistoryCoverage(
        target_symbol=target_symbol,
        expected_session_count=session_count,
        complete_session_count=len(complete_sessions),
        coverage_ratio=(
            len(complete_sessions) / session_count if session_count else 0.0
        ),
        incomplete_sessions=tuple(incomplete_sessions),
        missing_sessions=tuple(sorted(missing_sessions)),
        duplicate_row_sessions=tuple(sorted(duplicate_row_sessions)),
        invalid_value_sessions=tuple(sorted(invalid_value_sessions)),
    )
    feature_readiness = tuple(
        AshareObservationFeatureReadiness(
            feature_id=feature_id,
            history_mode=PROSPECTIVE_OBSERVATION_HISTORY,
            required_sessions=min_required_sessions,
            observed_sessions=len(complete_sessions),
            eligible=not ordered_blockers,
            blockers=ordered_blockers,
        )
        for feature_id in _FEATURE_IDS
    )
    return AshareObservationHistoryReadiness(
        history_mode=PROSPECTIVE_OBSERVATION_HISTORY,
        session_count=session_count,
        min_required_sessions=min_required_sessions,
        history_identity_sha256=history_identity_sha256,
        prospective_history_eligible=not ordered_blockers,
        feature_readiness=feature_readiness,
        coverage=coverage,
        blockers=ordered_blockers,
    )
