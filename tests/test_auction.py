from __future__ import annotations

import unittest

from Ashare.market_phases import closing_auction, opening_auction


class FakeIntradayReader:
    def __init__(self, bars: list[dict[str, object]]) -> None:
        self.bars = bars
        self.calls: list[tuple[str, str, str, object, object]] = []

    def get_bars_intraday(
        self,
        market: str,
        symbol: str,
        interval: str,
        start: object = None,
        end: object = None,
    ) -> list[dict[str, object]]:
        self.calls.append((market, symbol, interval, start, end))
        return self.bars


class AuctionStrategyTest(unittest.TestCase):
    def test_opening_auction_detects_gap_and_volume_surge_from_sharedsignals_5m(self) -> None:
        reader = FakeIntradayReader(
            [
                {"bar_time": "2026-06-30 09:15:00", "open": 10.35, "close": 10.4, "volume": 4000, "pre_close": 10.0},
                {"bar_time": "2026-06-30 09:20:00", "open": 10.4, "close": 10.38, "volume": 900},
            ]
        )

        signal = opening_auction.generate_signal(
            {
                "ts_code": "600000.SH",
                "trade_date": "20260630",
                "current_time": "09:16",
                "avg_5m_volume": 1000,
            },
            reader=reader,
        )

        self.assertEqual(signal.action, "caution")
        self.assertEqual(signal.code, "600000.SH")
        self.assertGreater(signal.meta["gap_pct"], 0.02)
        self.assertGreater(signal.meta["volume_ratio"], 3.0)
        self.assertEqual(reader.calls[0], ("Ashare", "600000", "5m", "20260630", "20260630"))

    def test_opening_auction_holds_outside_window(self) -> None:
        signal = opening_auction.generate_signal({"current_time": "09:30"})

        self.assertEqual(signal.action, "hold")
        self.assertIn("Outside", signal.reason)

    def test_closing_auction_returns_reverse_repo_with_vwap_and_tail_metrics(self) -> None:
        reader = FakeIntradayReader(
            [
                {"bar_time": "2026-06-30 14:50:00", "open": 10.0, "close": 10.1, "volume": 1000},
                {"bar_time": "2026-06-30 14:55:00", "open": 10.1, "close": 10.2, "volume": 1000},
                {"bar_time": "2026-06-30 15:00:00", "open": 10.2, "close": 10.25, "volume": 1000},
            ]
        )

        signal = closing_auction.generate_signal(
            {"ts_code": "600000.SH", "trade_date": "20260630", "current_time": "14:55"},
            capital_plan={"idle_cash": 12000},
            reader=reader,
        )

        self.assertEqual(signal.action, "reverse_repo")
        self.assertEqual(signal.code, "204001")
        self.assertEqual(signal.quantity, 12)
        self.assertIn("vwap_deviation", signal.meta)
        self.assertGreater(signal.meta["tail_momentum"], 0)

    def test_closing_auction_warns_on_weak_tail_without_idle_cash(self) -> None:
        signal = closing_auction.generate_signal(
            {
                "current_time": "14:58",
                "bars_5m": [
                    {"bar_time": "14:50", "open": 10.0, "close": 10.0, "volume": 1000},
                    {"bar_time": "14:55", "open": 10.0, "close": 9.8, "volume": 1000},
                    {"bar_time": "15:00", "open": 9.8, "close": 9.7, "volume": 1000},
                ],
            }
        )

        self.assertIn(signal.action, {"warn", "caution"})
        self.assertLess(signal.meta["tail_momentum"], 0)


if __name__ == "__main__":
    unittest.main()
