from __future__ import annotations

from typing import Any

import pytest

from shared.data.evidence_gate import (
    DataEvidenceGate,
    DatasetEvidencePolicy,
    EvidenceAction,
)
from shared.data.sharedsignals_v1 import parse_query_envelope


DATASET_ID = "fixture.cn.equity.daily.mainboard.v1"
CONTEXT_DATASET_ID = "fixture.cn.equity.sector.star.context.v1"
CATALOG_VERSION = "fixture-catalog-2026-07-16"


def _payload(
    *,
    dataset_id: str = DATASET_ID,
    state: str = "ready",
    degraded: bool = False,
    freshness: dict[str, Any] | None = None,
    quality: dict[str, Any] | None = None,
    lineage: dict[str, Any] | None = None,
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "api_version": "v1",
        "catalog_version": CATALOG_VERSION,
        "request_id": "request-001",
        "dataset_id": dataset_id,
        "data": [{"value": 1}],
        "metadata": {
            "state": state,
            "degraded": degraded,
            "freshness": freshness or {"state": "fresh", "stale": False},
            "quality": quality or {"state": "valid", "valid": True},
            "lineage": lineage
            or {
                "complete": True,
                "provider_neutral": True,
            },
            "receipt_id": "receipt-001",
            "data_through": "2026-07-15T07:00:00+00:00",
            "observed_at": "2026-07-16T01:00:00+00:00",
            "reasons": reasons or [],
        },
    }


def _gate(*policies: DatasetEvidencePolicy) -> DataEvidenceGate:
    return DataEvidenceGate({policy.dataset_id: policy for policy in policies})


def test_ready_dataset_is_accepted_at_full_weight() -> None:
    envelope = parse_query_envelope(_payload())
    gate = _gate(DatasetEvidencePolicy(dataset_id=DATASET_ID))

    decision = gate.evaluate(envelope)

    assert decision.dataset_id == DATASET_ID
    assert decision.action is EvidenceAction.ACCEPT
    assert decision.eligible is True
    assert decision.weight == 1.0
    assert decision.effective_state == "ready"
    assert decision.receipt_id == "receipt-001"
    assert decision.reasons == ()


def test_dataset_without_explicit_policy_fails_closed() -> None:
    envelope = parse_query_envelope(_payload())

    decision = DataEvidenceGate({}).evaluate(envelope)

    assert decision.action is EvidenceAction.REJECT
    assert decision.eligible is False
    assert decision.weight == 0.0
    assert decision.reasons == ("dataset_policy_missing",)


@pytest.mark.parametrize(
    ("state", "degraded"),
    [("degraded", False), ("ready", True)],
)
def test_degraded_dataset_defaults_to_fail_closed(
    state: str,
    degraded: bool,
) -> None:
    envelope = parse_query_envelope(_payload(state=state, degraded=degraded))
    gate = _gate(DatasetEvidencePolicy(dataset_id=DATASET_ID))

    decision = gate.evaluate(envelope)

    assert decision.effective_state == "degraded"
    assert decision.action is EvidenceAction.REJECT
    assert decision.eligible is False
    assert decision.weight == 0.0
    assert "dataset_degraded" in decision.reasons


def test_explicit_context_policy_can_deweight_degraded_dataset() -> None:
    envelope = parse_query_envelope(
        _payload(
            dataset_id=CONTEXT_DATASET_ID,
            state="degraded",
            reasons=["partial_sector_coverage"],
        )
    )
    gate = _gate(
        DatasetEvidencePolicy(
            dataset_id=CONTEXT_DATASET_ID,
            degraded_action=EvidenceAction.DEWEIGHT,
            degraded_weight=0.25,
        )
    )

    decision = gate.evaluate(envelope)

    assert decision.action is EvidenceAction.DEWEIGHT
    assert decision.eligible is True
    assert decision.weight == 0.25
    assert decision.reasons == ("partial_sector_coverage", "dataset_degraded")


