from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class PMWorkflowConfigTest(unittest.TestCase):
    def test_pm_workflow_loads_checked_in_config_as_usdc(self) -> None:
        from PM.workflow import PMWorkflow

        workflow = PMWorkflow()

        self.assertEqual(workflow.config.market, "pm")
        self.assertEqual(workflow.config.capital.currency, "USDC")
        self.assertEqual(workflow.config.capital.default_layer, "shadow")
        self.assertEqual(workflow.config.data.daily_table, "market_bars_daily")
        self.assertFalse(workflow.config.safety.real_money_enabled)
        self.assertFalse(workflow.config.safety.live_broker_enabled)
        self.assertFalse(workflow.config.safety.direct_execution_enabled)

    def test_pm_shadow_runner_writes_signal_state_machine_shadow_filled_for_local_fill(self) -> None:
        from PM.common import PMConfig
        from PM.shadow_runner import PMShadowRunner
        from shared.execution.signal_state_machine import read_json

        with tempfile.TemporaryDirectory() as tmp:
            runner = PMShadowRunner(config=PMConfig().to_market_tool_config())
            runner.signals_root = Path(tmp) / "signals"
            result = runner.write_shadow_record(
                {
                    "cycle_id": "pm-shadow-test",
                    "date": "2026-07-02",
                    "market": "pm",
                    "positions": [
                        {
                            "order_id": "PM-SHADOW-ONE",
                            "market_id": "fed-cut-2026",
                            "side": "buy",
                            "quantity": 1,
                            "fill_price": 0.52,
                            "status": "filled",
                        }
                    ],
                }
            )

            pending_files = list((Path(tmp) / "signals" / "shadow" / "pending").glob("*.json"))
            filled_files = list((Path(tmp) / "signals" / "shadow" / "filled").glob("*.json"))
            card = read_json(filled_files[0]) if filled_files else {}

        self.assertEqual(result["status"], "filled")
        self.assertEqual(result["queue_scope"], "shadow")
        self.assertEqual(len(pending_files), 0)
        self.assertEqual(len(filled_files), 1)
        self.assertEqual(card["market"], "pm")
        self.assertEqual(card["capital_layer"], "shadow")
        self.assertEqual(card["account_type"], "shadow")
        self.assertEqual(card["direct_execution"], False)
        self.assertEqual(card["real_execution"], False)
        json.dumps(card)

    def test_pm_simulator_rejects_real_capital_layer_and_returns_simulated(self) -> None:
        from PM.common import PMConfig
        from PM.simulator import PMSimulator

        simulator = PMSimulator(config=PMConfig().to_market_tool_config())

        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            simulator.simulate(
                {
                    "market_id": "fed-cut-2026",
                    "side": "buy",
                    "outcome": "yes",
                    "quantity": 1,
                    "price": 0.5,
                    "capital_layer": "real",
                },
                {"account_id": "pm_sim"},
            )

        result = simulator.simulate(
            {
                "market_id": "fed-cut-2026",
                "side": "buy",
                "outcome": "yes",
                "quantity": 1,
                "price": 0.5,
            },
            {"account_id": "pm_sim", "capital_layer": "real"},
        )

        self.assertEqual(result["capital_layer"], "simulated")
        self.assertEqual(result["account_type"], "simulated")


if __name__ == "__main__":
    unittest.main()
