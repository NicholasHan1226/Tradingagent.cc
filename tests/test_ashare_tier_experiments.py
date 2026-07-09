from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