@pytest.mark.parametrize(
    ("state", "expected_state", "expected_reason"),
    [
        ("unobserved", "failed", "dataset_failed"),
        ("paused", "failed", "dataset_failed"),
        ("failed", "failed", "dataset_failed"),
        ("stale", "stale", "dataset_evidence_incomplete"),
        ("empty", "failed", "dataset_failed"),
        ("degraded", "failed", "dataset_evidence_incomplete"),
    ],
)
def test_impaired_dataset_with_nullable_proof_always_fails_closed(
    state: str,
    expected_state: str,
    expected_reason: str,
) -> None:
    payload = _payload(state=state, degraded=state == "degraded")
    payload["data"] = []
    payload["metadata"].update(
        {
            "lineage": None,
            "receipt_id": None,
            "data_through": None,
            "observed_at": None,
        }
    )
    envelope = parse_query_envelope(payload)
    gate = _gate(
        DatasetEvidencePolicy(
            dataset_id=DATASET_ID,
            degraded_action=EvidenceAction.DEWEIGHT,
            stale_action=EvidenceAction.DEWEIGHT,
        )
    )

    decision = gate.evaluate(envelope)

    assert decision.receipt_id is None
    assert decision.effective_state == expected_state
    assert decision.action is EvidenceAction.REJECT
    assert decision.eligible is False
    assert decision.weight == 0.0
    assert expected_reason in decision.reasons


@pytest.mark.parametrize(
    "freshness",
    [
        {"state": "stale", "stale": True},
        {"state": "fresh", "stale": True},
        {"state": "fresh", "fresh": False},
    ],
)
def test_nested_freshness_red_flags_cannot_be_hidden_by_ready_state(
    freshness: dict[str, Any],
) -> None:
    envelope = parse_query_envelope(
        _payload(state="ready", degraded=False, freshness=freshness)
    )
    gate = _gate(
        DatasetEvidencePolicy(
            dataset_id=DATASET_ID,
            stale_action=EvidenceAction.DEWEIGHT,
            stale_weight=0.1,
        )
    )

    decision = gate.evaluate(envelope)

    assert decision.effective_state == "stale"
    assert decision.action is EvidenceAction.DEWEIGHT
    assert decision.eligible is True
    assert decision.weight == 0.1
    assert "dataset_stale" in decision.reasons


@pytest.mark.parametrize(
    ("state", "quality", "lineage"),
    [
        (
            "failed",
            {"state": "valid", "valid": True},
            {"complete": True, "provider_neutral": True},
        ),
        (
            "ready",
            {"state": "failed", "valid": True},
            {"complete": True, "provider_neutral": True},
        ),
        (
            "ready",
            {"state": "valid", "valid": False},
            {"complete": True, "provider_neutral": True},
        ),
        (
            "ready",
            {"state": "valid", "valid": True},
            {"complete": False, "provider_neutral": True},
        ),
    ],
)
def test_failed_quality_or_incomplete_lineage_always_rejects(
    state: str,
    quality: dict[str, Any],
    lineage: dict[str, Any],
) -> None:
    envelope = parse_query_envelope(
        _payload(state=state, quality=quality, lineage=lineage)
    )
    gate = _gate(
        DatasetEvidencePolicy(
            dataset_id=DATASET_ID,
            degraded_action=EvidenceAction.DEWEIGHT,
            stale_action=EvidenceAction.DEWEIGHT,
        )
    )

    decision = gate.evaluate(envelope)

    assert decision.effective_state == "failed"
    assert decision.action is EvidenceAction.REJECT
    assert decision.eligible is False
    assert decision.weight == 0.0
    assert "dataset_failed" in decision.reasons


@pytest.mark.parametrize("nested_field", ["freshness", "quality", "lineage"])
@pytest.mark.parametrize("failed_state", ["failed", "error", "invalid", "unavailable"])
def test_nested_failed_state_cannot_be_laundered_by_ready_top_state(
    nested_field: str,
    failed_state: str,
) -> None:
    payload = _payload()
    payload["metadata"][nested_field]["state"] = failed_state
    envelope = parse_query_envelope(payload)
    gate = _gate(
        DatasetEvidencePolicy(
            dataset_id=DATASET_ID,
            degraded_action=EvidenceAction.DEWEIGHT,
            stale_action=EvidenceAction.DEWEIGHT,
        )
    )

    decision = gate.evaluate(envelope)

    assert decision.effective_state == "failed"
    assert decision.action is EvidenceAction.REJECT
    assert decision.eligible is False
    assert decision.weight == 0.0
    assert "dataset_failed" in decision.reasons


