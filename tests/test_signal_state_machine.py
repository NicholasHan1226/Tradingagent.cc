#!/usr/bin/env python3
"""Tests for the file-backed signal state machine."""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.execution import hermes_bridge
from shared.execution.signal_state_machine import (
    CANCELLED,
    CLAIMED,
    EXPIRED,
    FILLED,
    PENDING,
    RUNNING,
    SignalStateConflict,
    SignalStateMachine,
    read_json,
)


class SignalStateMachineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.signals_dir = self.root / "signals"
        self.machine = SignalStateMachine(self.signals_dir)

        hermes_bridge.SIGNALS_DIR = self.signals_dir
        hermes_bridge.PENDING_DIR = self.signals_dir / "pending"
        hermes_bridge.CLAIMED_DIR = self.signals_dir / "claimed"
        hermes_bridge.RUNNING_DIR = self.signals_dir / "running"
        hermes_bridge.FILLED_DIR = self.signals_dir / "filled"
        hermes_bridge.EXPIRED_DIR = self.signals_dir / "expired"
        hermes_bridge.CANCELLED_DIR = self.signals_dir / "cancelled"
        hermes_bridge.FAILED_DIR = self.signals_dir / "failed"
        hermes_bridge.PARTIAL_DIR = self.signals_dir / "partial"
        hermes_bridge.POSITIONS_DIR = self.signals_dir / "positions"
        hermes_bridge.POSITIONS_FILE = self.signals_dir / "positions.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _card(self, order_id: str = "SIG-1", **overrides: object) -> dict[str, object]:
        now = datetime.now().astimezone()
        today = now.date().isoformat()
        card: dict[str, object] = {
            "order_id": order_id,
            "ts_code": "600000.SH",
            "direction": "buy",
            "quantity": 100,
            "price": 10.0,
            "stop_loss": 9.0,
            "strategy_name": "unit_test_strategy",
            "timestamp": now.isoformat(timespec="seconds"),
            "status": PENDING,
            "capital_layer": "shadow",
            "account_type": "none",
            "manual_confirm_required": False,
            "direct_execution": False,
            "trigger": {
                "condition_id": f"COND-{order_id}",
                "triggered_at": now.isoformat(timespec="seconds"),
                "trigger_price": 10.0,
            },
            "evidence_refs": ["unit-test"],
            "valid_until": today,
            "risk_check": {
                "passed": True,
                "checks": ["unit_test"],
            },
            "source_condition_id": f"COND-{order_id}",
            "idempotency_key": order_id,
            "t_plus_1": {
                "sellable_from": today,
                "sellable_date": today,
            },
        }
        card.update(overrides)
        return card

    def test_status_flow_pending_claimed_running_filled(self) -> None:
        queued = self.machine.write_pending(self._card())
        self.assertEqual(queued["status"], PENDING)
        self.assertTrue((self.signals_dir / "pending" / "SIG-1.json").exists())

        claimed = self.machine.claim("SIG-1", worker_id="worker-a")
        self.assertEqual(claimed["status"], CLAIMED)
        self.assertFalse((self.signals_dir / "pending" / "SIG-1.json").exists())
        self.assertTrue((self.signals_dir / "claimed" / "SIG-1.json").exists())
        self.assertEqual(claimed["signal_card"]["source_condition_id"], "COND-SIG-1")

        running = self.machine.mark_running("SIG-1", worker_id="worker-a")
        self.assertEqual(running["status"], RUNNING)
        self.assertTrue((self.signals_dir / "running" / "SIG-1.json").exists())

        filled = self.machine.fill("SIG-1", {"filled_price": 10.1, "filled_quantity": 100})
        self.assertEqual(filled["status"], FILLED)
        self.assertTrue((self.signals_dir / "filled" / "SIG-1.json").exists())
        filled_card = read_json(self.signals_dir / "filled" / "SIG-1.json")
        self.assertEqual(filled_card["filled_price"], 10.1)
        self.assertEqual(filled_card["filled_quantity"], 100)

    def test_order_id_and_idempotency_key_duplicates_are_rejected(self) -> None:
        self.machine.write_pending(self._card("SIG-DUP"))

        with self.assertRaises(SignalStateConflict):
            self.machine.write_pending(self._card("SIG-DUP", idempotency_key="NEW-KEY"))

        with self.assertRaises(SignalStateConflict):
            self.machine.write_pending(self._card("SIG-DUP-2", idempotency_key="SIG-DUP"))

    def test_sweep_expired_moves_only_expired_pending(self) -> None:
        now = datetime.now().astimezone()
        yesterday = (now - timedelta(days=1)).date().isoformat()
        tomorrow = (now + timedelta(days=1)).date().isoformat()
        self.machine.write_pending(self._card("SIG-OLD", valid_until=yesterday))
        self.machine.write_pending(self._card("SIG-NEW", valid_until=tomorrow))

        result = self.machine.sweep_expired(now=now)

        self.assertEqual(result["expired_count"], 1)
        self.assertTrue((self.signals_dir / "expired" / "SIG-OLD.json").exists())
        self.assertTrue((self.signals_dir / "pending" / "SIG-NEW.json").exists())
        expired_card = read_json(self.signals_dir / "expired" / "SIG-OLD.json")
        self.assertEqual(expired_card["status"], EXPIRED)

    def test_claim_competition_allows_only_one_winner(self) -> None:
        self.machine.write_pending(self._card("SIG-RACE"))
        results: list[str] = []

        def claim_once(worker_id: str) -> None:
            try:
                self.machine.claim("SIG-RACE", worker_id=worker_id)
                results.append("claimed")
            except SignalStateConflict:
                results.append("conflict")

        threads = [threading.Thread(target=claim_once, args=(f"worker-{idx}",)) for idx in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results.count("claimed"), 1)
        self.assertEqual(results.count("conflict"), 1)
        self.assertTrue((self.signals_dir / "claimed" / "SIG-RACE.json").exists())

    def test_cancel_requested_does_not_override_claimed_and_filled_wins(self) -> None:
        self.machine.write_pending(self._card("SIG-CANCEL-FILL"))
        self.machine.claim("SIG-CANCEL-FILL", worker_id="worker-a")

        cancelled = self.machine.cancel("SIG-CANCEL-FILL", reason="unit_test_cancel")
        self.assertEqual(cancelled["status"], "cancel_requested")
        claimed_card = read_json(self.signals_dir / "claimed" / "SIG-CANCEL-FILL.json")
        self.assertEqual(claimed_card["status"], CLAIMED)
        self.assertTrue(claimed_card["cancel_requested"])

        filled = self.machine.fill("SIG-CANCEL-FILL", {"filled_price": 10.2, "filled_quantity": 100})
        self.assertEqual(filled["status"], FILLED)
        self.assertFalse((self.signals_dir / "claimed" / "SIG-CANCEL-FILL.json").exists())
        self.assertTrue((self.signals_dir / "filled" / "SIG-CANCEL-FILL.json").exists())

    def test_cancel_pending_moves_to_cancelled(self) -> None:
        self.machine.write_pending(self._card("SIG-CANCEL"))

        result = self.machine.cancel("SIG-CANCEL")

        self.assertEqual(result["status"], CANCELLED)
        self.assertTrue((self.signals_dir / "cancelled" / "SIG-CANCEL.json").exists())
        self.assertFalse((self.signals_dir / "pending" / "SIG-CANCEL.json").exists())

    def test_hermes_bridge_uses_state_machine_wrappers(self) -> None:
        card = self._card("SIG-BRIDGE")
        send_result = hermes_bridge.send_order(card)
        self.assertEqual(send_result["status"], PENDING)

        duplicate = hermes_bridge.send_order(card)
        self.assertEqual(duplicate["status"], "duplicate")

        claim_result = hermes_bridge.claim_signal("SIG-BRIDGE", worker_id="bridge-worker")
        self.assertEqual(claim_result["status"], CLAIMED)

        cancel_result = hermes_bridge.cancel_order("SIG-BRIDGE")
        self.assertEqual(cancel_result["status"], "cancel_requested")

        fill_result = hermes_bridge.fill_signal("SIG-BRIDGE", {"filled_price": 10.3, "filled_quantity": 100})
        self.assertEqual(fill_result["status"], FILLED)


if __name__ == "__main__":
    unittest.main()
