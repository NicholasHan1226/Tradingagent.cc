import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.execution import shadow_broker
from shared.review import benchmark, daily_review


class MarketFieldTest(unittest.TestCase):
    def _patch_shadow_paths(self, tmp_path: Path) -> None:
        shadow_dir = tmp_path / "shadow"
        patches = (
            ("SHADOW_DIR", shadow_dir),
            ("SHADOW_TRADES", shadow_dir / "shadow_trades.jsonl"),
            ("SHADOW_POSITIONS", shadow_dir / "shadow_positions.json"),
            ("SHADOW_PNL", shadow_dir / "shadow_pnl.json"),
            ("SHADOW_LOCK", shadow_dir / ".shadow.lock"),
        )
        for name, value in patches:
            patcher = patch.object(shadow_broker, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _patch_review_paths(self, tmp_path: Path) -> None:
        patches = (
            (daily_review, "DAILY_LOG", tmp_path / "daily_reviews.jsonl"),
            (benchmark, "LAST_PERIOD_STORE", tmp_path / "last_period_return.json"),
            (benchmark, "BENCHMARK_STORE", tmp_path / "benchmark_history.json"),
        )
        for module, name, value in patches:
            patcher = patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_shadow_broker_filters_four_markets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._patch_shadow_paths(tmp_path)

            for market, symbol in (
                ("ashare", "600519.SH"),
                ("crypto", "BTCUSDT"),
                ("us", "AAPL"),
                ("pm", "will-btc-hit-100k"),
            ):
                result = shadow_broker.record_shadow(
                    {
                        "ts_code": symbol,
                        "side": "buy",
                        "quantity": 1,
                        "price": 10.0,
                        "trade_date": "2026-06-30",
                        "capital_layer": "shadow",
                    },
                    "multi_market_strategy",
                    market=market,
                )
                self.assertTrue(result["recorded"])
                self.assertEqual(result["market"], market)

            shadow_broker.SHADOW_DIR.mkdir(parents=True, exist_ok=True)
            with shadow_broker.SHADOW_TRADES.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "trade_id": "SHADOW-legacy",
                            "strategy_name": "legacy_strategy",
                            "trade_date": "2026-06-30",
                            "ts_code": "LEGACY",
                            "side": "buy",
                            "quantity": 1,
                            "price": 1.0,
                            "amount": 1.0,
                            "net_amount": 6.0,
                            "capital_layer": "shadow",
                            "created_at": "2026-06-30T10:00:00",
                        }
                    )
                    + "\n"
                )

            all_pnl = shadow_broker.get_shadow_pnl("multi_market_strategy", "2026-06-30")
            self.assertEqual(all_pnl["total_trades"], 4)
            self.assertEqual(all_pnl["market"], "all")

            for market in ("ashare", "crypto", "us", "pm"):
                pnl = shadow_broker.get_shadow_pnl("multi_market_strategy", "2026-06-30", market=market)
                self.assertEqual(pnl["market"], market)
                self.assertEqual(pnl["total_trades"], 1)
                self.assertEqual(pnl["buys"], 1)

            us_pnl = shadow_broker.get_all_shadow_pnl("2026-06-30", market="us")
            self.assertEqual(us_pnl["multi_market_strategy"]["total_trades"], 1)
            self.assertEqual(us_pnl["legacy_strategy"]["total_trades"], 0)

            legacy_unknown = shadow_broker.get_shadow_pnl("legacy_strategy", "2026-06-30", market="unknown")
            self.assertEqual(legacy_unknown["market"], "unknown")
            self.assertEqual(legacy_unknown["total_trades"], 1)

    def test_daily_review_outputs_market_reviews_without_mixing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._patch_review_paths(tmp_path)

            trades = [
                {"ts_code": "600519.SH", "market": "ashare", "pnl": 1.0, "capital_layer": "shadow"},
                {"ts_code": "BTCUSDT", "market": "crypto", "pnl": 2.0, "capital_layer": "shadow"},
                {"ts_code": "AAPL", "market": "us", "pnl": -1.0, "capital_layer": "shadow"},
                {"ts_code": "PM-1", "market": "pm", "pnl": 3.0, "capital_layer": "shadow"},
            ]

            result = daily_review.review_close(trades, [], benchmark_return=0.0)

            self.assertEqual(set(result["market_reviews"]), {"ashare", "crypto", "us", "pm"})
            self.assertEqual(set(result["capital_layer_reviews"]["shadow"]["market_reviews"]), {"ashare", "crypto", "us", "pm"})
            self.assertEqual(result["capital_layer_reviews"]["shadow"]["trades_summary"]["count"], 4)

            for market, expected_pnl in (("ashare", 1.0), ("crypto", 2.0), ("us", -1.0), ("pm", 3.0)):
                market_review = result["market_reviews"][market]
                self.assertEqual(market_review["trades"], 1)
                self.assertAlmostEqual(market_review["pnl"], expected_pnl)
                self.assertEqual(market_review["capital_layers"], ["shadow"])
                self.assertEqual(
                    result["capital_layer_reviews"]["shadow"]["market_reviews"][market]["trades"],
                    1,
                )

            rows = [
                json.loads(line)
                for line in daily_review.DAILY_LOG.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(rows), 4)
            self.assertEqual({row["market"] for row in rows}, {"ashare", "crypto", "us", "pm"})
            self.assertTrue(all(row["capital_layer"] == "shadow" for row in rows))


if __name__ == "__main__":
    unittest.main()
