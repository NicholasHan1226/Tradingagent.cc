from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shared.accounting.sim_ledger import SimLedger
from shared.markets.style_runner import StyleRunner


STYLE = {
    "name": "balanced",
    "position_pct": 0.1,
    "stop_loss_pct": -0.08,
    "take_profit_pct": 0.12,
    "max_hold_days": 5,
    "pyramid": False,
    "scale_in_steps": 1,
    "conviction_min": 0.3,
    "description": "unit test style",
    "status": "active",
    "weight": 1.0,
}


class FilledSimulator:
    def simulate(self, order, account):
        return {
            "status": "filled",
            "market": order["market"],
            "symbol": order["symbol"],
            "side": order["side"],
            "filled_qty": order["quantity"],
            "avg_price": order["price"],
            "fee": 0.25,
            "order_id": order["order_id"],
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
        }


class CapturingSimulator:
    def __init__(self) -> None:
        self.orders = []

    def simulate(self, order, account):
        self.orders.append(dict(order))
        return {
            "status": "filled",
            "market": order["market"],
            "symbol": order["symbol"],
            "side": order["side"],
            "filled_qty": order["quantity"],
            "avg_price": order["price"],
            "fee": 0.0,
            "order_id": order["order_id"],
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
        }


class StyleRunnerLedgerTest(unittest.TestCase):
    def test_records_filled_simulated_runs_to_market_style_ledger_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "styles"
            review_root = root / "review"
            ledger_root = root / "ledger"
            styles_dir.mkdir()
            (styles_dir / "balanced.json").write_text(json.dumps(STYLE), encoding="utf-8")
            runner = StyleRunner(
                "crypto",
                FilledSimulator(),
                styles_dir=styles_dir,
                review_root=review_root,
                ledger_root=ledger_root,
            )
            signals = [{"symbol": "BTCUSDT", "price": 100.0, "side": "buy", "conviction": 0.9}]

            first = runner.run(signals, date="20260704")
            second = runner.run(signals, date="20260704")

            self.assertEqual(first["runs"][0]["ledger"]["status"], "recorded")
            self.assertEqual(second["runs"][0]["ledger"]["status"], "duplicate")
            journal = ledger_root / "crypto" / "balanced" / "trade_journal.jsonl"
            rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["symbol"], "BTCUSDT")
            self.assertEqual(rows[0]["side"], "buy")
            self.assertGreater(rows[0]["fill_qty"], 0)
            positions = json.loads((ledger_root / "crypto" / "balanced" / "positions.json").read_text(encoding="utf-8"))
            self.assertIn("BTCUSDT", positions["positions"])

    def test_style_metrics_use_ledger_mark_to_market_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "styles"
            review_root = root / "review"
            ledger_root = root / "ledger"
            styles_dir.mkdir()
            (styles_dir / "balanced.json").write_text(json.dumps(STYLE), encoding="utf-8")
            runner = StyleRunner(
                "crypto",
                FilledSimulator(),
                styles_dir=styles_dir,
                review_root=review_root,
                ledger_root=ledger_root,
            )

            runner.run([{"symbol": "BTCUSDT", "price": 100.0, "side": "buy", "conviction": 0.9}], date="20260704")
            result = runner.run([{"symbol": "BTCUSDT", "price": 110.0, "side": "buy", "conviction": 0.9}], date="20260704")

            metric = result["style_comparison"][0]
            self.assertEqual(metric["pnl_source"], "sim_ledger_mark_to_market")
            self.assertEqual(metric["pnl_metric_source"], "sim_ledger_realized_unrealized_samples")
            self.assertEqual(metric["realized_pnl"], 0.0)
            self.assertGreater(metric["unrealized_pnl"], 900.0)
            self.assertEqual(metric["pnl"], metric["unrealized_pnl"])
            self.assertEqual(metric["win_rate"], 1.0)

    def test_style_metrics_include_realized_and_remaining_unrealized_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "styles"
            review_root = root / "review"
            ledger_root = root / "ledger"
            styles_dir.mkdir()
            (styles_dir / "balanced.json").write_text(json.dumps(STYLE), encoding="utf-8")
            runner = StyleRunner(
                "crypto",
                FilledSimulator(),
                styles_dir=styles_dir,
                review_root=review_root,
                ledger_root=ledger_root,
            )

            runner.run([{"symbol": "BTCUSDT", "price": 100.0, "side": "buy", "conviction": 0.9}], date="20260704")
            result = runner.run([{"symbol": "BTCUSDT", "price": 120.0, "side": "sell", "conviction": 0.9}], date="20260704")

            metric = result["style_comparison"][0]
            self.assertEqual(metric["pnl_source"], "sim_ledger_mark_to_market")
            self.assertEqual(metric["pnl_metric_source"], "sim_ledger_realized_unrealized_samples")
            self.assertGreater(metric["realized_pnl"], 1500.0)
            self.assertGreater(metric["unrealized_pnl"], 300.0)
            self.assertAlmostEqual(metric["pnl"], metric["realized_pnl"] + metric["unrealized_pnl"], places=6)
            self.assertEqual(metric["win_rate"], 1.0)

    def test_copies_market_snapshot_fields_from_signal_to_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "styles"
            styles_dir.mkdir()
            (styles_dir / "balanced.json").write_text(json.dumps(STYLE), encoding="utf-8")
            simulator = CapturingSimulator()
            runner = StyleRunner("ashare", simulator, styles_dir=styles_dir, review_root=root / "review", record_ledger=False)

            runner.run(
                [
                    {
                        "symbol": "600000.SH",
                        "price": 10.0,
                        "side": "buy",
                        "conviction": 0.9,
                        "bar_volume": 1500,
                        "previous_close": 9.8,
                        "counterparty_profile": "retail_panic",
                    }
                ],
                date="20260704",
            )

            self.assertEqual(simulator.orders[0]["bar_volume"], 1500)
            self.assertEqual(simulator.orders[0]["previous_close"], 9.8)
            self.assertEqual(simulator.orders[0]["counterparty_profile"], "retail_panic")

    def test_pm_orders_keep_outcome_and_market_id_in_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "styles"
            review_root = root / "review"
            ledger_root = root / "ledger"
            styles_dir.mkdir()
            (styles_dir / "balanced.json").write_text(json.dumps(STYLE), encoding="utf-8")
            runner = StyleRunner(
                "pm",
                FilledSimulator(),
                styles_dir=styles_dir,
                review_root=review_root,
                ledger_root=ledger_root,
            )

            runner.run([{"market_id": "558943", "price": 0.2, "side": "buy", "outcome": "no", "conviction": 0.9}], date="20260704")

            journal = ledger_root / "pm" / "balanced" / "trade_journal.jsonl"
            rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(rows[0]["symbol"], "558943:no")
            self.assertEqual(rows[0]["market_id"], "558943")
            self.assertEqual(rows[0]["outcome"], "no")
            positions = json.loads((ledger_root / "pm" / "balanced" / "positions.json").read_text(encoding="utf-8"))
            self.assertIn("558943:no", positions["positions"])
            self.assertEqual(positions["positions"]["558943:no"]["market_id"], "558943")
            self.assertEqual(positions["positions"]["558943:no"]["outcome"], "no")


