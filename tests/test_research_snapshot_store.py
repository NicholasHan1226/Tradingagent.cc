from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from shared.data.evidence_gate import EvidenceAction, EvidenceDecision
from shared.data.research_snapshot import (
    DatasetRequirement,
    ResearchDataProfile,
    build_research_data_snapshot,
)
from shared.data.research_snapshot_store import (
    FileResearchSnapshotStore,
    ResearchSnapshotStoreConflict,
    ResearchSnapshotStoreCorruption,
)
from shared.data.sharedsignals_v1 import parse_query_envelope


CATALOG = "fixture-catalog-2026-07-16"
PRICE_DATASET = "cn.equity.daily.mainboard.v1"
CONTEXT_DATASET = "cn.equity.sector.full_market.context.v1"
DECISION_AS_OF = datetime(2026, 7, 16, 1, 5, tzinfo=timezone.utc)


def _envelope(
    dataset_id: str,
    *,
    receipt_id: str,
    rows: list[dict],
):
    state = "ready" if dataset_id == PRICE_DATASET else "degraded"
    return parse_query_envelope(
        {
            "api_version": "v1",
            "catalog_version": CATALOG,
            "request_id": f"request-{dataset_id}",
            "dataset_id": dataset_id,
            "data": rows,
            "next_cursor": None,
            "metadata": {
                "state": state,
                "degraded": state == "degraded",
                "freshness": {"state": "fresh", "stale": False},
                "quality": {"state": "valid", "valid": True},
                "lineage": {"complete": True, "provider_neutral": True},
                "receipt_id": receipt_id,
                "data_through": "2026-07-15T07:00:00+00:00",
                "observed_at": "2026-07-16T01:00:00+00:00",
                "reasons": [] if state == "ready" else ["context_partial"],
            },
        }
    )


def _decision(
    dataset_id: str,
    receipt_id: str,
    *,
    action: EvidenceAction,
    weight: float,
) -> EvidenceDecision:
    return EvidenceDecision(
        dataset_id=dataset_id,
        receipt_id=receipt_id,
        effective_state="ready" if action is EvidenceAction.ACCEPT else "degraded",
        action=action,
        eligible=action is not EvidenceAction.REJECT,
        weight=weight,
        reasons=() if action is EvidenceAction.ACCEPT else ("context_partial",),
    )


def _snapshot(*, close: float = 10.5):
    profile = ResearchDataProfile(
        profile_id="mainboard-paper-mvp-input-v1",
        catalog_version=CATALOG,
        requirements=(
            DatasetRequirement(PRICE_DATASET, role="required_execution"),
            DatasetRequirement(CONTEXT_DATASET, role="optional_context"),
        ),
    )
    return build_research_data_snapshot(
        profile=profile,
        envelopes=(
            _envelope(
                PRICE_DATASET,
                receipt_id="price-receipt-v1",
                rows=[
                    {
                        "ts_code": "600000.SH",
                        "close": close,
                        "event_time": "2026-07-15T07:00:00+00:00",
                        "available_time": "2026-07-15T07:05:00+00:00",
                        "revision_id": "price-revision-v1",
                        "receipt_id": "price-row-receipt-v1",
                    }
                ],
            ),
            _envelope(
                CONTEXT_DATASET,
                receipt_id="context-receipt-v1",
                rows=[
                    {
                        "sector_id": "sw801080",
                        "breadth": 0.65,
                        "event_time": "2026-07-15T07:00:00+00:00",
                        "available_time": "2026-07-15T07:05:00+00:00",
                        "revision_id": "context-revision-v1",
                        "receipt_id": "context-row-receipt-v1",
                    }
                ],
            ),
        ),
        decisions=(
            _decision(
                PRICE_DATASET,
                "price-receipt-v1",
                action=EvidenceAction.ACCEPT,
                weight=1.0,
            ),
            _decision(
                CONTEXT_DATASET,
                "context-receipt-v1",
                action=EvidenceAction.DEWEIGHT,
                weight=0.25,
            ),
        ),
        decision_as_of=DECISION_AS_OF,
    )