def test_parsed_envelope_evidence_cannot_be_mutated_into_ready_state() -> None:
    envelope = parse_query_envelope(
        _payload(
            lineage={
                "state": "invalid",
                "complete": False,
                "provider_neutral": True,
            }
        )
    )
    gate = _gate(DatasetEvidencePolicy(dataset_id=DATASET_ID))

    leaked_lineage = envelope.metadata.lineage
    assert leaked_lineage is not None
    leaked_lineage["state"] = "complete"
    leaked_lineage["complete"] = True

    decision = gate.evaluate(envelope)

    assert envelope.metadata.lineage == {
        "state": "invalid",
        "complete": False,
        "provider_neutral": True,
    }
    assert decision.effective_state == "failed"
    assert decision.action is EvidenceAction.REJECT
    assert decision.eligible is False
    assert "dataset_failed" in decision.reasons


@pytest.mark.parametrize(
    ("freshness", "quality", "lineage"),
    [
        (
            {"state": "fresh", "stale": "false"},
            {"state": "valid", "valid": True},
            {"complete": True, "provider_neutral": True},
        ),
        (
            {"state": "fresh", "stale": False},
            {"state": "valid", "valid": "true"},
            {"complete": True, "provider_neutral": True},
        ),
        (
            {"state": "fresh", "stale": False},
            {"state": "valid", "valid": True},
            {"complete": "true", "provider_neutral": True},
        ),
    ],
)
def test_malformed_nested_boolean_flags_fail_closed(
    freshness: dict[str, Any],
    quality: dict[str, Any],
    lineage: dict[str, Any],
) -> None:
    envelope = parse_query_envelope(
        _payload(freshness=freshness, quality=quality, lineage=lineage)
    )
    gate = _gate(DatasetEvidencePolicy(dataset_id=DATASET_ID))

    decision = gate.evaluate(envelope)

    assert decision.effective_state == "failed"
    assert decision.action is EvidenceAction.REJECT
    assert decision.eligible is False
    assert "dataset_failed" in decision.reasons


def test_unknown_dataset_state_fails_closed() -> None:
    envelope = parse_query_envelope(_payload(state="mysterious"))
    gate = _gate(DatasetEvidencePolicy(dataset_id=DATASET_ID))

    decision = gate.evaluate(envelope)

    assert decision.effective_state == "unknown"
    assert decision.action is EvidenceAction.REJECT
    assert decision.eligible is False
    assert decision.reasons == ("dataset_state_unknown",)


@pytest.mark.parametrize(
    ("freshness", "quality", "lineage"),
    [
        (
            {"state": "mysterious", "stale": False},
            {"state": "valid", "valid": True},
            {"complete": True, "provider_neutral": True},
        ),
        (
            {"state": "fresh", "stale": False},
            {"state": "mysterious", "valid": True},
            {"complete": True, "provider_neutral": True},
        ),
        (
            {"state": "fresh", "stale": False},
            {"state": "valid", "valid": True},
            {
                "state": "mysterious",
                "complete": True,
                "provider_neutral": True,
            },
        ),
        (
            {"state": "fresh", "stale": False},
            {"state": "valid", "valid": True},
            {"complete": True, "provider_neutral": False},
        ),
    ],
)
def test_ready_top_state_cannot_launder_unknown_or_provider_specific_nested_state(
    freshness: dict[str, Any],
    quality: dict[str, Any],
    lineage: dict[str, Any],
) -> None:
    envelope = parse_query_envelope(
        _payload(freshness=freshness, quality=quality, lineage=lineage)
    )
    decision = _gate(DatasetEvidencePolicy(dataset_id=DATASET_ID)).evaluate(envelope)

    assert decision.effective_state == "failed"
    assert decision.action is EvidenceAction.REJECT
    assert decision.eligible is False


def test_policy_rejects_invalid_weights_and_failed_deweight_attempt() -> None:
    with pytest.raises(ValueError, match="degraded_weight"):
        DatasetEvidencePolicy(
            dataset_id=DATASET_ID,
            degraded_action=EvidenceAction.DEWEIGHT,
            degraded_weight=0.0,
        )
    with pytest.raises(ValueError, match="stale_weight"):
        DatasetEvidencePolicy(
            dataset_id=DATASET_ID,
            stale_action=EvidenceAction.DEWEIGHT,
            stale_weight=1.0,
        )


def test_dataset_reasons_are_preserved_without_overriding_gate_reason() -> None:
    envelope = parse_query_envelope(
        _payload(
            state="degraded",
            reasons=["upstream_receipt_delayed", "coverage_partial"],
        )
    )
    gate = _gate(DatasetEvidencePolicy(dataset_id=DATASET_ID))

    decision = gate.evaluate(envelope)

    assert decision.reasons == (
        "upstream_receipt_delayed",
        "coverage_partial",
        "dataset_degraded",
    )
