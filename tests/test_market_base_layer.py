from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shared.data.reader import TradingagentDataReader


class MarketBaseLayerTest(unittest.TestCase):
    def test_load_market_config_returns_dataclass_from_market_yaml(self) -> None:
        from shared.markets.config_schema import load_market_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            market_dir = root / "Demo"
            market_dir.mkdir()
            (market_dir / "config.yaml").write_text(
                """
market: demo
capital:
  default_layer: shadow
  allowed_layers: [shadow, simulated]
  initial_capital: 12345
  currency: USD
safety:
  real_money_enabled: false
  live_broker_enabled: false
  direct_execution_enabled: false
data:
  reader: shared.data.reader.TradingagentDataReader
  daily_table: market_bars_daily
  intraday_table: market_bars_intraday
  events_table: market_events
universe:
  max_symbols: 12
  min_close: 0.01
  active_only: true
session:
  timezone: UTC
  type: 24x7
risk:
  max_positions: 3
  max_single_position_pct: 0.2
fees:
  taker_bps: 10
  maker_bps: 5
reporting:
  daily_report_path: shared/review/demo/daily
  notify_on_trigger_only: true
promotion:
  min_shadow_trades: 20
  min_positive_days_pct: 0.55
""".strip(),
                encoding="utf-8",
            )

            config = load_market_config("Demo", root=root)

        self.assertEqual(config.market, "demo")
        self.assertEqual(config.capital.default_layer, "shadow")
        self.assertEqual(config.capital.allowed_layers, ("shadow", "simulated"))
        self.assertEqual(config.capital.initial_capital, 12345.0)
        self.assertEqual(config.safety.real_money_enabled, False)
        self.assertEqual(config.universe.max_symbols, 12)
        self.assertEqual(config.session.timezone, "UTC")
        self.assertEqual(config.risk.max_single_position_pct, 0.2)
        self.assertEqual(config.fees.taker_bps, 10.0)
        self.assertTrue(config.reporting.notify_on_trigger_only)
        self.assertEqual(config.promotion.min_shadow_trades, 20)

    def test_safety_helpers_reject_real_and_direct_execution(self) -> None:
        from shared.markets.config_schema import MarketToolConfig
        from shared.markets.safety import (
            SafetyViolation,
            assert_no_real_execution,
            assert_public_data_only,
            assert_shadow_or_sim_only,
        )

        real_config = MarketToolConfig(
            market="danger",
            capital={"default_layer": "real", "allowed_layers": ["shadow", "real"]},
        )
        broker_config = MarketToolConfig(
            market="broker",
            safety={"live_broker_enabled": True},
        )
        direct_config = MarketToolConfig(
            market="direct",
            safety={"direct_execution_enabled": True},
        )

        with self.assertRaisesRegex(SafetyViolation, "shadow/simulated"):
            assert_shadow_or_sim_only(real_config)
        with self.assertRaisesRegex(SafetyViolation, "live broker"):
            assert_public_data_only(broker_config)
        with self.assertRaisesRegex(SafetyViolation, "direct execution"):
            assert_no_real_execution(direct_config)

    def test_base_classes_create_reader_and_reject_real_execution(self) -> None:
        from shared.markets.base_tools import (
            BaseMarketData,
            BaseReport,
            BaseShadowRunner,
            BaseSimulator,
        )
        from shared.markets.config_schema import MarketToolConfig
        from shared.markets.safety import SafetyViolation

        class DemoMarketData(BaseMarketData):
            def get_daily(self, symbol: str, start: str, end: str):
                return []

            def get_latest_price(self, symbol: str, date: str):
                return None

            def get_universe(self, date: str):
                return []

            def health_check(self):
                return {"ok": True}

        class DemoSimulator(BaseSimulator):
            def simulate(self, order, account):
                return {"status": "simulated"}

            def fill_price(self, symbol: str, date: str):
                return 1.0

        class DemoShadowRunner(BaseShadowRunner):
            def run_shadow(self, date: str):
                return {"date": date}

            def get_signals(self, date: str):
                return []

            def write_shadow_record(self, record):
                return record

        class DemoReport(BaseReport):
            def render_daily(self, date: str):
                return {"date": date}

            def render_scorecard(self, date: str):
                return {"date": date, "score": 0}

            def delivery_policy(self, result):
                return {"send": False}

        safe_config = MarketToolConfig(market="demo")
        market_data = DemoMarketData("demo", safe_config)
        simulator = DemoSimulator("demo", safe_config, market_data)
        runner = DemoShadowRunner("demo", safe_config, market_data, simulator)
        report = DemoReport("demo", safe_config)

        self.assertIsInstance(market_data.reader, TradingagentDataReader)
        self.assertIs(runner.market_data, market_data)
        self.assertIs(runner.simulator, simulator)
        self.assertEqual(report.market, "demo")

        unsafe_config = MarketToolConfig(
            market="demo",
            safety={"real_money_enabled": True},
        )
        with self.assertRaises(SafetyViolation):
            DemoMarketData("demo", unsafe_config)


if __name__ == "__main__":
    unittest.main()
