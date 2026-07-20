from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.review import benchmark, daily_review


class FakeSharedSignalsReader:
    closes = {
        "600000": 10.5,
        "000001": 19.0,
        "300001": 29.0,
    }

    def get_bars_daily(
        self,
        market: str,
        symbol: str,
        start: object = None,
        end: object = None,
    ) -> list[dict[str, object]]:
        close = self.closes.get(symbol)
        if close is None:
            return []
        return [{"trade_date": "20260630", "close": close}]


class PredictionCalibrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp_path = Path(self.tmpdir.name)
        self.shadow_log = (
            self.tmp_path / "shared" / "logs" / "shadow" / "shadow_trades.jsonl"
        )
        self.review_data = self.tmp_path / "shared" / "review" / "data"

        self._patch(daily_review, "SHADOW_TRADES_LOG", self.shadow_log)
        self._patch(
            daily_review,
            "DIRECTION_HIT_LOG",
            self.review_data / "direction_hit_reviews.jsonl",
        )
        self._patch(daily_review, "DAILY_LOG", self.review_data / "daily_reviews.jsonl")
        self._patch(
            daily_review, "FILLED_SIGNALS_DIR", self.tmp_path / "signals" / "filled"
        )
        self._patch(daily_review, "SharedSignalsReader", FakeSharedSignalsReader)
        self._patch(
            benchmark, "LAST_PERIOD_STORE", self.review_data / "last_period_return.json"
        )
        self._patch(
            benchmark, "BENCHMARK_STORE", self.review_data / "benchmark_history.json"
        )
        self._patch(daily_review, "load_positions", lambda as_of_date: [])

    def _patch(self, module: object, name: str, value: object) -> None:
        patcher = patch.object(module, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _append_trade(self, payload: dict[str, object]) -> None:
        self.shadow_log.parent.mkdir(parents=True, exist_ok=True)
        with self.shadow_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def test_load_direction_hits_writes_layered_direction_accuracy(self) -> None:
        self._append_trade(
            {
                "trade_date": "2026-06-30",
                "created_at": "2026-06-30T10:00:00",
                "ts_code": "600000.SH",
                "market": "Ashare",
                "side": "buy",
                "price": 10.0,
                "capital_layer": "paper",
                "account_scope": "ashare-shadow",
                "strategy": "trend",
            }
        )
        self._append_trade(
            {
                "trade_date": "2026-06-30",
                "created_at": "2026-06-30T11:00:00",
                "ts_code": "000001.SZ",
                "market": "Ashare",
                "side": "sell",
                "price": 20.0,
                "capital_layer": "shadow",
                "account_scope": "ashare-shadow",
                "strategy": "reversal",
            }
        )
        self._append_trade(
            {
                "trade_date": "2026-06-30",
                "created_at": "2026-06-30T14:00:00",
                "ts_code": "300001.SZ",
                "market": "Ashare",
                "side": "buy",
                "price": 30.0,
                "capital_layer": "shadow",
                "account_scope": "ashare-shadow",
                "strategy": "breakout",
            }
        )

        records = daily_review.load_direction_hits("20260630")

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["capital_layer"], "shadow")
        self.assertEqual(record["market"], "ashare")
        self.assertEqual(record["account_scope"], "ashare-shadow")
        self.assertEqual(record["evaluated_count"], 3)
        self.assertEqual(record["hits"], 2)
        self.assertAlmostEqual(record["direction_accuracy"], 0.6667)
        self.assertEqual(
            {item["capital_layer"] for item in record["reviews"]}, {"shadow"}
        )

        written = [
            json.loads(line)
            for line in daily_review.DIRECTION_HIT_LOG.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        self.assertEqual(written, records)

    def test_close_daily_review_includes_direction_hit_reviews(self) -> None:
        self._append_trade(
            {
                "trade_date": "2026-06-30",
                "created_at": "2026-06-30T10:00:00",
                "ts_code": "600000.SH",
                "market": "Ashare",
                "side": "buy",
                "price": 10.0,
                "pnl": 1.0,
                "capital_layer": "shadow",
                "account_scope": "ashare-shadow",
            }
        )

        result = daily_review.run_daily_review("20260630", session="close")

        self.assertEqual(result["review_outcome_count"], 1)
        self.assertEqual(result["direction_hit_reviews"][0]["hits"], 1)
        self.assertEqual(result["direction_hit_reviews"][0]["capital_layer"], "shadow")
        self.assertEqual(
            result["direction_hit_reviews"][0]["account_scope"],
            "ashare-shadow",
        )

    def test_direction_hit_without_account_scope_is_not_published(self) -> None:
        self._append_trade(
            {
                "trade_date": "2026-06-30",
                "created_at": "2026-06-30T10:00:00",
                "ts_code": "600000.SH",
                "market": "Ashare",
                "side": "buy",
                "price": 10.0,
                "capital_layer": "shadow",
            }
        )

        self.assertEqual(daily_review.load_direction_hits("20260630"), [])
        self.assertFalse(daily_review.DIRECTION_HIT_LOG.exists())


if __name__ == "__main__":
    unittest.main()
