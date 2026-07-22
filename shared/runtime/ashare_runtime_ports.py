"""Frozen A-share current-observation authorities for research and planning.

This module binds an already-built :class:`ResearchDataSnapshot` to the
integration-probe and observation receipts that produced it.  Its strict
loader reads one fully committed transaction from a private state root while
holding the writer's session lock.  It owns no transport, clock, model,
broker, capital, or execution state.

The first TradingDatas slice contains daily/current observations only.  A
valid bundle therefore authorizes observation, but not the existing Champion
rank or a stock plan until complete numeric feature authority exists.  It
never authorizes a simulated fill.  Missing Champion and minute/L1 evidence
remain separate, explicit blockers.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from shared.data.research_snapshot import (
    ResearchDataSnapshot,
    ResearchDatasetSnapshot,
)
from shared.data.research_snapshot_store import (
    FileResearchSnapshotStore,
    ResearchSnapshotStoreCorruption,
)
from shared.runtime.ashare_observation import (
    OBSERVATION_RECEIPT_SCHEMA_ID,
    OBSERVATION_TRANSACTION_ARTIFACTS,
    OBSERVATION_TRANSACTION_COMPLETE_SCHEMA_ID,
)
from shared.runtime.ashare_observation_ledger import (
    AshareObservationLedgerContractError,
    AshareObservationLedgerCorruption,
    AshareObservationMembershipArtifact,
    FileAshareObservationMembershipLedger,
    build_ashare_observation_membership_artifact,
)
from shared.runtime_test.sharedsignals_v1_integration_probe import (
    PROBE_VERSION,
    RECEIPT_SCHEMA_ID,
)


ASHARE_RUNTIME_AUTHORITY_SCHEMA_ID = "tradingagent.ashare.runtime-authority-bundle.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MAX_AUTHORITY_FILE_BYTES = 4_194_304
_VERIFIED_COMMITTED_STATE_CAPABILITY = object()


class AshareRuntimeAuthorityLoadBlocked(RuntimeError):
    """Controlled refusal to mint authority from incomplete durable state."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _canonical_sha256(value: object) -> str | None:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _append(blockers: list[str], reason: str) -> None:
    if reason not in blockers:
        blockers.append(reason)


def _receipt_sha256(
    receipt: Mapping[str, Any],
    *,
    invalid_reason: str,
    blockers: list[str],
) -> str | None:
    payload = dict(receipt)
    claimed = payload.pop("receipt_sha256", None)
    computed = _canonical_sha256(payload)
    if not _is_sha256(claimed) or computed is None or claimed != computed:
        _append(blockers, invalid_reason)
        return None
    return claimed


