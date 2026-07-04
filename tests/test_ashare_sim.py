from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Ashare.adapter import AshareAdapter
from Ashare.sim_executor import ashare_sim_execute
from mini.mini_consumer import MiniConsumer
from shared.execution.signal_state_machine import PENDING, read_json
from shared.execution.sim_broker import SimResult
from shared.execution.sim_executor_registry import get_sim_executor


class AshareSimExecutorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp_path = Path(self.tmpdir.name)
        self.signals_dir = self.tmp_path / "signals"

    def test_adapter_exposes_ashare_sim_account_without_breaking_shadow_account(self) -> None:
        adapter = AshareAdapter(reader=object())

        self.assertEqual(adapter.get_shadow_account(), "ashare_shadow")
        self.assertEqual(adapter.get_sim_account(), "ashare_sim")

    def test_ashare_sim_execute_queues_pending_signal_card_when_hermes_enabled(self) -> None:
        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-1",
                "ts_code": "600000.SH",
                "quantity": 100,
                "price": 10.5,
                "side": "buy",
            },
            account={"account_id": "ashare_sim"},
            config={"signals_dir": self.signals_dir, "hermes_enabled": True},
        )

        self.assertIsInstance(result, SimResult)
        self.assertEqual(result.status, "pending")
        self.assertEqual(result.capital_layer, "simulated")
        self.assertEqual(result.account_type, "simulated")
        self.assertEqual(result.order_id, "SIM-ASHARE-1")
        self.assertEqual(result.market, "ashare")
        pending_path = self.signals_dir / "pending" / "SIM-ASHARE-1.json"
        self.assertTrue(pending_path.exists())
        card = read_json(pending_path)
        self.assertEqual(card["status"], PENDING)
        self.assertEqual(card["market"], "ashare")
        self.assertEqual(card["capital_layer"], "simulated")
        self.assertEqual(card["account_type"], "simulated")
        self.assertEqual(card["quantity"], 100)
        self.assertEqual(card["price"], 10.5)

    def test_ashare_sim_execute_rejects_non_a_share_before_bridge(self) -> None:
        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-BSHARE",
                "ts_code": "200521.SZ",
                "quantity": 100,
                "price": 10.5,
                "side": "buy",
            },
            account={"account_id": "ashare_sim"},
            config={"signals_dir": self.signals_dir, "hermes_enabled": True},
        )

        self.assertEqual(result.status, "rejected")
        self.assertFalse((self.signals_dir / "pending" / "SIM-ASHARE-BSHARE.json").exists())

    def test_ashare_sim_execute_defaults_to_server_local_fill_without_hermes(self) -> None:
        with patch("Ashare.sim_executor.send_sim_signal_to_mini") as send_mock:
            result = ashare_sim_execute(
                order={
                    "order_id": "SIM-ASHARE-LOCAL",
                    "ts_code": "600000.SH",
                    "quantity": 100,
                    "price": 10.5,
                    "side": "buy",
                },
                account={"account_id": "ashare_sim"},
            )

        self.assertEqual(result.status, "filled")
        self.assertEqual(result.filled_qty, 100)
        self.assertGreaterEqual(result.avg_price, 10.5)
        self.assertEqual(result.capital_layer, "simulated")
        self.assertEqual(result.account_type, "simulated")
        self.assertEqual(result.raw_response["mode"], "server_local_sim_engine")
        self.assertEqual(result.raw_response["engine_record"]["state"], "filled")
        send_mock.assert_not_called()

    def test_ashare_server_local_fill_rejects_non_lot_buy(self) -> None:
        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-NONLOT",
                "ts_code": "600000.SH",
                "quantity": 120,
                "price": 10.5,
                "side": "buy",
            },
            account={"account_id": "ashare_sim"},
        )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.raw_response["engine_record"]["reason"], "buy_quantity_not_lot_aligned")

    def test_ashare_sim_execute_sends_webhook_when_hermes_explicitly_enabled(self) -> None:
        with patch(
            "Ashare.sim_executor.send_sim_signal_to_mini",
            return_value={"status": "sent", "success": True, "order_id": "SIM-ASHARE-WEBHOOK"},
        ) as send_mock:
            result = ashare_sim_execute(
                order={
                    "order_id": "SIM-ASHARE-WEBHOOK",
                    "ts_code": "600000.SH",
                    "quantity": 100,
                    "price": 10.5,
                    "side": "buy",
                },
                account={"account_id": "ashare_sim"},
                config={"hermes_enabled": True},
            )

        self.assertEqual(result.status, "pending")
        self.assertEqual(result.capital_layer, "simulated")
        self.assertEqual(result.account_type, "simulated")
        self.assertEqual(result.raw_response["mode"], "mini_webhook_sent")
        send_mock.assert_called_once()

    def test_ashare_sim_execute_supports_local_mock_fill(self) -> None:
        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-MOCK",
                "ts_code": "600519.SH",
                "quantity": 200,
                "price": 123.45,
                "side": "buy",
            },
            account="ashare_sim",
            config={"mock_filled": True, "mock_fee": 1.23},
        )

        self.assertEqual(result.status, "filled")
        self.assertEqual(result.filled_qty, 200)
        self.assertEqual(result.avg_price, 123.45)
        self.assertEqual(result.fee, 1.23)
        self.assertEqual(result.capital_layer, "simulated")
        self.assertEqual(result.account_type, "simulated")
        self.assertEqual(result.raw_response["mode"], "mock_filled")

    def test_registered_executor_and_mini_consumer_can_consume_ashare_signal(self) -> None:
        executor = get_sim_executor("ashare")
        self.assertIs(executor, ashare_sim_execute)

        queue_result = executor(
            order={
                "order_id": "SIM-ASHARE-2",
                "ts_code": "600000.SH",
                "quantity": 100,
                "price": 10.0,
                "side": "buy",
            },
            account={"account_id": "ashare_sim"},
            config={"signals_dir": self.signals_dir, "hermes_enabled": True},
        )
        self.assertEqual(queue_result.status, "pending")

        consumer = MiniConsumer(
            signals_dir=self.signals_dir,
            executor_path=self.tmp_path / "a_share_simulated_trade_executor.py",
            worker_id="ashare-sim-test",
        )
        claimed = consumer.claim_next_pending()
        self.assertIsNotNone(claimed)

        stdout = json.dumps(
            {
                "status": "ok",
                "avg_price": 10.08,
                "filled_qty": 100,
                "fee": 0.8,
            }
        )
        completed = subprocess.CompletedProcess(args=["executor"], returncode=0, stdout=stdout, stderr="")
        with patch("mini.mini_consumer.subprocess.run", return_value=completed) as run_mock:
            result = consumer.dispatch(claimed or {})

        self.assertEqual(result["status"], "filled")
        run_mock.assert_called_once()
        filled_path = self.signals_dir / "filled" / "SIM-ASHARE-2.json"
        self.assertTrue(filled_path.exists())
        filled = read_json(filled_path)
        self.assertEqual(filled["market"], "ashare")
        self.assertEqual(filled["account_type"], "simulated")
        self.assertEqual(filled["capital_layer"], "simulated")
        self.assertEqual(filled["filled_price"], 10.08)
        self.assertEqual(filled["filled_qty"], 100)


if __name__ == "__main__":
    unittest.main()
