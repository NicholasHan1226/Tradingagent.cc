from __future__ import annotations

import unittest
from unittest.mock import patch

from shared.screening.candidate_pool import build_pool
from shared.screening.universe_filter import filter_universe


class CandidatePoolAshareFilterTest(unittest.TestCase):
    def test_candidate_pool_excludes_b_share_codes_for_ashare(self) -> None:
        with patch("shared.screening.six_dimension_scorer.score_stock", return_value={"combined": 0.7}):
            pool = build_pool(
                date="20260701",
                holdings=["200521.SZ", "000001.SZ"],
                universe=["200521.SZ", "900901.SH", "000001.SZ", "600000.SH"],
                market="ashare",
                reader=object(),
            )

        self.assertEqual(pool["holdings"], ["000001.SZ"])
        self.assertNotIn("200521.SZ", pool["candidate"])
        self.assertNotIn("900901.SH", pool["candidate"])
        self.assertIn("600000.SH", pool["candidate"])

    def test_universe_filter_excludes_b_share_codes_for_ashare(self) -> None:
        class Reader:
            def get_assets(self, market: str):
                return [
                    {"symbol": "000001.SZ", "name": "A", "list_date": "20000101", "status": "active"},
                    {"symbol": "200521.SZ", "name": "B", "list_date": "20000101", "status": "active"},
                    {"symbol": "900901.SH", "name": "B", "list_date": "20000101", "status": "active"},
                    {"symbol": "600000.SH", "name": "No Bar", "list_date": "20000101", "status": "active"},
                ]

            def get_coverage(self, market: str, date: str):
                return []

            def get_bars_daily(self, market: str, symbol: str, start=None, end=None):
                if symbol in {"600000", "600000.SH"}:
                    return []
                return [{"trade_date": "20260701", "vol": 100, "amount": 1000000}]

        result = filter_universe(
            "20260701",
            ["000001.SZ", "200521.SZ", "900901.SH", "600000.SH"],
            reader=Reader(),
            market="ashare",
        )

        self.assertEqual(result, ["000001.SZ"])


if __name__ == "__main__":
    unittest.main()
