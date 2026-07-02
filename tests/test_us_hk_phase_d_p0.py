from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import SafetyViolation


class FakeReader:
    def __init__(self) -> None:
        self.assets = {
            "us": [
                {"symbol": "AAPL", "status": "active", "exchange": "NASDAQ"},
                {"symbol": "MSFT", "status": "active", "exchange": "NYSE"},
            ],
            "hk": [
                {"symbol": "00700.HK", "status": "active", "name": "Tencent"},
                {"symbol": "09988.HK", "status": "active", "name": "Alibaba"},
            ],
        }
        self.bars = {
            ("us", "AAPL"): [
                {"trade_date": "2026-07-01", "close": 195.25},
                {"trade_date": "2026-07-02", "close": 196.5},
            ],
            ("hk", "00700.HK"): [
                {"trade_date": "2026-07-01", "close": 380.0},
                {"trade_date": "2026-07-02", "close": 382.4},
            ],
        }

    def get_assets(self, market: str) -> list[dict[str, object]]:
        return list(self.assets.get(market.lower(), []))

    def get_bars_daily(
        self,
        market: str,
        symbol: str,
        start: str = "",
        end: str = "",
    ) -> list[dict[str, object]]:
        rows = list(self.bars.get((market.lower(), symbol.upper()), []))
        if start:
            rows = [row for row in rows if str(row["trade_date"]) >= start]
        if end:
            rows = [row for row in rows if str(row["trade_date"]) <= end]
        return rows

    def get_events(self, market: str | None = None, symbol: str = "", start: str = "", end: str = ""):
        return []

    def get_factors(self, market: str | None = None, symbol: str = ""):
        return []


class USHKPhaseDP0Test(unittest.TestCase):
    def test_us_market_data_and_workflow_use_sharedsignals_shadow_only(self) -> None:
        from US.common import USConfig
        from US.market_data import USMarketData
        from US.simulator import USSimulator
        from US.shadow_runner import USShadowRunner
        from US.workflow import USWorkflow, run_us_shadow_cycle

        config = USConfig()
        reader = FakeReader()
        market_data = USMarketData(config=config, reader=reader)
        simulator = USSimulator(config=config, market_data=market_data)

        self.assertEqual(config.market, "us")
        self.assertEqual(config.currency, "USD")
        self.assertEqual(config.sessions["NYSE"]["timezone"], "America/New_York")
        self.assertEqual(config.sessions["NASDAQ"]["regular"], ("09:30", "16:00"))
        self.assertEqual(market_data.get_latest_price("AAPL", "2026-07-02"), 196.5)
        self.assertEqual(market_data.get_universe("2026-07-02"), ["AAPL", "MSFT"])

        fill = simulator.simulate(
            {"symbol": "AAPL", "side": "buy", "quantity": 2, "trade_date": "2026-07-02"},
            {"cash": 1000},
        )

        self.assertEqual(fill["status"], "filled")
        self.assertEqual(fill["broker"], "local_mock")
        self.assertEqual(fill["capital_layer"], "simulated")

        with tempfile.TemporaryDirectory() as tmp:
            runner = USShadowRunner(
                config=config,
                market_data=market_data,
                simulator=simulator,
                signals_root=Path(tmp),
            )
            result = runner.run_shadow("2026-07-02")
            self.assertEqual(result["market"], "us")
            self.assertEqual(result["status"], "ok")
            self.assertGreaterEqual(result["written"], 1)
            self.assertTrue(list((Path(tmp) / "shadow" / "pending").glob("*.json")))

        workflow = USWorkflow(config=config, reader=reader)
        cycle = workflow.run_us_shadow_cycle("2026-07-02")
        self.assertEqual(cycle["market"], "us")
        self.assertEqual(cycle["status"], "ok")
        self.assertEqual(run_us_shadow_cycle("2026-07-02", reader=reader)["market"], "us")

    def test_hk_market_data_adapter_simulator_and_shadow_runner_are_local_only(self) -> None:
        from HK.adapter import HKAdapter
        from HK.common import HKConfig
        from HK.market_data import HKMarketData
        from HK.shadow_runner import HKShadowRunner
        from HK.simulator import HKSimulator
        from HK.workflow import HKWorkflow, run_hk_shadow_cycle

        config = HKConfig()
        reader = FakeReader()
        market_data = HKMarketData(config=config, reader=reader)
        adapter = HKAdapter(config=config, reader=reader)
        simulator = HKSimulator(config=config, market_data=market_data)

        self.assertEqual(config.market, "hk")
        self.assertEqual(config.currency, "HKD")
        self.assertEqual(config.sessions["HKEX"]["regular"], (("09:30", "12:00"), ("13:00", "16:00")))
        self.assertEqual(adapter.normalize_symbol("700"), "00700.HK")
        self.assertEqual(adapter.normalize_symbol("00700"), "00700.HK")
        self.assertEqual(adapter.to_sharedsignals_symbol("9988"), "09988.HK")
        self.assertEqual(adapter.get_universe("2026-07-02"), ["00700.HK", "09988.HK"])
        self.assertEqual(market_data.get_latest_price("700", "2026-07-02"), 382.4)

        fill = simulator.simulate(
            {"symbol": "700", "side": "buy", "quantity": 100, "trade_date": "2026-07-02"},
            {"cash": 100000},
        )

        self.assertEqual(fill["status"], "filled")
        self.assertEqual(fill["broker"], "local_mock")
        self.assertEqual(fill["currency"], "HKD")

        with tempfile.TemporaryDirectory() as tmp:
            runner = HKShadowRunner(
                config=config,
                market_data=market_data,
                simulator=simulator,
                signals_root=Path(tmp),
            )
            result = runner.run_shadow("2026-07-02")
            self.assertEqual(result["market"], "hk")
            self.assertEqual(result["status"], "ok")
            self.assertTrue(list((Path(tmp) / "shadow" / "pending").glob("*.json")))

        workflow = HKWorkflow(config=config, reader=reader)
        cycle = workflow.run_hk_shadow_cycle("2026-07-02")
        self.assertEqual(cycle["market"], "hk")
        self.assertEqual(cycle["status"], "ok")
        self.assertEqual(run_hk_shadow_cycle("2026-07-02", reader=reader)["market"], "hk")

    def test_market_simulators_reject_real_execution_config(self) -> None:
        from HK.market_data import HKMarketData
        from HK.simulator import HKSimulator
        from US.market_data import USMarketData
        from US.simulator import USSimulator

        unsafe = MarketToolConfig(
            market="us",
            safety={"live_broker_enabled": True, "real_money_enabled": True},
        )

        with self.assertRaises(SafetyViolation):
            USMarketData(config=unsafe, reader=FakeReader())

        safe_data = USMarketData(config=MarketToolConfig(market="us"), reader=FakeReader())
        direct = MarketToolConfig(market="us", safety={"direct_execution_enabled": True})
        with self.assertRaises(SafetyViolation):
            USSimulator(config=direct, market_data=safe_data)

        us_live_broker = MarketToolConfig(market="us", safety={"live_broker_enabled": True})
        with self.assertRaises(SafetyViolation):
            USSimulator(config=us_live_broker, market_data=safe_data)

        hk_safe_data = HKMarketData(config=MarketToolConfig(market="hk"), reader=FakeReader())
        hk_live_broker = MarketToolConfig(market="hk", safety={"live_broker_enabled": True})
        with self.assertRaises(SafetyViolation):
            HKSimulator(config=hk_live_broker, market_data=hk_safe_data)


if __name__ == "__main__":
    unittest.main()
