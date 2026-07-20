from __future__ import annotations

from datetime import datetime, timezone

import pytest

from shared.data.evidence_gate import EvidenceAction, EvidenceDecision
from shared.data.research_snapshot import (
    DatasetRequirement,
    ResearchDataContractError,
    ResearchDataProfile,
    build_research_data_snapshot,
)
from shared.data.sharedsignals_v1 import parse_query_envelope


CATALOG = "fixture-catalog-2026-07-16"
PRICE_DATASET = "fixture.cn.equity.daily.mainboard.v1"
CONTEXT_DATASET = "fixture.cn.equity.sector.full-market-context.v1"
DECISION_AS_OF = datetime(2026, 7, 16, 1, 5, tzinfo=timezone.utc)


def _envelope(
    dataset_id: str,
    *,
    receipt_id: str | None,
    rows: list[dict] | None = None,
    catalog_version: str = CATALOG,
    observed_at: str | None = "2026-07-16T01:00:00+00:00",
    data_through: str | None = "2026-07-15T07:00:00+00:00",
    state: str = "ready",
    degraded: bool = False,
    lineage: dict | None = None,
    enrich_rows: bool = True,
):
    source_rows = rows if rows is not None else [{"value": 1}]
    if enrich_rows:
        source_rows = [
            {
                **row,
                "event_time": row.get("event_time", "2026-07-15T07:00:00+00:00"),
                "available_time": row.get(
                    "available_time", "2026-07-16T00:59:00+00:00"
                ),
                "revision_id": row.get("revision_id", "r1"),
                "receipt_id": row.get("receipt_id", f"row-{receipt_id}"),
            }
            for row in source_rows
        ]
    return parse_query_envelope(
        {
            "api_version": "v1",
            "catalog_version": catalog_version,
            "request_id": f"request-{dataset_id}",
            "dataset_id": dataset_id,
            "data": source_rows,
            "next_cursor": None,
            "metadata": {
                "state": state,
                "degraded": degraded,
                "freshness": {"state": "fresh", "stale": False},
                "quality": {"state": "valid", "valid": True},
                "lineage": (
                    {"complete": True, "provider_neutral": True}
                    if lineage is None and receipt_id is not None
                    else lineage
                ),
                "receipt_id": receipt_id,
                "data_through": data_through,
                "observed_at": observed_at,
                "reasons": [] if state == "ready" else ["provider_not_observed"],
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
        reasons=() if action is EvidenceAction.ACCEPT else ("dataset_degraded",),
    )


def _profile() -> ResearchDataProfile:
    return ResearchDataProfile(
        profile_id="mainboard-paper-mvp-input-v1",
        catalog_version=CATALOG,
        requirements=(
            DatasetRequirement(PRICE_DATASET, role="required_execution"),
            DatasetRequirement(CONTEXT_DATASET, role="optional_context"),
        ),
    )


def test_required_ready_and_deweighted_context_form_one_pit_snapshot() -> None:
    price = _envelope(
        PRICE_DATASET,
        receipt_id="price-receipt",
        rows=[{"ts_code": "600000.SH", "close": 10.5}],
    )
    context = _envelope(
        CONTEXT_DATASET,
        receipt_id="context-receipt",
        rows=[{"sector_id": "sw801080", "breadth": 0.65}],
    )
    snapshot = build_research_data_snapshot(
        profile=_profile(),
        envelopes=(context, price),
        decisions=(
            _decision(
                CONTEXT_DATASET,
                "context-receipt",
                action=EvidenceAction.DEWEIGHT,
                weight=0.25,
            ),
            _decision(
                PRICE_DATASET,
                "price-receipt",
                action=EvidenceAction.ACCEPT,
                weight=1.0,
            ),
        ),
        decision_as_of=DECISION_AS_OF,
    )

    assert snapshot.execution_eligible is True
    assert snapshot.blocking_reasons == ()
    assert snapshot.profile_id == "mainboard-paper-mvp-input-v1"
    assert [row.dataset_id for row in snapshot.datasets] == [
        PRICE_DATASET,
        CONTEXT_DATASET,
    ]
    assert snapshot.datasets[1].weight == 0.25
    assert snapshot.datasets[0].decoded_rows() == [
        {
            "available_time": "2026-07-16T00:59:00+00:00",
            "close": 10.5,
            "event_time": "2026-07-15T07:00:00+00:00",
            "receipt_id": "row-price-receipt",
            "revision_id": "r1",
            "ts_code": "600000.SH",
        }
    ]
    assert len(snapshot.snapshot_sha256) == 64
    assert snapshot.to_evidence_payload() == {
        "profile_id": "mainboard-paper-mvp-input-v1",
        "catalog_version": CATALOG,
        "decision_as_of": "2026-07-16T01:05:00+00:00",
        "snapshot_sha256": snapshot.snapshot_sha256,
        "execution_eligible": True,
        "blocking_reasons": [],
        "datasets": [
            {
                "dataset_id": PRICE_DATASET,
                "role": "required_execution",
                "state": "ready",
                "evidence_action": "accept",
                "effective_weight": 1.0,
                "source_proof_complete": True,
                "receipt_id": "price-receipt",
                "row_count": 1,
                "row_pit_sha256": snapshot.datasets[0].row_pit_sha256,
                "max_row_available_time": "2026-07-16T00:59:00+00:00",
                "reasons": [],
            },
            {
                "dataset_id": CONTEXT_DATASET,
                "role": "optional_context",
                "state": "degraded",
                "evidence_action": "deweight",
                "effective_weight": 0.25,
                "source_proof_complete": True,
                "receipt_id": "context-receipt",
                "row_count": 1,
                "row_pit_sha256": snapshot.datasets[1].row_pit_sha256,
                "max_row_available_time": "2026-07-16T00:59:00+00:00",
                "reasons": ["dataset_degraded"],
            },
        ],
    }


def test_required_dataset_rejection_blocks_execution_but_preserves_evidence() -> None:
    price = _envelope(PRICE_DATASET, receipt_id="price-receipt")
    context = _envelope(CONTEXT_DATASET, receipt_id="context-receipt")
    snapshot = build_research_data_snapshot(
        profile=_profile(),
        envelopes=(price, context),
        decisions=(
            _decision(
                PRICE_DATASET,
                "price-receipt",
                action=EvidenceAction.REJECT,
                weight=0.0,
            ),
            _decision(
                CONTEXT_DATASET,
                "context-receipt",
                action=EvidenceAction.ACCEPT,
                weight=1.0,
            ),
        ),
        decision_as_of=DECISION_AS_OF,
    )

    assert snapshot.execution_eligible is False
    assert snapshot.blocking_reasons == (f"required_dataset_rejected:{PRICE_DATASET}",)
    assert len(snapshot.datasets) == 2


def test_null_source_proof_forms_a_deterministic_audit_snapshot_without_rows() -> None:
    price = _envelope(
        PRICE_DATASET,
        receipt_id=None,
        rows=[{"ts_code": "600000.SH", "close": 10.5}],
        state="unobserved",
        degraded=True,
        lineage=None,
        data_through=None,
        observed_at=None,
    )
    context = _envelope(CONTEXT_DATASET, receipt_id="context-receipt")
    snapshot = build_research_data_snapshot(
        profile=_profile(),
        envelopes=(price, context),
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
                "context-receipt",
                action=EvidenceAction.ACCEPT,
                weight=1.0,
            ),
        ),
        decision_as_of=DECISION_AS_OF,
    )

    impaired = snapshot.datasets[0]
    assert snapshot.execution_eligible is False
    assert snapshot.blocking_reasons == (
        f"required_dataset_rejected:{PRICE_DATASET}",
        f"dataset_source_proof_incomplete:{PRICE_DATASET}",
    )
    assert impaired.source_proof_complete is False
    assert impaired.receipt_id is None
    assert impaired.data_through is None
    assert impaired.observed_at is None
    assert impaired.row_count == 0
    assert impaired.max_row_available_time is None
    assert impaired.decoded_rows() == []
    assert snapshot.to_evidence_payload()["datasets"][0] == {
        "dataset_id": PRICE_DATASET,
        "role": "required_execution",
        "state": "failed",
        "evidence_action": "reject",
        "effective_weight": 0.0,
        "source_proof_complete": False,
        "receipt_id": None,
        "row_count": 0,
        "row_pit_sha256": impaired.row_pit_sha256,
        "max_row_available_time": None,
        "reasons": ["provider_not_observed", "dataset_failed"],
    }


