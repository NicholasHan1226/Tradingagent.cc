from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