class SimLedgerTotalPnlTest(unittest.TestCase):
    def test_total_pnl_combines_realized_and_unrealized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SimLedger(Path(tmp) / "ledger", starting_cash=10_000.0)
            ledger.record_fill(
                {"order_id": "B1", "symbol": "BTCUSDT", "side": "buy"},
                {"fill_id": "F1", "order_id": "B1", "fill_qty": 1.0, "fill_price": 100.0, "fill_time": "2026-07-04T00:00:00+00:00"},
                fees={"total": 0.0},
            )
            ledger.record_fill(
                {"order_id": "S1", "symbol": "BTCUSDT", "side": "sell"},
                {"fill_id": "F2", "order_id": "S1", "fill_qty": 0.5, "fill_price": 120.0, "fill_time": "2026-07-04T00:00:00+00:00"},
                fees={"total": 0.0},
            )

            without_prices = ledger.total_pnl()
            self.assertEqual(without_prices["realized_pnl"], 10.0)
            self.assertEqual(without_prices["unrealized_pnl"], 0.0)
            self.assertEqual(without_prices["total_pnl"], 10.0)
            self.assertEqual(without_prices["pnl_samples"], [10.0])

            with_prices = ledger.total_pnl(prices={"BTCUSDT": 130.0})
            self.assertEqual(with_prices["realized_pnl"], 10.0)
            self.assertEqual(with_prices["unrealized_pnl"], 15.0)
            self.assertEqual(with_prices["total_pnl"], 25.0)
            self.assertEqual(with_prices["missing_mark_count"], 0)
            self.assertEqual(with_prices["pnl_samples"], [10.0, 15.0])

    def test_total_pnl_marks_missing_symbols_at_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SimLedger(Path(tmp) / "ledger", starting_cash=10_000.0)
            ledger.record_fill(
                {"order_id": "B1", "symbol": "BTCUSDT", "side": "buy"},
                {"fill_id": "F1", "order_id": "B1", "fill_qty": 1.0, "fill_price": 100.0, "fill_time": "2026-07-04T00:00:00+00:00"},
                fees={"total": 0.0},
            )

            result = ledger.total_pnl(prices={"ETHUSDT": 200.0})
            self.assertEqual(result["unrealized_pnl"], 0.0)
            self.assertEqual(result["missing_mark_count"], 1)


if __name__ == "__main__":
    unittest.main()
