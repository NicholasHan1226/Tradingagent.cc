from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.execution.signal_state_machine import SignalStateMachine


def _sim_card(order_id: str, *, intent: str = "open") -> dict[str, object]:
    return {
        "order_id": order_id,
        "idempotency_key": order_id,
        "symbol": "RB2610.SHF",
        "side": "buy",
        "quantity": 2,
        "price": 3500.0,
        "order_intent": intent,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
        "timestamp": "2026-07-13T09:35:00+08:00",
    }


def _write_partial_directory_projection(
    signals_dir: Path,
    *,
    order_id: str = "SIM-CNF-ORDER-1",
) -> tuple[dict[str, object], dict[str, object]]:
    card = _sim_card(order_id)
    machine = SignalStateMachine(signals_dir)
    machine.write_pending(card)
    machine.claim(order_id, worker_id="cn_futures_sim")
    machine.mark_running(order_id, worker_id="cn_futures_sim")
    result = machine.fill(
        order_id,
        {
            "filled_qty": 1,
            "filled_quantity": 1,
            "filled_price": 3500.2,
            "fee": 3.1,
            "fill_time": "2026-07-13T09:35:01+08:00",
        },
        partial=True,
    )
    return card, dict(result["signal_card"])


def test_local_ioc_partial_is_explicit_terminal_event_and_reconciles(
    tmp_path: Path,
) -> None:
    from CNFutures.order_events import (
        load_order_event_projection,
        record_local_sim_order_lifecycle,
        startup_reconcile_order_projection,
    )

    signals_dir = tmp_path / "signals"
    card, final_card = _write_partial_directory_projection(signals_dir)
    receipt = {
        "status": "partial",
        "filled_qty": 1,
        "avg_price": 3500.2,
        "fee": 3.1,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
    }

    recorded = record_local_sim_order_lifecycle(
        signals_dir,
        card=card,
        receipt=receipt,
        final_card=final_card,
    )
    projection = load_order_event_projection(signals_dir)
    order = projection["orders"]["SIM-CNF-ORDER-1"]
    reconcile = startup_reconcile_order_projection(signals_dir)
    journal_rows = [
        json.loads(line)
        for line in (signals_dir / "order_events" / "cn_futures_order_events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]

    assert recorded["appended_event_count"] == 4
    assert [row["order_sequence"] for row in journal_rows] == [1, 2, 3, 4]
    assert order["status"] == "partial"
    assert order["terminal"] is True
    assert order["lifecycle_state"] == "TERMINAL"
    assert order["execution_model"] == "local_ioc_sim.v1"
    assert order["promotion_evidence_eligible"] is False
    assert projection["projection_sha256"]
    assert reconcile["ready"] is True
    assert reconcile["state"] == "ACTIVE"
    assert reconcile["mismatch_orders"] == []


def test_event_model_keeps_nonterminal_reducing_partial_for_future_adapter_design(
    tmp_path: Path,
) -> None:
    from CNFutures.order_events import append_order_events, load_order_event_projection

    signals_dir = tmp_path / "signals"
    common = {
        "order_id": "FUTURE-ASYNC-REDUCE-1",
        "order_intent": "reduce_only",
        "symbol": "RB2610.SHF",
        "side": "sell",
        "execution_model": "future_async_adapter_design",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
    }
    append_order_events(
        signals_dir,
        [
            {
                **common,
                "event_type": "submitted",
                "status": "pending",
                "terminal": False,
                "event_time": "2026-07-13T09:35:00+08:00",
            },
            {
                **common,
                "event_type": "claimed",
                "status": "claimed",
                "terminal": False,
                "event_time": "2026-07-13T09:35:01+08:00",
            },
            {
                **common,
                "event_type": "running",
                "status": "running",
                "terminal": False,
                "event_time": "2026-07-13T09:35:02+08:00",
            },
            {
                **common,
                "event_type": "partial_fill",
                "status": "partial",
                "terminal": False,
                "filled_quantity_delta": 1,
                "fill_price": 3500.0,
                "fee": 3.0,
                "event_time": "2026-07-13T09:35:03+08:00",
            },
        ],
    )
    append_order_events(
        signals_dir,
        [
            {
                **common,
                "event_type": "partial_fill",
                "status": "partial",
                "terminal": False,
                "filled_quantity_delta": 1,
                "fill_price": 3501.0,
                "fee": 3.0,
                "event_time": "2026-07-13T09:36:03+08:00",
            }
        ],
    )

    order = load_order_event_projection(signals_dir)["orders"]["FUTURE-ASYNC-REDUCE-1"]
    event_rows = [
        json.loads(line)
        for line in (signals_dir / "order_events" / "cn_futures_order_events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert order["status"] == "partial"
    assert order["terminal"] is False
    assert order["lifecycle_state"] == "REDUCING"
    assert order["filled_quantity"] == 2
    assert event_rows[-1]["order_sequence"] == 5
    assert order["promotion_evidence_eligible"] is False


def test_startup_reconcile_halts_on_directory_projection_drift(
    tmp_path: Path,
) -> None:
    from CNFutures.order_events import (
        record_local_sim_order_lifecycle,
        startup_reconcile_order_projection,
    )

    signals_dir = tmp_path / "signals"
    card, final_card = _write_partial_directory_projection(signals_dir)
    record_local_sim_order_lifecycle(
        signals_dir,
        card=card,
        receipt={
            "status": "partial",
            "filled_qty": 1,
            "avg_price": 3500.2,
            "fee": 3.1,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_trading_enabled": False,
        },
        final_card=final_card,
    )
    partial_path = signals_dir / "partial" / "SIM-CNF-ORDER-1.json"
    tampered = json.loads(partial_path.read_text(encoding="utf-8"))
    tampered["filled_qty"] = 2
    tampered["filled_quantity"] = 2
    partial_path.write_text(json.dumps(tampered), encoding="utf-8")

    reconcile = startup_reconcile_order_projection(signals_dir)
    assert reconcile["ready"] is False
    assert reconcile["state"] == "HALTED"
    assert reconcile["reason"] == "order_directory_projection_mismatch"
    assert reconcile["mismatch_orders"] == ["SIM-CNF-ORDER-1"]


def test_startup_reconcile_halts_on_event_chain_tamper(tmp_path: Path) -> None:
    from CNFutures.order_events import (
        order_event_journal_path,
        record_local_sim_order_lifecycle,
        startup_reconcile_order_projection,
    )

    signals_dir = tmp_path / "signals"
    card, final_card = _write_partial_directory_projection(signals_dir)
    record_local_sim_order_lifecycle(
        signals_dir,
        card=card,
        receipt={
            "status": "partial",
            "filled_qty": 1,
            "avg_price": 3500.2,
            "fee": 3.1,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_trading_enabled": False,
        },
        final_card=final_card,
    )
    journal = order_event_journal_path(signals_dir)
    rows = journal.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(rows[-1])
    tampered["filled_quantity_delta"] = 2
    rows[-1] = json.dumps(tampered)
    journal.write_text("\n".join(rows) + "\n", encoding="utf-8")

    reconcile = startup_reconcile_order_projection(signals_dir)
    assert reconcile["ready"] is False
    assert reconcile["state"] == "HALTED"
    assert reconcile["reason"] == "order_event_journal_invalid"
    assert reconcile["error"] == "order_event_checksum_mismatch"


def test_startup_reconcile_rebuilds_missing_derived_projection_from_valid_events(
    tmp_path: Path,
) -> None:
    from CNFutures.order_events import (
        order_event_projection_path,
        record_local_sim_order_lifecycle,
        startup_reconcile_order_projection,
    )

    signals_dir = tmp_path / "signals"
    card, final_card = _write_partial_directory_projection(signals_dir)
    record_local_sim_order_lifecycle(
        signals_dir,
        card=card,
        receipt={
            "status": "partial",
            "filled_qty": 1,
            "avg_price": 3500.2,
            "fee": 3.1,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_trading_enabled": False,
        },
        final_card=final_card,
    )
    projection_path = order_event_projection_path(signals_dir)
    projection_path.unlink()

    reconcile = startup_reconcile_order_projection(signals_dir)

    assert reconcile["ready"] is True
    assert reconcile["state"] == "ACTIVE"
    assert reconcile["projection_rebuilt"] is True
    assert projection_path.exists()


def test_cn_sim_signal_writer_persists_event_authority_and_projection(
    tmp_path: Path,
) -> None:
    from CNFutures.order_events import startup_reconcile_order_projection
    from CNFutures.sim_runner import _write_filled_signal

    signals_dir = tmp_path / "signals"
    card = _sim_card("SIM-CNF-WRITER-1")
    card["market"] = "cn_futures"
    receipt = {
        "status": "partial",
        "filled_qty": 1,
        "avg_price": 3500.2,
        "fee": 3.1,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
        "raw_response": {"fill_evidence_type": "bar_volume_participation"},
    }

    result = _write_filled_signal(signals_dir, card, receipt)
    reconcile = startup_reconcile_order_projection(signals_dir)

    assert result["status"] == "partial"
    assert result["order_event_result"]["appended_event_count"] == 4
    assert reconcile["ready"] is True
    assert reconcile["state"] == "ACTIVE"


@pytest.mark.parametrize(
    ("container", "field", "value"),
    [
        ("card", "capital_layer", "live"),
        ("receipt", "account_type", "real"),
        ("receipt", "real_trading_enabled", True),
        ("final_card", "real_trading_enabled", True),
    ],
)
def test_local_lifecycle_refuses_any_live_or_real_marker(
    tmp_path: Path,
    container: str,
    field: str,
    value: object,
) -> None:
    from CNFutures.order_events import (
        OrderEventError,
        record_local_sim_order_lifecycle,
    )

    signals_dir = tmp_path / "signals"
    card, final_card = _write_partial_directory_projection(signals_dir)
    receipt: dict[str, object] = {
        "status": "partial",
        "filled_qty": 1,
        "avg_price": 3500.2,
        "fee": 3.1,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
    }
    targets = {"card": card, "receipt": receipt, "final_card": final_card}
    targets[container][field] = value

    with pytest.raises(OrderEventError, match="local_sim_lifecycle_sim_only_required"):
        record_local_sim_order_lifecycle(
            signals_dir,
            card=card,
            receipt=receipt,
            final_card=final_card,
        )
