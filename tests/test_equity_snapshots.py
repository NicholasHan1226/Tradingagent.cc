from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.accounting.sim_ledger import SimLedger
from shared.review.equity_snapshots import write_sim_ledger_equity_snapshots
from shared.review.pnl_summary import load_mark_prices_for_positions


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

    def test_daily_mark_to_market_inherits_dashboard_exclusion_from_positions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._seed_ledger(Path(tmp) / "crypto" / "balanced")
            state = json.loads(ledger.positions_path.read_text(encoding="utf-8"))
            state["exclude_from_dashboard"] = True
            state["run_context"] = "legacy_usd_capital_quarantine"
            ledger.positions_path.write_text(json.dumps(state), encoding="utf-8")

            payload = ledger.daily_mark_to_market({"BTCUSDT": 120.0}, date="20260705")

            self.assertTrue(payload["exclude_from_dashboard"])
            self.assertEqual(payload["run_context"], "legacy_usd_capital_quarantine")

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
                for line in (style_dir / "daily_mark_to_market.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(rows[-1]["date"], "20260705")
            self.assertEqual(rows[-1]["target_return_pct"], 8.0)
            self.assertEqual(rows[-1]["currency"], "USDT")
            self.assertEqual(rows[-1]["display_currency"], "CNY")
            self.assertEqual(rows[-1]["fx_to_cny"], 7.2)
            self.assertEqual(rows[-1]["total_pnl_cny"], 360.0)

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

    def test_writer_uses_ashare_local_sim_instead_of_legacy_style_ledgers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "logs" / "sim_ledger"
            local_sim_dir = base / "logs" / "local_sim"
            legacy_style = root / "ashare" / "aggressive"
            self._seed_ledger(legacy_style)
            local_sim_dir.mkdir(parents=True)
            (local_sim_dir / "local_sim_pnl.json").write_text(
                json.dumps(
                    {
                        "ashare_sim": {
                            "cash_available": 50_000.0,
                            "positions": {},
                            "total_pnl": 0.0,
                        }
                    }
                ),
                encoding="utf-8",
            )

            result = write_sim_ledger_equity_snapshots(
                markets=["ashare"],
                ledger_root=root,
                local_sim_dir=local_sim_dir,
                trade_date="20260706",
            )

            self.assertEqual(result["totals"]["ledger_count"], 1)
            self.assertEqual(result["totals"]["written_count"], 1)
            self.assertEqual(result["ledgers"][0]["style"], "ashare_sim")
            self.assertEqual(result["ledgers"][0]["equity"], 50_000.0)
            self.assertFalse((legacy_style / "daily_mark_to_market.jsonl").exists())
            self.assertTrue(
                (root / "ashare" / "ashare_sim" / "daily_mark_to_market.jsonl").exists()
            )
            row = json.loads(
                (root / "ashare" / "ashare_sim" / "daily_mark_to_market.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )
            self.assertIsNone(row["benchmark_return"])
            self.assertIsNone(row["benchmark_pnl"])
            self.assertIsNone(row["pnl_vs_benchmark"])
            self.assertEqual(row["benchmark_status"], "unavailable")

    def test_writer_marks_ashare_local_sim_with_recent_prices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "logs" / "sim_ledger"
            local_sim_dir = base / "logs" / "local_sim"
            local_sim_dir.mkdir(parents=True)
            (local_sim_dir / "local_sim_trades.jsonl").write_text(
                json.dumps(
                    {
                        "account": "ashare_sim",
                        "status": "filled",
                        "ts_code": "600000.SH",
                        "side": "buy",
                        "quantity": 100,
                        "filled_price": 10.0,
                        "net_amount": 1000.0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with patch(
                "shared.review.equity_snapshots.load_mark_prices_for_positions",
                return_value={"600000.SH": 12.0},
            ):
                result = write_sim_ledger_equity_snapshots(
                    markets=["ashare"],
                    ledger_root=root,
                    local_sim_dir=local_sim_dir,
                    trade_date="20260706",
                )

            self.assertEqual(result["ledgers"][0]["total_pnl"], 200.0)
            self.assertEqual(result["ledgers"][0]["equity"], 50_200.0)
            rows = [
                json.loads(line)
                for line in (
                    root / "ashare" / "ashare_sim" / "daily_mark_to_market.jsonl"
                )
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(rows[-1]["pnl_source"], "ashare_local_sim_mark_to_market")
            self.assertEqual(rows[-1]["open_position_count"], 1)

    def test_ashare_writer_drawdown_uses_daily_account_mtm_equity_curve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "logs" / "sim_ledger"
            local_sim_dir = base / "logs" / "local_sim"
            local_sim_dir.mkdir(parents=True)
            (local_sim_dir / "local_sim_trades.jsonl").write_text(
                json.dumps(
                    {
                        "account": "ashare_sim",
                        "status": "filled",
                        "ts_code": "600000.SH",
                        "side": "buy",
                        "quantity": 100,
                        "filled_price": 10.0,
                        "net_amount": 1000.0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with patch(
                "shared.review.equity_snapshots.load_mark_prices_for_positions",
                return_value={"600000.SH": 15.0},
            ):
                write_sim_ledger_equity_snapshots(
                    markets=["ashare"],
                    ledger_root=root,
                    local_sim_dir=local_sim_dir,
                    trade_date="20260713",
                )
            with patch(
                "shared.review.equity_snapshots.load_mark_prices_for_positions",
                return_value={"600000.SH": 5.0},
            ):
                write_sim_ledger_equity_snapshots(
                    markets=["ashare"],
                    ledger_root=root,
                    local_sim_dir=local_sim_dir,
                    trade_date="20260714",
                )

            rows = [
                json.loads(line)
                for line in (
                    root / "ashare" / "ashare_sim" / "daily_mark_to_market.jsonl"
                )
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(rows[-1]["equity"], 49_500.0)
            self.assertEqual(rows[-1]["max_drawdown_cny"], 1_000.0)
            self.assertAlmostEqual(
                rows[-1]["max_drawdown_pct"], 1_000.0 / 50_500.0 * 100.0, places=6
            )
            self.assertEqual(rows[-1]["drawdown_source"], "account_daily_mtm_equity")

    def test_ashare_local_sim_equity_includes_cash_and_market_value_after_buys(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "logs" / "sim_ledger"
            local_sim_dir = base / "logs" / "local_sim"
            local_sim_dir.mkdir(parents=True)
            (local_sim_dir / "local_sim_pnl.json").write_text(
                json.dumps(
                    {
                        "ashare_sim": {
                            "cash_available": 80_000.0,
                            "market_value": 120_000.0,
                            "total_pnl": 0.0,
                        }
                    }
                ),
                encoding="utf-8",
            )
            (local_sim_dir / "local_sim_trades.jsonl").write_text(
                json.dumps(
                    {
                        "account": "ashare_sim",
                        "status": "filled",
                        "ts_code": "600000.SH",
                        "side": "buy",
                        "quantity": 100,
                        "filled_price": 10.0,
                        "net_amount": 1000.0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with patch(
                "shared.review.equity_snapshots.load_mark_prices_for_positions",
                return_value={"600000.SH": 12.0},
            ):
                result = write_sim_ledger_equity_snapshots(
                    markets=["ashare"],
                    ledger_root=root,
                    local_sim_dir=local_sim_dir,
                    trade_date="20260706",
                )

            self.assertEqual(result["ledgers"][0]["equity"], 50_200.0)
            rows = [
                json.loads(line)
                for line in (
                    root / "ashare" / "ashare_sim" / "daily_mark_to_market.jsonl"
                )
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(rows[-1]["capital_base"], 50_000.0)
            self.assertEqual(rows[-1]["cash"], 49_000.0)
            self.assertEqual(rows[-1]["market_value"], 1_200.0)
            self.assertEqual(rows[-1]["total_equity"], 50_200.0)

    def test_load_mark_prices_uses_recent_daily_bar_for_weekend_snapshot(self) -> None:
        class FakeReader:
            def __init__(self, api_client=None):
                self.calls = []

            def get_bars_daily(self, market, symbol, start="", end=""):
                self.calls.append((market, symbol, start, end))
                return [
                    {
                        "market": market,
                        "symbol": symbol,
                        "trade_date": "20260703",
                        "close": 12.34,
                    },
                    {
                        "market": market,
                        "symbol": symbol,
                        "trade_date": "20260701",
                        "close": 11.11,
                    },
                ]

        with patch("shared.data.reader.TradingagentDataReader", FakeReader):
            prices = load_mark_prices_for_positions(
                {"600000.SH": {"quantity": 100}},
                "ashare",
                trade_date="20260705",
            )

        self.assertEqual(prices, {"600000.SH": 12.34})

    def test_load_mark_prices_uses_crypto_endpoint(self) -> None:
        class FakeReader:
            def __init__(self, api_client=None):
                pass

            def get_crypto_klines(self, symbol, limit=None):
                return [
                    {"symbol": symbol, "trade_date": "20260704", "close": 61000.0},
                    {"symbol": symbol, "trade_date": "20260705", "close": 62500.5},
                ]

        with patch("shared.data.reader.TradingagentDataReader", FakeReader):
            prices = load_mark_prices_for_positions(
                {"BTCUSDT": {"quantity": 0.1}},
                "crypto",
                trade_date="20260705",
            )

        self.assertEqual(prices, {"BTCUSDT": 62500.5})

    def test_load_mark_prices_uses_pm_market_prices(self) -> None:
        class FakeReader:
            def __init__(self, api_client=None):
                pass

            def get_pm_markets(self, limit=100):
                return [
                    {"market_id": "111111", "latest_price": 0.31},
                    {"market_id": "558943", "latest_price": 0.9765},
                ]

        with patch("shared.data.reader.TradingagentDataReader", FakeReader):
            prices = load_mark_prices_for_positions(
                {"558943": {"quantity": 100}},
                "pm",
                trade_date="20260705",
            )

        self.assertEqual(prices, {"558943": 0.9765})

    def test_load_mark_prices_marks_pm_no_positions_from_yes_price(self) -> None:
        class FakeReader:
            def __init__(self, api_client=None):
                pass

            def get_pm_markets(self, limit=100):
                return [
                    {"market_id": "558943", "yes_price": 0.8},
                ]

        with patch("shared.data.reader.TradingagentDataReader", FakeReader):
            prices = load_mark_prices_for_positions(
                {
                    "558943:no": {
                        "quantity": 100,
                        "market_id": "558943",
                        "outcome": "no",
                    }
                },
                "pm",
                trade_date="20260705",
            )

        self.assertIn("558943:no", prices)
        self.assertAlmostEqual(prices["558943:no"], 0.2, places=6)

    def test_load_mark_prices_prefers_explicit_pm_no_price(self) -> None:
        class FakeReader:
            def __init__(self, api_client=None):
                pass

            def get_pm_markets(self, limit=100):
                return [
                    {"market_id": "558943", "yes_price": 0.8, "no_price": 0.35},
                ]

        with patch("shared.data.reader.TradingagentDataReader", FakeReader):
            prices = load_mark_prices_for_positions(
                {
                    "558943:no": {
                        "quantity": 100,
                        "market_id": "558943",
                        "outcome": "no",
                    }
                },
                "pm",
                trade_date="20260705",
            )

        self.assertEqual(prices, {"558943:no": 0.35})


if __name__ == "__main__":
    unittest.main()
