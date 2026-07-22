from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from shared.data.evidence_gate import EvidenceAction, EvidenceDecision
from shared.data.research_snapshot import (
    DatasetRequirement,
    ResearchDataProfile,
    ResearchDataSnapshot,
    ResearchDatasetSnapshot,
    build_research_data_snapshot,
)
from shared.data.research_snapshot_store import FileResearchSnapshotStore
from shared.data.sharedsignals_v1 import QueryRequest, parse_query_envelope
from shared.data.tradingdatas_pagination import bind_complete_page
from shared.runtime.ashare_observation_ledger import (
    FileAshareObservationMembershipLedger,
)
from shared.runtime.ashare_runtime_ports import (
    ASHARE_RUNTIME_AUTHORITY_SCHEMA_ID,
    AshareRuntimeAuthorityBundle,
    AshareRuntimeAuthorityLoadBlocked,
    build_ashare_runtime_authority_bundle,
    load_verified_ashare_runtime_authority_bundle,
)
from shared.runtime.ashare_observation_ledger import (
    OBSERVED_REASON_CODE,
    AshareObservationMembershipArtifact,
    AshareObservationMembershipRecord,
    build_ashare_observation_membership_artifact,
)


CATALOG_VERSION = "catalog-ashare-runtime-fixture-v1"
DECISION_AS_OF = "2026-07-22T07:10:00+00:00"
MANIFEST_AS_OF = "2026-07-22T15:10:00+08:00"
PROFILE_ID = "ashare-phase1-current-observation-v1"
PROFILE_SHA256 = "1" * 64
DATASET_ID = "fixture.cn.equity.daily.v1"
SCHEMA_MAJOR = 2


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _dataset() -> ResearchDatasetSnapshot:
    return ResearchDatasetSnapshot(
        dataset_id=DATASET_ID,
        role="required_execution",
        api_version="v1",
        catalog_version=CATALOG_VERSION,
        request_id="request-daily-fixture",
        receipt_id="receipt-daily-fixture",
        evidence_state="ready",
        evidence_action="accept",
        eligible=True,
        weight=1.0,
        reasons=(),
        source_proof_complete=True,
        lineage_sha256="2" * 64,
        source_proof_sha256="3" * 64,
        data_through="2026-07-22T07:00:00+00:00",
        observed_at="2026-07-22T07:05:00+00:00",
        next_cursor=None,
        row_count=2,
        observation_mode="current_observation",
        historical_pit_eligible=False,
        query_as_of_mode="decision_as_of",
        minimum_row_count=1,
        max_pages=2,
        max_rows=10,
        identity_fields=("ts_code", "trade_date"),
        row_event_time_field="trade_date",
        row_event_time_format="yyyymmdd",
        row_event_timezone="Asia/Shanghai",
        row_event_time_semantic="session",
        identity_sha256="4" * 64,
        row_observation_sha256="5" * 64,
        max_row_observed_at="2026-07-22T07:05:00+00:00",
        max_row_event_value="20260722",
        page_count=1,
        pagination_trace_sha256="6" * 64,
        pagination_semantic_sha256="7" * 64,
        page_request_set_sha256="8" * 64,
        page_response_set_sha256="9" * 64,
        cursor_chain_sha256="a" * 64,
        response_sha256="b" * 64,
        _rows_json='[{"close":10.0,"trade_date":"20260722",'
        '"ts_code":"600000.SH"},{"close":12.0,'
        '"trade_date":"20260722","ts_code":"000001.SZ"}]',
    )


