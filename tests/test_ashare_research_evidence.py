from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Ashare import research_evidence


class FakeReader:
    def __init__(
        self,
        bars_by_symbol: dict[str, list[dict[str, object]]],
        daily_by_symbol: dict[tuple[str, str], list[dict[str, object]]] | None = None,
    ) -> None:
        self.bars_by_symbol = bars_by_symbol
        self.daily_by_symbol = daily_by_symbol or {}

    def get_assets(self, market: str | None = None) -> list[dict[str, object]]:
        return [{"symbol": symbol, "market": market or "Ashare"} for symbol in self.bars_by_symbol]

    def get_bars_intraday(self, market: str, symbol: str, interval: str, start: str, end: str) -> list[dict[str, object]]:
        return self.bars_by_symbol.get(symbol, [])

    def get_bars_daily(self, market: str, symbol: str, start: str, end: str) -> list[dict[str, object]]:
        return self.daily_by_symbol.get((symbol, start), [])


class AshareResearchEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.styles = root / "styles"
        self.style_weights = root / "style_weights.json"
        self.signals = root / "signals"
        self.no_trade = root / "ashare_no_trade_explanations.jsonl"
        self.styles.mkdir(parents=True)
        (self.styles / "closing_momentum.json").write_text(
            json.dumps({"name": "closing_momentum", "status": "paused", "weight": 1.0}),
            encoding="utf-8",
        )
        (self.styles / "balanced.json").write_text(
            json.dumps({"name": "balanced", "status": "active", "weight": 1.0}),
            encoding="utf-8",
        )
        (self.signals / "filled").mkdir(parents=True)
        (self.signals / "filled" / "SIM-1.json").write_text(
            json.dumps({"market": "ashare", "trade_date": "20260706", "strategy_name": "balanced", "status": "filled"}),
            encoding="utf-8",
        )
        self.no_trade.write_text(
            json.dumps({"date": "20260706", "no_trade_explanation": {"category": "all_rejected_by_risk"}}) + "\n",
            encoding="utf-8",
        )

    def test_reverse_repo_accrual_estimates_interest_without_booking_pnl(self) -> None:
        result = research_evidence.estimate_reverse_repo_accrual(12500, annualized_yield=0.02)

        self.assertEqual(result["action"], "lend")
        self.assertEqual(result["amount"], 12000.0)
        self.assertEqual(result["lots"], 12)
        self.assertAlmostEqual(result["estimated_interest"], round(12000 * 0.02 / 365, 4))
        self.assertFalse(result["booked_to_pnl"])

    def test_reverse_repo_yield_prefers_daily_bar_from_reader(self) -> None:
        reader = FakeReader({}, {("204001.SH", "20260706"): [{"close": 2.05}]})

        yld, source = research_evidence.resolve_reverse_repo_yield(reader, "20260706")

        self.assertAlmostEqual(yld, 0.0205)
        self.assertEqual(source, "daily_bar:close")

    def test_closing_momentum_detects_tail_candidate(self) -> None:
        result = research_evidence.closing_momentum_evidence(
            {
                "600000.SH": [
                    {"bar_time": "2026-07-06 14:35:00", "close": 9.8, "volume": 1000},
                    {"bar_time": "2026-07-06 14:40:00", "open": 10.0, "close": 10.05, "high": 10.05, "volume": 1500},
                    {"bar_time": "2026-07-06 14:45:00", "open": 10.05, "close": 10.12, "high": 10.12, "volume": 1800},
                    {"bar_time": "2026-07-06 14:55:00", "open": 10.12, "close": 10.18, "high": 10.18, "volume": 2000},
                ]
            }
        )

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["candidate_count"], 1)
        self.assertGreater(result["candidates"][0]["tail_momentum"], 0.003)

    def test_closing_momentum_labels_next_day_open_and_high_when_daily_bar_exists(self) -> None:
        reader = FakeReader(
            {},
            {("600000.SH", "20260707"): [{"open": 10.3, "high": 10.6, "close": 10.4}]},
        )
        result = research_evidence.closing_momentum_evidence(
            {
                "600000.SH": [
                    {"bar_time": "2026-07-06 14:35:00", "close": 9.8, "volume": 1000},
                    {"bar_time": "2026-07-06 14:40:00", "open": 10.0, "close": 10.05, "high": 10.05, "volume": 1500},
                    {"bar_time": "2026-07-06 14:45:00", "open": 10.05, "close": 10.12, "high": 10.12, "volume": 1800},
                    {"bar_time": "2026-07-06 14:55:00", "open": 10.12, "close": 10.18, "high": 10.18, "volume": 2000},
                ]
            },
            trade_date="20260706",
            reader=reader,
        )

        candidate = result["candidates"][0]
        self.assertEqual(candidate["next_trading_day"], "20260707")
        self.assertEqual(candidate["label_state"], "labeled")
        self.assertAlmostEqual(candidate["next_day_open_return"], round((10.3 - 10.18) / 10.18, 6))
        self.assertAlmostEqual(candidate["next_day_high_return"], round((10.6 - 10.18) / 10.18, 6))

    def test_opening_auction_reports_no_cancel_phase_anomaly(self) -> None:
        result = research_evidence.opening_auction_evidence(
            {
                "600000.SH": [
                    {"bar_time": "2026-07-06 09:15:00", "open": 10.4, "pre_close": 10.0, "volume": 5000},
                    {"bar_time": "2026-07-06 09:20:00", "open": 10.35, "pre_close": 10.0, "volume": 1000},
                ]
            },
            current_time="09:22",
        )

        self.assertEqual(result["phase"], "no_cancel")
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["anomaly_count"], 1)

    def test_opening_auction_marks_first_five_minute_proxy_when_auction_data_is_missing(self) -> None:
        result = research_evidence.opening_auction_evidence(
            {
                "600000.SH": [
                    {"bar_time": "2026-07-06 09:30:00", "open": 10.4, "pre_close": 10.0, "volume": 5000},
                    {"bar_time": "2026-07-06 09:35:00", "open": 10.35, "pre_close": 10.0, "volume": 1000},
                ]
            },
            current_time="09:32",
        )

        self.assertEqual(result["data_mode"], "first_5m_proxy")
        self.assertEqual(result["proxy_symbols_with_bars"], 1)
        self.assertEqual(result["anomalies"][0]["data_mode"], "first_5m_proxy")

    def test_style_evidence_separates_paused_active_and_blocked(self) -> None:
        self.style_weights.write_text(
            json.dumps({
                "styles": {
                    "balanced": {"status": "active", "weight": 3.0},
                    "closing_momentum": {"status": "paused", "weight": 1.0},
                    "runtime_breakout": {"status": "active", "weight": 1.0},
                }
            }),
            encoding="utf-8",
        )
        result = research_evidence.style_evidence(
            trade_date="20260706",
            styles_dir=self.styles,
            style_weights_path=self.style_weights,
            signals_dir=self.signals,
            no_trade_log=self.no_trade,
            total_capital=200000,
        )
        rows = {item["style"]: item for item in result["styles"]}

        self.assertEqual(rows["balanced"]["state"], "active_sample")
        self.assertEqual(rows["closing_momentum"]["state"], "paused")
        self.assertEqual(rows["balanced"]["blocked_reasons"]["all_rejected_by_risk"], 1)
        self.assertEqual(rows["balanced"]["virtual_budget"], 150000)
        self.assertEqual(rows["runtime_breakout"]["virtual_budget"], 50000)
        self.assertEqual(rows["closing_momentum"]["virtual_budget"], 0)
        self.assertEqual(result["summary"]["virtual_capital"], 200000)

    def test_build_report_uses_reader_and_remains_read_only(self) -> None:
        reader = FakeReader(
            {"600000.SH": [{"bar_time": "2026-07-06 09:15:00", "open": 10.4, "pre_close": 10.0, "volume": 5000}]},
            {("204001.SH", "20260706"): [{"close": 2.2}]},
        )
        with patch.object(research_evidence, "style_evidence", return_value={"state": "ready", "styles": []}):
            report = research_evidence.build_research_evidence(
                trade_date="20260706",
                idle_cash=2000,
                reader=reader,
                max_symbols=1,
            )

        self.assertTrue(report["read_only"])
        self.assertFalse(report["real_trading_enabled"])
        self.assertEqual(report["reverse_repo"]["lots"], 2)
        self.assertEqual(report["reverse_repo"]["yield_source"], "daily_bar:close")


if __name__ == "__main__":
    unittest.main()
