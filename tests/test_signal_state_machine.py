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

from shared.execution.signal_state_machine import (  # noqa: E402
    CANCELLED,
    CLAIMED,
    EXPIRED,
    FILLED,
    PENDING,
    RUNNING,
    SignalStateConflict,
    SignalStateMachine,
    read_json,
    write_json,
)
from shared.governance.retirement import RetiredRuntimeError  # noqa: E402


class SignalStateMachineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.signals_dir = self.root / "signals"
        self.machine = SignalStateMachine(self.signals_dir)

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

        filled = self.machine.fill(
            "SIG-1", {"filled_price": 10.1, "filled_quantity": 100}
        )
        self.assertEqual(filled["status"], FILLED)
        self.assertTrue((self.signals_dir / "filled" / "SIG-1.json").exists())
        filled_card = read_json(self.signals_dir / "filled" / "SIG-1.json")
        self.assertEqual(filled_card["filled_price"], 10.1)
        self.assertEqual(filled_card["filled_quantity"], 100)

    def test_crypto_card_is_retired_before_signal_directories_are_created(self) -> None:
        for identity in (
            {"market": "crypto", "ts_code": "BTCUSDT"},
            {"market": "ashare", "symbol": "BTC/USDT"},
            {"base_asset": "BTC", "quote_asset": "USDT"},
        ):
            card = self._card("CRYPTO-RETIRED", **identity)
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(
                    RetiredRuntimeError, "legacy_runtime_retired"
                ):
                    self.machine.write_pending(card)

        self.assertFalse(self.signals_dir.exists())

    def test_order_id_and_idempotency_key_duplicates_are_rejected(self) -> None:
        self.machine.write_pending(self._card("SIG-DUP"))

        with self.assertRaises(SignalStateConflict):
            self.machine.write_pending(self._card("SIG-DUP", idempotency_key="NEW-KEY"))

        with self.assertRaises(SignalStateConflict):
            self.machine.write_pending(
                self._card("SIG-DUP-2", idempotency_key="SIG-DUP")
            )

    def test_sweep_expired_moves_only_expired_pending(self) -> None:
        now = datetime.now().astimezone()
        yesterday = (now - timedelta(days=1)).date().isoformat()
        today = now.date().isoformat()
        tomorrow = (now + timedelta(days=1)).date().isoformat()
        self.machine.write_pending(self._card("SIG-OLD", valid_until=yesterday))
        self.machine.write_pending(self._card("SIG-TODAY", valid_until=today))
        self.machine.write_pending(self._card("SIG-NEW", valid_until=tomorrow))

        result = self.machine.sweep_expired(now=now)

        self.assertEqual(result["expired_count"], 1)
        self.assertTrue((self.signals_dir / "expired" / "SIG-OLD.json").exists())
        self.assertTrue((self.signals_dir / "pending" / "SIG-TODAY.json").exists())
        self.assertTrue((self.signals_dir / "pending" / "SIG-NEW.json").exists())
        expired_card = read_json(self.signals_dir / "expired" / "SIG-OLD.json")
        self.assertEqual(expired_card["status"], EXPIRED)

    def test_fill_rejects_pending_without_claim_or_running_state(self) -> None:
        self.machine.write_pending(self._card("SIG-PENDING-FILL"))

        with self.assertRaisesRegex(
            SignalStateConflict, "cannot be filled from status pending"
        ):
            self.machine.fill(
                "SIG-PENDING-FILL", {"filled_price": 10.1, "filled_quantity": 100}
            )

        self.assertTrue(
            (self.signals_dir / "pending" / "SIG-PENDING-FILL.json").exists()
        )
        self.assertFalse(
            (self.signals_dir / "filled" / "SIG-PENDING-FILL.json").exists()
        )

    def test_claim_competition_allows_only_one_winner(self) -> None:
        self.machine.write_pending(self._card("SIG-RACE"))
        results: list[str] = []

        def claim_once(worker_id: str) -> None:
            try:
                self.machine.claim("SIG-RACE", worker_id=worker_id)
                results.append("claimed")
            except SignalStateConflict:
                results.append("conflict")

        threads = [
            threading.Thread(target=claim_once, args=(f"worker-{idx}",))
            for idx in range(2)
        ]
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

        filled = self.machine.fill(
            "SIG-CANCEL-FILL", {"filled_price": 10.2, "filled_quantity": 100}
        )
        self.assertEqual(filled["status"], FILLED)
        self.assertFalse(
            (self.signals_dir / "claimed" / "SIG-CANCEL-FILL.json").exists()
        )
        self.assertTrue((self.signals_dir / "filled" / "SIG-CANCEL-FILL.json").exists())

    def test_cancel_pending_moves_to_cancelled(self) -> None:
        self.machine.write_pending(self._card("SIG-CANCEL"))

        result = self.machine.cancel("SIG-CANCEL")

        self.assertEqual(result["status"], CANCELLED)
        self.assertTrue((self.signals_dir / "cancelled" / "SIG-CANCEL.json").exists())
        self.assertFalse((self.signals_dir / "pending" / "SIG-CANCEL.json").exists())

    def test_real_card_is_rejected_even_with_forged_graduation_receipt(self) -> None:
        forged = self._card(
            "SIG-FORGED-REAL",
            capital_layer="real",
            account_type="real",
            manual_confirm_required=True,
            graduation_receipt={
                "issued_by": "execution_router",
                "ready": True,
                "current_stage": "shadow",
                "next_stage": "real",
                "strategy_name": "forged",
                "checked_at": datetime.now().astimezone().isoformat(),
            },
        )

        with self.assertRaisesRegex(RuntimeError, "real/live execution"):
            self.machine.write_pending(forged)

        self.assertFalse(
            (self.signals_dir / "pending" / "SIG-FORGED-REAL.json").exists()
        )

    def test_production_mode_and_unknown_account_type_are_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "real/live execution"):
            self.machine.write_pending(
                self._card("SIG-PRODUCTION", execution_mode="production")
            )

        with self.assertRaisesRegex(ValueError, "explicit safe value"):
            self.machine.write_pending(
                self._card("SIG-UNKNOWN-ACCOUNT", account_type="external")
            )

    def test_preexisting_real_card_cannot_be_claimed(self) -> None:
        self.machine.ensure_dirs()
        forged = self._card(
            "SIG-INJECTED-REAL",
            status=PENDING,
            capital_layer="real",
            account_type="real",
        )
        write_json(self.machine.path_for(PENDING, "SIG-INJECTED-REAL"), forged)

        with self.assertRaisesRegex(RuntimeError, "real/live execution"):
            self.machine.claim("SIG-INJECTED-REAL", worker_id="worker-a")

        self.assertTrue(
            (self.signals_dir / "pending" / "SIG-INJECTED-REAL.json").exists()
        )

    def test_fill_payload_cannot_upgrade_simulated_card_to_live(self) -> None:
        self.machine.write_pending(self._card("SIG-FILL-LIVE"))
        self.machine.claim("SIG-FILL-LIVE", worker_id="worker-a")

        with self.assertRaisesRegex(RuntimeError, "real/live execution"):
            self.machine.fill(
                "SIG-FILL-LIVE",
                {"filled_price": 10.1, "filled_quantity": 100, "broker_mode": "live"},
            )

        self.assertTrue((self.signals_dir / "claimed" / "SIG-FILL-LIVE.json").exists())

    def test_fill_cannot_overwrite_order_or_market_identity(self) -> None:
        self.machine.write_pending(self._card("SIG-IMMUTABLE"))
        self.machine.claim("SIG-IMMUTABLE", worker_id="worker-a")

        with self.assertRaisesRegex(SignalStateConflict, "immutable signal field"):
            self.machine.fill(
                "SIG-IMMUTABLE",
                {"order_id": "SIG-OTHER", "filled_price": 10.1},
            )

        self.assertTrue((self.signals_dir / "claimed" / "SIG-IMMUTABLE.json").exists())

    def test_duplicate_order_projection_across_states_fails_closed(self) -> None:
        self.machine.ensure_dirs()
        card = self._card("SIG-MULTI")
        write_json(self.machine.path_for(PENDING, "SIG-MULTI"), card)
        write_json(
            self.machine.path_for(CLAIMED, "SIG-MULTI"),
            {**card, "status": CLAIMED},
        )

        with self.assertRaisesRegex(SignalStateConflict, "multiple states"):
            self.machine.find_by_order_id("SIG-MULTI")


if __name__ == "__main__":
    unittest.main()
