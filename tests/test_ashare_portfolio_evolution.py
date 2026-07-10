from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.assertEqual(report["state"], "evidence_pending")
        self.assertEqual(report["strategy_sample_count"], 1)
        self.assertEqual(report["evolution_evidence"]["eligible_sample_count"], 0)
        self.assertIn("weak_fill_price_evidence", report["evolution_evidence"]["blockers"])
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
            mark_prices={"600000.SH": 10.0},
        )

        latest = self.review_dir / "portfolio_evolution_latest.json"
        log = self.review_dir / "portfolio_evolution_log.jsonl"
        self.assertTrue(latest.exists())
        self.assertTrue(log.exists())
        self.assertEqual(json.loads(latest.read_text(encoding="utf-8"))["state"], report["state"])
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)
        decision = json.loads((self.review_dir / "evolution_decision_latest.json").read_text(encoding="utf-8"))
        self.assertEqual(decision["recommended_action"], "observe_and_label_candidates")
        self.assertEqual(decision["state"], "evidence_pending")
        self.assertEqual(decision["policy"]["min_evolution_evidence_samples"], 20)
        tier_accounts = {row["account"] for row in report["tier_experiments"]["accounts"]}
        self.assertEqual(tier_accounts, {"ashare_50000", "ashare_100000"})
        ranking_names = {row["style_name"] for row in report["rankings"]}
        self.assertIn("ashare_50000", ranking_names)
        self.assertIn("ashare_100000", ranking_names)

    def test_write_uses_same_mark_prices_for_tier_ledgers_and_pnl_summary(self) -> None:
        self._write_trade(
            {
                "trade_id": "LSIM-B",
                "order_id": "SIM-B",
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

        expected_prices = {"600000.SH": 12.34}
        captured: dict[str, object] = {}

        def fake_refresh(local_trades_path: Path | None) -> dict[str, object]:
            return {"status": "ok", "mark_prices": expected_prices}

        def fake_write_tier_ledgers(*, mark_prices: object = None, **kwargs: object) -> dict[str, object]:
            captured["tier_ledgers_mark_prices"] = mark_prices
            return {"accounts": []}

        def fake_sim_ledger_pnl_summary(*, ashare_mark_prices: object = None, **kwargs: object) -> dict[str, object]:
            captured["pnl_summary_mark_prices"] = ashare_mark_prices
            return {"ashare": {}}

        with patch("Ashare.portfolio_evolution._refresh_local_sim_snapshot_for_review", fake_refresh), \
             patch("Ashare.tier_experiments.write_tier_ledgers", fake_write_tier_ledgers), \
             patch("Ashare.portfolio_evolution.sim_ledger_pnl_summary", fake_sim_ledger_pnl_summary):
            write_portfolio_evolution(
                trade_date="20260709",
                review_dir=self.review_dir,
                local_trades_path=self.local_trades,
            )

        self.assertIs(captured["tier_ledgers_mark_prices"], expected_prices)
        self.assertIs(captured["pnl_summary_mark_prices"], expected_prices)

    def test_write_does_not_refresh_tier_ledgers_without_mark_prices(self) -> None:
        self._write_trade(
            {
                "trade_id": "LSIM-NO-MARK",
                "order_id": "SIM-NO-MARK",
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

        with patch(
            "Ashare.portfolio_evolution._refresh_local_sim_snapshot_for_review",
            return_value={"status": "skipped", "reason": "no_mark_prices"},
        ), patch("Ashare.tier_experiments.write_tier_ledgers") as tier_writer:
            report = write_portfolio_evolution(
                trade_date="20260709",
                review_dir=self.review_dir,
                local_trades_path=self.local_trades,
            )

        tier_writer.assert_not_called()
        self.assertEqual(report["valuation_status"], "unavailable")
        self.assertEqual(report["tier_experiments"]["account_count"], 0)
        self.assertIn("mark_prices_unavailable", report["evolution_evidence"]["blockers"])
        self.assertEqual(report["tier_experiment_refresh"]["reason"], "no_mark_prices")


if __name__ == "__main__":
    unittest.main()
