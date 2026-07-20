#!/usr/bin/env python3
# ruff: noqa: E402
"""Hard-boundary tests for real-money execution paths."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.execution import execution_router


class RealMoneyBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._patch_module_attr(
            execution_router, "ROUTER_LOG", self.root / "router_decisions.jsonl"
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _patch_module_attr(self, module: object, name: str, value: Path) -> None:
        patcher = patch.object(module, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_real_route_rejects_ungraduated_strategy(self) -> None:
        result = execution_router.route(
            {
                "order_id": "REAL-NOT-GRADUATED",
                "market": "ashare",
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

    def test_thresholds_never_queue_or_graduate_to_real(self) -> None:
        result = execution_router.route(
            {
                "order_id": "REAL-GRADUATED",
                "market": "ashare",
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

if __name__ == "__main__":
    unittest.main()
