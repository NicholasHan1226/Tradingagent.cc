from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.execution import shadow_broker


class FakeReader:
    def get_bars_daily(self, market: str, symbol: str, start: object = None, end: object = None) -> list[dict[str, object]]:
        if symbol == "600000.SH":
            return [{"trade_date": "20260701", "close": 12.0}]
        return []

    def close(self) -> None:
        pass


class ShadowValuationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        shadow_dir = Path(self.tmp.name)
        for name, value in (
            ("SHADOW_DIR", shadow_dir),
            ("SHADOW_TRADES", shadow_dir / "shadow_trades.jsonl"),
            ("SHADOW_POSITIONS", shadow_dir / "shadow_positions.json"),
            ("SHADOW_PNL", shadow_dir / "shadow_pnl.json"),
            ("SHADOW_LOCK", shadow_dir / ".shadow.lock"),
        ):
            patcher = patch.object(shadow_broker, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_ashare_pnl_uses_reader_close_before_trade_price(self) -> None:
        shadow_broker.record_shadow({"ts_code": "600000.SH", "side": "buy", "quantity": 100, "price": 10, "trade_date": "20260701"}, "ashare_shadow", market="ashare")
        with patch.object(shadow_broker, "SharedSignalsReader", return_value=FakeReader()):
            pnl = shadow_broker.get_shadow_pnl("ashare_shadow", "20260701", market="ashare")

        self.assertEqual(pnl["positions"]["600000.SH"]["last_price"], 12.0)
        self.assertEqual(pnl["market_value"], 1200.0)
        self.assertEqual(pnl["valuation_source"], "sharedsignals_market_bars_daily_close")


if __name__ == "__main__":
    unittest.main()