def _null_proof_snapshot():
    profile = ResearchDataProfile(
        profile_id="mainboard-paper-mvp-input-v1-null-proof",
        catalog_version=CATALOG,
        requirements=(
            DatasetRequirement(PRICE_DATASET, role="required_execution"),
            DatasetRequirement(CONTEXT_DATASET, role="optional_context"),
        ),
    )
    impaired = parse_query_envelope(
        {
            "api_version": "v1",
            "catalog_version": CATALOG,
            "request_id": f"request-{PRICE_DATASET}-unobserved",
            "dataset_id": PRICE_DATASET,
            "data": [{"ts_code": "600000.SH", "close": 10.5}],
            "next_cursor": None,
            "metadata": {
                "state": "unobserved",
                "degraded": True,
                "freshness": {"state": "unobserved", "stale": False},
                "quality": {"state": "unobserved", "valid": False},
                "lineage": None,
                "receipt_id": None,
                "data_through": None,
                "observed_at": None,
                "reasons": ["provider_not_observed"],
            },
        }
    )
    context = _envelope(
        CONTEXT_DATASET,
        receipt_id="context-receipt-v1",
        rows=[
            {
                "sector_id": "sw801080",
                "breadth": 0.65,
                "event_time": "2026-07-15T07:00:00+00:00",
                "available_time": "2026-07-15T07:05:00+00:00",
                "revision_id": "context-revision-v1",
                "receipt_id": "context-row-receipt-v1",
            }
        ],
    )
    return build_research_data_snapshot(
        profile=profile,
        envelopes=(impaired, context),
        decisions=(
            EvidenceDecision(
                dataset_id=PRICE_DATASET,
                receipt_id=None,
                effective_state="failed",
                action=EvidenceAction.REJECT,
                eligible=False,
                weight=0.0,
                reasons=("provider_not_observed", "dataset_failed"),
            ),
            _decision(
                CONTEXT_DATASET,
                "context-receipt-v1",
                action=EvidenceAction.DEWEIGHT,
                weight=0.25,
            ),
        ),
        decision_as_of=DECISION_AS_OF,
    )


def _receipt_ids() -> dict[str, str]:
    return {
        PRICE_DATASET: "price-receipt-v1",
        CONTEXT_DATASET: "context-receipt-v1",
    }


def test_null_proof_blocked_snapshot_round_trips_as_content_addressed_audit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "research-snapshots"
    snapshot = _null_proof_snapshot()
    store = FileResearchSnapshotStore(root)

    store.compare_and_swap(snapshot=snapshot, expected_snapshot_sha256=None)
    recovered = FileResearchSnapshotStore(root).load(
        profile_id=snapshot.profile_id,
        decision_as_of=DECISION_AS_OF,
        expected_snapshot_sha256=snapshot.snapshot_sha256,
        catalog_version=CATALOG,
        receipt_ids={PRICE_DATASET: None, CONTEXT_DATASET: "context-receipt-v1"},
    )

    assert recovered == snapshot
    assert recovered.execution_eligible is False
    assert recovered.datasets[0].source_proof_complete is False
    assert recovered.datasets[0].decoded_rows() == []
    artifact = json.loads(
        (root / f"snapshot-{snapshot.snapshot_sha256}.json").read_text(encoding="utf-8")
    )
    assert artifact["datasets"][0]["receipt_id"] is None
    assert artifact["datasets"][0]["data_through"] is None
    assert artifact["datasets"][0]["observed_at"] is None
    assert artifact["datasets"][0]["source_proof_complete"] is False
    assert artifact["datasets"][0]["rows"] == []
    binding = json.loads(next(root.glob("decision-*.json")).read_text(encoding="utf-8"))
    assert binding["receipt_ids"][PRICE_DATASET] is None


