#!/usr/bin/env python3
"""Tests for the Mac Mini signal-card consumer contract."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mini.mini_consumer import MiniConsumer
from shared.execution.signal_state_machine import FAILED, FILLED, PENDING, SignalStateMachine, read_json


class MiniConsumerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.signals_dir = self.root / "signals"
        self.machine = SignalStateMachine(self.signals_dir)
        self.consumer = MiniConsumer(
            signals_dir=self.signals_dir,
            executor_path=self.root / "a_share_simulated_trade_executor.py",
            worker_id="unit-test-mini",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _card(self, order_id: str = "MINI-1", **overrides: object) -> dict[str, object]:
        now = datetime.now().astimezone()
        today = now.date().isoformat()
        card: dict[str, object] = {
            "order_id": order_id,
            "ts_code": "600000.SH",
            "direction": "buy",
            "quantity": 100,
            "price": 10.0,
            "stop_loss": 9.0,
            "strategy_name": "mini_consumer_unit_test",
            "timestamp": now.isoformat(timespec="seconds"),
            "status": PENDING,
            "capital_layer": "simulated",
            "account_type": "simulated",
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
        if overrides.get("capital_layer") == "real":
            card["graduation_receipt"] = {
                "issued_by": "execution_router",
                "checked_at": now.isoformat(timespec="seconds"),
                "strategy_name": str(overrides.get("strategy_name") or "mini_consumer_unit_test"),
                "current_stage": "shadow",
                "next_stage": "real",
                "ready": True,
                "thresholds": {},
                "met": {},
                "message": "unit test receipt",
            }
        card.update(overrides)
        return card

    def test_sim_signal_claim_execute_and_write_filled(self) -> None:
        self.machine.write_pending(self._card("MINI-SIM"))
        claimed = self.consumer.claim_next_pending()

        stdout = json.dumps(
            {
                "status": "ok",
                "avg_price": 10.12,
                "filled_qty": 100,
                "slippage": 0.02,
                "fee": 1.1,
            }
        )
        completed = subprocess.CompletedProcess(args=["executor"], returncode=0, stdout=stdout, stderr="")
        with patch("mini.mini_consumer.subprocess.run", return_value=completed) as run_mock:
            result = self.consumer.dispatch(claimed or {})

        self.assertEqual(result["status"], FILLED)
        run_mock.assert_called_once()
        filled_path = self.signals_dir / "filled" / "MINI-SIM.json"
        self.assertTrue(filled_path.exists())
        filled = read_json(filled_path)
        self.assertEqual(filled["account_type"], "simulated")
        self.assertEqual(filled["capital_layer"], "simulated")
        self.assertEqual(filled["filled_qty"], 100)
        self.assertEqual(filled["filled_price"], 10.12)
        self.assertEqual(filled["idempotency_key"], "MINI-SIM")

    def test_real_signal_notifies_and_never_executes(self) -> None:
        self.machine.write_pending(
            self._card(
                "MINI-REAL",
                capital_layer="real",
                account_type="real",
                manual_confirm_required=True,
                direct_execution=False,
            )
        )
        claimed = self.consumer.claim_next_pending()

        with patch.object(self.consumer, "execute_simulated", side_effect=AssertionError("must not execute")):
            result = self.consumer.dispatch(claimed or {})

        self.assertEqual(result["status"], "manual_notified")
        self.assertEqual(result["email"]["status"], "queued_stub")
        self.assertEqual(result["positions"]["status"], "ok")
        position_files = list((self.signals_dir / "positions").glob("*.json"))
        self.assertEqual(len(position_files), 1)
        snapshot = read_json(position_files[0])
        self.assertEqual(snapshot["account_type"], "real")
        self.assertEqual(snapshot["source"], "tonghuashun_readonly")

    def test_real_direct_execution_true_is_rejected(self) -> None:
        self.machine.write_pending(
            self._card(
                "MINI-REAL-DIRECT",
                capital_layer="real",
                account_type="real",
                manual_confirm_required=True,
                direct_execution=True,
            )
        )
        claimed = self.consumer.claim_next_pending()
        result = self.consumer.dispatch(claimed or {})

        self.assertEqual(result["status"], "rejected")
        self.assertIn("direct_execution=true", result["message"])
        failed_path = self.signals_dir / FAILED / "MINI-REAL-DIRECT.json"
        self.assertTrue(failed_path.exists())
        self.assertEqual(read_json(failed_path)["status"], FAILED)

    def test_sim_account_type_mismatch_is_rejected(self) -> None:
        self.machine.write_pending(self._card("MINI-SIM-BAD", account_type="real"))
        claimed = self.consumer.claim_next_pending()

        with patch("mini.mini_consumer.subprocess.run") as run_mock:
            result = self.consumer.dispatch(claimed or {})

        self.assertEqual(result["status"], "rejected")
        self.assertIn("account_type=simulated", result["message"])
        run_mock.assert_not_called()
        failed_path = self.signals_dir / FAILED / "MINI-SIM-BAD.json"
        self.assertTrue(failed_path.exists())


if __name__ == "__main__":
    unittest.main()