def _snapshot(*, historical_pit_eligible: bool = False) -> ResearchDataSnapshot:
    dataset = _dataset()
    payload = {
        "profile_id": PROFILE_ID,
        "profile_contract_sha256": PROFILE_SHA256,
        "catalog_version": CATALOG_VERSION,
        "decision_as_of": DECISION_AS_OF,
        "datasets": [
            {
                "dataset_id": dataset.dataset_id,
                "role": dataset.role,
                "response_sha256": dataset.response_sha256,
            }
        ],
        "blocking_reasons": [],
    }
    return ResearchDataSnapshot(
        profile_id=PROFILE_ID,
        profile_contract_sha256=PROFILE_SHA256,
        catalog_version=CATALOG_VERSION,
        decision_as_of=DECISION_AS_OF,
        datasets=(dataset,),
        execution_eligible=True,
        historical_pit_eligible=historical_pit_eligible,
        blocking_reasons=(),
        snapshot_sha256=_sha256(payload),
    )


def _probe(snapshot: ResearchDataSnapshot) -> dict[str, object]:
    dataset = snapshot.datasets[0]
    payload: dict[str, object] = {
        "schema_id": "tradingagent.tradingdatas.integration-readiness.v2",
        "probe_version": 2,
        "authority": "non_authority",
        "production_verified": False,
        "real_trading_enabled": False,
        "profile_id": PROFILE_ID,
        "as_of": MANIFEST_AS_OF,
        "catalog_version": CATALOG_VERSION,
        "manifest_sha256": "c" * 64,
        "status": "pass",
        "blocking": False,
        "reason_codes": [],
        "same_as_of_match": True,
        "semantic_snapshot_sha256": "d" * 64,
        "snapshot_runs": [
            {
                "snapshot_sha256": snapshot.snapshot_sha256,
                "execution_eligible": True,
                "historical_pit_eligible": False,
                "profile_contract_sha256": snapshot.profile_contract_sha256,
                "blocking_reasons": [],
            },
            {
                "snapshot_sha256": snapshot.snapshot_sha256,
                "execution_eligible": True,
                "historical_pit_eligible": False,
                "profile_contract_sha256": snapshot.profile_contract_sha256,
                "blocking_reasons": [],
            },
        ],
        "datasets": [
            {
                "probe_role": "daily_bars",
                "dataset_id": dataset.dataset_id,
                "schema_major": SCHEMA_MAJOR,
                "requirement_role": dataset.role,
                "observation_mode": "current_observation",
                "historical_pit_eligible": False,
                "source_proof_complete": True,
                "eligible": True,
                "pagination_complete": True,
                "same_as_of_match": True,
                "identity_sha256": dataset.identity_sha256,
                "pagination_semantic_sha256": (dataset.pagination_semantic_sha256),
                "row_count": dataset.row_count,
                "page_count": dataset.page_count,
                "receipt_id": dataset.receipt_id,
                "lineage_sha256": dataset.lineage_sha256,
                "source_proof_sha256": dataset.source_proof_sha256,
            }
        ],
    }
    payload["receipt_sha256"] = _sha256(payload)
    return payload


def _observation(
    snapshot: ResearchDataSnapshot,
    probe: dict[str, object],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_id": "tradingagent.ashare.observation-receipt.v1",
        "profile_id": PROFILE_ID,
        "catalog_version": CATALOG_VERSION,
        "decision_as_of": DECISION_AS_OF,
        "manifest_sha256": "c" * 64,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "probe_receipt_sha256": probe["receipt_sha256"],
        "tradable_universe_count": 2,
        "tradable_universe_sha256": _sha256(["000001.SZ", "600000.SH"]),
        "excluded_reason_counts": {},
        "context_probe_roles": ["industry_context"],
        "mode": "observation_only",
        "marketgraph_mode": "mg_off",
        "real_trading_enabled": False,
        "historical_pit_eligible": False,
        "execution_authority": False,
    }
    payload["receipt_sha256"] = _sha256(payload)
    return payload