def test_null_proof_artifact_tamper_fails_deep_readback_validation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "research-snapshots"
    snapshot = _null_proof_snapshot()
    store = FileResearchSnapshotStore(root)
    store.compare_and_swap(snapshot=snapshot, expected_snapshot_sha256=None)
    artifact_path = root / f"snapshot-{snapshot.snapshot_sha256}.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["datasets"][0]["source_proof_complete"] = True
    dataset_unsigned = dict(artifact["datasets"][0])
    dataset_unsigned.pop("dataset_artifact_sha256")
    artifact["datasets"][0]["dataset_artifact_sha256"] = hashlib.sha256(
        _canonical_json(dataset_unsigned).encode("utf-8")
    ).hexdigest()
    artifact_unsigned = dict(artifact)
    artifact_unsigned.pop("content_sha256")
    artifact["content_sha256"] = hashlib.sha256(
        _canonical_json(artifact_unsigned).encode("utf-8")
    ).hexdigest()
    artifact_path.write_text(_canonical_json(artifact), encoding="utf-8")

    with pytest.raises(
        ResearchSnapshotStoreCorruption,
        match="source_proof_semantics_invalid",
    ):
        store.load(
            profile_id=snapshot.profile_id,
            decision_as_of=DECISION_AS_OF,
            expected_snapshot_sha256=snapshot.snapshot_sha256,
            catalog_version=CATALOG,
            receipt_ids={PRICE_DATASET: None, CONTEXT_DATASET: "context-receipt-v1"},
        )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def test_round_trip_survives_new_instance_with_rows_and_exact_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "research-snapshots"
    snapshot = _snapshot()

    FileResearchSnapshotStore(root).compare_and_swap(
        snapshot=snapshot,
        expected_snapshot_sha256=None,
    )
    recovered = FileResearchSnapshotStore(root).load(
        profile_id=snapshot.profile_id,
        decision_as_of=DECISION_AS_OF,
        expected_snapshot_sha256=snapshot.snapshot_sha256,
        catalog_version=CATALOG,
        receipt_ids=_receipt_ids(),
    )

    assert recovered == snapshot
    assert recovered.datasets[0].decoded_rows() == [
        {
            "available_time": "2026-07-15T07:05:00+00:00",
            "close": 10.5,
            "event_time": "2026-07-15T07:00:00+00:00",
            "receipt_id": "price-row-receipt-v1",
            "revision_id": "price-revision-v1",
            "ts_code": "600000.SH",
        }
    ]
    assert recovered.datasets[1].evidence_state == "degraded"
    assert recovered.datasets[1].evidence_action == "deweight"
    assert recovered.datasets[1].weight == 0.25
    artifact = json.loads(
        (root / f"snapshot-{snapshot.snapshot_sha256}.json").read_text(encoding="utf-8")
    )
    assert artifact["identity"] == {
        "catalog_version": CATALOG,
        "decision_as_of": "2026-07-16T01:05:00+00:00",
        "profile_id": snapshot.profile_id,
        "snapshot_sha256": snapshot.snapshot_sha256,
    }
    assert artifact["evidence_projection"] == snapshot.to_evidence_payload()
    assert artifact["datasets"][0]["rows"] == [
        {
            "available_time": "2026-07-15T07:05:00+00:00",
            "close": 10.5,
            "event_time": "2026-07-15T07:00:00+00:00",
            "receipt_id": "price-row-receipt-v1",
            "revision_id": "price-revision-v1",
            "ts_code": "600000.SH",
        }
    ]


def test_bound_decision_recovery_uses_the_immutable_binding_as_replay_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "research-snapshots"
    snapshot = _snapshot()
    store = FileResearchSnapshotStore(root)

    assert (
        store.load_bound_decision(
            profile_id=snapshot.profile_id,
            decision_as_of=DECISION_AS_OF,
            catalog_version=CATALOG,
        )
        is None
    )
    store.compare_and_swap(snapshot=snapshot, expected_snapshot_sha256=None)

    recovered = FileResearchSnapshotStore(root).load_bound_decision(
        profile_id=snapshot.profile_id,
        decision_as_of=DECISION_AS_OF,
        catalog_version=CATALOG,
    )

    assert recovered == snapshot


