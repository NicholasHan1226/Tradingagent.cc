from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from Ashare import forward_validation


class FakeAshareReader:
    def get_bars_intraday(self, market: str, symbol: str, interval: str, start: str, end: str) -> list[dict[str, object]]:
        return [
            {"bar_time": "2026-07-06 10:30:00", "close": 10.5},
            {"bar_time": "2026-07-06 11:00:00", "close": 10.8},
        ]

    def get_bars_daily(self, market: str, symbol: str, start: str, end: str) -> list[dict[str, object]]:
        return [
            {"trade_date": "20260706", "open": 9.8, "high": 10.9, "close": 10.7},
            {"trade_date": "20260707", "open": 10.9, "high": 11.4, "close": 11.1},
        ]


class AshareForwardValidationTest(unittest.TestCase):
    def test_labels_strategy_trade_without_writing_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trades = root / "local_sim_trades.jsonl"
            trades.write_text(
                json.dumps(
                    {
                        "trade_id": "LSIM-1",
                        "order_id": "SIM-1",
                        "market": "ashare",
                        "trade_date": "20260706",
                        "ts_code": "600000.SH",
                        "side": "buy",
                        "filled_price": 10.0,
                        "quantity": 100,
                        "candidate_pool_layer": "candidate",
                        "execution_source": "ashare_candidate_layer",
                        "fill_price_source": "market_snapshot",
                        "trade_timestamp_bj": "2026-07-06T10:00:00+08:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            report = forward_validation.build_forward_validation_report(
                date="20260706",
                reader=FakeAshareReader(),
                local_trades_path=trades,
                output=None,
                history=None,
            )

        self.assertTrue(report["read_only"])
        self.assertFalse(report["real_trading_enabled"])
        self.assertEqual(report["strategy_label_count"], 1)
        label = report["labels"][0]
        self.assertEqual(label["labels"]["m30"]["return_pct"], 0.05)
        self.assertEqual(label["labels"]["m60"]["return_pct"], 0.08)
        self.assertEqual(label["labels"]["close"]["return_pct"], 0.07)
        self.assertEqual(label["labels"]["next_day"]["high_return_pct"], 0.14)

    def test_skips_validation_sample(self) -> None:
        trade = {
            "trade_id": "LSIM-2",
            "market": "ashare",
            "trade_date": "20260706",
            "ts_code": "600000.SH",
            "side": "buy",
            "filled_price": 10.0,
            "quantity": 100,
        }

        label = forward_validation.label_trade(trade, reader=FakeAshareReader())

        self.assertEqual(label["status"], "skipped")
        self.assertEqual(label["reason"], "not_strategy_sample")


if __name__ == "__main__":
    unittest.main()