def test_optional_rejection_does_not_block_but_required_deweight_does() -> None:
    price = _envelope(PRICE_DATASET, receipt_id="price-receipt")
    context = _envelope(CONTEXT_DATASET, receipt_id="context-receipt")

    optional_rejected = build_research_data_snapshot(
        profile=_profile(),
        envelopes=(price, context),
        decisions=(
            _decision(
                PRICE_DATASET,
                "price-receipt",
                action=EvidenceAction.ACCEPT,
                weight=1.0,
            ),
            _decision(
                CONTEXT_DATASET,
                "context-receipt",
                action=EvidenceAction.REJECT,
                weight=0.0,
            ),
        ),
        decision_as_of=DECISION_AS_OF,
    )
    assert optional_rejected.execution_eligible is True
    assert optional_rejected.blocking_reasons == ()

    required_deweighted = build_research_data_snapshot(
        profile=_profile(),
        envelopes=(price, context),
        decisions=(
            _decision(
                PRICE_DATASET,
                "price-receipt",
                action=EvidenceAction.DEWEIGHT,
                weight=0.25,
            ),
            _decision(
                CONTEXT_DATASET,
                "context-receipt",
                action=EvidenceAction.ACCEPT,
                weight=1.0,
            ),
        ),
        decision_as_of=DECISION_AS_OF,
    )
    assert required_deweighted.execution_eligible is False
    assert required_deweighted.blocking_reasons == (
        f"required_dataset_deweighted:{PRICE_DATASET}",
    )


