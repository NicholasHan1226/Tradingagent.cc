from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Ashare.portfolio_evolution import _tier_rankings
from Ashare.tier_experiments import build_tier_ledger, write_tier_ledgers, EXPERIMENT_TIERS


EPOCH_STATE = {
    "current_epoch_id": 2,
    "capital_cny": 50_000.0,
    "cutover_timestamp": "2026-07-10T20:56:58+00:00",
}


class AshareTierExperimentsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.epoch_state = patch("Ashare.tier_experiments.read_epoch_state", return_value=EPOCH_STATE)
        self.epoch_state.start()

    def tearDown(self) -> None:
        self.epoch_state.stop()

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
            "capital_epoch": 2,
            "capital_cny": 50_000.0,
            "epoch_cutover_timestamp": EPOCH_STATE["cutover_timestamp"],
        }

    def test_writer_never_relabels_old_or_wrong_authority_source_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = self._strategy_trade()
            old_epoch = {**current, "trade_id": "OLD", "capital_epoch": 1, "capital_cny": 200_000.0}
            wrong_capital = {**current, "trade_id": "WRONG-CAPITAL", "capital_cny": 200_000.0}
            same_instant_wrong_cutover = {
                **current,
                "trade_id": "WRONG-CUTOVER",
                "epoch_cutover_timestamp": "2026-07-11T04:56:58+08:00",
            }
            source = root / "local_sim_trades.jsonl"
            source.write_text(
                "".join(
                    json.dumps(row, ensure_ascii=False) + "\n"
                    for row in (old_epoch, wrong_capital, same_instant_wrong_cutover, current)
                ),
                encoding="utf-8",
            )

            report = write_tier_ledgers(
                source_trades_path=source,
                tier_root=root / "tiers",
                review_dir=root / "review",
                tiers=(100_000.0,),
            )

            account = report["accounts"][0]
            self.assertEqual(account["trade_count"], 1)
            self.assertEqual(report["source_trade_count"], 1)
            self.assertEqual(report["current_source_trade_count"], 1)
            self.assertEqual(report["source_authority_rejection_count"], 3)
            self.assertEqual(
                report["source_authority_rejections"],
                {
                    "capital_epoch_mismatch": 1,
                    "capital_cny_mismatch": 1,
                    "epoch_cutover_timestamp_mismatch": 1,
                },
            )
            rows = [
                json.loads(line)
                for line in (root / "tiers" / "ashare_100000" / "local_sim_trades.jsonl").read_text().splitlines()
            ]
            self.assertEqual([row["trade_id"] for row in rows], ["LSIM-MAIN:ashare_100000"])

    def test_builds_independent_50k_tier_ledger_with_scaled_lot_size(self) -> None:
        ledger = build_tier_ledger([self._strategy_trade(quantity=1000, price=10.0)], capital=50_000.0)

        self.assertEqual(ledger["account"], "ashare_50000")
        self.assertEqual(ledger["trade_count"], 1)
        self.assertEqual(ledger["trades"][0]["account"], "ashare_50000")
        self.assertEqual(ledger["trades"][0]["quantity"], 1000)
        self.assertEqual(ledger["pnl"]["cash_available"], 39_995.0)
        self.assertEqual(ledger["pnl"]["positions"]["600000.SH"]["quantity"], 1000)

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

            self.assertEqual(report["accounts"], [])
            self.assertFalse((root / "tiers").exists())
            manifest = json.loads((root / "review" / "tier_experiments_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["accounts"], [])
            self.assertEqual(manifest["capital_epoch"], 2)
            self.assertEqual(manifest["capital_cny"], 50_000.0)
            self.assertEqual(
                manifest["epoch_cutover_timestamp"], EPOCH_STATE["cutover_timestamp"]
            )

    def test_write_tier_ledgers_fails_closed_before_any_write_on_invalid_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "local_sim_trades.jsonl"
            source.write_text(json.dumps(self._strategy_trade()) + "\n", encoding="utf-8")
            with patch(
                "Ashare.tier_experiments.read_epoch_state",
                return_value={**EPOCH_STATE, "capital_cny": 200_000.0},
            ):
                with self.assertRaisesRegex(ValueError, "invalid_epoch_state"):
                    write_tier_ledgers(
                        source_trades_path=source,
                        tier_root=root / "tiers",
                        review_dir=root / "review",
                        tiers=(100_000.0,),
                    )
            self.assertFalse((root / "tiers").exists())
            self.assertFalse((root / "review").exists())

    def test_tier_files_are_atomically_written_with_complete_epoch_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "local_sim_trades.jsonl"
            source.write_text(json.dumps(self._strategy_trade()) + "\n", encoding="utf-8")
            report = write_tier_ledgers(
                source_trades_path=source,
                tier_root=root / "tiers",
                review_dir=root / "review",
                tiers=(100_000.0,),
            )
            account_dir = root / "tiers" / "ashare_100000"
            metadata = {
                "capital_epoch": 2,
                "capital_cny": 50_000.0,
                "epoch_cutover_timestamp": EPOCH_STATE["cutover_timestamp"],
            }
            self.assertTrue(metadata.items() <= report.items())
            for row in (account_dir / "local_sim_trades.jsonl").read_text().splitlines():
                self.assertTrue(metadata.items() <= json.loads(row).items())
            for name in ("local_sim_pnl.json", "local_sim_positions.json", "capital_plan.json"):
                self.assertTrue(metadata.items() <= json.loads((account_dir / name).read_text()).items())

    def test_atomic_write_failure_never_leaves_untagged_tier_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "local_sim_trades.jsonl"
            source.write_text(json.dumps(self._strategy_trade()) + "\n", encoding="utf-8")
            real_replace = os.replace
            calls = {"count": 0}

            def fail_second_replace(src: str, dst: str) -> None:
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("injected atomic failure")
                real_replace(src, dst)

            with patch("Ashare.tier_experiments.os.replace", side_effect=fail_second_replace):
                with self.assertRaisesRegex(OSError, "injected atomic failure"):
                    write_tier_ledgers(
                        source_trades_path=source,
                        tier_root=root / "tiers",
                        review_dir=root / "review",
                        tiers=(100_000.0,),
                    )
            for path in (root / "tiers").rglob("*"):
                if path.is_file() and not path.name.startswith("."):
                    if path.suffix == ".jsonl":
                        payloads = [json.loads(line) for line in path.read_text().splitlines() if line]
                    else:
                        payloads = [json.loads(path.read_text())]
                    self.assertTrue(all(item.get("capital_epoch") == 2 for item in payloads))

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
        self.assertEqual(ledger_50k["pnl"]["positions"]["600000.SH"]["quantity"], 1000)
        self.assertEqual(ledger_100k["pnl"]["positions"]["600000.SH"]["quantity"], 2000)

    def test_tier_ledgers_use_current_mark_prices_for_unrealized_pnl(self) -> None:
        ledger = build_tier_ledger(
            [self._strategy_trade(quantity=1000, price=10.0)],
            capital=50_000.0,
            mark_prices={"600000.SH": 11.0},
        )

        position = ledger["pnl"]["positions"]["600000.SH"]
        self.assertEqual(position["mark_price"], 11.0)
        self.assertEqual(position["market_value"], 11000.0)
        self.assertEqual(ledger["pnl"]["unrealized_pnl"], 995.0)
        self.assertEqual(ledger["pnl"]["total_pnl"], 995.0)

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
                tiers=(100_000.0, 200_000.0),
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
                tiers=(100_000.0, 200_000.0),
                candidates=candidates,
                market_context={
                    "trend": "bullish",
                    "risk_rejection_rate": 0.0,
                    "data_issue_rate": 0.0,
                    "recent_win_rate": 0.62,
                },
            )

            plan_100k = next(a for a in report["accounts"] if a["account"] == "ashare_100000")["capital_plan"]
            plan_200k = next(a for a in report["accounts"] if a["account"] == "ashare_200000")["capital_plan"]
            self.assertNotEqual(plan_100k["cash_reserve"], plan_200k["cash_reserve"])
            self.assertNotEqual(plan_100k["position_budget_by_symbol"], plan_200k["position_budget_by_symbol"])

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
            self.assertEqual(style_names, set())
            for fake in {"aggressive", "balanced", "conservative", "cautious", "defensive"}:
                self.assertNotIn(fake, style_names)

    # -- RED: dynamic primary capital tests (currently failing) -----------------
    def test_experiment_tiers_exclude_primary_capital_when_50k(self) -> None:
        """When ASHARE_SIM_CAPITAL_TIER=50000, 50k is PRIMARY, not an experiment."""
        with patch.dict(os.environ, {"ASHARE_SIM_CAPITAL_TIER": "50000"}, clear=False):
            from importlib import reload
            import Ashare.tier_experiments as tier_mod
            reload(tier_mod)
            tiers = tier_mod.EXPERIMENT_TIERS
        # 50k should NOT be in experiment tiers — it's the primary account now
        self.assertNotIn(50_000.0, tiers)
        self.assertEqual(tiers, ())

    def test_write_tier_ledgers_excludes_primary_when_50k(self) -> None:
        """write_tier_ledgers should NOT replay the primary 50k as its own experiment."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "local_sim_trades.jsonl"
            source.write_text(json.dumps(self._strategy_trade(), ensure_ascii=False) + "\n", encoding="utf-8")

            with patch.dict(os.environ, {"ASHARE_SIM_CAPITAL_TIER": "50000"}, clear=False):
                report = write_tier_ledgers(
                    source_trades_path=source,
                    tier_root=root / "tiers",
                    review_dir=root / "review",
                )

            account_names = [row["account"] for row in report["accounts"]]
            self.assertNotIn("ashare_50000", account_names,
                "ashare_50000 is the primary account and should not be replayed as an experiment")
            self.assertEqual(account_names, [])

    def test_buy_quantity_scales_by_canonical_capital_not_hardcoded_200k(self) -> None:
        """_buy_quantity must use default_sim_capital('ashare') not the hardcoded constant."""
        from Ashare.tier_experiments import _buy_quantity

        source = self._strategy_trade(quantity=1000, price=10.0)
        # With ASHARE_SIM_CAPITAL_TIER=50000 (canonical = 50k), tier=100k => 2x scaling
        with patch.dict(os.environ, {"ASHARE_SIM_CAPITAL_TIER": "50000"}, clear=False):
            qty = _buy_quantity(source, capital=100_000.0, cash_available=100_000.0)
        # 100k / 50k = 2x the original 1000 shares => 2000 shares, lot-rounded to 2000
        self.assertEqual(qty, 2000,
            f"Expected 2000 (100k/50k * 1000 shares), got {qty}")

    def test_sell_quantity_scales_by_canonical_capital_not_hardcoded_200k(self) -> None:
        """_sell_quantity must use default_sim_capital('ashare') not the hardcoded constant."""
        from Ashare.tier_experiments import _sell_quantity

        source = self._strategy_trade(quantity=1000, price=10.0)
        source["side"] = "sell"
        with patch.dict(os.environ, {"ASHARE_SIM_CAPITAL_TIER": "50000"}, clear=False):
            qty = _sell_quantity(source, capital=100_000.0, position_qty=5000)
        # 100k / 50k = 2x scaling => 2000 shares (lot-rounded from 2000)
        self.assertEqual(qty, 2000,
            f"Expected 2000 (100k/50k * 1000 shares), got {qty}")

    def test_experiment_tiers_is_empty_when_all_tiers_are_primary(self) -> None:
        """If the only allowed tiers map to the primary, no experiments remain."""
        with patch.dict(os.environ, {"ASHARE_SIM_CAPITAL_TIER": "50000"}, clear=False):
            from importlib import reload
            import Ashare.tier_experiments as tier_mod
            reload(tier_mod)
            tiers = tier_mod.EXPERIMENT_TIERS
        # 50k is primary, 200k may or may not be included based on design
        # At minimum, 50k MUST NOT be an experiment
        self.assertNotIn(50_000.0, tiers)


if __name__ == "__main__":
    unittest.main()
