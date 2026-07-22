from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from shared.data.evidence_gate import EvidenceAction, EvidenceDecision
from shared.data.research_snapshot import (
    DatasetRequirement,
    ResearchDataContractError,
    ResearchDataProfile,
    build_research_data_snapshot,
)
from shared.data.sharedsignals_v1 import (
    ContractViolation,
    QueryRequest,
    parse_query_envelope,
)
from shared.data.tradingdatas_pagination import bind_complete_page


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
):
    source_rows = rows if rows is not None else [{"value": 1}]
    source_rows = [
        {"entity_id": row.get("entity_id", f"row-{index}"), **row}
        for index, row in enumerate(source_rows)
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
            DatasetRequirement(
                PRICE_DATASET,
                role="required_execution",
                identity_fields=("entity_id",),
            ),
            DatasetRequirement(
                CONTEXT_DATASET,
                role="optional_context",
                identity_fields=("entity_id",),
            ),
        ),
    )


def _runs(*envelopes):
    return tuple(
        bind_complete_page(
            request=QueryRequest(
                dataset_id=envelope.dataset_id,
                schema_major=1,
                as_of=DECISION_AS_OF.isoformat(),
            ),
            envelope=envelope,
            identity_fields=("entity_id",),
        )
        for envelope in envelopes
    )


def test_required_ready_and_deweighted_context_form_current_observation_snapshot() -> None:
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
        page_runs=_runs(context, price),
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
        {"entity_id": "row-0", "ts_code": "600000.SH", "close": 10.5}
    ]
    assert snapshot.historical_pit_eligible is False
    assert snapshot.datasets[0].observation_mode == "current_observation"
    assert snapshot.datasets[0].historical_pit_eligible is False
    assert snapshot.datasets[0].max_row_observed_at == (
        "2026-07-16T01:00:00+00:00"
    )
    assert snapshot.datasets[0].max_row_event_value is None
    assert len(snapshot.snapshot_sha256) == 64
    evidence = snapshot.to_evidence_payload()
    assert evidence["profile_contract_sha256"] == snapshot.profile_contract_sha256
    assert evidence["historical_pit_eligible"] is False
    assert evidence["datasets"][0]["row_observation_sha256"] == (
        snapshot.datasets[0].row_observation_sha256
    )
    assert evidence["datasets"][0]["source_proof_sha256"] == (
        snapshot.datasets[0].source_proof_sha256
    )
    assert evidence["datasets"][0]["page_count"] == 1


def test_required_dataset_rejection_blocks_execution_but_preserves_evidence() -> None:
    price = _envelope(PRICE_DATASET, receipt_id="price-receipt")
    context = _envelope(CONTEXT_DATASET, receipt_id="context-receipt")
    snapshot = build_research_data_snapshot(
        profile=_profile(),
        page_runs=_runs(price, context),
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
        page_runs=_runs(price, context),
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
    assert impaired.max_row_observed_at is None
    assert impaired.source_proof_sha256 is None
    assert impaired.decoded_rows() == []
    evidence = snapshot.to_evidence_payload()["datasets"][0]
    assert evidence["dataset_id"] == PRICE_DATASET
    assert evidence["evidence_action"] == "reject"
    assert evidence["source_proof_complete"] is False
    assert evidence["historical_pit_eligible"] is False
    assert evidence["row_observation_sha256"] == impaired.row_observation_sha256


def test_optional_rejection_does_not_block_but_required_deweight_does() -> None:
    price = _envelope(PRICE_DATASET, receipt_id="price-receipt")
    context = _envelope(CONTEXT_DATASET, receipt_id="context-receipt")

    optional_rejected = build_research_data_snapshot(
        profile=_profile(),
        page_runs=_runs(price, context),
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
        page_runs=_runs(price, context),
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
            page_runs=_runs(*envelopes),
            decisions=tuple(decisions),
            decision_as_of=DECISION_AS_OF,
        )


def test_tampered_pagination_trace_fails_before_snapshot_acceptance() -> None:
    price = _envelope(PRICE_DATASET, receipt_id="price-receipt")
    context = _envelope(CONTEXT_DATASET, receipt_id="context-receipt")
    price_run, context_run = _runs(price, context)
    tampered = replace(price_run, ordered_rows_sha256="0" * 64)

    with pytest.raises(ResearchDataContractError, match="pagination_trace_mismatch"):
        build_research_data_snapshot(
            profile=_profile(),
            page_runs=(tampered, context_run),
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
                    action=EvidenceAction.ACCEPT,
                    weight=1.0,
                ),
            ),
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
            page_runs=_runs(late, context),
            decisions=decisions,
            decision_as_of=DECISION_AS_OF,
        )