@pytest.mark.parametrize("problem", ["missing", "extra", "receipt_mismatch"])
def test_dataset_set_and_receipt_identity_fail_closed(problem: str) -> None:
    price = _envelope(PRICE_DATASET, receipt_id="price-receipt")
    context = _envelope(CONTEXT_DATASET, receipt_id="context-receipt")
    envelopes = [price, context]
    decisions = [
        _decision(
            PRICE_DATASET,
            "price-receipt",
            action=EvidenceAction.ACCEPT,
            weight=1.0,
        ),
        _decision(
            CONTEXT_DATASET,
            "context-receipt",
            action=EvidenceAction.ACCEPT,
            weight=1.0,
        ),
    ]
    if problem == "missing":
        envelopes.pop()
    elif problem == "extra":
        extra = _envelope("unexpected.dataset.v1", receipt_id="extra")
        envelopes.append(extra)
        decisions.append(
            _decision(
                "unexpected.dataset.v1",
                "extra",
                action=EvidenceAction.ACCEPT,
                weight=1.0,
            )
        )
    else:
        decisions[0] = _decision(
            PRICE_DATASET,
            "wrong-receipt",
            action=EvidenceAction.ACCEPT,
            weight=1.0,
        )

    with pytest.raises(ResearchDataContractError):
        build_research_data_snapshot(
            profile=_profile(),
            envelopes=tuple(envelopes),
            decisions=tuple(decisions),
            decision_as_of=DECISION_AS_OF,
        )


