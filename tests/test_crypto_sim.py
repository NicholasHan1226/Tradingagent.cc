from __future__ import annotations

import unittest

from Crypto.adapter import CryptoAdapter
from Crypto.sim_executor import crypto_sim_execute
from shared.execution.sim_broker import SimResult, execute_sim_order
from shared.execution.sim_executor_registry import get_sim_executor


class FakeBinanceClient:
    def __init__(self, price: str = "43210.50") -> None:
        self.price = price
        self.calls: list[str] = []

    def get_symbol_ticker(self, *, symbol: str) -> dict[str, str]:
        self.calls.append(symbol)
        return {"symbol": symbol, "price": self.price}


class CryptoSimExecutorTest(unittest.TestCase):
    def test_adapter_exposes_dedicated_sim_account(self) -> None:
        adapter = CryptoAdapter()

        self.assertEqual(adapter.get_shadow_account(), "crypto_shadow")
        self.assertEqual(adapter.get_sim_account(), "crypto_sim")

    def test_crypto_sim_execute_uses_mock_binance_ticker(self) -> None:
        client = FakeBinanceClient(price="50000.25")

        result = crypto_sim_execute(
            order={"order_id": "SIM-1", "symbol": "BTCUSDT", "quantity": 2},
            account={"account_id": "crypto_sim"},
            config={"market_data_client": client},
        )

        self.assertIsInstance(result, SimResult)
        self.assertEqual(result.status, "filled")
        self.assertEqual(result.filled_qty, 2)
        self.assertEqual(result.avg_price, 50000.25)
        self.assertAlmostEqual(result.fee, 100.0005, places=8)
        self.assertEqual(result.capital_layer, "simulated")
        self.assertEqual(result.account_type, "simulated")
        self.assertEqual(result.market, "crypto")
        self.assertEqual(client.calls, ["BTCUSDT"])

    def test_registered_crypto_executor_dispatches_via_sim_broker(self) -> None:
        client = FakeBinanceClient(price="42000.00")

        executor = get_sim_executor("crypto")
        self.assertIs(executor, crypto_sim_execute)

        result = execute_sim_order(
            order={
                "order_id": "SIM-2",
                "ts_code": "ETHUSDT",
                "quantity": 3,
                "capital_layer": "real",
                "account_type": "real",
            },
            market="crypto",
            account={"account_id": "crypto_sim", "account_type": "real"},
            config={"market_data_client": client, "capital_layer": "real"},
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.filled_qty, 0)
        self.assertEqual(result.avg_price, 0.0)
        self.assertEqual(result.capital_layer, "simulated")
        self.assertEqual(result.account_type, "simulated")
        self.assertEqual(result.order_id, "SIM-2")
        self.assertIn("real/live execution is rejected", result.message)
        self.assertEqual(client.calls, [])

    def test_crypto_sim_executor_rejects_real_execution_payload_directly(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            crypto_sim_execute(
                order={
                    "order_id": "SIM-REAL",
                    "symbol": "BTCUSDT",
                    "quantity": 1,
                    "capital_layer": "real",
                },
                account={"account_id": "crypto_sim"},
                config={"market_data_client": FakeBinanceClient()},
            )


if __name__ == "__main__":
    unittest.main()