def _authorities() -> tuple[
    ResearchDataSnapshot,
    dict[str, object],
    dict[str, object],
    AshareObservationMembershipArtifact,
]:
    snapshot = _snapshot()
    probe = _probe(snapshot)
    observation = _observation(snapshot, probe)
    membership = build_ashare_observation_membership_artifact(
        observation_session="20260722",
        research_snapshot=snapshot,
        observation_receipt=observation,
        records=(
            AshareObservationMembershipRecord(
                symbol="000001.SZ",
                disposition="observed",
                reason_code=OBSERVED_REASON_CODE,
            ),
            AshareObservationMembershipRecord(
                symbol="600000.SH",
                disposition="observed",
                reason_code=OBSERVED_REASON_CODE,
            ),
        ),
    )
    return snapshot, probe, observation, membership


def _complete(
    snapshot: ResearchDataSnapshot,
    probe: dict[str, object],
    observation: dict[str, object],
    membership: AshareObservationMembershipArtifact,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_id": "tradingagent.ashare.observation-transaction-complete.v1",
        "profile_id": snapshot.profile_id,
        "catalog_version": snapshot.catalog_version,
        "decision_as_of": snapshot.decision_as_of,
        "observation_session": membership.observation_session,
        "manifest_sha256": probe["manifest_sha256"],
        "snapshot_sha256": snapshot.snapshot_sha256,
        "probe_receipt_sha256": probe["receipt_sha256"],
        "observation_receipt_sha256": observation["receipt_sha256"],
        "observation_membership_sha256": membership.content_sha256,
        "required_artifacts": [
            "integration_probe_receipt",
            "research_snapshot",
            "observation_receipt",
            "observation_membership",
        ],
        "mode": "observation_only",
        "marketgraph_mode": "mg_off",
        "historical_pit_eligible": False,
        "real_trading_enabled": False,
        "execution_authority": False,
    }
    payload["content_sha256"] = _sha256(payload)
    return payload


