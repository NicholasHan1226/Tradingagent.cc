from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from shared.runtime_test import ashare_preopen_dry_run


def _test_symbol(index: int) -> str:
    prefix = (600, 601, 603, 605, 688)[index // 1000]
    return f"{prefix * 1000 + (index % 1000):06d}.SH"


class FakeAshareReader:
    def __init__(self) -> None:
        self.symbols = ["600000.SH", "000001.SZ"]

    def get_assets(self, market: str | None = None) -> list[dict]:
        return [
            {"market": market or "ashare", "symbol": "600000.SH", "name": "浦发银行", "exchange": "SH", "status": "active", "list_date": "19991110"},
            {"market": market or "ashare", "symbol": "600001.SH", "name": "邯郸钢铁", "exchange": "SH", "status": "active", "list_date": "19980218"},
            {"market": market or "ashare", "symbol": "000001.SZ", "name": "平安银行", "exchange": "SZ", "status": "active", "list_date": "19910403"},
        ]

    def get_latest_daily_batch(self, market: str = "Ashare", *, limit: int = 5000) -> list[dict]:
        return [
            {
                "market": market,
                "symbol": _test_symbol(i),
                "trade_date": "20260706",
                "close": 10.0,
                "amount": 100_000.0,
            }
            for i in range(1000)
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

    def get_latest_daily_batch(self, market: str = "Ashare", *, limit: int = 5000) -> list[dict]:
        return [
            self.get_bars_daily(market, symbol)[0]
            for symbol in ("000001.SZ", "600000.SH", "000002.SZ")
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


class PartialCoverageReader(FakeAshareReader):
    """Reader with a large asset universe but only partial daily coverage."""
    def __init__(self, asset_count: int = 5000, daily_count: int = 3266,
                 daily_date: str = "20260708", intraday_rows: list[dict] | None = None) -> None:
        super().__init__()
        self._asset_count = asset_count
        self._daily_count = daily_count
        self._daily_date = daily_date
        self._intraday_rows = intraday_rows or []

    def get_assets(self, market: str | None = None) -> list[dict]:
        return [
            {"market": "Ashare", "symbol": _test_symbol(i), "name": f"测试{i:03d}",
             "exchange": "SH", "status": "active", "list_date": "20000101"}
            for i in range(self._asset_count)
        ]

    def get_latest_daily_batch(self, market: str = "Ashare", *, limit: int = 5000) -> list[dict]:
        return [
            {"market": "Ashare", "symbol": _test_symbol(i),
             "trade_date": self._daily_date, "close": 10.0, "amount": 100_000.0}
            for i in range(self._daily_count)
        ]

    def get_realtime_5min_batch(self, market: str, date: str | None = None,
                                *, limit: int | None = None) -> list[dict]:
        return list(self._intraday_rows)


class NoAssetsReader(FakeAshareReader):
    """Reader that returns no assets but sufficient daily bars for min_symbols check."""
    def get_assets(self, market: str | None = None) -> list[dict]:
        return []

    def get_latest_daily_batch(self, market: str = "Ashare", *, limit: int = 5000) -> list[dict]:
        return [
            {"market": "Ashare", "symbol": _test_symbol(i),
             "trade_date": "20260708", "close": 10.0, "amount": 100_000.0}
            for i in range(1100)
        ]


class AsharePreopenDryRunTest(unittest.TestCase):
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
        self.assertEqual(report["run_config"]["score_limit"], 2)
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

    def test_preopen_dry_run_uses_sample_count_for_probe_position(self) -> None:
        reader = FakeAshareReader()
        strategy_positions = [
            {"ts_code": "300759.SZ", "quantity": 1900, "market_value": 57589.0},
            {"ts_code": "600030.SH", "quantity": 2100, "market_value": 58800.0},
        ]
        account = {
            "account": "ashare_sim",
            "sim_capital": 200_000.0,
            "cash_available": 83_461.87,
            "available_cash": 83_461.87,
            "positions": strategy_positions,
            "strategy_cash_available": 83_461.87,
            "strategy_positions": strategy_positions,
            "capital_plan_sample_adjustment": {
                "view": "strategy_valid_samples_only",
                "strategy_sample_valid_count": 2,
                "min_strategy_samples": 5,
            },
            "source": "test",
        }
        with (
            mock.patch.object(ashare_preopen_dry_run.AshareAdapter, "get_sim_account", return_value=account),
            mock.patch(
                "shared.runtime_test.ashare_preopen_dry_run.score_universe",
                return_value=[
                    ("600000.SH", {"combined": 0.60, "macro": 0.5, "event": 0.5, "fundamental": 0.8, "capital": 0.7, "technical": 0.7, "sentiment": 0.6}),
                ],
            ),
        ):
            report = ashare_preopen_dry_run.run_preopen_dry_run(
                now=datetime.fromisoformat("2026-07-06T08:35:00+08:00"),
                reader=reader,
                score_limit=1,
            )

        self.assertEqual(report["capital_plan"]["risk_mode"], "sample_collection")
        self.assertEqual(report["capital_plan"]["max_new_positions"], 1)
        self.assertEqual(report["execution_gate"]["reason"], "synthetic_order_gate_ready")
        self.assertEqual(report["execution_gate"]["synthetic_order"]["ts_code"], "600000.SH")

    def test_preopen_dry_run_does_not_create_order_from_daily_sample_gate(self) -> None:
        reader = FakeAshareReader()
        strategy_positions = [
            {"ts_code": "300759.SZ", "quantity": 1900, "market_value": 57589.0},
            {"ts_code": "600030.SH", "quantity": 2100, "market_value": 58800.0},
        ]
        account = {
            "account": "ashare_sim",
            "sim_capital": 200_000.0,
            "cash_available": 83_461.87,
            "available_cash": 83_461.87,
            "positions": strategy_positions,
            "strategy_cash_available": 83_461.87,
            "strategy_positions": strategy_positions,
            "capital_plan_sample_adjustment": {
                "view": "strategy_valid_samples_only",
                "strategy_sample_valid_count": 8,
                "min_strategy_samples": 5,
            },
            "source": "test",
        }
        decision = {
            "recommended_action": "observe_and_label_candidates",
            "policy": {
                "today_strategy_sample_count": 0,
                "strategy_sample_count": 8,
                "min_strategy_samples": 5,
                "sample_collection_min_score": 0.55,
            },
        }
        with (
            mock.patch.object(ashare_preopen_dry_run.AshareAdapter, "get_sim_account", return_value=account),
            mock.patch.object(ashare_preopen_dry_run, "load_latest_decision", return_value=decision),
            mock.patch(
                "shared.runtime_test.ashare_preopen_dry_run.score_universe",
                return_value=[
                    ("600000.SH", {"combined": 0.60, "macro": 0.5, "event": 0.5, "fundamental": 0.8, "capital": 0.7, "technical": 0.7, "sentiment": 0.6}),
                ],
            ),
        ):
            report = ashare_preopen_dry_run.run_preopen_dry_run(
                now=datetime.fromisoformat("2026-07-06T08:35:00+08:00"),
                reader=reader,
                score_limit=1,
            )

        self.assertNotEqual(report["capital_plan"]["risk_mode"], "sample_collection")
        self.assertEqual(report["capital_plan"]["max_new_positions"], 0)
        self.assertFalse(report["execution_gate"]["ready"])
        self.assertEqual(report["capital_plan"]["evolution_decision"]["recommended_action"], "observe_and_label_candidates")

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
                reader=reader,
                score_limit=2,
            )

        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["candidate_pool"]["reason"], "no_candidate_layer_after_scoring")
        self.assertEqual(report["candidate_pool"]["score_diagnostics"]["scored_count"], 2)
        self.assertEqual(report["candidate_pool"]["score_diagnostics"]["evidence_reason_summary"]["capital"]["missing_capital_flow_rows"], 1)
        self.assertFalse(report["execution_gate"]["ready"])
        self.assertIn("candidate_pool:no_candidate_layer_after_scoring", report["warnings"])

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
        gate = ashare_preopen_dry_run._execution_gate(
            reader=object(),
            date="20260708",
            candidate={"ts_code": "600000.SH"},
            capital_plan={"max_new_positions": 0, "position_budget_by_symbol": {}, "suggested_buys": []},
            now=datetime.fromisoformat("2026-07-08T08:35:00+08:00"),
        )

        self.assertEqual(gate["status"], "pass")
        self.assertEqual(gate["reason"], "capital_plan_no_new_buy_budget")
        self.assertFalse(gate["ready"])
        self.assertEqual(gate["blockers"], [])
        self.assertEqual(gate["synthetic_order"]["price"], 0.0)
        self.assertIn("capital_plan_no_new_buy_budget", gate["warnings"])

    def test_fails_when_daily_data_is_stale(self) -> None:
        reader = PartialCoverageReader(asset_count=5000, daily_count=4800, daily_date="20260625")
        with mock.patch.object(ashare_preopen_dry_run.AshareAdapter, "get_sim_account", return_value=self._account()):
            report = ashare_preopen_dry_run.run_preopen_dry_run(
                now=datetime.fromisoformat("2026-07-06T08:35:00+08:00"),
                reader=reader,
                score_limit=2,
            )

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["data"]["status"], "fail")
        self.assertIn("data:api_daily_bars_stale", report["blockers"])

    # ------------------------------------------------------------------
    # Daily-coverage-ratio gate
    # ------------------------------------------------------------------

    def test_coverage_ratio_below_threshold_fails_direct(self) -> None:
        """3266/5000 = 0.6532 < 0.90 → api_daily_coverage_incomplete."""
        reader = PartialCoverageReader(asset_count=5000, daily_count=3266, daily_date="20260708")
        now = datetime.fromisoformat("2026-07-10T08:30:00+08:00")
        data = ashare_preopen_dry_run._api_daily_coverage_from_reader(
            reader, now=now, min_symbols=1000, min_coverage_ratio=0.90,
        )
        self.assertEqual(data["status"], "fail")
        self.assertEqual(data["reason"], "api_daily_coverage_incomplete")
        self.assertEqual(data["asset_count"], 5000)
        self.assertEqual(data["symbol_count"], 3266)
        self.assertAlmostEqual(data["daily_coverage_ratio"], 0.6532, places=4)
        self.assertEqual(data["min_coverage_ratio"], 0.90)

    def test_coverage_ratio_above_threshold_passes_direct(self) -> None:
        """4800/5000 = 0.96 > 0.90 → pass when intraday evidence is coherent."""
        reader = PartialCoverageReader(asset_count=5000, daily_count=4800, daily_date="20260708")
        now = datetime.fromisoformat("2026-07-10T08:30:00+08:00")
        data = ashare_preopen_dry_run._api_daily_coverage_from_reader(
            reader, now=now, min_symbols=1000, min_coverage_ratio=0.90,
        )
        self.assertEqual(data["status"], "pass")
        self.assertEqual(data["reason"], "api_daily_bars_ready")
        self.assertEqual(data["asset_count"], 5000)
        self.assertEqual(data["symbol_count"], 4800)
        self.assertAlmostEqual(data["daily_coverage_ratio"], 0.96, places=4)

    def test_coverage_ratio_missing_still_passes_when_assets_unavailable(self) -> None:
        """No get_assets → ratio is None; fall back to absolute min_symbols."""
        reader = NoAssetsReader()
        now = datetime.fromisoformat("2026-07-10T08:30:00+08:00")
        data = ashare_preopen_dry_run._api_daily_coverage_from_reader(
            reader, now=now, min_symbols=1000, min_coverage_ratio=0.90,
        )
        self.assertEqual(data["status"], "pass")
        self.assertEqual(data["reason"], "api_daily_bars_ready")
        self.assertEqual(data["asset_count"], 0)
        self.assertIsNone(data["daily_coverage_ratio"])
        # Still governed by absolute min_symbols: 1100 >= 1000
        self.assertGreaterEqual(data["symbol_count"], 1000)

    def test_coverage_ratio_uses_explicit_parameter_not_env(self) -> None:
        """min_coverage_ratio comes from explicit parameter, not env fallback."""
        reader = PartialCoverageReader(asset_count=5000, daily_count=4200, daily_date="20260708")
        now = datetime.fromisoformat("2026-07-10T08:30:00+08:00")
        # 4200/5000 = 0.84, passes with threshold 0.80, fails with 0.90
        data_strict = ashare_preopen_dry_run._api_daily_coverage_from_reader(
            reader, now=now, min_symbols=1000, min_coverage_ratio=0.90,
        )
        self.assertEqual(data_strict["status"], "fail")
        self.assertEqual(data_strict["reason"], "api_daily_coverage_incomplete")

        data_relaxed = ashare_preopen_dry_run._api_daily_coverage_from_reader(
            reader, now=now, min_symbols=1000, min_coverage_ratio=0.80,
        )
        self.assertEqual(data_relaxed["status"], "pass")

    # ------------------------------------------------------------------
    # Intraday-vs-daily evidence-date gate
    # ------------------------------------------------------------------

    def test_preopen_intraday_newer_than_daily_fails(self) -> None:
        """At preopen (before 09:30), intraday date > daily date → fail."""
        intraday = [
            {"market": "Ashare", "symbol": "600000.SH",
             "trade_date": "20260710", "bar_time": "2026-07-10 08:00:00",
             "close": 10.0},
        ]
        reader = PartialCoverageReader(
            asset_count=5000, daily_count=4800, daily_date="20260708",
            intraday_rows=intraday,
        )
        now = datetime.fromisoformat("2026-07-10T08:30:00+08:00")
        data = ashare_preopen_dry_run._api_daily_coverage_from_reader(
            reader, now=now, min_symbols=1000, min_coverage_ratio=0.90,
        )
        self.assertEqual(data["status"], "fail")
        self.assertEqual(data["reason"], "api_daily_bars_behind_intraday")
        self.assertEqual(data["expected_evidence_date"], "20260710")
        self.assertEqual(data["latest_trade_date"], "20260708")

    def test_intraday_current_session_allows_previous_business_day_daily(self) -> None:
        """After 09:30, the exact previous business-day daily bar is valid."""
        intraday = [
            {"market": "Ashare", "symbol": "600000.SH",
             "trade_date": "20260710", "bar_time": "2026-07-10 10:00:00",
             "close": 10.0},
        ]
        reader = PartialCoverageReader(
            asset_count=5000, daily_count=4800, daily_date="20260709",
            intraday_rows=intraday,
        )
        now = datetime.fromisoformat("2026-07-10T10:00:00+08:00")
        data = ashare_preopen_dry_run._api_daily_coverage_from_reader(
            reader, now=now, min_symbols=1000, min_coverage_ratio=0.90,
        )
        # Not behind because current session is in progress
        self.assertEqual(data["status"], "pass")
        self.assertEqual(data["reason"], "api_daily_bars_ready")

    def test_intraday_current_session_rejects_daily_older_than_previous_business_day(self) -> None:
        intraday = [
            {"market": "Ashare", "symbol": "600000.SH",
             "trade_date": "20260710", "bar_time": "2026-07-10 10:00:00",
             "close": 10.0},
        ]
        reader = PartialCoverageReader(
            asset_count=5000, daily_count=4800, daily_date="20260708",
            intraday_rows=intraday,
        )
        data = ashare_preopen_dry_run._api_daily_coverage_from_reader(
            reader,
            now=datetime.fromisoformat("2026-07-10T10:00:00+08:00"),
            min_symbols=1000,
            min_coverage_ratio=0.90,
        )

        self.assertEqual(data["status"], "fail")
        self.assertEqual(data["reason"], "api_daily_bars_behind_intraday")

    def test_intraday_empty_does_not_trigger_date_check(self) -> None:
        """Empty intraday batch → no intraday evidence, skip behind-intraday check."""
        reader = PartialCoverageReader(
            asset_count=5000, daily_count=4800, daily_date="20260708",
            intraday_rows=[],
        )
        now = datetime.fromisoformat("2026-07-10T08:30:00+08:00")
        data = ashare_preopen_dry_run._api_daily_coverage_from_reader(
            reader, now=now, min_symbols=1000, min_coverage_ratio=0.90,
        )
        self.assertEqual(data["status"], "pass")
        self.assertEqual(data["expected_evidence_date"], "20260708")

    # ------------------------------------------------------------------
    # Propagation into dry-run blockers / execution not-ready
    # ------------------------------------------------------------------

    def test_coverage_failure_propagates_to_blockers(self) -> None:
        """Data coverage failure → blockers list and execution not-ready."""
        reader = PartialCoverageReader(asset_count=5000, daily_count=3266, daily_date="20260708")
        with (
            mock.patch.object(ashare_preopen_dry_run.AshareAdapter, "get_sim_account", return_value=self._account()),
            mock.patch(
                "shared.runtime_test.ashare_preopen_dry_run.score_universe",
                return_value=[
                    ("600000.SH", {"combined": 0.8, "macro": 0.5, "event": 0.5, "fundamental": 0.8, "capital": 0.6, "technical": 0.7, "sentiment": 0.5}),
                    ("600001.SH", {"combined": 0.7, "macro": 0.5, "event": 0.5, "fundamental": 0.7, "capital": 0.6, "technical": 0.7, "sentiment": 0.5}),
                ],
            ),
            mock.patch(
                "shared.runtime_test.ashare_preopen_dry_run._build_capital_plan",
                return_value={
                    "status": "pass",
                    "reason": "capital_plan_ready",
                    "max_new_positions": 0,
                    "position_budget_by_symbol": {},
                    "suggested_buys": [],
                },
            ),
        ):
            report = ashare_preopen_dry_run.run_preopen_dry_run(
                now=datetime.fromisoformat("2026-07-10T08:30:00+08:00"),
                reader=reader,
                score_limit=2,
            )

        self.assertEqual(report["status"], "fail")
        self.assertIn("data:api_daily_coverage_incomplete", report["blockers"])
        self.assertIn("asset_count", report["data"])
        self.assertEqual(report["data"]["asset_count"], 5000)
        self.assertIn("daily_coverage_ratio", report["data"])
        self.assertAlmostEqual(report["data"]["daily_coverage_ratio"], 0.6532, places=4)
        self.assertIn("expected_evidence_date", report["data"])
        # Data failure should propagate: execution must not be ready
        self.assertFalse(report["execution_gate"]["ready"])
        self.assertEqual(report["execution_gate"]["status"], "fail")
        self.assertEqual(report["execution_gate"]["reason"], "api_data_failure")
        self.assertIn("api_data_failure", report["execution_gate"]["blockers"])
        self.assertIn("execution_gate:api_data_failure", report["blockers"])

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