def test_bound_decision_recovery_rejects_catalog_drift(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    store = FileResearchSnapshotStore(tmp_path / "research-snapshots")
    store.compare_and_swap(snapshot=snapshot, expected_snapshot_sha256=None)

    with pytest.raises(
        ResearchSnapshotStoreCorruption,
        match="catalog_version_mismatch",
    ):
        store.load_bound_decision(
            profile_id=snapshot.profile_id,
            decision_as_of=DECISION_AS_OF,
            catalog_version="drifted-catalog",
        )


def test_compare_and_swap_is_set_once_idempotent_and_conflict_safe(
    tmp_path: Path,
) -> None:
    root = tmp_path / "research-snapshots"
    store = FileResearchSnapshotStore(root)
    snapshot = _snapshot()
    store.compare_and_swap(snapshot=snapshot, expected_snapshot_sha256=None)
    before = {path.name: path.read_bytes() for path in root.iterdir()}

    store.compare_and_swap(snapshot=snapshot, expected_snapshot_sha256=None)
    store.compare_and_swap(
        snapshot=snapshot,
        expected_snapshot_sha256=snapshot.snapshot_sha256,
    )
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before

    changed = _snapshot(close=10.6)
    with pytest.raises(ResearchSnapshotStoreConflict, match="immutable"):
        store.compare_and_swap(
            snapshot=changed,
            expected_snapshot_sha256=snapshot.snapshot_sha256,
        )
    with pytest.raises(ResearchSnapshotStoreConflict, match="compare_and_swap"):
        store.compare_and_swap(
            snapshot=snapshot,
            expected_snapshot_sha256="f" * 64,
        )
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"profile_id": "wrong-profile"}, "profile_id_mismatch"),
        (
            {"decision_as_of": "2026-07-16T01:04:59+00:00"},
            "decision_as_of_mismatch",
        ),
        ({"catalog_version": "wrong-catalog"}, "catalog_version_mismatch"),
        (
            {"receipt_ids": {**_receipt_ids(), PRICE_DATASET: "wrong-receipt"}},
            "receipt_ids_mismatch",
        ),
        ({"expected_snapshot_sha256": "e" * 64}, "snapshot_sha256_mismatch"),
    ],
)
def test_load_requires_exact_external_identity(
    tmp_path: Path,
    overrides: dict[str, object],
    reason: str,
) -> None:
    snapshot = _snapshot()
    store = FileResearchSnapshotStore(tmp_path / "research-snapshots")
    store.compare_and_swap(snapshot=snapshot, expected_snapshot_sha256=None)
    kwargs: dict[str, object] = {
        "profile_id": snapshot.profile_id,
        "decision_as_of": DECISION_AS_OF,
        "expected_snapshot_sha256": snapshot.snapshot_sha256,
        "catalog_version": CATALOG,
        "receipt_ids": _receipt_ids(),
    }
    kwargs.update(overrides)

    with pytest.raises(ResearchSnapshotStoreCorruption, match=reason):
        store.load(**kwargs)


def test_tampering_even_with_recomputed_outer_hash_fails_deep_validation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "research-snapshots"
    snapshot = _snapshot()
    store = FileResearchSnapshotStore(root)
    store.compare_and_swap(snapshot=snapshot, expected_snapshot_sha256=None)
    artifact_path = root / f"snapshot-{snapshot.snapshot_sha256}.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["evidence_projection"]["datasets"][1]["state"] = "ready"
    unsigned = dict(artifact)
    unsigned.pop("content_sha256")
    artifact["content_sha256"] = hashlib.sha256(
        _canonical_json(unsigned).encode("utf-8")
    ).hexdigest()
    artifact_path.write_text(_canonical_json(artifact), encoding="utf-8")

    with pytest.raises(
        ResearchSnapshotStoreCorruption,
        match="evidence_projection_mismatch",
    ):
        store.load(
            profile_id=snapshot.profile_id,
            decision_as_of=DECISION_AS_OF,
            expected_snapshot_sha256=snapshot.snapshot_sha256,
            catalog_version=CATALOG,
            receipt_ids=_receipt_ids(),
        )


def test_idempotent_replay_revalidates_the_complete_decision_binding(
    tmp_path: Path,
) -> None:
    root = tmp_path / "research-snapshots"
    snapshot = _snapshot()
    store = FileResearchSnapshotStore(root)
    store.compare_and_swap(snapshot=snapshot, expected_snapshot_sha256=None)
    binding_path = next(root.glob("decision-*.json"))
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    binding["receipt_ids"][PRICE_DATASET] = "forged-receipt"
    binding["receipt_ids_sha256"] = hashlib.sha256(
        _canonical_json(binding["receipt_ids"]).encode("utf-8")
    ).hexdigest()
    unsigned = dict(binding)
    unsigned.pop("content_sha256")
    binding["content_sha256"] = hashlib.sha256(
        _canonical_json(unsigned).encode("utf-8")
    ).hexdigest()
    binding_path.write_text(_canonical_json(binding), encoding="utf-8")

    with pytest.raises(
        ResearchSnapshotStoreCorruption,
        match="binding_payload_mismatch",
    ):
        store.compare_and_swap(snapshot=snapshot, expected_snapshot_sha256=None)


