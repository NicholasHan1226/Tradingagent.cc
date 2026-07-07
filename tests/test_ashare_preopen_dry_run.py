from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from shared.runtime_test import ashare_preopen_dry_run


class FakeAshareReader:
    def __init__(self) -> None:
        self.symbols = ["600000.SH", "000001.SZ"]

    def get_assets(self, market: str | None = None) -> list[dict]:
        return [
            {"market": market or "ashare", "symbol": "600000.SH", "name": "浦发银行", "exchange": "SH", "status": "active", "list_date": "19991110"},
            {"market": market or "ashare", "symbol": "000001.SZ", "name": "平安银行", "exchange": "SZ", "status": "active", "list_date": "19910403"},
        ]

    def get_coverage(self, market: str, trade_date: str) -> list[dict]:
        return []

    def get_bars_daily(self, market: str, symbol: str, start_date: str = "", end_date: str = "") -> list[dict]:
        rows: list[dict] = []
        for idx in range(30):
            rows.append(
                {
                    "market": market,
                    "symbol": symbol,
                    "trade_date": f"202606{idx + 1:02d}" if idx < 30 else "20260701",
                    "close": 10.0 + idx * 0.1,
                    "amount": 100_000.0,
                }
            )
        rows.append({"market": market, "symbol": symbol, "trade_date": "20260706", "close": 13.2, "amount": 100_000.0})
        return rows

    def get_bars_intraday(self, market: str, symbol: str, interval: str = "5m", start_time: str = "", end_time: str = "") -> list[dict]:
        return []

    def get_regime(self) -> dict:
        return {"regime": "balanced", "regime_confidence": 0.5}

    def get_events(self, market: str | None = None, symbol: str = "", start_date: str = "", end_date: str = "") -> list[dict]:
        return []

    def get_factors(self, market: str | None = None, symbol: str = "") -> list[dict]:
        return [
            {"factor_name": "value", "value": 0.8},
            {"factor_name": "quality", "value": 0.8},
        ]

    def get_sentiment(self) -> list[dict]:
        return []


class AsharePreopenDryRunTest(unittest.TestCase):
    def _db(self, latest_date: str = "20260706", count: int = 1000) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "marketdata.sqlite"
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE market_bars_daily (
                market TEXT,
                symbol TEXT,
                trade_date TEXT,
                close REAL
            )
            """
        )
        rows = [("Ashare", f"600{i:03d}.SH", latest_date, 10.0) for i in range(count)]
        conn.executemany("INSERT INTO market_bars_daily VALUES (?, ?, ?, ?)", rows)
        conn.commit()
        conn.close()
        return path

    def _account(self) -> dict:
        return {
            "account": "ashare_sim",
            "sim_capital": 200_000.0,
            "cash_available": 200_000.0,
            "available_cash": 200_000.0,
            "positions": [],
            "source": "test",
        }

    def test_passes_when_candidate_capital_and_gate_are_ready(self) -> None:
        reader = FakeAshareReader()
        with (
            mock.patch.object(ashare_preopen_dry_run.AshareAdapter, "get_sim_account", return_value=self._account()),
            mock.patch(
                "shared.runtime_test.ashare_preopen_dry_run.score_universe",
                return_value=[
                    ("600000.SH", {"combined": 0.8, "macro": 0.5, "event": 0.5, "fundamental": 0.8, "capital": 0.6, "technical": 0.7, "sentiment": 0.5}),
                    ("000001.SZ", {"combined": 0.7, "macro": 0.5, "event": 0.5, "fundamental": 0.7, "capital": 0.6, "technical": 0.7, "sentiment": 0.5}),
                ],
            ),
        ):
            report = ashare_preopen_dry_run.run_preopen_dry_run(
                now=datetime.fromisoformat("2026-07-06T08:35:00+08:00"),
                sqlite_db=self._db(),
                reader=reader,
                score_limit=2,
            )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["data"]["status"], "pass")
        self.assertEqual(report["candidate_pool"]["candidate_count"], 2)
        self.assertTrue(report["execution_gate"]["ready"])
        self.assertEqual(report["execution_gate"]["synthetic_order"]["candidate_pool_layer"], "candidate")
        self.assertEqual(report["execution_gate"]["synthetic_order"]["execution_source"], "ashare_candidate_layer")
        self.assertTrue(report["read_only"])
        self.assertIn("outside_regular_session_now_expected_for_preopen", report["warnings"])

    def test_warns_and_safe_empty_when_no_candidate_passes_threshold(self) -> None:
        reader = FakeAshareReader()
        with (
            mock.patch.object(ashare_preopen_dry_run.AshareAdapter, "get_sim_account", return_value=self._account()),
            mock.patch(
                "shared.runtime_test.ashare_preopen_dry_run.score_universe",
                return_value=[
                    (
                        "600000.SH",
                        {
                            "combined": 0.5,
                            "macro": 0.5,
                            "event": 0.5,
                            "fundamental": 0.5,
                            "capital": 0.5,
                            "technical": 0.5,
                            "sentiment": 0.5,
                            "evidence_coverage": 0.0,
                            "missing_evidence_dimensions": ["macro", "event", "fundamental", "capital", "technical", "sentiment"],
                            "evidence_sources": {
                                "technical": {"has_evidence": False, "source": "SharedSignals daily bars", "reason": "insufficient_daily_bars"},
                                "capital": {"has_evidence": False, "source": "SharedSignals capital flow/factors", "reason": "missing_capital_flow_rows"},
                            },
                        },
                    ),
                    ("000001.SZ", {"combined": 0.45}),
                ],
            ),
        ):
            report = ashare_preopen_dry_run.run_preopen_dry_run(
                now=datetime.fromisoformat("2026-07-06T08:35:00+08:00"),
                sqlite_db=self._db(),
                reader=reader,
                score_limit=2,
            )

        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["candidate_pool"]["reason"], "no_candidate_layer_after_scoring")
        self.assertEqual(report["candidate_pool"]["score_diagnostics"]["scored_count"], 2)
        self.assertEqual(report["candidate_pool"]["score_diagnostics"]["evidence_reason_summary"]["capital"]["missing_capital_flow_rows"], 1)
        self.assertFalse(report["execution_gate"]["ready"])
        self.assertIn("candidate_pool:no_candidate_layer_after_scoring", report["warnings"])

    def test_fails_when_daily_data_is_stale(self) -> None:
        reader = FakeAshareReader()
        with mock.patch.object(ashare_preopen_dry_run.AshareAdapter, "get_sim_account", return_value=self._account()):
            report = ashare_preopen_dry_run.run_preopen_dry_run(
                now=datetime.fromisoformat("2026-07-06T08:35:00+08:00"),
                sqlite_db=self._db(latest_date="20260625"),
                reader=reader,
                score_limit=2,
            )

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["data"]["status"], "fail")
        self.assertIn("data:pre_open_daily_bars_stale", report["blockers"])


if __name__ == "__main__":
    unittest.main()
