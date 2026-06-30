from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from US.adapter import USAdapter
from US import sim_executor
from shared.execution.sim_broker import SimResult
from shared.execution.sim_executor_registry import get_sim_executor


class USSimExecutorTest(unittest.TestCase):
    def test_adapter_exposes_us_sim_account_without_breaking_shadow_account(self) -> None:
        adapter = USAdapter(reader=object())

        self.assertEqual(adapter.get_shadow_account(), "us_shadow")
        self.assertEqual(adapter.get_sim_account(), "us_sim")

    def test_us_sim_execute_returns_valid_sim_result_from_mock_alpaca(self) -> None:
        mocked_api_order = {
            "id": "alpaca-paper-001",
            "status": "filled",
            "qty": 5,
            "filled_qty": 5,
            "filled_avg_price": 189.55,
            "symbol": "AAPL",
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
        }

        with patch.object(
            sim_executor,
            "_mock_alpaca_submit_order",
            return_value=mocked_api_order,
        ) as mock_submit:
            result = sim_executor.us_sim_execute(
                order={
                    "order_id": "SIM-US-1",
                    "ts_code": "AAPL",
                    "quantity": 5,
                    "side": "buy",
                    "order_type": "market",
                },
                account={"account_id": "us_sim"},
                config={"settlement": "T+2", "time_in_force": "day"},
            )

        self.assertIsInstance(result, SimResult)
        self.assertEqual(result.status, "filled")
        self.assertEqual(result.filled_qty, 5)
        self.assertEqual(result.avg_price, 189.55)
        self.assertEqual(result.fee, 0.001)
        self.assertEqual(result.capital_layer, "simulated")
        self.assertEqual(result.account_type, "simulated")
        self.assertEqual(result.order_id, "alpaca-paper-001")
        self.assertEqual(result.market, "us")
        self.assertEqual(result.raw_response["broker"], "alpaca_paper_mock")
        self.assertEqual(result.raw_response["settlement"], "T+2")
        self.assertEqual(result.raw_response["api_order"]["symbol"], "AAPL")
        self.assertIn("settlement declared as T+2", result.message)
        mock_submit.assert_called_once()

    def test_us_executor_is_registered_for_market(self) -> None:
        executor = get_sim_executor("US")

        self.assertIs(executor, sim_executor.us_sim_execute)


if __name__ == "__main__":
    unittest.main()
