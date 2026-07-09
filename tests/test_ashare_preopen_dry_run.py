from __future__ import annotations

import os
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
            {"market": market or "ashare", "symbol": "600001.SH", "name": "邯郸钢铁", "exchange": "SH", "status": "active", "list_date": "19980218"},
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

    def get_event_candidates(self) -> list[dict]:
        return []

    def get_factors(self, market: str | None = None, symbol: str = "") -> list[dict]:
        return [
            {"factor_name": "value", "value": 0.8},
            {"factor_name": "quality", "value": 0.8},
        ]

    def get_sentiment(self) -> list[dict]:
        return []


class LiquidityOrderedReader(FakeAshareReader):
    def get_assets(self, market: str | None = None) -> list[dict]:
        return [
            {"market": market or "ashare", "symbol": "000001.SZ", "name": "平安银行", "exchange": "SZ", "status": "active"},
            {"market": market or "ashare", "symbol": "600000.SH", "name": "浦发银行", "exchange": "SH", "status": "active"},
            {"market": market or "ashare", "symbol": "000002.SZ", "name": "万科A", "exchange": "SZ", "status": "active"},
        ]

    def get_bars_daily(self, market: str, symbol: str, start_date: str = "", end_date: str = "") -> list[dict]:
        amount_by_symbol = {
            "000001.SZ": 60_000.0,
            "600000.SH": 300_000.0,
            "000002.SZ": 120_000.0,
        }
        return [
            {
                "market": market,
                "symbol": symbol,
                "trade_date": "20260708",
                "close": 10.0,
                "amount": amount_by_symbol.get(symbol, 0.0),
            }
        ]


class BulkDailyReader(FakeAshareReader):
    def get_assets(self, market: str | None = None) -> list[dict]:
        return [
            {"market": market or "ashare", "symbol": "000001.SZ", "name": "平安银行", "exchange": "SZ", "status": "active"},
            {"market": market or "ashare", "symbol": "600000.SH", "name": "浦发银行", "exchange": "SH", "status": "active"},
            {"market": market or "ashare", "symbol": "000002.SZ", "name": "万科A", "exchange": "SZ", "status": "active"},
            {"market": market or "ashare", "symbol": "300750.SZ", "name": "宁德时代", "exchange": "SZ", "status": "active"},
            {"market": market or "ashare", "symbol": "600519.SH", "name": "贵州茅台", "exchange": "SH", "status": "active"},
        ]

    def get_latest_daily_batch(self, market: str = "Ashare", *, limit: int = 5000) -> list[dict]:
        return [
            {"market": market, "symbol": "000001.SZ", "trade_date": "20260707", "close": 10.0, "amount": 999_999.0},
            {"market": market, "symbol": "600000.SH", "trade_date": "20260708", "close": 10.0, "amount": 120_000.0},
            {"market": market, "symbol": "000002.SZ", "trade_date": "20260708", "close": 10.0, "amount": 80_000.0},
            {"market": market, "symbol": "300750.SZ", "trade_date": "20260708", "close": 10.0, "amount": 650_000.0},
            {"market": market, "symbol": "600519.SH", "trade_date": "20260708", "close": 10.0, "amount": 45_000.0},
        ]

    def get_bars_daily(self, market: str, symbol: str, start_date: str = "", end_date: str = "") -> list[dict]:
        raise AssertionError("batch daily rows should be used before per-symbol daily reads")


class APICoverageReader(FakeAshareReader):
    def get_latest_daily_batch(self, market: str = "Ashare", *, limit: int = 5000) -> list[dict]:
        rows = [
            {
                "market": market,
                "symbol": f"600{i:03d}.SH",
                "trade_date": "20260706",
                "close": 10.0,
                "amount": 100_000.0,
            }
            for i in range(1000)
        ]
        rows[0]["symbol"] = "600000.SH"
        rows[1]["symbol"] = "600001.SH"
        return rows


class AsharePreopenDryRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_diag = os.environ.get("TRADINGAGENT_ALLOW_SHARED_SIGNALS_SQLITE")
        os.environ["TRADINGAGENT_ALLOW_SHARED_SIGNALS_SQLITE"] = "1"
        self.addCleanup(self._restore_diag_env)

    def _restore_diag_env(self) -> None:
        if self._old_diag is None:
            os.environ.pop("TRADINGAGENT_ALLOW_SHARED_SIGNALS_SQLITE", None)
        else:
            os.environ["TRADINGAGENT_ALLOW_SHARED_SIGNALS_SQLITE"] = self._old_diag

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
                    ("600001.SH", {"combined": 0.7, "macro": 0.5, "event": 0.5, "fundamental": 0.7, "capital": 0.6, "technical": 0.7, "sentiment": 0.5}),
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

    def test_uses_strategy_account_view_when_validation_samples_occupy_snapshot(self) -> None:
        reader = FakeAshareReader()
        account = {
            "account": "ashare_sim",
            "sim_capital": 200_000.0,
            "cash_available": 82_683.89,
            "available_cash": 82_683.89,
            "positions": [
                {"ts_code": "000101.SZ", "quantity": 100, "market_value": 50_000.0},
                {"ts_code": "000102.SZ", "quantity": 100, "market_value": 60_000.0},
            ],
            "strategy_cash_available": 200_000.0,
            "strategy_positions": [],
            "capital_plan_sample_adjustment": {
                "view": "strategy_valid_samples_only",
                "ignored_validation_sample_count": 2,
                "reason": "chain_validation_samples_do_not_consume_strategy_capital",
            },
            "source": "test",
        }
        with (
            mock.patch.object(ashare_preopen_dry_run.AshareAdapter, "get_sim_account", return_value=account),
            mock.patch(
                "shared.runtime_test.ashare_preopen_dry_run.score_universe",
                return_value=[
                    ("600000.SH", {"combined": 0.8, "macro": 0.5, "event": 0.5, "fundamental": 0.8, "capital": 0.6, "technical": 0.7, "sentiment": 0.5}),
                    ("600001.SH", {"combined": 0.7, "macro": 0.5, "event": 0.5, "fundamental": 0.7, "capital": 0.6, "technical": 0.7, "sentiment": 0.5}),
                ],
            ),
        ):
            report = ashare_preopen_dry_run.run_preopen_dry_run(
                now=datetime.fromisoformat("2026-07-06T08:35:00+08:00"),
                sqlite_db=self._db(),
                reader=reader,
                score_limit=2,
            )

        self.assertEqual(report["capital_plan"]["cash_available"], 200000.0)
        self.assertEqual(report["capital_plan"]["account_cash_available"], 82683.89)
        self.assertEqual(report["capital_plan"]["existing_position_count"], 0)
        self.assertEqual(report["capital_plan"]["account_position_count"], 2)
        self.assertEqual(report["capital_plan"]["sample_adjustment"]["ignored_validation_sample_count"], 2)
        self.assertTrue(report["execution_gate"]["ready"])
        self.assertIn("timings_seconds", report)

    def test_data_section_prefers_sharedsignals_api_daily_batch(self) -> None:
        reader = APICoverageReader()
        with (
            mock.patch.object(ashare_preopen_dry_run.AshareAdapter, "get_sim_account", return_value=self._account()),
            mock.patch(
                "shared.runtime_test.ashare_preopen_dry_run.score_universe",
                return_value=[
                    ("600000.SH", {"combined": 0.8, "macro": 0.5, "event": 0.5, "fundamental": 0.8, "capital": 0.6, "technical": 0.7, "sentiment": 0.5}),
                ],
            ),
        ):
            report = ashare_preopen_dry_run.run_preopen_dry_run(
                now=datetime.fromisoformat("2026-07-06T08:35:00+08:00"),
                sqlite_db=Path("/tmp/nonexistent-sharedsignals.sqlite"),
                reader=reader,
                score_limit=2,
            )

        self.assertEqual(report["data"]["status"], "pass")
        self.assertEqual(report["data"]["data_source"], "SharedSignals API /tushare daily read model")
        self.assertEqual(report["data"]["symbol_count"], 1000)

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

    def test_uses_latest_regular_stock_date_when_bonds_are_newer(self) -> None:
        path = self._db(latest_date="20260706", count=2)
        conn = sqlite3.connect(path)
        conn.executemany(
            "INSERT INTO market_bars_daily VALUES (?, ?, ?, ?)",
            [
                ("Ashare", "110073.SH", "20260707", 106.88),
                ("Ashare", "110074.SH", "20260707", 271.31),
            ],
        )
        conn.commit()
        conn.close()

        universe = ashare_preopen_dry_run._latest_liquid_universe_from_read_model(path, "20260708", limit=2)

        self.assertEqual(universe, ["600000.SH", "600001.SH"])

    def test_reader_universe_prefers_latest_liquid_daily_amount(self) -> None:
        universe = ashare_preopen_dry_run._latest_liquid_universe_from_reader(
            LiquidityOrderedReader(),
            limit=2,
        )

        self.assertEqual(universe, ["600000.SH", "000002.SZ"])

    def test_reader_universe_prefers_sharedsignals_batch_daily_amount(self) -> None:
        universe = ashare_preopen_dry_run._latest_liquid_universe_from_reader(
            BulkDailyReader(),
            limit=3,
        )

        self.assertEqual(universe, ["300750.SZ", "600000.SH", "000002.SZ"])

    def test_execution_gate_observes_when_capital_plan_has_no_new_budget(self) -> None:
        db_path = self._db(latest_date="20260706", count=1)
        gate = ashare_preopen_dry_run._execution_gate(
            reader=object(),
            sqlite_db=db_path,
            date="20260708",
            candidate={"ts_code": "600000.SH"},
            capital_plan={"max_new_positions": 0, "position_budget_by_symbol": {}, "suggested_buys": []},
            now=datetime.fromisoformat("2026-07-08T08:35:00+08:00"),
        )

        self.assertEqual(gate["status"], "pass")
        self.assertEqual(gate["reason"], "capital_plan_no_new_buy_budget")
        self.assertFalse(gate["ready"])
        self.assertEqual(gate["blockers"], [])
        self.assertEqual(gate["synthetic_order"]["price"], 10.0)
        self.assertIn("price_from_latest_daily_close", gate["warnings"])
        self.assertIn("capital_plan_no_new_buy_budget", gate["warnings"])

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

    def test_write_outputs_does_not_touch_execution_or_review_paths(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        latest = root / "runtime_test" / "ashare_preopen_dry_run_latest.json"
        history = root / "runtime_test" / "ashare_preopen_dry_run_history.jsonl"

        with mock.patch.object(ashare_preopen_dry_run, "LATEST", latest), mock.patch.object(ashare_preopen_dry_run, "HISTORY", history):
            ashare_preopen_dry_run.write_outputs({"status": "pass", "read_only": True, "writes_excluded": ["signals", "ledger", "pending", "review"]})

        self.assertTrue(latest.exists())
        self.assertTrue(history.exists())
        for excluded in ("signals", "ledger", "pending", "review"):
            self.assertFalse((root / excluded).exists(), excluded)


if __name__ == "__main__":
    unittest.main()
