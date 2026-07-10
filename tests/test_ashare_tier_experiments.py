from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from Ashare.portfolio_evolution import _tier_rankings
from Ashare.tier_experiments import build_tier_ledger, write_tier_ledgers


class AshareTierExperimentsTest(unittest.TestCase):
    def _strategy_trade(self, quantity: int = 1000, price: float = 10.0) -> dict[str, object]:
        return {
            "trade_id": "LSIM-MAIN",
            "order_id": "SIM-MAIN",
            "idempotency_key": "main",
            "market": "ashare",
            "account": "ashare_server_sim",
            "trade_date": "2026-07-09",
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": quantity,
            "requested_price": price,
            "filled_price": price,
            "amount": round(quantity * price, 2),
            "commission": 5.0,
            "net_amount": round(quantity * price + 5.0, 2),
            "status": "filled",
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "fill_price_source": "signal_card.price",
            "fill_price_source_class": "signal_card_price",
            "trade_timestamp_bj": "2026-07-09T10:00:00+08:00",
        }

    def test_builds_independent_50k_tier_ledger_with_scaled_lot_size(self) -> None:
        ledger = build_tier_ledger([self._strategy_trade(quantity=1000, price=10.0)], capital=50_000.0)

        self.assertEqual(ledger["account"], "ashare_50000")
        self.assertEqual(ledger["trade_count"], 1)
        self.assertEqual(ledger["trades"][0]["account"], "ashare_50000")
        self.assertEqual(ledger["trades"][0]["quantity"], 200)
        self.assertEqual(ledger["pnl"]["cash_available"], 47_995.0)
        self.assertEqual(ledger["pnl"]["positions"]["600000.SH"]["quantity"], 200)

    def test_small_tier_skips_when_lot_size_or_cash_insufficient(self) -> None:
        ledger = build_tier_ledger([self._strategy_trade(quantity=100, price=800.0)], capital=50_000.0)

        self.assertEqual(ledger["trade_count"], 0)
        self.assertEqual(ledger["skipped_count"], 1)
        self.assertEqual(ledger["skipped"][0]["reason"], "tier_cash_or_lot_size_insufficient")

    def test_write_tier_ledgers_creates_independent_files_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "local_sim_trades.jsonl"
            source.write_text(json.dumps(self._strategy_trade(), ensure_ascii=False) + "\n", encoding="utf-8")

            report = write_tier_ledgers(
                source_trades_path=source,
                tier_root=root / "tiers",
                review_dir=root / "review",
            )

            self.assertEqual([row["account"] for row in report["accounts"]], ["ashare_50000", "ashare_100000"])
            self.assertTrue((root / "tiers" / "ashare_50000" / "local_sim_trades.jsonl").exists())
            self.assertTrue((root / "tiers" / "ashare_100000" / "local_sim_pnl.json").exists())
            manifest = json.loads((root / "review" / "tier_experiments_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["accounts"]), 2)

    def test_tiers_have_independent_cash_and_positions(self) -> None:
        ledger_50k = build_tier_ledger([self._strategy_trade(quantity=1000, price=10.0)], capital=50_000.0)
        ledger_100k = build_tier_ledger([self._strategy_trade(quantity=1000, price=10.0)], capital=100_000.0)

        self.assertNotEqual(
            ledger_50k["pnl"]["cash_available"],
            ledger_100k["pnl"]["cash_available"],
        )
        self.assertNotEqual(
            ledger_50k["pnl"]["positions"]["600000.SH"]["quantity"],
            ledger_100k["pnl"]["positions"]["600000.SH"]["quantity"],
        )
        self.assertEqual(ledger_50k["pnl"]["positions"]["600000.SH"]["quantity"], 200)
        self.assertEqual(ledger_100k["pnl"]["positions"]["600000.SH"]["quantity"], 500)

    def test_tier_ledgers_use_current_mark_prices_for_unrealized_pnl(self) -> None:
        ledger = build_tier_ledger(
            [self._strategy_trade(quantity=1000, price=10.0)],
            capital=50_000.0,
            mark_prices={"600000.SH": 11.0},
        )

        position = ledger["pnl"]["positions"]["600000.SH"]
        self.assertEqual(position["mark_price"], 11.0)
        self.assertEqual(position["market_value"], 2200.0)
        self.assertEqual(ledger["pnl"]["unrealized_pnl"], 195.0)
        self.assertEqual(ledger["pnl"]["total_pnl"], 195.0)

    def test_each_tier_has_independent_capital_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "local_sim_trades.jsonl"
            source.write_text(json.dumps(self._strategy_trade(), ensure_ascii=False) + "\n", encoding="utf-8")
            candidates = [
                {"ts_code": "600000.SH", "combined": 0.86},
                {"ts_code": "000001.SZ", "combined": 0.78},
            ]

            report = write_tier_ledgers(
                source_trades_path=source,
                tier_root=root / "tiers",
                review_dir=root / "review",
                candidates=candidates,
                market_context={
                    "trend": "bullish",
                    "risk_rejection_rate": 0.0,
                    "data_issue_rate": 0.0,
                    "recent_win_rate": 0.62,
                },
            )

            for account in report["accounts"]:
                self.assertIn("capital_plan", account)
                plan = account["capital_plan"]
                self.assertEqual(plan["risk_mode"], "aggressive")
                self.assertEqual(plan["max_new_positions"], 2)
                self.assertTrue(len(plan["suggested_buys"]) > 0)
                max_alloc = max(b["allocation"] for b in plan["suggested_buys"])
                self.assertLessEqual(max_alloc, account["capital"] * 0.35 + 1e-6)
                self.assertTrue((root / "tiers" / account["account"] / "capital_plan.json").exists())

    def test_tier_capital_plans_differ_by_capital(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "local_sim_trades.jsonl"
            source.write_text(json.dumps(self._strategy_trade(), ensure_ascii=False) + "\n", encoding="utf-8")
            candidates = [
                {"ts_code": "600000.SH", "combined": 0.86},
                {"ts_code": "000001.SZ", "combined": 0.78},
            ]

            report = write_tier_ledgers(
                source_trades_path=source,
                tier_root=root / "tiers",
                review_dir=root / "review",
                candidates=candidates,
                market_context={
                    "trend": "bullish",
                    "risk_rejection_rate": 0.0,
                    "data_issue_rate": 0.0,
                    "recent_win_rate": 0.62,
                },
            )

            plan_50k = next(a for a in report["accounts"] if a["account"] == "ashare_50000")["capital_plan"]
            plan_100k = next(a for a in report["accounts"] if a["account"] == "ashare_100000")["capital_plan"]
            self.assertNotEqual(plan_50k["cash_reserve"], plan_100k["cash_reserve"])
            self.assertNotEqual(plan_50k["position_budget_by_symbol"], plan_100k["position_budget_by_symbol"])

    def test_no_fake_style_attribution_in_tier_rankings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "local_sim_trades.jsonl"
            source.write_text(json.dumps(self._strategy_trade(), ensure_ascii=False) + "\n", encoding="utf-8")

            report = write_tier_ledgers(
                source_trades_path=source,
                tier_root=root / "tiers",
                review_dir=root / "review",
            )

            rankings = _tier_rankings(report)
            style_names = {r["style_name"] for r in rankings}
            self.assertEqual(style_names, {"ashare_50000", "ashare_100000"})
            for fake in {"aggressive", "balanced", "conservative", "cautious", "defensive"}:
                self.assertNotIn(fake, style_names)


if __name__ == "__main__":
    unittest.main()
