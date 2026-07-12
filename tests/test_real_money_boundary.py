#!/usr/bin/env python3
# ruff: noqa: E402
"""Hard-boundary tests for real-money execution paths."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.execution import execution_router, hermes_bridge


class RealMoneyBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.signals_dir = self.root / "signals"
        self._patch_module_attr(hermes_bridge, "SIGNALS_DIR", self.signals_dir)
        self._patch_module_attr(
            hermes_bridge, "PENDING_DIR", self.signals_dir / "pending"
        )
        self._patch_module_attr(
            hermes_bridge, "FILLED_DIR", self.signals_dir / "filled"
        )
        self._patch_module_attr(
            hermes_bridge, "CANCELLED_DIR", self.signals_dir / "cancelled"
        )
        self._patch_module_attr(
            hermes_bridge, "POSITIONS_DIR", self.signals_dir / "positions"
        )
        self._patch_module_attr(
            hermes_bridge, "POSITIONS_FILE", self.signals_dir / "positions.json"
        )
        self._patch_module_attr(
            execution_router, "ROUTER_LOG", self.root / "router_decisions.jsonl"
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _patch_module_attr(self, module: object, name: str, value: Path) -> None:
        patcher = patch.object(module, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _valid_card(self, **overrides: object) -> dict[str, object]:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        card: dict[str, object] = {
            "order_id": "REAL-BOUNDARY-1",
            "ts_code": "600000.SH",
            "direction": "buy",
            "quantity": 100,
            "price": 10.0,
            "stop_loss": 9.2,
            "strategy_name": "graduated_strategy",
            "timestamp": now,
            "capital_layer": "real",
            "account_type": "real",
            "manual_confirm_required": True,
            "direct_execution": False,
            "trigger": {
                "condition_id": "COND-1",
                "triggered_at": now,
                "trigger_price": 10.0,
            },
            "evidence_refs": ["report://test"],
            "valid_until": now[:10],
            "risk_check": {
                "passed": True,
                "checks": ["unit_test"],
            },
            "source_condition_id": "COND-1",
            "idempotency_key": "REAL-BOUNDARY-1",
            "t_plus_1": {
                "sellable_from": now[:10],
                "sellable_date": now[:10],
            },
            "graduation_receipt": {
                "issued_by": "execution_router",
                "checked_at": now,
                "strategy_name": "graduated_strategy",
                "current_stage": "shadow",
                "next_stage": "real",
                "ready": True,
                "thresholds": {},
                "met": {},
                "message": "unit test receipt",
            },
        }
        card.update(overrides)
        return card

    def test_real_route_rejects_ungraduated_strategy(self) -> None:
        result = execution_router.route(
            {
                "order_id": "REAL-NOT-GRADUATED",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "strategy_name": "early_strategy",
                "stats": {
                    "total_trades": 3,
                    "positive_days_pct": 0.5,
                    "max_drawdown_pct": 5.0,
                },
            },
            "real",
        )

        self.assertFalse(result["executed"])
        self.assertEqual(result["message"], "automatic real transition disabled")
        self.assertEqual(result["result"]["status"], "manual_authorization_required")
        self.assertEqual(
            result["result"]["reason"],
            "automatic_shadow_to_real_transition_disabled",
        )
        self.assertFalse(hermes_bridge.PENDING_DIR.exists())

    def test_real_signal_card_without_manual_confirm_is_rejected(self) -> None:
        card = self._valid_card()
        del card["manual_confirm_required"]

        result = hermes_bridge.send_order(card)

        self.assertEqual(result["status"], "rejected")
        self.assertIn("manual_confirm_required", result["message"])
        self.assertFalse((hermes_bridge.PENDING_DIR / "REAL-BOUNDARY-1.json").exists())

    def test_direct_real_send_order_is_rejected_even_with_receipt(self) -> None:
        result = hermes_bridge.send_order(self._valid_card())

        self.assertEqual(result["status"], "rejected")
        self.assertIn("execution_router", result["message"])
        self.assertFalse((hermes_bridge.PENDING_DIR / "REAL-BOUNDARY-1.json").exists())

    def test_thresholds_never_queue_or_graduate_to_real(self) -> None:
        result = execution_router.route(
            {
                "order_id": "REAL-GRADUATED",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "stop_loss": 9.2,
                "strategy_name": "graduated_strategy",
                "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                "evidence_refs": ["report://test"],
                "risk_passed": True,
                "risk_checks": ["unit_test"],
                "stats": {
                    "total_trades": 120,
                    "positive_days_pct": 0.70,
                    "max_drawdown_pct": 4.0,
                },
            },
            "real",
        )

        self.assertFalse(result["executed"])
        self.assertEqual(result["result"]["status"], "manual_authorization_required")
        self.assertFalse((hermes_bridge.PENDING_DIR / "REAL-GRADUATED.json").exists())
        graduation = execution_router.check_graduation(
            "graduated_strategy",
            "shadow",
            {
                "total_trades": 10_000,
                "positive_days_pct": 1.0,
                "max_drawdown_pct": 0.0,
            },
        )
        self.assertFalse(graduation["ready"])
        self.assertEqual(graduation["next_stage"], "shadow")
        self.assertEqual(
            graduation["reason"],
            "automatic_shadow_to_real_transition_disabled",
        )

    def test_cancel_real_order_without_manual_confirm_is_rejected(self) -> None:
        hermes_bridge.ensure_signal_dirs()
        pending_path = hermes_bridge.PENDING_DIR / "REAL-BOUNDARY-1.json"
        with open(pending_path, "w", encoding="utf-8") as fh:
            json.dump(self._valid_card(), fh)

        result = hermes_bridge.cancel_order("REAL-BOUNDARY-1")

        self.assertEqual(result["status"], "rejected")
        self.assertIn("Manual confirmation required", result["message"])
        self.assertTrue(pending_path.exists())
        self.assertFalse(
            (hermes_bridge.CANCELLED_DIR / "REAL-BOUNDARY-1.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