def _snapshot_sha256(
    snapshot: ResearchDataSnapshot,
    *,
    blockers: list[str],
) -> str | None:
    if not snapshot.datasets or any(
        not isinstance(item, ResearchDatasetSnapshot) for item in snapshot.datasets
    ):
        _append(blockers, "research_snapshot_contract_invalid")
        return None
    payload = {
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
    computed = _canonical_sha256(payload)
    if (
        not _is_sha256(snapshot.snapshot_sha256)
        or computed is None
        or snapshot.snapshot_sha256 != computed
    ):
        _append(blockers, "research_snapshot_sha256_invalid")
        return None
    return snapshot.snapshot_sha256


def _validate_snapshot(
    snapshot: ResearchDataSnapshot,
    *,
    blockers: list[str],
) -> None:
    _snapshot_sha256(snapshot, blockers=blockers)
    dataset_ids = [item.dataset_id for item in snapshot.datasets]
    if (
        not snapshot.execution_eligible
        or snapshot.blocking_reasons
        or len(set(dataset_ids)) != len(dataset_ids)
        or any(
            item.catalog_version != snapshot.catalog_version
            or item.observation_mode != "current_observation"
            or item.historical_pit_eligible is not False
            or item.eligible is not True
            or item.source_proof_complete is not True
            for item in snapshot.datasets
        )
    ):
        _append(blockers, "research_snapshot_not_observation_eligible")
    if snapshot.historical_pit_eligible is not False:
        _append(blockers, "research_snapshot_historical_pit_claim_forbidden")
    if _aware_utc(snapshot.decision_as_of) is None:
        _append(blockers, "research_snapshot_decision_as_of_invalid")


def _validate_probe_dataset_binding(
    snapshot: ResearchDataSnapshot,
    probe: Mapping[str, Any],
    *,
    schema_major: int,
    blockers: list[str],
) -> None:
    raw_datasets = probe.get("datasets")
    if not isinstance(raw_datasets, list) or any(
        not isinstance(item, Mapping) for item in raw_datasets
    ):
        _append(blockers, "integration_probe_dataset_binding_invalid")
        return
    probe_datasets = {
        item.get("dataset_id"): item
        for item in raw_datasets
        if isinstance(item.get("dataset_id"), str)
    }
    snapshot_datasets = {item.dataset_id: item for item in snapshot.datasets}
    if len(probe_datasets) != len(raw_datasets) or set(probe_datasets) != set(
        snapshot_datasets
    ):
        _append(blockers, "integration_probe_dataset_binding_invalid")
        return
    for dataset_id, frozen in snapshot_datasets.items():
        item = probe_datasets[dataset_id]
        if item.get("schema_major") != schema_major:
            _append(blockers, "integration_probe_schema_major_mismatch")
        if (
            item.get("requirement_role") != frozen.role
            or item.get("observation_mode") != "current_observation"
            or item.get("historical_pit_eligible") is not False
            or item.get("source_proof_complete") is not True
            or item.get("eligible") is not True
            or item.get("pagination_complete") is not True
            or item.get("same_as_of_match") is not True
            or item.get("identity_sha256") != frozen.identity_sha256
            or item.get("pagination_semantic_sha256")
            != frozen.pagination_semantic_sha256
            or item.get("row_count") != frozen.row_count
            or item.get("page_count") != frozen.page_count
            or item.get("receipt_id") != frozen.receipt_id
            or item.get("lineage_sha256") != frozen.lineage_sha256
            or item.get("source_proof_sha256") != frozen.source_proof_sha256
        ):
            _append(blockers, "integration_probe_dataset_binding_invalid")


def _validate_probe_snapshot_binding(
    snapshot: ResearchDataSnapshot,
    probe: Mapping[str, Any],
    *,
    blockers: list[str],
) -> None:
    runs = probe.get("snapshot_runs")
    if (
        not isinstance(runs, list)
        or len(runs) != 2
        or any(not isinstance(item, Mapping) for item in runs)
        or any(
            not _is_sha256(item.get("snapshot_sha256"))
            or item.get("snapshot_sha256") != snapshot.snapshot_sha256
            or item.get("execution_eligible") is not True
            or item.get("historical_pit_eligible") is not False
            or item.get("profile_contract_sha256") != snapshot.profile_contract_sha256
            or item.get("blocking_reasons") != []
            for item in runs
        )
    ):
        _append(blockers, "integration_probe_snapshot_binding_invalid")


def _validate_probe(
    snapshot: ResearchDataSnapshot,
    probe: Mapping[str, Any],
    *,
    schema_major: int,
    blockers: list[str],
) -> str | None:
    receipt_sha256 = _receipt_sha256(
        probe,
        invalid_reason="integration_probe_receipt_sha256_invalid",
        blockers=blockers,
    )
    if (
        probe.get("schema_id") != RECEIPT_SCHEMA_ID
        or probe.get("probe_version") != PROBE_VERSION
    ):
        _append(blockers, "integration_probe_schema_invalid")
    if (
        probe.get("authority") != "non_authority"
        or probe.get("production_verified") is not False
        or probe.get("real_trading_enabled") is not False
    ):
        _append(blockers, "integration_probe_authority_flags_invalid")
    if (
        probe.get("status") != "pass"
        or probe.get("blocking") is not False
        or probe.get("same_as_of_match") is not True
        or probe.get("reason_codes") != []
        or not _is_sha256(probe.get("semantic_snapshot_sha256"))
    ):
        _append(blockers, "integration_probe_not_eligible")
    if probe.get("profile_id") != snapshot.profile_id:
        _append(blockers, "authority_profile_id_mismatch")
    if probe.get("catalog_version") != snapshot.catalog_version:
        _append(blockers, "authority_catalog_version_mismatch")
    probe_as_of = _aware_utc(probe.get("as_of"))
    snapshot_as_of = _aware_utc(snapshot.decision_as_of)
    if probe_as_of is None or snapshot_as_of is None or probe_as_of != snapshot_as_of:
        _append(blockers, "authority_decision_as_of_mismatch")
    if not _is_sha256(probe.get("manifest_sha256")):
        _append(blockers, "integration_probe_manifest_sha256_invalid")
    _validate_probe_dataset_binding(
        snapshot,
        probe,
        schema_major=schema_major,
        blockers=blockers,
    )
    _validate_probe_snapshot_binding(snapshot, probe, blockers=blockers)
    return receipt_sha256


def _validate_observation(
    snapshot: ResearchDataSnapshot,
    probe: Mapping[str, Any],
    observation: Mapping[str, Any],
    *,
    probe_receipt_sha256: str | None,
    blockers: list[str],
) -> str | None:
    receipt_sha256 = _receipt_sha256(
        observation,
        invalid_reason="observation_receipt_sha256_invalid",
        blockers=blockers,
    )
    if observation.get("schema_id") != OBSERVATION_RECEIPT_SCHEMA_ID:
        _append(blockers, "observation_receipt_schema_invalid")
    if (
        observation.get("mode") != "observation_only"
        or observation.get("marketgraph_mode") != "mg_off"
        or observation.get("real_trading_enabled") is not False
        or observation.get("historical_pit_eligible") is not False
        or observation.get("execution_authority") is not False
    ):
        _append(blockers, "observation_authority_flags_invalid")
    if observation.get("profile_id") != snapshot.profile_id:
        _append(blockers, "authority_profile_id_mismatch")
    if observation.get("catalog_version") != snapshot.catalog_version:
        _append(blockers, "authority_catalog_version_mismatch")
    observation_as_of = _aware_utc(observation.get("decision_as_of"))
    snapshot_as_of = _aware_utc(snapshot.decision_as_of)
    if (
        observation_as_of is None
        or snapshot_as_of is None
        or observation_as_of != snapshot_as_of
    ):
        _append(blockers, "authority_decision_as_of_mismatch")
    if observation.get("snapshot_sha256") != snapshot.snapshot_sha256:
        _append(blockers, "observation_snapshot_binding_invalid")
    if (
        probe_receipt_sha256 is None
        or observation.get("probe_receipt_sha256") != probe_receipt_sha256
    ):
        _append(blockers, "observation_probe_binding_invalid")
    if observation.get("manifest_sha256") != probe.get("manifest_sha256"):
        _append(blockers, "authority_manifest_binding_invalid")
    if (
        isinstance(observation.get("tradable_universe_count"), bool)
        or not isinstance(observation.get("tradable_universe_count"), int)
        or observation.get("tradable_universe_count", 0) <= 0
        or not _is_sha256(observation.get("tradable_universe_sha256"))
    ):
        _append(blockers, "observation_universe_binding_invalid")
    return receipt_sha256


def _validate_observation_membership(
    snapshot: ResearchDataSnapshot,
    observation: Mapping[str, Any],
    membership: AshareObservationMembershipArtifact,
    *,
    blockers: list[str],
) -> str | None:
    try:
        rebuilt = build_ashare_observation_membership_artifact(
            observation_session=membership.observation_session,
            research_snapshot=snapshot,
            observation_receipt=observation,
            records=membership.records,
        )
    except (
        AshareObservationLedgerContractError,
        AshareObservationLedgerCorruption,
    ):
        _append(blockers, "observation_membership_binding_invalid")
        return None
    if rebuilt != membership:
        _append(blockers, "observation_membership_binding_invalid")
        return None
    return membership.content_sha256


def _validate_observation_transaction_complete(
    snapshot: ResearchDataSnapshot,
    probe: Mapping[str, Any],
    observation: Mapping[str, Any],
    membership: AshareObservationMembershipArtifact,
    complete: Mapping[str, Any],
    *,
    probe_receipt_sha256: str | None,
    observation_receipt_sha256: str | None,
    membership_sha256: str | None,
    blockers: list[str],
) -> str | None:
    payload = dict(complete)
    claimed = payload.pop("content_sha256", None)
    computed = _canonical_sha256(payload)
    expected = {
        "schema_id": OBSERVATION_TRANSACTION_COMPLETE_SCHEMA_ID,
        "profile_id": snapshot.profile_id,
        "catalog_version": snapshot.catalog_version,
        "decision_as_of": snapshot.decision_as_of,
        "observation_session": membership.observation_session,
        "manifest_sha256": probe.get("manifest_sha256"),
        "snapshot_sha256": snapshot.snapshot_sha256,
        "probe_receipt_sha256": probe_receipt_sha256,
        "observation_receipt_sha256": observation_receipt_sha256,
        "observation_membership_sha256": membership_sha256,
        "required_artifacts": list(OBSERVATION_TRANSACTION_ARTIFACTS),
        "mode": "observation_only",
        "marketgraph_mode": "mg_off",
        "historical_pit_eligible": False,
        "real_trading_enabled": False,
        "execution_authority": False,
    }
    if (
        not _is_sha256(claimed)
        or computed is None
        or claimed != computed
        or payload != expected
        or observation.get("receipt_sha256") != observation_receipt_sha256
    ):
        _append(blockers, "observation_transaction_complete_invalid")
        return None
    return claimed


def _historical_claims(value: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, tuple) or any(
        not isinstance(item, str) or not item or item != item.strip() for item in value
    ):
        raise ValueError("historical_feature_claims_must_be_canonical_tuple")
    if len(set(value)) != len(value):
        raise ValueError("historical_feature_claims_must_be_unique")
    return tuple(sorted(value))


@dataclass(frozen=True)
class AshareRuntimeAuthorityBundle:
    """Immutable stage eligibility derived from one committed transaction."""

    research_snapshot: ResearchDataSnapshot | None = field(repr=False)
    profile_id: str | None
    catalog_version: str | None
    decision_as_of: str | None
    schema_major: int
    snapshot_sha256: str | None
    probe_receipt_sha256: str | None
    observation_receipt_sha256: str | None
    observation_membership_sha256: str | None
    observation_transaction_complete_sha256: str | None
    historical_pit_eligible: bool
    historical_feature_claims: tuple[str, ...]
    observation_eligible: bool
    ranking_eligible: bool
    planning_eligible: bool
    execution_evidence_eligible: bool
    blockers: tuple[str, ...]
    observation_membership: AshareObservationMembershipArtifact | None = field(
        default=None,
        repr=False,
    )
    _committed_state_capability: object | None = field(
        init=False,
        repr=False,
        compare=False,
    )
    schema_id: str = ASHARE_RUNTIME_AUTHORITY_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema_id != ASHARE_RUNTIME_AUTHORITY_SCHEMA_ID:
            raise ValueError("runtime_authority_schema_invalid")
        object.__setattr__(self, "_committed_state_capability", None)
        if self.observation_eligible:
            raise ValueError("observation_eligible_requires_verified_committed_state")

    @property
    def committed_state_verified(self) -> bool:
        """Whether the strict durable-state loader minted this bundle."""

        return (
            self._committed_state_capability
            is _VERIFIED_COMMITTED_STATE_CAPABILITY
        )


def _promote_verified_committed_bundle(
    bundle: AshareRuntimeAuthorityBundle,
) -> AshareRuntimeAuthorityBundle:
    snapshot = bundle.research_snapshot
    if (
        type(bundle) is not AshareRuntimeAuthorityBundle
        or bundle.observation_eligible is not False
        or bundle.committed_state_verified is not False
        or not isinstance(snapshot, ResearchDataSnapshot)
        or not _is_sha256(bundle.snapshot_sha256)
        or not _is_sha256(bundle.probe_receipt_sha256)
        or not _is_sha256(bundle.observation_receipt_sha256)
        or not _is_sha256(bundle.observation_membership_sha256)
        or not _is_sha256(bundle.observation_transaction_complete_sha256)
        or type(bundle.observation_membership)
        is not AshareObservationMembershipArtifact
        or bundle.observation_membership.content_sha256
        != bundle.observation_membership_sha256
        or bundle.profile_id != snapshot.profile_id
        or bundle.catalog_version != snapshot.catalog_version
        or bundle.decision_as_of != snapshot.decision_as_of
        or bundle.snapshot_sha256 != snapshot.snapshot_sha256
        or bundle.historical_pit_eligible is not False
        or bundle.ranking_eligible is not False
        or bundle.planning_eligible is not False
        or bundle.execution_evidence_eligible is not False
    ):
        raise AshareRuntimeAuthorityLoadBlocked("observation_committed_state_invalid")
    object.__setattr__(
        bundle,
        "_committed_state_capability",
        _VERIFIED_COMMITTED_STATE_CAPABILITY,
    )
    object.__setattr__(bundle, "observation_eligible", True)
    return bundle


def _assemble_ashare_runtime_authority_bundle(
    *,
    research_snapshot: ResearchDataSnapshot | None,
    integration_probe: Mapping[str, Any] | None,
    observation_receipt: Mapping[str, Any] | None,
    schema_major: int,
    observation_membership: AshareObservationMembershipArtifact | None = None,
    observation_transaction_complete: Mapping[str, Any] | None = None,
    historical_feature_claims: tuple[str, ...] = (),
    verified_committed_state: bool,
) -> AshareRuntimeAuthorityBundle:
    """Bind current-observation evidence without creating new authority.

    Missing, malformed, or cross-bound evidence returns a closed bundle with
    stable blockers.  Caller configuration errors remain explicit exceptions.
    """

    if (
        isinstance(schema_major, bool)
        or not isinstance(schema_major, int)
        or schema_major <= 0
    ):
        raise ValueError("schema_major_must_be_positive_integer")
    claims = _historical_claims(historical_feature_claims)
    blockers: list[str] = []

    if research_snapshot is None:
        _append(blockers, "research_snapshot_missing")
    elif not isinstance(research_snapshot, ResearchDataSnapshot):
        _append(blockers, "research_snapshot_invalid")
    else:
        _validate_snapshot(research_snapshot, blockers=blockers)

    if integration_probe is None:
        _append(blockers, "integration_probe_missing")
    elif not isinstance(integration_probe, Mapping):
        _append(blockers, "integration_probe_invalid")

    if observation_receipt is None:
        _append(blockers, "observation_receipt_missing")
    elif not isinstance(observation_receipt, Mapping):
        _append(blockers, "observation_receipt_invalid")

    if observation_membership is None:
        _append(blockers, "observation_membership_missing")
    elif not isinstance(
        observation_membership,
        AshareObservationMembershipArtifact,
    ):
        _append(blockers, "observation_membership_invalid")

    if observation_transaction_complete is None:
        _append(blockers, "observation_transaction_complete_missing")
    elif not isinstance(observation_transaction_complete, Mapping):
        _append(blockers, "observation_transaction_complete_invalid")

    probe_sha256: str | None = None
    observation_sha256: str | None = None
    membership_sha256: str | None = None
    transaction_complete_sha256: str | None = None
    if isinstance(research_snapshot, ResearchDataSnapshot) and isinstance(
        integration_probe, Mapping
    ):
        probe_sha256 = _validate_probe(
            research_snapshot,
            integration_probe,
            schema_major=schema_major,
            blockers=blockers,
        )
    if (
        isinstance(research_snapshot, ResearchDataSnapshot)
        and isinstance(integration_probe, Mapping)
        and isinstance(observation_receipt, Mapping)
    ):
        observation_sha256 = _validate_observation(
            research_snapshot,
            integration_probe,
            observation_receipt,
            probe_receipt_sha256=probe_sha256,
            blockers=blockers,
        )
    if (
        isinstance(research_snapshot, ResearchDataSnapshot)
        and isinstance(observation_receipt, Mapping)
        and isinstance(
            observation_membership,
            AshareObservationMembershipArtifact,
        )
    ):
        membership_sha256 = _validate_observation_membership(
            research_snapshot,
            observation_receipt,
            observation_membership,
            blockers=blockers,
        )
    if (
        isinstance(research_snapshot, ResearchDataSnapshot)
        and isinstance(integration_probe, Mapping)
        and isinstance(observation_receipt, Mapping)
        and isinstance(
            observation_membership,
            AshareObservationMembershipArtifact,
        )
        and isinstance(observation_transaction_complete, Mapping)
    ):
        transaction_complete_sha256 = _validate_observation_transaction_complete(
            research_snapshot,
            integration_probe,
            observation_receipt,
            observation_membership,
            observation_transaction_complete,
            probe_receipt_sha256=probe_sha256,
            observation_receipt_sha256=observation_sha256,
            membership_sha256=membership_sha256,
            blockers=blockers,
        )

    evidence_blocker_count = len(blockers)
    if not verified_committed_state:
        _append(blockers, "verified_observation_state_required")
    if claims:
        _append(blockers, "historical_pit_evidence_unavailable")
    _append(blockers, "champion_numeric_features_unavailable")
    _append(blockers, "minute_execution_evidence_unavailable")

    observation_eligible = evidence_blocker_count == 0 and verified_committed_state
    ranking_eligible = False
    planning_eligible = False
    bound_snapshot = research_snapshot if observation_eligible else None
    bundle = AshareRuntimeAuthorityBundle(
        research_snapshot=bound_snapshot,
        profile_id=(
            research_snapshot.profile_id
            if observation_eligible
            and isinstance(research_snapshot, ResearchDataSnapshot)
            else None
        ),
        catalog_version=(
            research_snapshot.catalog_version
            if observation_eligible
            and isinstance(research_snapshot, ResearchDataSnapshot)
            else None
        ),
        decision_as_of=(
            research_snapshot.decision_as_of
            if observation_eligible
            and isinstance(research_snapshot, ResearchDataSnapshot)
            else None
        ),
        schema_major=schema_major,
        snapshot_sha256=(
            research_snapshot.snapshot_sha256
            if observation_eligible
            and isinstance(research_snapshot, ResearchDataSnapshot)
            else None
        ),
        probe_receipt_sha256=probe_sha256 if observation_eligible else None,
        observation_receipt_sha256=(
            observation_sha256 if observation_eligible else None
        ),
        observation_membership_sha256=(
            membership_sha256 if observation_eligible else None
        ),
        observation_transaction_complete_sha256=(
            transaction_complete_sha256 if observation_eligible else None
        ),
        historical_pit_eligible=False,
        historical_feature_claims=claims,
        observation_eligible=False,
        ranking_eligible=ranking_eligible,
        planning_eligible=planning_eligible,
        execution_evidence_eligible=False,
        blockers=tuple(blockers),
        observation_membership=(
            observation_membership if observation_eligible else None
        ),
    )
    if observation_eligible:
        return _promote_verified_committed_bundle(bundle)
    return bundle


def build_ashare_runtime_authority_bundle(
    *,
    research_snapshot: ResearchDataSnapshot | None,
    integration_probe: Mapping[str, Any] | None,
    observation_receipt: Mapping[str, Any] | None,
    schema_major: int,
    observation_membership: AshareObservationMembershipArtifact | None = None,
    observation_transaction_complete: Mapping[str, Any] | None = None,
    historical_feature_claims: tuple[str, ...] = (),
) -> AshareRuntimeAuthorityBundle:
    """Validate caller values without minting committed-state eligibility.

    Ordinary mappings and hashes are useful for contract diagnostics, but are
    not durable commit evidence.  Only
    :func:`load_verified_ashare_runtime_authority_bundle` can issue an
    observation-eligible bundle.
    """

    return _assemble_ashare_runtime_authority_bundle(
        research_snapshot=research_snapshot,
        integration_probe=integration_probe,
        observation_receipt=observation_receipt,
        schema_major=schema_major,
        observation_membership=observation_membership,
        observation_transaction_complete=observation_transaction_complete,
        historical_feature_claims=historical_feature_claims,
        verified_committed_state=False,
    )


def _trusted_state_root(value: Path | str) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
        raise AshareRuntimeAuthorityLoadBlocked("observation_state_root_invalid")
    raw = Path(os.fspath(value))
    if not raw.is_absolute() or ".." in raw.parts:
        raise AshareRuntimeAuthorityLoadBlocked("observation_state_root_invalid")
    root = Path(os.path.abspath(os.fspath(raw)))
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise AshareRuntimeAuthorityLoadBlocked(
                "observation_state_root_invalid"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise AshareRuntimeAuthorityLoadBlocked("observation_state_root_invalid")
        if current != root and not stat.S_ISDIR(metadata.st_mode):
            raise AshareRuntimeAuthorityLoadBlocked("observation_state_root_invalid")
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise AshareRuntimeAuthorityLoadBlocked(
            "observation_state_root_invalid"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise AshareRuntimeAuthorityLoadBlocked("observation_state_root_invalid")
    return root


def _read_committed_state_json(path: Path, *, reason_code: str) -> dict[str, Any]:
    try:
        descriptor = os.open(
            os.fspath(path),
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise AshareRuntimeAuthorityLoadBlocked(reason_code) from exc
    try:
        try:
            before = os.fstat(descriptor)
            named = path.lstat()
        except OSError as exc:
            raise AshareRuntimeAuthorityLoadBlocked(reason_code) from exc
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or before.st_uid != os.geteuid()
            or named.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or stat.S_IMODE(named.st_mode) != 0o600
            or before.st_nlink != 1
            or named.st_nlink != 1
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
            or before.st_size <= 0
            or before.st_size > _MAX_AUTHORITY_FILE_BYTES
        ):
            raise AshareRuntimeAuthorityLoadBlocked(reason_code)
        chunks: list[bytes] = []
        remaining = _MAX_AUTHORITY_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1_048_576, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_dev,
            before.st_ino,
        ) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_dev,
            after.st_ino,
        ):
            raise AshareRuntimeAuthorityLoadBlocked(reason_code)
    except OSError as exc:
        raise AshareRuntimeAuthorityLoadBlocked(reason_code) from exc
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AshareRuntimeAuthorityLoadBlocked(reason_code) from exc
    if not isinstance(payload, dict):
        raise AshareRuntimeAuthorityLoadBlocked(reason_code)
    return payload


@contextmanager
def _committed_observation_session_lock(
    root: Path,
    *,
    observation_session: str,
):
    path = root / f"observation-session-lock-{observation_session}.lock"
    try:
        descriptor = os.open(
            os.fspath(path),
            os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise AshareRuntimeAuthorityLoadBlocked(
            "observation_transaction_lock_invalid"
        ) from exc
    try:
        before = os.fstat(descriptor)
        named = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or before.st_uid != os.geteuid()
            or named.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or stat.S_IMODE(named.st_mode) != 0o600
            or before.st_nlink != 1
            or named.st_nlink != 1
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise AshareRuntimeAuthorityLoadBlocked(
                "observation_transaction_lock_invalid"
            )
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        locked = os.fstat(descriptor)
        named_locked = path.lstat()
        if (
            (locked.st_dev, locked.st_ino) != (before.st_dev, before.st_ino)
            or (named_locked.st_dev, named_locked.st_ino)
            != (before.st_dev, before.st_ino)
            or locked.st_nlink != 1
            or named_locked.st_nlink != 1
        ):
            raise AshareRuntimeAuthorityLoadBlocked(
                "observation_transaction_lock_invalid"
            )
        yield
    except OSError as exc:
        raise AshareRuntimeAuthorityLoadBlocked(
            "observation_transaction_lock_invalid"
        ) from exc
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def load_verified_ashare_runtime_authority_bundle(
    *,
    state_root: Path | str,
    profile_id: str,
    catalog_version: str,
    decision_as_of: str,
    manifest_as_of: str,
    manifest_sha256: str,
    schema_major: int,
    historical_feature_claims: tuple[str, ...] = (),
) -> AshareRuntimeAuthorityBundle:
    """Load and bind one fully committed observation transaction.

    All four artifacts and the complete marker are read from one trusted root
    while holding the writer's per-session lock.  Caller-provided mappings and
    hashes never enter the authority-minting path.  ``manifest_as_of`` is the
    writer's exact manifest string used in durable artifact filenames, while
    ``decision_as_of`` is the canonical snapshot key; they must identify the
    same instant.
    """

    root = _trusted_state_root(state_root)
    if (
        not isinstance(profile_id, str)
        or not profile_id
        or profile_id != profile_id.strip()
        or not isinstance(catalog_version, str)
        or not catalog_version
        or catalog_version != catalog_version.strip()
        or not isinstance(decision_as_of, str)
        or not decision_as_of
        or decision_as_of != decision_as_of.strip()
        or not isinstance(manifest_as_of, str)
        or not manifest_as_of
        or manifest_as_of != manifest_as_of.strip()
        or not _is_sha256(manifest_sha256)
    ):
        raise AshareRuntimeAuthorityLoadBlocked(
            "observation_transaction_identity_invalid"
        )
    decision = _aware_utc(decision_as_of)
    manifest_instant = _aware_utc(manifest_as_of)
    if decision is None or manifest_instant is None or decision != manifest_instant:
        raise AshareRuntimeAuthorityLoadBlocked(
            "observation_transaction_identity_invalid"
        )
    observation_session = manifest_instant.astimezone(_SHANGHAI).strftime("%Y%m%d")
    transaction_identity = _canonical_sha256(
        {
            "profile_id": profile_id,
            "catalog_version": catalog_version,
            "as_of": manifest_as_of,
            "manifest_sha256": manifest_sha256,
        }
    )
    if transaction_identity is None:
        raise AshareRuntimeAuthorityLoadBlocked(
            "observation_transaction_identity_invalid"
        )
    probe_path = root / f"integration-{transaction_identity}.json"
    observation_path = root / f"observation-{transaction_identity}.json"
    complete_path = root / f"observation-complete-{transaction_identity}.json"

    with _committed_observation_session_lock(
        root,
        observation_session=observation_session,
    ):
        if not complete_path.exists() and not complete_path.is_symlink():
            raise AshareRuntimeAuthorityLoadBlocked(
                "observation_transaction_complete_missing"
            )
        complete = _read_committed_state_json(
            complete_path,
            reason_code="observation_transaction_complete_invalid",
        )
        probe = _read_committed_state_json(
            probe_path,
            reason_code="integration_probe_receipt_invalid",
        )
        observation = _read_committed_state_json(
            observation_path,
            reason_code="observation_receipt_invalid",
        )
        try:
            snapshot = FileResearchSnapshotStore(root).load_bound_decision(
                profile_id=profile_id,
                decision_as_of=decision_as_of,
                catalog_version=catalog_version,
            )
        except (ValueError, ResearchSnapshotStoreCorruption) as exc:
            raise AshareRuntimeAuthorityLoadBlocked(
                "research_snapshot_store_invalid"
            ) from exc
        if snapshot is None:
            raise AshareRuntimeAuthorityLoadBlocked("research_snapshot_missing")
        try:
            membership = FileAshareObservationMembershipLedger(
                root / "observation-membership"
            ).load_bound_session(observation_session=observation_session)
        except (
            ValueError,
            AshareObservationLedgerContractError,
            AshareObservationLedgerCorruption,
        ) as exc:
            raise AshareRuntimeAuthorityLoadBlocked(
                "observation_membership_invalid"
            ) from exc
        if membership is None:
            raise AshareRuntimeAuthorityLoadBlocked("observation_membership_missing")

        bundle = _assemble_ashare_runtime_authority_bundle(
            research_snapshot=snapshot,
            integration_probe=probe,
            observation_receipt=observation,
            schema_major=schema_major,
            observation_membership=membership,
            observation_transaction_complete=complete,
            historical_feature_claims=historical_feature_claims,
            verified_committed_state=True,
        )
        if bundle.observation_eligible is not True:
            evidence_blockers = tuple(
                reason
                for reason in bundle.blockers
                if reason
                not in {
                    "historical_pit_evidence_unavailable",
                    "champion_numeric_features_unavailable",
                    "minute_execution_evidence_unavailable",
                }
            )
            raise AshareRuntimeAuthorityLoadBlocked(
                evidence_blockers[0]
                if evidence_blockers
                else "observation_committed_state_invalid"
            )
        return bundle


__all__ = [
    "ASHARE_RUNTIME_AUTHORITY_SCHEMA_ID",
    "AshareRuntimeAuthorityBundle",
    "AshareRuntimeAuthorityLoadBlocked",
    "build_ashare_runtime_authority_bundle",
    "load_verified_ashare_runtime_authority_bundle",
]