def _persist_committed_state(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    profile = ResearchDataProfile(
        profile_id=PROFILE_ID,
        catalog_version=CATALOG_VERSION,
        requirements=(
            DatasetRequirement(
                DATASET_ID,
                role="required_execution",
                identity_fields=("ts_code", "trade_date"),
                row_event_time_field="trade_date",
                row_event_time_format="yyyymmdd",
                row_event_timezone="Asia/Shanghai",
                row_event_time_semantic="session",
                max_pages=2,
                max_rows=10,
            ),
        ),
    )
    envelope = parse_query_envelope(
        {
            "api_version": "v1",
            "catalog_version": CATALOG_VERSION,
            "request_id": "request-daily-fixture",
            "dataset_id": DATASET_ID,
            "data": [
                {
                    "close": 10.0,
                    "trade_date": "20260722",
                    "ts_code": "600000.SH",
                },
                {
                    "close": 12.0,
                    "trade_date": "20260722",
                    "ts_code": "000001.SZ",
                },
            ],
            "next_cursor": None,
            "metadata": {
                "state": "ready",
                "degraded": False,
                "freshness": {"state": "fresh", "stale": False},
                "quality": {"state": "valid", "valid": True},
                "lineage": {
                    "state": "complete",
                    "complete": True,
                    "provider_neutral": True,
                    "provider": "fixture",
                    "transport_service": "fixture",
                },
                "receipt_id": "receipt-daily-fixture",
                "data_through": "2026-07-22T07:00:00+00:00",
                "observed_at": "2026-07-22T07:05:00+00:00",
                "reasons": [],
            },
        }
    )
    page_run = bind_complete_page(
        request=QueryRequest(
            dataset_id=DATASET_ID,
            schema_major=SCHEMA_MAJOR,
            fields=("ts_code", "trade_date", "close"),
            filters={"trade_date": {"eq": "20260722"}},
            as_of=DECISION_AS_OF,
            limit=10,
        ),
        envelope=envelope,
        identity_fields=("ts_code", "trade_date"),
    )
    snapshot = build_research_data_snapshot(
        profile=profile,
        page_runs=(page_run,),
        decisions=(
            EvidenceDecision(
                dataset_id=DATASET_ID,
                receipt_id="receipt-daily-fixture",
                effective_state="ready",
                action=EvidenceAction.ACCEPT,
                eligible=True,
                weight=1.0,
                reasons=(),
            ),
        ),
        decision_as_of=datetime.fromisoformat(DECISION_AS_OF),
    )
    probe = _probe(snapshot)
    observation = _observation(snapshot, probe)
    membership = build_ashare_observation_membership_artifact(
        observation_session="20260722",
        research_snapshot=snapshot,
        observation_receipt=observation,
        records=(
            AshareObservationMembershipRecord(
                symbol="000001.SZ",
                disposition="observed",
                reason_code=OBSERVED_REASON_CODE,
            ),
            AshareObservationMembershipRecord(
                symbol="600000.SH",
                disposition="observed",
                reason_code=OBSERVED_REASON_CODE,
            ),
        ),
    )
    complete = _complete(snapshot, probe, observation, membership)
    root = tmp_path / "committed-observation"
    FileResearchSnapshotStore(root).compare_and_swap(
        snapshot=snapshot,
        expected_snapshot_sha256=None,
    )
    FileAshareObservationMembershipLedger(
        root / "observation-membership"
    ).compare_and_swap(
        observation_session=membership.observation_session,
        research_snapshot=snapshot,
        observation_receipt=observation,
        records=membership.records,
        expected_content_sha256=None,
    )
    identity = _sha256(
        {
            "profile_id": PROFILE_ID,
            "catalog_version": CATALOG_VERSION,
            "as_of": MANIFEST_AS_OF,
            "manifest_sha256": probe["manifest_sha256"],
        }
    )
    for name, payload in (
        (f"integration-{identity}.json", probe),
        (f"observation-{identity}.json", observation),
        (f"observation-complete-{identity}.json", complete),
    ):
        path = root / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
    lock = root / "observation-session-lock-20260722.lock"
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    return root, complete


def _load_verified(
    tmp_path: Path,
    *,
    historical_feature_claims: tuple[str, ...] = (),
) -> AshareRuntimeAuthorityBundle:
    root, _ = _persist_committed_state(tmp_path)
    return load_verified_ashare_runtime_authority_bundle(
        state_root=root,
        profile_id=PROFILE_ID,
        catalog_version=CATALOG_VERSION,
        decision_as_of=DECISION_AS_OF,
        manifest_as_of=MANIFEST_AS_OF,
        manifest_sha256="c" * 64,
        schema_major=SCHEMA_MAJOR,
        historical_feature_claims=historical_feature_claims,
    )


def _build(
    *,
    snapshot: ResearchDataSnapshot | None = None,
    probe: dict[str, object] | None = None,
    observation: dict[str, object] | None = None,
    historical_feature_claims: tuple[str, ...] = (),
):
    default_snapshot, default_probe, default_observation, default_membership = (
        _authorities()
    )
    return build_ashare_runtime_authority_bundle(
        research_snapshot=default_snapshot if snapshot is None else snapshot,
        integration_probe=default_probe if probe is None else probe,
        observation_receipt=(
            default_observation if observation is None else observation
        ),
        observation_membership=default_membership,
        observation_transaction_complete=_complete(
            default_snapshot,
            default_probe,
            default_observation,
            default_membership,
        ),
        schema_major=SCHEMA_MAJOR,
        historical_feature_claims=historical_feature_claims,
    )


def test_committed_state_loader_observes_but_abstains_without_champion_features(
    tmp_path: Path,
) -> None:
    bundle = _load_verified(tmp_path)

    assert bundle.schema_id == ASHARE_RUNTIME_AUTHORITY_SCHEMA_ID
    assert bundle.research_snapshot is not None
    assert bundle.profile_id == PROFILE_ID
    assert bundle.catalog_version == CATALOG_VERSION
    assert bundle.decision_as_of == DECISION_AS_OF
    assert bundle.schema_major == SCHEMA_MAJOR
    assert bundle.snapshot_sha256 == bundle.research_snapshot.snapshot_sha256
    assert all(
        isinstance(value, str) and len(value) == 64
        for value in (
            bundle.probe_receipt_sha256,
            bundle.observation_receipt_sha256,
            bundle.observation_membership_sha256,
            bundle.observation_transaction_complete_sha256,
        )
    )
    assert bundle.historical_pit_eligible is False
    assert bundle.historical_feature_claims == ()
    assert bundle.observation_eligible is True
    assert bundle.ranking_eligible is False
    assert bundle.planning_eligible is False
    assert bundle.execution_evidence_eligible is False
    assert bundle.committed_state_verified is True
    assert bundle.blockers == (
        "champion_numeric_features_unavailable",
        "minute_execution_evidence_unavailable",
    )


def test_public_mapping_builder_cannot_self_authorize_observation() -> None:
    snapshot, probe, observation, membership = _authorities()

    bundle = build_ashare_runtime_authority_bundle(
        research_snapshot=snapshot,
        integration_probe=probe,
        observation_receipt=observation,
        observation_membership=membership,
        observation_transaction_complete=_complete(
            snapshot, probe, observation, membership
        ),
        schema_major=SCHEMA_MAJOR,
    )

    assert bundle.observation_eligible is False
    assert bundle.committed_state_verified is False
    assert bundle.research_snapshot is None
    assert "verified_observation_state_required" in bundle.blockers

    with pytest.raises(AttributeError):
        object.__setattr__(bundle, "committed_state_verified", True)
    object.__setattr__(bundle, "observation_eligible", True)
    assert bundle.committed_state_verified is False


def test_direct_dataclass_cannot_self_authorize_with_arbitrary_hashes() -> None:
    snapshot, probe, observation, membership = _authorities()

    with pytest.raises(
        ValueError,
        match="observation_eligible_requires_verified_committed_state",
    ):
        AshareRuntimeAuthorityBundle(
            research_snapshot=snapshot,
            profile_id=snapshot.profile_id,
            catalog_version=snapshot.catalog_version,
            decision_as_of=snapshot.decision_as_of,
            schema_major=SCHEMA_MAJOR,
            snapshot_sha256=snapshot.snapshot_sha256,
            probe_receipt_sha256=str(probe["receipt_sha256"]),
            observation_receipt_sha256=str(observation["receipt_sha256"]),
            observation_membership_sha256=membership.content_sha256,
            observation_transaction_complete_sha256="f" * 64,
            historical_pit_eligible=False,
            historical_feature_claims=(),
            observation_eligible=True,
            ranking_eligible=False,
            planning_eligible=False,
            execution_evidence_eligible=False,
            blockers=(
                "champion_numeric_features_unavailable",
                "minute_execution_evidence_unavailable",
            ),
        )


def test_committed_state_loader_rejects_half_written_transaction_without_complete(
    tmp_path: Path,
) -> None:
    root, _ = _persist_committed_state(tmp_path)
    next(root.glob("observation-complete-*.json")).unlink()

    with pytest.raises(
        AshareRuntimeAuthorityLoadBlocked,
        match="observation_transaction_complete_missing",
    ):
        load_verified_ashare_runtime_authority_bundle(
            state_root=root,
            profile_id=PROFILE_ID,
            catalog_version=CATALOG_VERSION,
            decision_as_of=DECISION_AS_OF,
            manifest_as_of=MANIFEST_AS_OF,
            manifest_sha256="c" * 64,
            schema_major=SCHEMA_MAJOR,
        )


def test_committed_state_loader_rejects_manifest_time_that_is_not_decision_instant(
    tmp_path: Path,
) -> None:
    root, _ = _persist_committed_state(tmp_path)

    with pytest.raises(
        AshareRuntimeAuthorityLoadBlocked,
        match="observation_transaction_identity_invalid",
    ):
        load_verified_ashare_runtime_authority_bundle(
            state_root=root,
            profile_id=PROFILE_ID,
            catalog_version=CATALOG_VERSION,
            decision_as_of=DECISION_AS_OF,
            manifest_as_of="2026-07-22T15:11:00+08:00",
            manifest_sha256="c" * 64,
            schema_major=SCHEMA_MAJOR,
        )


def test_probe_snapshot_runs_must_equal_bound_snapshot_even_with_valid_sha() -> None:
    snapshot, probe, observation, membership = _authorities()
    probe["snapshot_runs"][0]["snapshot_sha256"] = "f" * 64
    probe["receipt_sha256"] = _sha256(
        {key: value for key, value in probe.items() if key != "receipt_sha256"}
    )
    observation["probe_receipt_sha256"] = probe["receipt_sha256"]
    observation["receipt_sha256"] = _sha256(
        {key: value for key, value in observation.items() if key != "receipt_sha256"}
    )

    bundle = build_ashare_runtime_authority_bundle(
        research_snapshot=snapshot,
        integration_probe=probe,
        observation_receipt=observation,
        observation_membership=membership,
        observation_transaction_complete=_complete(
            snapshot, probe, observation, membership
        ),
        schema_major=SCHEMA_MAJOR,
    )

    assert bundle.observation_eligible is False
    assert "integration_probe_snapshot_binding_invalid" in bundle.blockers


def test_membership_must_be_rebuilt_against_the_exact_observation_receipt() -> None:
    snapshot, probe, observation, _membership = _authorities()
    other_observation = dict(observation)
    other_observation["context_probe_roles"] = ["market_breadth"]
    other_observation["receipt_sha256"] = _sha256(
        {
            key: value
            for key, value in other_observation.items()
            if key != "receipt_sha256"
        }
    )
    other_membership = build_ashare_observation_membership_artifact(
        observation_session="20260722",
        research_snapshot=snapshot,
        observation_receipt=other_observation,
        records=(
            AshareObservationMembershipRecord(
                symbol="000001.SZ",
                disposition="observed",
                reason_code=OBSERVED_REASON_CODE,
            ),
            AshareObservationMembershipRecord(
                symbol="600000.SH",
                disposition="observed",
                reason_code=OBSERVED_REASON_CODE,
            ),
        ),
    )

    bundle = build_ashare_runtime_authority_bundle(
        research_snapshot=snapshot,
        integration_probe=probe,
        observation_receipt=observation,
        observation_membership=other_membership,
        observation_transaction_complete=_complete(
            snapshot, probe, observation, other_membership
        ),
        schema_major=SCHEMA_MAJOR,
    )

    assert bundle.observation_eligible is False
    assert "observation_membership_binding_invalid" in bundle.blockers


def test_direct_eligible_bundle_cannot_omit_membership_identity(
    tmp_path: Path,
) -> None:
    bundle = _load_verified(tmp_path)

    with pytest.raises(
        ValueError,
        match="observation_eligible_requires_verified_committed_state",
    ):
        replace(bundle, observation_membership_sha256=None)


def test_historical_feature_claim_fails_closed_without_pit_authority(
    tmp_path: Path,
) -> None:
    bundle = _load_verified(
        tmp_path,
        historical_feature_claims=("momentum_20d", "adv_20d"),
    )

    assert bundle.observation_eligible is True
    assert bundle.ranking_eligible is False
    assert bundle.planning_eligible is False
    assert bundle.execution_evidence_eligible is False
    assert bundle.historical_feature_claims == ("adv_20d", "momentum_20d")
    assert bundle.blockers == (
        "historical_pit_evidence_unavailable",
        "champion_numeric_features_unavailable",
        "minute_execution_evidence_unavailable",
    )


@pytest.mark.parametrize(
    ("missing", "reason"),
    (
        ("snapshot", "research_snapshot_missing"),
        ("probe", "integration_probe_missing"),
        ("observation", "observation_receipt_missing"),
        ("membership", "observation_membership_missing"),
        ("complete", "observation_transaction_complete_missing"),
    ),
)
def test_missing_authority_evidence_fails_closed(missing: str, reason: str) -> None:
    snapshot, probe, observation, membership = _authorities()
    values = {
        "research_snapshot": snapshot,
        "integration_probe": probe,
        "observation_receipt": observation,
        "observation_membership": membership,
        "observation_transaction_complete": _complete(
            snapshot, probe, observation, membership
        ),
    }
    values[
        {
            "snapshot": "research_snapshot",
            "probe": "integration_probe",
            "observation": "observation_receipt",
            "membership": "observation_membership",
            "complete": "observation_transaction_complete",
        }[missing]
    ] = None

    bundle = build_ashare_runtime_authority_bundle(
        **values,
        schema_major=SCHEMA_MAJOR,
    )

    assert bundle.research_snapshot is None
    assert bundle.profile_id is None
    assert bundle.catalog_version is None
    assert bundle.decision_as_of is None
    assert bundle.snapshot_sha256 is None
    assert bundle.probe_receipt_sha256 is None
    assert bundle.observation_receipt_sha256 is None
    assert bundle.observation_membership_sha256 is None
    assert bundle.observation_transaction_complete_sha256 is None
    assert bundle.observation_eligible is False
    assert bundle.ranking_eligible is False
    assert bundle.planning_eligible is False
    assert bundle.execution_evidence_eligible is False
    assert reason in bundle.blockers


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("snapshot_sha", "research_snapshot_sha256_invalid"),
        ("probe_sha", "integration_probe_receipt_sha256_invalid"),
        ("probe_snapshot", "integration_probe_snapshot_binding_invalid"),
        ("decision", "authority_decision_as_of_mismatch"),
        ("catalog", "authority_catalog_version_mismatch"),
        ("schema", "integration_probe_schema_major_mismatch"),
        ("dataset", "integration_probe_dataset_binding_invalid"),
        ("observation_sha", "observation_receipt_sha256_invalid"),
        ("observation_snapshot", "observation_snapshot_binding_invalid"),
        ("observation_probe", "observation_probe_binding_invalid"),
        ("manifest", "authority_manifest_binding_invalid"),
    ),
)
def test_cross_binding_mutations_fail_closed(mutation: str, reason: str) -> None:
    snapshot, probe, observation, membership = _authorities()
    if mutation == "snapshot_sha":
        snapshot = replace(snapshot, snapshot_sha256="f" * 64)
    elif mutation == "probe_sha":
        probe["receipt_sha256"] = "f" * 64
    elif mutation == "probe_snapshot":
        probe["snapshot_runs"][0]["snapshot_sha256"] = "not-a-sha256"
        probe["receipt_sha256"] = _sha256(
            {key: value for key, value in probe.items() if key != "receipt_sha256"}
        )
    elif mutation == "decision":
        probe["as_of"] = "2026-07-23T15:10:00+08:00"
        probe["receipt_sha256"] = _sha256(
            {key: value for key, value in probe.items() if key != "receipt_sha256"}
        )
    elif mutation == "catalog":
        observation["catalog_version"] = "other-catalog"
        observation["receipt_sha256"] = _sha256(
            {
                key: value
                for key, value in observation.items()
                if key != "receipt_sha256"
            }
        )
    elif mutation == "schema":
        probe["datasets"][0]["schema_major"] = 3
        probe["receipt_sha256"] = _sha256(
            {key: value for key, value in probe.items() if key != "receipt_sha256"}
        )
    elif mutation == "dataset":
        probe["datasets"][0]["identity_sha256"] = "f" * 64
        probe["receipt_sha256"] = _sha256(
            {key: value for key, value in probe.items() if key != "receipt_sha256"}
        )
    elif mutation == "observation_sha":
        observation["receipt_sha256"] = "f" * 64
    elif mutation == "observation_snapshot":
        observation["snapshot_sha256"] = "f" * 64
        observation["receipt_sha256"] = _sha256(
            {
                key: value
                for key, value in observation.items()
                if key != "receipt_sha256"
            }
        )
    elif mutation == "observation_probe":
        observation["probe_receipt_sha256"] = "f" * 64
        observation["receipt_sha256"] = _sha256(
            {
                key: value
                for key, value in observation.items()
                if key != "receipt_sha256"
            }
        )
    elif mutation == "manifest":
        observation["manifest_sha256"] = "f" * 64
        observation["receipt_sha256"] = _sha256(
            {
                key: value
                for key, value in observation.items()
                if key != "receipt_sha256"
            }
        )

    bundle = build_ashare_runtime_authority_bundle(
        research_snapshot=snapshot,
        integration_probe=probe,
        observation_receipt=observation,
        observation_membership=membership,
        observation_transaction_complete=_complete(
            snapshot, probe, observation, membership
        ),
        schema_major=SCHEMA_MAJOR,
    )

    assert bundle.research_snapshot is None
    assert bundle.observation_eligible is False
    assert bundle.ranking_eligible is False
    assert bundle.planning_eligible is False
    assert bundle.execution_evidence_eligible is False
    assert reason in bundle.blockers


