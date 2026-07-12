from __future__ import annotations

import json
from hashlib import sha256

import pytest

from CNFutures.execution_evidence import (
    build_execution_evidence,
    build_round_trip_evidence,
    validate_execution_evidence,
)


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _fixture() -> tuple[
    dict[str, object], dict[str, object], dict[str, object], dict[str, object]
]:
    raw = {
        "real_trading_enabled": False,
        "order_id": "SIM-CNF-1",
        "symbol": "RB2610.SHF",
        "side": "buy",
        "quantity": 1,
        "price": 3500.0,
        "requested_price": 3499.3,
        "slippage_bps": 2.0,
        "fill_evidence_type": "bar_volume_participation",
        "evidence_timestamp": "2026-07-13T09:35:00+08:00",
        "margin_required": 4550.0,
        "contract_multiplier": 10,
    }
    receipt = {
        "status": "filled",
        "filled_qty": 1,
        "avg_price": 3500.0,
        "fee": 7.0,
        "message": "filled",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "order_id": "SIM-CNF-1",
        "market": "cn_futures",
        "raw_response": raw,
    }
    order = {
        "order_id": "SIM-CNF-1",
        "symbol": "RB2610.SHF",
        "side": "buy",
        "capital_commit_action": "fill_commit",
        "capital_commit_action_id": "MCAP-ACTION-1",
        "capital_commit_reference_id": "MCAPFILL:1:lineage:reservation:fill",
    }
    request = {
        "authority_id": "cn-futures-capital-v1",
        "authority_generation": 1,
        "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
        "order_id": "SIM-CNF-1",
        "risk_unit_key": "RB2610.SHF",
        "side": "buy",
        "execution_fill_id": "CNF-FILL-1",
        "actual_filled_quantity": 1,
        "actual_fill_price": 3500.0,
        "actual_fee_cash_cny": 7.0,
        "actual_margin_cny": 4550.0,
        "contract_multiplier": 10.0,
        "contract_spec_version": "cn-futures-contract-spec.v1",
        "contract_spec_sha256": "c" * 64,
        "source_sha256": "a" * 64,
        "receipt_sha256": _digest(receipt),
        "local_trade_sha256": "d" * 64,
    }
    result = {
        "committed": True,
        "status": "committed",
        "event_id": "MCAP-EVENT-1",
        "snapshot": {"event_checksum": "e" * 64},
    }
    return order, receipt, request, result


def test_builds_hash_bound_execution_evidence_from_committed_fill() -> None:
    order, receipt, request, result = _fixture()
    evidence = build_execution_evidence(
        order=order,
        receipt=receipt,
        capital_commit_request=request,
        capital_commit_result=result,
        source_snapshot_sha256="a" * 64,
    )

    valid, reason = validate_execution_evidence(
        evidence,
        source_snapshot_sha256="a" * 64,
    )
    assert valid is True
    assert reason == "complete"
    assert evidence["capital_authority_id"] == "cn-futures-capital-v1"
    assert evidence["capital_commit_status"] == "committed"
    assert evidence["receipt_sha256"] == _digest(receipt)
    assert evidence["requested_price"] == 3499.3
    assert evidence["slippage_cny"] == pytest.approx(7.0)


def test_rejects_missing_or_tampered_execution_fact() -> None:
    order, receipt, request, result = _fixture()
    evidence = build_execution_evidence(
        order=order,
        receipt=receipt,
        capital_commit_request=request,
        capital_commit_result=result,
        source_snapshot_sha256="a" * 64,
    )
    evidence["fee_cash_cny"] = 0.0

    assert validate_execution_evidence(
        evidence,
        source_snapshot_sha256="a" * 64,
    ) == (False, "execution_evidence_sha256_mismatch")
    assert validate_execution_evidence({}, source_snapshot_sha256="a" * 64) == (
        False,
        "execution_evidence_schema_invalid",
    )


def test_builder_rejects_uncommitted_or_receipt_hash_mismatch() -> None:
    order, receipt, request, result = _fixture()
    result["committed"] = False
    with pytest.raises(ValueError, match="capital_commit_not_completed"):
        build_execution_evidence(
            order=order,
            receipt=receipt,
            capital_commit_request=request,
            capital_commit_result=result,
            source_snapshot_sha256="a" * 64,
        )


def test_round_trip_evidence_binds_entry_exit_actual_costs_and_slippage() -> None:
    order, receipt, request, result = _fixture()
    entry = build_execution_evidence(
        order=order,
        receipt=receipt,
        capital_commit_request=request,
        capital_commit_result=result,
        source_snapshot_sha256="a" * 64,
    )
    exit_evidence = dict(entry)
    exit_evidence.update(
        {
            "side": "sell",
            "execution_fill_id": "CNF-FILL-EXIT-1",
            "filled_quantity": 1,
            "fill_price": 3520.0,
            "requested_price": 3520.7,
            "fee_cash_cny": 4.0,
            "slippage_cny": 7.0,
            "capital_commit_action": "position_close_commit",
            "capital_commit_action_id": "MCAP-CLOSE-ACTION-1",
            "capital_commit_reference_id": "MCAPCLOSE:1:lineage:rb:fill",
            "capital_commit_event_id": "MCAP-CLOSE-EVENT-1",
            "capital_commit_event_checksum": "9" * 64,
        }
    )
    exit_evidence.pop("execution_evidence_sha256")
    exit_evidence["execution_evidence_sha256"] = _digest(exit_evidence)

    round_trip = build_round_trip_evidence(
        entry_execution_evidence=entry,
        exit_execution_evidence=exit_evidence,
        closed_quantity=1,
        actual_fill_gross_pnl_cny=200.0,
    )

    assert round_trip["round_trip_complete"] is True
    assert round_trip["gross_pnl_cny"] == pytest.approx(214.0)
    assert round_trip["fee_cny"] == pytest.approx(11.0)
    assert round_trip["slippage_cny"] == pytest.approx(14.0)
    assert round_trip["net_pnl_cny"] == pytest.approx(189.0)
    assert len(round_trip["round_trip_evidence_sha256"]) == 64

    order, receipt, request, result = _fixture()
    request["receipt_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="receipt_sha256_mismatch"):
        build_execution_evidence(
            order=order,
            receipt=receipt,
            capital_commit_request=request,
            capital_commit_result=result,
            source_snapshot_sha256="a" * 64,
        )