def test_forged_healthy_snapshot_and_incomplete_page_are_rejected(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    required = snapshot.datasets[0]
    rejected_required = replace(
        required,
        evidence_state="failed",
        evidence_action="reject",
        eligible=False,
        weight=0.0,
        reasons=("dataset_failed",),
    )
    forged = replace(
        snapshot,
        datasets=(rejected_required, *snapshot.datasets[1:]),
        execution_eligible=True,
        blocking_reasons=(),
    )
    store = FileResearchSnapshotStore(tmp_path / "research-snapshots")
    with pytest.raises(ResearchSnapshotStoreCorruption):
        store.compare_and_swap(snapshot=forged, expected_snapshot_sha256=None)

    incomplete = replace(
        snapshot,
        datasets=(replace(required, next_cursor="next-page"), *snapshot.datasets[1:]),
    )
    with pytest.raises(ResearchSnapshotStoreCorruption, match="pagination_incomplete"):
        store.compare_and_swap(snapshot=incomplete, expected_snapshot_sha256=None)

    optional = snapshot.datasets[1]
    optional_only_payload = {
        "profile_id": snapshot.profile_id,
        "catalog_version": snapshot.catalog_version,
        "decision_as_of": snapshot.decision_as_of,
        "datasets": [
            {
                "dataset_id": optional.dataset_id,
                "role": optional.role,
                "response_sha256": optional.response_sha256,
            }
        ],
        "blocking_reasons": [],
    }
    optional_only = replace(
        snapshot,
        datasets=(optional,),
        snapshot_sha256=hashlib.sha256(
            _canonical_json(optional_only_payload).encode("utf-8")
        ).hexdigest(),
    )
    with pytest.raises(
        ResearchSnapshotStoreCorruption,
        match="required_dataset_missing",
    ):
        store.compare_and_swap(
            snapshot=optional_only,
            expected_snapshot_sha256=None,
        )


def test_symlink_hardlink_and_traversal_paths_fail_closed(tmp_path: Path) -> None:
    snapshot = _snapshot()
    target = tmp_path / "target"
    target.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(target, target_is_directory=True)
    with pytest.raises(ResearchSnapshotStoreCorruption, match="symlink"):
        FileResearchSnapshotStore(linked_root)
    with pytest.raises(ValueError, match="traversal"):
        FileResearchSnapshotStore(tmp_path / "safe" / ".." / "escape")

    root = tmp_path / "research-snapshots"
    store = FileResearchSnapshotStore(root)
    store.compare_and_swap(snapshot=snapshot, expected_snapshot_sha256=None)
    artifact = root / f"snapshot-{snapshot.snapshot_sha256}.json"
    os.link(artifact, tmp_path / "artifact-hardlink")
    with pytest.raises(ResearchSnapshotStoreCorruption, match="hardlink"):
        store.load(
            profile_id=snapshot.profile_id,
            decision_as_of=DECISION_AS_OF,
            expected_snapshot_sha256=snapshot.snapshot_sha256,
            catalog_version=CATALOG,
            receipt_ids=_receipt_ids(),
        )


def test_root_is_explicit_and_atomic_failure_never_publishes_decision_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for invalid in (None, ""):
        with pytest.raises(ValueError, match="explicitly configured"):
            FileResearchSnapshotStore(invalid)  # type: ignore[arg-type]

    root = tmp_path / "research-snapshots"
    snapshot = _snapshot()
    store = FileResearchSnapshotStore(root)
    real_link = os.link
    calls = 0

    def fail_second_link(source, destination, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected binding publication failure")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(
        "shared.data.research_snapshot_store.os.link",
        fail_second_link,
    )
    with pytest.raises(ResearchSnapshotStoreCorruption, match="atomic_write_failed"):
        store.compare_and_swap(snapshot=snapshot, expected_snapshot_sha256=None)

    assert not list(root.glob("decision-*.json"))
    assert not list(root.glob(".tmp-*.json"))
