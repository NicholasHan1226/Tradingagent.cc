from datetime import date
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Ashare import t_plus_1


class TestTPlusOne(unittest.TestCase):
    def setUp(self):
        self.trade_calendar_patch = patch.object(
            t_plus_1, "TRADE_CALENDAR_SEARCH_ROOTS", ()
        )
        self.trade_calendar_patch.start()
        t_plus_1._load_trade_calendar_data.cache_clear()

    def tearDown(self):
        t_plus_1._load_trade_calendar_data.cache_clear()
        self.trade_calendar_patch.stop()

    def test_to_date_supports_expected_formats(self):
        cases = [
            ("20260630", date(2026, 6, 30)),
            ("2026-06-30", date(2026, 6, 30)),
            ("2026-06-30T10:00:00", date(2026, 6, 30)),
            ("2026-06-30T10:00:00+08:00", date(2026, 6, 30)),
        ]

        for raw_value, expected in cases:
            with self.subTest(raw_value=raw_value):
                self.assertEqual(t_plus_1._to_date(raw_value), expected)

    def test_to_date_rejects_invalid_strings(self):
        with self.assertRaisesRegex(ValueError, "Unsupported date string format"):
            t_plus_1._to_date("2026/06/30")

    def test_can_sell_requires_next_trading_day_after_friday_buy(self):
        self.assertEqual(t_plus_1.can_sell("2026-06-26", "2026-06-27"), False)
        self.assertEqual(t_plus_1.can_sell("2026-06-26", "2026-06-29"), True)

    def test_can_sell_skips_known_holidays(self):
        self.assertEqual(t_plus_1.can_sell("2026-09-24", "2026-09-25"), False)
        self.assertEqual(t_plus_1.can_sell("2026-09-24", "2026-09-28"), True)

    def test_can_sell_returns_false_when_open_date_missing(self):
        self.assertEqual(t_plus_1.can_sell(None, "2026-06-30"), False)
        self.assertEqual(t_plus_1.can_sell("", "2026-06-30"), False)

    def test_filter_sellable_uses_t_plus_1_logic(self):
        positions = [
            {"ts_code": "600000.SH", "open_date": "2026-06-26"},
            {"ts_code": "600001.SH", "open_date": "2026-06-29"},
            {"ts_code": "600002.SH", "open_date": None},
        ]

        sellable = t_plus_1.filter_sellable(positions, "2026-06-29")

        self.assertEqual(
            [position["ts_code"] for position in sellable],
            ["600000.SH"],
        )

    def test_get_trading_calendar_uses_external_trade_cal_when_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            calendar_path = temp_path / "trade_cal.csv"
            calendar_path.write_text(
                "cal_date,is_open\n"
                "2026-06-26,1\n"
                "2026-06-27,0\n"
                "2026-06-29,1\n",
                encoding="utf-8",
            )
            with patch.object(
                t_plus_1, "TRADE_CALENDAR_SEARCH_ROOTS", (temp_path,)
            ):
                t_plus_1._load_trade_calendar_data.cache_clear()
                self.assertEqual(
                    t_plus_1.get_trading_calendar("2026-06-26", "2026-06-29"),
                    [
                        date(2026, 6, 26),
                        date(2026, 6, 29),
                    ],
                )
                t_plus_1._load_trade_calendar_data.cache_clear()


if __name__ == "__main__":
    unittest.main()