def test_current_observation_does_not_invent_revision_and_future_session_fails() -> None:
    profile = ResearchDataProfile(
        profile_id="provider-native-session-v2",
        catalog_version=CATALOG,
        requirements=(
            DatasetRequirement(
                PRICE_DATASET,
                role="required_execution",
                identity_fields=("entity_id",),
                row_event_time_field="trade_date",
                row_event_time_format="yyyymmdd",
                row_event_timezone="Asia/Shanghai",
                row_event_time_semantic="session",
            ),
            DatasetRequirement(
                CONTEXT_DATASET,
                role="optional_context",
                identity_fields=("entity_id",),
            ),
        ),
    )
    price = _envelope(
        PRICE_DATASET,
        receipt_id="price-receipt",
        rows=[{"value": 1, "trade_date": "20260717"}],
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

    with pytest.raises(ResearchDataContractError, match="row_event_after_observation"):
        build_research_data_snapshot(
            profile=profile,
            page_runs=_runs(price, context),
            decisions=decisions,
            decision_as_of=DECISION_AS_OF,
        )


def test_future_scheduled_date_is_domain_time_not_forged_availability() -> None:
    profile = ResearchDataProfile(
        profile_id="provider-native-calendar-v2",
        catalog_version=CATALOG,
        requirements=(
            DatasetRequirement(
                PRICE_DATASET,
                role="required_execution",
                identity_fields=("entity_id", "cal_date"),
                row_event_time_field="cal_date",
                row_event_time_format="yyyymmdd",
                row_event_timezone="Asia/Shanghai",
                row_event_time_semantic="scheduled",
            ),
        ),
    )
    calendar = _envelope(
        PRICE_DATASET,
        receipt_id="calendar-receipt",
        rows=[{"cal_date": "20260717", "is_open": True}],
    )

    snapshot = build_research_data_snapshot(
        profile=profile,
        page_runs=(
            bind_complete_page(
                request=QueryRequest(
                    dataset_id=calendar.dataset_id,
                    schema_major=1,
                    as_of=DECISION_AS_OF.isoformat(),
                ),
                envelope=calendar,
                identity_fields=("entity_id", "cal_date"),
            ),
        ),
        decisions=(
            _decision(
                PRICE_DATASET,
                "calendar-receipt",
                action=EvidenceAction.ACCEPT,
                weight=1.0,
            ),
        ),
        decision_as_of=DECISION_AS_OF,
    )

    dataset = snapshot.datasets[0]
    assert dataset.max_row_event_value == "2026-07-17"
    assert dataset.max_row_observed_at == "2026-07-16T01:00:00+00:00"
    assert dataset.historical_pit_eligible is False


def test_manual_accept_cannot_launder_incomplete_lineage() -> None:
    price = _envelope(
        PRICE_DATASET,
        receipt_id="price-receipt",
        lineage={"complete": False, "provider_neutral": False},
    )
    context = _envelope(CONTEXT_DATASET, receipt_id="context-receipt")

    with pytest.raises(
        ResearchDataContractError,
        match="incomplete_source_proof_must_reject",
    ):
        build_research_data_snapshot(
            profile=_profile(),
            page_runs=_runs(price, context),
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
                    action=EvidenceAction.ACCEPT,
                    weight=1.0,
                ),
            ),
            decision_as_of=DECISION_AS_OF,
        )


def test_envelope_receipt_lineage_and_observation_fields_bind_source_proof() -> None:
    context = _envelope(CONTEXT_DATASET, receipt_id="context-receipt")

    def proof(
        *,
        receipt_id: str,
        lineage: dict,
        observed_at: str,
        data_through: str,
    ) -> str:
        price = _envelope(
            PRICE_DATASET,
            receipt_id=receipt_id,
            lineage=lineage,
            observed_at=observed_at,
            data_through=data_through,
        )
        snapshot = build_research_data_snapshot(
            profile=_profile(),
            page_runs=_runs(price, context),
            decisions=(
                _decision(
                    PRICE_DATASET,
                    receipt_id,
                    action=EvidenceAction.ACCEPT,
                    weight=1.0,
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
        source_proof = snapshot.datasets[0].source_proof_sha256
        assert source_proof is not None
        return source_proof

    baseline = {
        "receipt_id": "receipt-a",
        "lineage": {
            "complete": True,
            "provider_neutral": True,
            "provider": "fixture-a",
        },
        "observed_at": "2026-07-16T01:00:00+00:00",
        "data_through": "2026-07-16T00:59:00+00:00",
    }
    proofs = {
        proof(**baseline),
        proof(**{**baseline, "receipt_id": "receipt-b"}),
        proof(
            **{
                **baseline,
                "lineage": {**baseline["lineage"], "provider": "fixture-b"},
            }
        ),
        proof(**{**baseline, "observed_at": "2026-07-16T01:01:00+00:00"}),
        proof(**{**baseline, "data_through": "2026-07-16T00:58:00+00:00"}),
    }
    assert len(proofs) == 5


def test_data_through_after_observation_fails_closed() -> None:
    with pytest.raises(
        ContractViolation,
        match="metadata.data_through must not be after observed_at",
    ):
        _envelope(
            PRICE_DATASET,
            receipt_id="price-receipt",
            observed_at="2026-07-16T01:00:00+00:00",
            data_through="2026-07-16T01:01:00+00:00",
        )



def test_catalog_drift_fails_closed() -> None:
    wrong_catalog = _envelope(
        PRICE_DATASET,
        receipt_id="price-receipt",
        catalog_version="wrong",
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
    with pytest.raises(ResearchDataContractError, match="catalog_version"):
        build_research_data_snapshot(
            profile=_profile(),
            page_runs=_runs(wrong_catalog, context),
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
        page_runs=_runs(price, context),
        decisions=decisions,
        decision_as_of=DECISION_AS_OF,
    )
    right = build_research_data_snapshot(
        profile=_profile(),
        page_runs=_runs(context, price),
        decisions=tuple(reversed(decisions)),
        decision_as_of=DECISION_AS_OF,
    )

    rows = left.datasets[0].decoded_rows()
    rows[0]["value"] = 999
    assert left.datasets[0].decoded_rows()[0]["value"] == 1
    assert left.snapshot_sha256 == right.snapshot_sha256
