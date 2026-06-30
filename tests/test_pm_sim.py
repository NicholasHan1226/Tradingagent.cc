from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PM.adapter import PMAdapter
from PM.sim_executor import pm_sim_execute
from shared.execution.sim_broker import SimResult, execute_sim_order


class PMSimExecutorTest(unittest.TestCase):
    def test_adapter_exposes_pm_sim_account(self) -> None:
        adapter = PMAdapter(reader=object())

        self.assertEqual(adapter.get_sim_account(), "pm_sim")
        self.assertEqual(adapter.get_shadow_account(), "pm_shadow")

    def test_pm_sim_execute_uses_mock_clob_matcher(self) -> None:
        calls: list[dict[str, object]] = []

        def mock_clob(
            order: dict[str, object],
            account: dict[str, object],
            config: dict[str, object],
        ) -> dict[str, object]:
            calls.append(
                {
                    "order_id": order["order_id"],
                    "account_id": account["account_id"],
                    "mode": config["mode"],
                }
            )
            return {
                "matched": True,
                "avg_price": 0.61,
                "opponent_order_id": "opp-001",
            }

        result = pm_sim_execute(
            order={
                "order_id": "PM-SIM-1",
                "market_id": "fed-cut-2026",
                "side": "buy",
                "outcome": "YES",
                "price": 0.60,
            },
            account={"account_id": "pm_sim"},
            config={"mode": "research_only", "clob_matcher": mock_clob},
        )

        self.assertEqual(
            calls,
            [{"order_id": "PM-SIM-1", "account_id": "pm_sim", "mode": "research_only"}],
        )
        self.assertIsInstance(result, SimResult)
        self.assertEqual(result.status, "filled")
        self.assertEqual(result.filled_qty, 1)
        self.assertEqual(result.avg_price, 0.61)
        self.assertEqual(result.fee, 0.0)
        self.assertEqual(result.capital_layer, "simulated")
        self.assertEqual(result.account_type, "simulated")
        self.assertEqual(result.market, "pm")
        self.assertIn("research-only", result.message)
        self.assertEqual(result.raw_response["venue"], "pm_clob_sandbox")
        self.assertEqual(result.raw_response["mode"], "research_only")
        self.assertEqual(result.raw_response["opponent_order_id"], "opp-001")

    def test_execute_sim_order_dispatches_registered_pm_executor(self) -> None:
        result = execute_sim_order(
            order={
                "order_id": "PM-SIM-2",
                "market_id": "btc-100k",
                "side": "sell",
                "outcome": "NO",
                "price": 0.42,
                "capital_layer": "real",
            },
            market="PM",
            account={"account_id": "pm_sim", "account_type": "real"},
            config={"sandbox_spread": 0.02, "capital_layer": "real"},
        )

        self.assertEqual(result.status, "filled")
        self.assertEqual(result.filled_qty, 1)
        self.assertAlmostEqual(result.avg_price, 0.41, places=6)
        self.assertEqual(result.fee, 0.0)
        self.assertEqual(result.capital_layer, "simulated")
        self.assertEqual(result.account_type, "simulated")
        self.assertEqual(result.market, "pm")
        self.assertEqual(result.raw_response["mode"], "research_only")


if __name__ == "__main__":
    unittest.main()
