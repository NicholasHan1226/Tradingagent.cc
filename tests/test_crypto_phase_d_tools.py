from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from Crypto.common import CryptoConfig
from Crypto.market_data import CryptoMarketData
from Crypto.simulator import CryptoSimulator
from Crypto.workflow import CryptoWorkflow


class FakeCryptoReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []
        self.assets = [
            {"symbol": "BTCUSDT", "status": "TRADING"},
            {"symbol": "ETHUSDT", "status": "TRADING"},
        ]
        self.bars = {
            "BTCUSDT": [
                {"trade_date": "20260701", "close": 100.0},
                {"trade_date": "20260702", "close": 110.0},
            ],
            "ETHUSDT": [
                {"trade_date": "20260701", "close": 50.0},
                {"trade_date": "20260702", "close": 49.0},
            ],
        }

    def get_bars_daily(self, market: str, symbol: str, start: str = "", end: str = ""):
        self.calls.append((market, symbol, start, end))
        rows = list(self.bars.get(symbol, []))
        if start:
            rows = [row for row in rows if row["trade_date"] >= start]
        if end:
            rows = [row for row in rows if row["trade_date"] <= end]
        return rows

    def get_assets(self, market: str):
        return self.assets if market == "Crypto" else []


class CryptoPhaseDToolsTest(unittest.TestCase):
    def test_crypto_config_is_usdt_24x7_shadow_only(self) -> None:
        config = CryptoConfig()

        self.assertEqual(config.market, "crypto")
        self.assertEqual(config.capital.currency, "USDT")
        self.assertEqual(config.session.type, "24x7")
        self.assertEqual(config.capital.allowed_layers, ("shadow", "simulated"))

    def test_market_data_reads_sharedsignals_crypto_market(self) -> None:
        reader = FakeCryptoReader()
        market_data = CryptoMarketData(CryptoConfig(), reader=reader)

        latest = market_data.get_latest_price("btcusdt", "20260702")

        self.assertEqual(latest, 110.0)
        self.assertIn(("Crypto", "BTCUSDT", "", "20260702"), reader.calls)

    def test_simulator_rejects_real_or_signed_payloads(self) -> None:
        simulator = CryptoSimulator(CryptoConfig(), CryptoMarketData(CryptoConfig(), reader=FakeCryptoReader()))

        with self.assertRaisesRegex(RuntimeError, "unsafe fields"):
            simulator.simulate(
                {"symbol": "BTCUSDT", "quantity": 1, "trade_date": "20260702", "api_key": "x"},
                {"account_type": "simulated"},
            )
        with self.assertRaisesRegex(RuntimeError, "real/live"):
            simulator.simulate(
                {"symbol": "BTCUSDT", "quantity": 1, "trade_date": "20260702"},
                {"account_type": "real"},
            )

    def test_workflow_writes_shadow_pending_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workflow = CryptoWorkflow(
                CryptoConfig(),
                reader=FakeCryptoReader(),
                signals_dir=Path(tmp) / "signals",
            )

            result = workflow.run_crypto_shadow_cycle("20260702")
            pending = list((Path(tmp) / "signals" / "shadow" / "pending").glob("*.json"))
            filled = list((Path(tmp) / "signals" / "shadow" / "filled").glob("*.json"))
            root_pending = Path(tmp) / "signals" / "pending"

        self.assertEqual(result["market"], "crypto")
        self.assertEqual(result["capital_layer"], "shadow")
        self.assertEqual(result["real_execution"], False)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["filled_count"], 1)
        self.assertEqual(len(pending), 0)
        self.assertEqual(len(filled), 1)
        self.assertFalse(root_pending.exists())


if __name__ == "__main__":
    unittest.main()