def test_late_observation_and_catalog_drift_fail_closed() -> None:
    context = _envelope(CONTEXT_DATASET, receipt_id="context-receipt")
    decisions = (
        _decision(
            PRICE_DATASET,
            "price-receipt",
            action=EvidenceAction.ACCEPT,
            weight=1.0,
        ),
        _decision(
            CONTEXT_DATASET,
            "context-receipt",
            action=EvidenceAction.ACCEPT,
            weight=1.0,
        ),
    )
    late = _envelope(
        PRICE_DATASET,
        receipt_id="price-receipt",
        observed_at="2026-07-16T01:06:00+00:00",
    )
    with pytest.raises(ResearchDataContractError, match="observed_after_decision"):
        build_research_data_snapshot(
            profile=_profile(),
            envelopes=(late, context),
            decisions=decisions,
            decision_as_of=DECISION_AS_OF,
        )


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (
            {
                "value": 1,
                "event_time": "2026-07-15T07:00:00+00:00",
                "available_time": "2026-07-16T01:05:01+00:00",
                "revision_id": "r1",
                "receipt_id": "row-price-receipt",
            },
            "row_available_after_decision",
        ),
        (
            {
                "value": 1,
                "event_time": "2026-07-15T07:00:00+00:00",
                "revision_id": "r1",
                "receipt_id": "row-price-receipt",
            },
            "available_time",
        ),
        (
            {
                "value": 1,
                "event_time": "2026-07-15T07:00:00+00:00",
                "available_time": "2026-07-16T00:59:00+00:00",
                "receipt_id": "row-price-receipt",
            },
            "revision_id",
        ),
    ],
)
def test_hidden_future_or_unversioned_row_cannot_be_laundered_by_early_envelope(
    row: dict,
    reason: str,
) -> None:
    price = _envelope(
        PRICE_DATASET,
        receipt_id="price-receipt",
        rows=[row],
        enrich_rows=False,
    )
    context = _envelope(CONTEXT_DATASET, receipt_id="context-receipt")
    decisions = (
        _decision(
            PRICE_DATASET,
            "price-receipt",
            action=EvidenceAction.ACCEPT,
            weight=1.0,
        ),
        _decision(
            CONTEXT_DATASET,
            "context-receipt",
            action=EvidenceAction.ACCEPT,
            weight=1.0,
        ),
    )

    with pytest.raises(ResearchDataContractError, match=reason):
        build_research_data_snapshot(
            profile=_profile(),
            envelopes=(price, context),
            decisions=decisions,
            decision_as_of=DECISION_AS_OF,
        )

    wrong_catalog = _envelope(
        PRICE_DATASET,
        receipt_id="price-receipt",
        catalog_version="wrong",
    )
    with pytest.raises(ResearchDataContractError, match="catalog_version"):
        build_research_data_snapshot(
            profile=_profile(),
            envelopes=(wrong_catalog, context),
            decisions=decisions,
            decision_as_of=DECISION_AS_OF,
        )


def test_snapshot_is_order_independent_and_rows_are_copy_on_read() -> None:
    price = _envelope(PRICE_DATASET, receipt_id="price-receipt")
    context = _envelope(CONTEXT_DATASET, receipt_id="context-receipt")
    decisions = (
        _decision(
            PRICE_DATASET,
            "price-receipt",
            action=EvidenceAction.ACCEPT,
            weight=1.0,
        ),
        _decision(
            CONTEXT_DATASET,
            "context-receipt",
            action=EvidenceAction.ACCEPT,
            weight=1.0,
        ),
    )
    left = build_research_data_snapshot(
        profile=_profile(),
        envelopes=(price, context),
        decisions=decisions,
        decision_as_of=DECISION_AS_OF,
    )
    right = build_research_data_snapshot(
        profile=_profile(),
        envelopes=(context, price),
        decisions=tuple(reversed(decisions)),
        decision_as_of=DECISION_AS_OF,
    )

    rows = left.datasets[0].decoded_rows()
    rows[0]["value"] = 999
    assert left.datasets[0].decoded_rows()[0]["value"] == 1
    assert left.snapshot_sha256 == right.snapshot_sha256
