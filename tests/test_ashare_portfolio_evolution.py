from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from Ashare.portfolio_evolution import build_portfolio_evolution, write_portfolio_evolution


class AsharePortfolioEvolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.local_trades = self.root / "local_sim_trades.jsonl"
        self.review_dir = self.root / "review" / "ashare"

    def _write_trade(self, payload: dict[str, object]) -> None:
        self.local_trades.parent.mkdir(parents=True, exist_ok=True)
        with self.local_trades.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def test_build_records_strategy_samples_without_style_attribution(self) -> None:
        self._write_trade(
            {
                "trade_id": "LSIM-A",
                "order_id": "SIM-A",
                "market": "ashare",
                "account": "ashare_server_sim",
                "trade_date": "2026-07-09",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "filled_price": 10.0,
                "amount": 1000.0,
                "commission": 5.0,
                "net_amount": 1005.0,
                "status": "filled",
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
                "fill_price_source": "signal_card.price",
                "fill_price_source_class": "signal_card_price",
                "trade_timestamp_bj": "2026-07-09T10:00:00+08:00",
            }
        )

        report = build_portfolio_evolution(
            trade_date="20260709",
            review_dir=self.review_dir,
            local_trades_path=self.local_trades,
            min_samples=5,
        )

        self.assertEqual(report["market"], "ashare")
        self.assertEqual(report["state"], "sample_insufficient")
        self.assertEqual(report["strategy_sample_count"], 1)
        self.assertEqual(report["today_strategy_sample_count"], 1)
        self.assertEqual(report["rankings"][0]["style_name"], "ashare_portfolio")
        self.assertEqual(report["rankings"][0]["trades"], 1)
        self.assertEqual(report["weights"], {"ashare_portfolio": {"status": "active", "weight": 1.0, "scope": "portfolio_account"}})
        self.assertEqual(report["actions"][0]["action"], "observe")

    def test_write_updates_latest_and_log(self) -> None:
        self._write_trade(
            {
                "trade_id": "LSIM-A",
                "order_id": "SIM-A",
                "market": "ashare",
                "account": "ashare_server_sim",
                "trade_date": "2026-07-09",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 1000,
                "requested_price": 10.0,
                "filled_price": 10.0,
                "amount": 10000.0,
                "commission": 5.0,
                "net_amount": 10005.0,
                "status": "filled",
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
                "fill_price_source": "signal_card.price",
                "fill_price_source_class": "signal_card_price",
                "trade_timestamp_bj": "2026-07-09T10:00:00+08:00",
            }
        )
        report = write_portfolio_evolution(
            trade_date="20260709",
            review_dir=self.review_dir,
            local_trades_path=self.local_trades,
        )

        latest = self.review_dir / "portfolio_evolution_latest.json"
        log = self.review_dir / "portfolio_evolution_log.jsonl"
        self.assertTrue(latest.exists())
        self.assertTrue(log.exists())
        self.assertEqual(json.loads(latest.read_text(encoding="utf-8"))["state"], report["state"])
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)
        tier_accounts = {row["account"] for row in report["tier_experiments"]["accounts"]}
        self.assertEqual(tier_accounts, {"ashare_50000", "ashare_100000"})
        ranking_names = {row["style_name"] for row in report["rankings"]}
        self.assertIn("ashare_50000", ranking_names)
        self.assertIn("ashare_100000", ranking_names)


if __name__ == "__main__":
    unittest.main()
