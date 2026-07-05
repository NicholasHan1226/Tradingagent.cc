from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.accounting.sim_ledger import SimLedger
from shared.review.equity_snapshots import write_sim_ledger_equity_snapshots


class EquitySnapshotTest(unittest.TestCase):
    def _seed_ledger(self, ledger_dir: Path) -> SimLedger:
        ledger = SimLedger(ledger_dir, starting_cash=10_000.0)
        ledger.record_fill(
            {"order_id": "B1", "symbol": "BTCUSDT", "side": "buy"},
            {
                "fill_id": "F1",
                "order_id": "B1",
                "fill_qty": 2.0,
                "fill_price": 100.0,
                "fill_time": "2026-07-04T00:00:00+00:00",
            },
            fees={"total": 0.0},
        )
        return ledger

    def test_daily_mark_to_market_writes_dashboard_ready_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._seed_ledger(Path(tmp) / "crypto" / "balanced")

            payload = ledger.daily_mark_to_market(
                {"BTCUSDT": 120.0},
                date="20260705",
                target_return_pct=8.0,
            )

            self.assertEqual(payload["capital_layer"], "simulated")
            self.assertIs(payload["real_execution"], False)
            self.assertEqual(payload["source"], "sim_ledger_daily_mark_to_market")
            self.assertEqual(payload["pnl_source"], "sim_ledger_mark_to_market")
            self.assertEqual(payload["capital_base"], 10_000.0)
            self.assertEqual(payload["realized_pnl"], 0.0)
            self.assertEqual(payload["unrealized_pnl"], 40.0)
            self.assertEqual(payload["total_pnl"], 40.0)
            self.assertEqual(payload["pnl"], 40.0)
            self.assertEqual(payload["return_pct"], 0.4)
            self.assertEqual(payload["target_return_pct"], 8.0)
            self.assertEqual(payload["benchmark_return_pct"], 0.0)
            self.assertEqual(payload["max_drawdown_pct"], 0.0)
            self.assertEqual(payload["missing_mark_count"], 0)
            self.assertEqual(payload["open_position_count"], 1)
            self.assertEqual(payload["trade_count"], 1)

            rows = [
                json.loads(line)
                for line in ledger.mtm_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(rows[-1]["total_equity"], payload["equity"])

    def test_daily_mark_to_market_flags_missing_marks_as_cost_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._seed_ledger(Path(tmp) / "crypto" / "balanced")

            payload = ledger.daily_mark_to_market({}, date="20260705")

            self.assertEqual(payload["pnl_source"], "sim_ledger_cost_fallback")
            self.assertEqual(payload["missing_mark_count"], 1)
            self.assertEqual(payload["total_pnl"], 0.0)

    def test_daily_mark_to_market_uses_cash_ledger_capital_after_deposit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._seed_ledger(Path(tmp) / "crypto" / "balanced")
            ledger.record_deposit(5_000.0, note="additional simulated capital")

            payload = ledger.daily_mark_to_market({"BTCUSDT": 120.0}, date="20260705")

            self.assertEqual(payload["capital_base"], 15_000.0)
            self.assertEqual(payload["total_pnl"], 40.0)
            self.assertAlmostEqual(payload["return_pct"], 0.266667, places=6)

    def test_daily_mark_to_market_drawdown_uses_prior_equity_high_water(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._seed_ledger(Path(tmp) / "crypto" / "balanced")

            first = ledger.daily_mark_to_market({"BTCUSDT": 150.0}, date="20260704")
            second = ledger.daily_mark_to_market({"BTCUSDT": 90.0}, date="20260705")

            self.assertEqual(first["max_drawdown_pct"], 0.0)
            self.assertGreater(second["max_drawdown_pct"], 1.0)

    def test_daily_mark_to_market_keeps_capital_base_as_drawdown_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._seed_ledger(Path(tmp) / "crypto" / "balanced")

            first = ledger.daily_mark_to_market({"BTCUSDT": 75.0}, date="20260704")
            second = ledger.daily_mark_to_market({"BTCUSDT": 70.0}, date="20260705")

            self.assertEqual(first["capital_base"], 10_000.0)
            self.assertEqual(first["max_drawdown_pct"], 0.5)
            self.assertEqual(second["max_drawdown_pct"], 0.6)

    def test_writer_discovers_style_ledgers_and_appends_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sim_ledger"
            style_dir = root / "crypto" / "balanced"
            self._seed_ledger(style_dir)

            with patch(
                "shared.review.equity_snapshots.load_mark_prices_for_positions",
                return_value={"BTCUSDT": 125.0},
            ):
                result = write_sim_ledger_equity_snapshots(
                    markets=["crypto"],
                    ledger_root=root,
                    trade_date="20260705",
                    target_return_pct=8.0,
                )

            self.assertEqual(result["totals"]["ledger_count"], 1)
            self.assertEqual(result["totals"]["written_count"], 1)
            self.assertEqual(result["totals"]["skipped_count"], 0)
            self.assertEqual(result["totals"]["missing_mark_count"], 0)
            self.assertEqual(result["ledgers"][0]["status"], "written")
            self.assertEqual(result["ledgers"][0]["total_pnl"], 50.0)
            rows = [
                json.loads(line)
                for line in (style_dir / "daily_mark_to_market.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(rows[-1]["date"], "20260705")
            self.assertEqual(rows[-1]["target_return_pct"], 8.0)

    def test_writer_dry_run_does_not_write_snapshot_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sim_ledger"
            style_dir = root / "crypto" / "balanced"
            self._seed_ledger(style_dir)

            result = write_sim_ledger_equity_snapshots(
                markets=["crypto"],
                ledger_root=root,
                trade_date="20260705",
                dry_run=True,
            )

            self.assertEqual(result["ledgers"][0]["status"], "dry_run")
            self.assertFalse((style_dir / "daily_mark_to_market.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
