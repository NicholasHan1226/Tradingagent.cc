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

    def test_candidate_pool_uses_precomputed_scores_without_rescoring(self) -> None:
        with patch("shared.screening.six_dimension_scorer.score_stock") as score_stock:
            pool = build_pool(
                date="20260701",
                universe=["000001.SZ", "600000.SH", "000002.SZ"],
                market="ashare",
                reader=object(),
                scores_by_symbol={
                    "000001.SZ": {"combined": 0.56},
                    "600000.SH": {"combined": 0.50},
                    "000002.SZ": {"combined": 0.44},
                },
            )

        score_stock.assert_not_called()
        self.assertEqual(pool["candidate"], ["000001.SZ"])
        self.assertEqual(pool["watch"], ["600000.SH"])

    def test_ashare_candidate_requires_research_evidence_when_metadata_present(self) -> None:
        technical_capital_only = {
            "combined": 0.66,
            "technical": 1.0,
            "capital": 1.0,
            "event": 0.5,
            "fundamental": 0.5,
            "sentiment": 0.5,
            "evidence_coverage": 0.5,
            "missing_evidence_dimensions": ["event", "fundamental", "sentiment"],
            "evidence_sources": {
                "event": {"has_evidence": False, "reason": "no_matched_event_evidence"},
                "fundamental": {"has_evidence": False, "reason": "missing_fundamental_rows"},
                "sentiment": {"has_evidence": False, "reason": "missing_sentiment_rows"},
                "capital": {"has_evidence": True},
                "technical": {"has_evidence": True},
                "macro": {"has_evidence": True},
            },
        }
        fundamental_supported = {
            "combined": 0.61,
            "technical": 0.8,
            "capital": 0.8,
            "fundamental": 0.58,
            "event": 0.5,
            "sentiment": 0.5,
            "evidence_coverage": 0.5,
            "missing_evidence_dimensions": ["event", "sentiment", "macro"],
            "evidence_sources": {
                "event": {"has_evidence": False, "reason": "no_matched_event_evidence"},
                "fundamental": {"has_evidence": True, "reason": "sharedsignals_fundamentals"},
                "sentiment": {"has_evidence": False, "reason": "missing_sentiment_rows"},
                "capital": {"has_evidence": True},
                "technical": {"has_evidence": True},
                "macro": {"has_evidence": False},
            },
        }

        pool = build_pool(
            date="20260701",
            universe=["000001.SZ", "600000.SH"],
            market="ashare",
            reader=object(),
            scores_by_symbol={
                "000001.SZ": technical_capital_only,
                "600000.SH": fundamental_supported,
            },
        )

        self.assertEqual(pool["candidate"], ["600000.SH"])
        self.assertIn("000001.SZ", pool["watch"])

    def test_ashare_candidate_requires_minimum_evidence_coverage(self) -> None:
        pool = build_pool(
            date="20260701",
            universe=["000001.SZ"],
            market="ashare",
            reader=object(),
            scores_by_symbol={
                "000001.SZ": {
                    "combined": 0.88,
                    "fundamental": 0.9,
                    "evidence_coverage": 0.17,
                    "missing_evidence_dimensions": ["macro", "event", "capital", "technical", "sentiment"],
                    "evidence_sources": {
                        "fundamental": {"has_evidence": True},
                        "event": {"has_evidence": False},
                        "sentiment": {"has_evidence": False},
                    },
                }
            },
        )

        self.assertEqual(pool["candidate"], [])
        self.assertEqual(pool["watch"], ["000001.SZ"])

    def test_universe_filter_excludes_b_share_codes_for_ashare(self) -> None:
        class Reader:
            daily_calls = []

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
                self.daily_calls.append((symbol, start, end))
                if symbol in {"600000", "600000.SH"}:
                    return []
                return [{"trade_date": "20260701", "vol": 100, "amount": 1000000}]

        reader = Reader()
        result = filter_universe(
            "20260701",
            ["000001.SZ", "200521.SZ", "900901.SH", "600000.SH"],
            reader=reader,
            market="ashare",
        )

        self.assertEqual(result, ["000001.SZ"])
        self.assertIn(("000001", "20260617", "20260701"), reader.daily_calls)


if __name__ == "__main__":
    unittest.main()
