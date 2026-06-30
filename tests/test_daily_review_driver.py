import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.accounting import position_ledger
from shared.review import benchmark, daily_review


class DailyReviewDriverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp_path = Path(self.tmpdir.name)
        self.shadow_dir = self.tmp_path / "shared" / "logs" / "shadow"
        self.filled_dir = self.tmp_path / "signals" / "filled"
        self.review_dir = self.tmp_path / "shared" / "review" / "data"
        self.ledger_dir = self.tmp_path / "shared" / "logs"

        self._patch(daily_review, "SHADOW_TRADES_LOG", self.shadow_dir / "shadow_trades.jsonl")
        self._patch(daily_review, "FILLED_SIGNALS_DIR", self.filled_dir)
        self._patch(daily_review, "DAILY_LOG", self.review_dir / "daily_reviews.jsonl")
        self._patch(benchmark, "LAST_PERIOD_STORE", self.review_dir / "last_period_return.json")
        self._patch(benchmark, "BENCHMARK_STORE", self.review_dir / "benchmark_history.json")
        self._patch(position_ledger, "LEDGER_DIR", self.ledger_dir)
        self._patch(position_ledger, "POSITION_CSV", self.ledger_dir / "position_ledger.csv")
        self._patch(position_ledger, "POSITION_LOCK", self.ledger_dir / "position_ledger.csv.lock")

    def _patch(self, module: object, name: str, value: object) -> None:
        patcher = patch.object(module, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_shadow_trade(self, payload: dict[str, object]) -> None:
        self.shadow_dir.mkdir(parents=True, exist_ok=True)
        with (self.shadow_dir / "shadow_trades.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def test_shadow_trade_driver_runs_lunch_and_close_without_legacy_ashare_reads(self) -> None:
        position_ledger.open_position(
            "600000.SH",
            100,
            10.0,
            capital_layer="shadow",
            entry_date="2026-06-30",
            note="unit-test",
        )
        self._write_shadow_trade({
            "trade_id": "SHADOW-1",
            "trade_date": "2026-06-30",
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": 100,
            "price": 10.0,
            "capital_layer": "paper",
            "created_at": "2026-06-30T10:30:00",
            "strategy_name": "trend",
            "pnl": 0.12,
        })
        self._write_shadow_trade({
            "trade_id": "SHADOW-2",
            "trade_date": "2026-06-30",
            "ts_code": "600000.SH",
            "side": "sell",
            "quantity": 100,
            "price": 10.5,
            "capital_layer": "shadow",
            "created_at": "2026-06-30T14:20:00",
            "strategy_name": "trend",
            "pnl": -0.03,
        })

        with patch.object(daily_review, "_read_csv_dicts", side_effect=AssertionError("legacy csv path should not be used")):
            lunch = daily_review.run_daily_review("20260630", session="lunch")
            close = daily_review.run_daily_review("20260630", session="close")

        self.assertEqual(lunch["capital_layer"], "shadow")
        self.assertFalse(lunch["stale"])
        self.assertEqual(lunch["capital_layer_reviews"]["shadow"]["capital_layer"], "shadow")
        self.assertIn("comparisons", lunch["capital_layer_reviews"]["shadow"])
        self.assertIn("next_plan", lunch["capital_layer_reviews"]["shadow"])

        self.assertEqual(close["capital_layer"], "shadow")
        self.assertFalse(close["stale"])
        self.assertEqual(close["capital_layer_reviews"]["shadow"]["capital_layer"], "shadow")
        self.assertIn("comparisons", close["capital_layer_reviews"]["shadow"])
        self.assertIn("next_day_plan", close["capital_layer_reviews"]["shadow"])

        rows = [
            json.loads(line)
            for line in daily_review.DAILY_LOG.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["capital_layer"] == "shadow" for row in rows))
        self.assertEqual({row["session"] for row in rows}, {"lunch", "close"})

    def test_daily_review_marks_stale_when_no_shadow_trades(self) -> None:
        lunch = daily_review.run_daily_review("20260630", session="lunch")
        close = daily_review.run_daily_review("20260630", session="close")

        self.assertTrue(lunch["stale"])
        self.assertTrue(close["stale"])
        self.assertEqual(lunch["capital_layer_reviews"]["shadow"]["capital_layer"], "shadow")
        self.assertEqual(close["capital_layer_reviews"]["shadow"]["capital_layer"], "shadow")


if __name__ == "__main__":
    unittest.main()
