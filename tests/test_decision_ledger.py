from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from shared.review.decision_ledger import (
    DecisionExposureRecord,
    DecisionLedgerContractError,
    ExposureDisposition,
    InMemoryDecisionLedger,
)


NOW = datetime(2026, 7, 16, 2, 30, tzinfo=timezone.utc)


def _record(
    decision_id: str,
    disposition: ExposureDisposition,
    **overrides: object,
) -> DecisionExposureRecord:
    values = {
        "decision_id": decision_id,
        "decision_cluster_id": "cluster-1",
        "decision_time": NOW,
        "symbol": "600000.SH",
        "model_id": "ashare-champion",
        "model_version": "1.0.0",
        "manifest_sha256": "a" * 64,
        "action": "buy",
        "disposition": disposition,
        "requested_notional_cny": 5_000.0,
        "filled_quantity": 0,
        "filled_notional_cny": 0.0,
        "actual_cost_cny": 0.0,
        "simulated_fill_id": None,
        "rejection_reason": None,
        "nonfill_reason": None,
    }
    values.update(overrides)
    return DecisionExposureRecord(**values)


def test_ledger_records_filled_nonfilled_and_rejected_exposures() -> None:
    ledger = InMemoryDecisionLedger()
    filled = _record(
        "decision-filled",
        ExposureDisposition.PAPER_FILLED,
        filled_quantity=100,
        filled_notional_cny=1_200.0,
        actual_cost_cny=6.0,
        simulated_fill_id="paper-fill-1",
    )
    nonfilled = _record(
        "decision-nonfilled",
        ExposureDisposition.PAPER_NOT_FILLED,
        nonfill_reason="limit_up_no_fill",
    )
    rejected = _record(
        "decision-rejected",
        ExposureDisposition.REJECTED,
        rejection_reason="insufficient_net_edge_after_cost",
    )

    ledger.append(filled)
    ledger.append(nonfilled)
    ledger.append(rejected)

    assert tuple(record.decision_id for record in ledger.records()) == (
        "decision-filled",
        "decision-nonfilled",
        "decision-rejected",
    )
    assert ledger.by_disposition(ExposureDisposition.PAPER_FILLED) == (filled,)
    assert ledger.by_disposition(ExposureDisposition.PAPER_NOT_FILLED) == (nonfilled,)
    assert ledger.by_disposition(ExposureDisposition.REJECTED) == (rejected,)


def test_decision_records_are_immutable_and_ids_are_idempotent() -> None:
    ledger = InMemoryDecisionLedger()
    record = _record("decision-1", ExposureDisposition.SHADOW_ONLY)
    ledger.append(record)

    with pytest.raises(FrozenInstanceError):
        record.action = "sell"  # type: ignore[misc]

    assert ledger.append(record) is False
    conflicting = _record(
        "decision-1",
        ExposureDisposition.REJECTED,
        rejection_reason="different_payload",
    )
    with pytest.raises(DecisionLedgerContractError, match="conflicting_decision_id"):
        ledger.append(conflicting)


def test_disposition_specific_fields_fail_closed() -> None:
    with pytest.raises(DecisionLedgerContractError, match="simulated_fill"):
        _record(
            "bad-fill",
            ExposureDisposition.PAPER_FILLED,
            filled_quantity=100,
            filled_notional_cny=1_200.0,
            actual_cost_cny=6.0,
        )

    with pytest.raises(DecisionLedgerContractError, match="nonfill_reason"):
        _record("bad-nonfill", ExposureDisposition.PAPER_NOT_FILLED)

    with pytest.raises(DecisionLedgerContractError, match="rejection_reason"):
        _record("bad-reject", ExposureDisposition.REJECTED)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("real_trading_enabled", True),
        ("live_transition_authorized", True),
        ("broker_order_id", "real-order-1"),
        ("account_type", "live"),
        ("capital_layer", "real"),
    ],
)
def test_every_real_or_live_marker_fails_closed(field: str, value: object) -> None:
    with pytest.raises(DecisionLedgerContractError, match="simulation_only"):
        _record(
            "decision-live-marker",
            ExposureDisposition.SHADOW_ONLY,
            **{field: value},
        )