@pytest.mark.parametrize(
    ("target", "field", "value", "reason"),
    (
        ("probe", "status", "fail", "integration_probe_not_eligible"),
        ("probe", "blocking", True, "integration_probe_not_eligible"),
        (
            "observation",
            "execution_authority",
            True,
            "observation_authority_flags_invalid",
        ),
        (
            "observation",
            "historical_pit_eligible",
            True,
            "observation_authority_flags_invalid",
        ),
    ),
)
def test_non_observation_authority_flags_fail_closed(
    target: str,
    field: str,
    value: object,
    reason: str,
) -> None:
    snapshot, probe, observation, membership = _authorities()
    receipt = probe if target == "probe" else observation
    receipt[field] = value
    receipt["receipt_sha256"] = _sha256(
        {key: item for key, item in receipt.items() if key != "receipt_sha256"}
    )

    bundle = build_ashare_runtime_authority_bundle(
        research_snapshot=snapshot,
        integration_probe=probe,
        observation_receipt=observation,
        observation_membership=membership,
        observation_transaction_complete=_complete(
            snapshot, probe, observation, membership
        ),
        schema_major=SCHEMA_MAJOR,
    )

    assert bundle.observation_eligible is False
    assert reason in bundle.blockers


def test_bundle_does_not_retain_mutable_receipt_mappings() -> None:
    snapshot, probe, observation, membership = _authorities()
    bundle = build_ashare_runtime_authority_bundle(
        research_snapshot=snapshot,
        integration_probe=probe,
        observation_receipt=observation,
        observation_membership=membership,
        observation_transaction_complete=_complete(
            snapshot, probe, observation, membership
        ),
        schema_major=SCHEMA_MAJOR,
    )

    probe["catalog_version"] = "mutated"
    observation["snapshot_sha256"] = "f" * 64

    assert bundle.catalog_version is None
    assert bundle.snapshot_sha256 is None
    assert bundle.committed_state_verified is False
    assert not hasattr(bundle, "integration_probe")
    assert not hasattr(bundle, "observation_receipt")


@pytest.mark.parametrize("schema_major", (True, 0, -1, 1.5, "2"))
def test_schema_major_is_explicit_positive_integer(schema_major: object) -> None:
    snapshot, probe, observation, membership = _authorities()
    with pytest.raises(ValueError, match="schema_major_must_be_positive_integer"):
        build_ashare_runtime_authority_bundle(
            research_snapshot=snapshot,
            integration_probe=probe,
            observation_receipt=observation,
            observation_membership=membership,
            observation_transaction_complete=_complete(
                snapshot, probe, observation, membership
            ),
            schema_major=schema_major,
        )
