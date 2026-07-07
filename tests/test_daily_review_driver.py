import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.accounting import position_ledger
from shared.review import benchmark, daily_review, sim_ledger_reader


class DailyReviewDriverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp_path = Path(self.tmpdir.name)
        self.shadow_dir = self.tmp_path / "shared" / "logs" / "shadow"
        self.filled_dir = self.tmp_path / "signals" / "filled"
        self.review_dir = self.tmp_path / "shared" / "review" / "data"
        self.ledger_dir = self.tmp_path / "shared" / "logs"
        self.sim_ledger_dir = self.ledger_dir / "sim_ledger"
        self.local_sim_trades = self.ledger_dir / "local_sim" / "local_sim_trades.jsonl"

        self._patch(daily_review, "SHADOW_TRADES_LOG", self.shadow_dir / "shadow_trades.jsonl")
        self._patch(daily_review, "FILLED_SIGNALS_DIR", self.filled_dir)
        self._patch(daily_review, "DAILY_LOG", self.review_dir / "daily_reviews.jsonl")
        self._patch(daily_review, "DIRECTION_HIT_LOG", self.review_dir / "direction_hit_reviews.jsonl")
        self._patch(sim_ledger_reader, "DEFAULT_SIM_LEDGER_ROOT", self.sim_ledger_dir)
        self._patch(sim_ledger_reader, "DEFAULT_LOCAL_SIM_TRADES", self.local_sim_trades)
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

    def _write_jsonl(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
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

    def test_daily_review_reads_unified_and_local_sim_ledgers(self) -> None:
        self._write_jsonl(
            self.sim_ledger_dir / "crypto" / "grid" / "trade_journal.jsonl",
            {
                "timestamp": "2026-06-30T10:15:00+00:00",
                "order_id": "SIM-CRYPTO-1",
                "fill_id": "FILL-1",
                "symbol": "BTCUSDT",
                "side": "buy",
                "fill_qty": 1,
                "fill_price": 65000,
                "realized_pnl": 25,
                "capital_layer": "simulated",
            },
        )
        self._write_jsonl(
            self.local_sim_trades,
            {
                "trade_id": "LSIM-1",
                "order_id": "SIM-ASHARE-1",
                "idempotency_key": "idem-1",
                "market": "ashare",
                "account": "ashare_server_sim",
                "trade_date": "2026-06-30",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "requested_price": 10.0,
                "filled_price": 10.01,
                "amount": 1001,
                "commission": 5,
                "stamp_duty": 0,
                "net_amount": 1006,
                "status": "filled",
                "source": "server_local_sim_backup",
                "created_at": "2026-06-30T10:35:00+00:00",
            },
        )

        lunch = daily_review.run_daily_review("20260630", session="lunch")
        close = daily_review.run_daily_review("20260630", session="close")

        self.assertEqual(lunch["capital_layer"], "simulated")
        self.assertEqual(close["capital_layer"], "simulated")
        self.assertFalse(lunch["stale"])
        self.assertFalse(close["stale"])
        self.assertEqual(close["review_trade_count"], 2)
        self.assertEqual(close["source_trade_counts"]["by_capital_layer"]["simulated"], 2)
        self.assertEqual(close["source_trade_counts"]["sample_quality"]["validation_sample_count"], 1)
        self.assertEqual(close["source_trade_counts"]["sample_quality"]["strategy_sample_valid_count"], 1)
        self.assertIn("crypto", close["capital_layer_reviews"]["simulated"]["market_reviews"])
        self.assertIn("ashare", close["capital_layer_reviews"]["simulated"]["market_reviews"])
        ashare_review = close["capital_layer_reviews"]["simulated"]["market_reviews"]["ashare"]
        self.assertEqual(ashare_review["trades"], 1)
        self.assertEqual(ashare_review["strategy_trades"], 0)
        self.assertEqual(ashare_review["validation_sample_count"], 1)

    def test_daily_review_ignores_retired_ashare_style_ledgers(self) -> None:
        self._write_jsonl(
            self.sim_ledger_dir / "ashare" / "aggressive" / "trade_journal.jsonl",
            {
                "timestamp": "2026-06-30T10:15:00+00:00",
                "order_id": "OLD-ASHARE-STYLE",
                "fill_id": "OLD-FILL",
                "symbol": "600000.SH",
                "side": "buy",
                "fill_qty": 100,
                "fill_price": 10.0,
                "realized_pnl": 99,
                "capital_layer": "simulated",
            },
        )
        self._write_jsonl(
            self.local_sim_trades,
            {
                "trade_id": "LSIM-1",
                "order_id": "SIM-ASHARE-1",
                "idempotency_key": "idem-1",
                "market": "ashare",
                "account": "ashare_server_sim",
                "trade_date": "2026-06-30",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "requested_price": 10.0,
                "filled_price": 10.01,
                "amount": 1001,
                "commission": 5,
                "stamp_duty": 0,
                "net_amount": 1006,
                "status": "filled",
                "source": "server_local_sim_backup",
                "created_at": "2026-06-30T10:35:00+00:00",
            },
        )

        close = daily_review.run_daily_review("20260630", session="close")

        self.assertEqual(close["review_trade_count"], 1)
        self.assertEqual(
            close["source_trade_counts"]["by_source"],
            {str(self.local_sim_trades): 1},
        )

    def test_daily_review_marks_after_hours_ashare_trade_as_validation_sample(self) -> None:
        self._write_jsonl(
            self.local_sim_trades,
            {
                "trade_id": "LSIM-AFTER-HOURS",
                "order_id": "SIM-AFTER-HOURS",
                "idempotency_key": "after-hours",
                "market": "ashare",
                "account": "ashare_server_sim",
                "trade_date": "2026-07-07",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "requested_price": 10.0,
                "filled_price": 10.01,
                "amount": 1001,
                "commission": 5,
                "stamp_duty": 0,
                "net_amount": 1006,
                "status": "filled",
                "source": "server_local_sim_backup",
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
                "created_at": "2026-07-07T08:26:30+00:00",
            },
        )

        close = daily_review.run_daily_review("20260707", session="close")

        quality = close["source_trade_counts"]["sample_quality"]
        self.assertEqual(quality["validation_sample_count"], 1)
        self.assertEqual(quality["strategy_sample_valid_count"], 0)
        self.assertEqual(quality["by_reason"], {"outside_ashare_regular_session": 1})
        ashare_review = close["capital_layer_reviews"]["simulated"]["market_reviews"]["ashare"]
        self.assertEqual(ashare_review["trades"], 1)
        self.assertEqual(ashare_review["strategy_trades"], 0)


if __name__ == "__main__":
    unittest.main()
