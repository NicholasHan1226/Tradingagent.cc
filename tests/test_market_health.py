from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.runtime_test import market_health


class MarketHealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = patch.object(market_health, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_json(self, rel: str, payload: object) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_signal_queue_isolation_fails_when_shadow_leaks_into_execution_pending(self) -> None:
        self._write_json("signals/pending/SHADOW-ashare-000001.json", {"capital_layer": "shadow", "order_id": "SHADOW-1"})

        check = market_health._check_signal_queues()

        self.assertEqual(check.status, "fail")
        self.assertEqual(check.details["execution_queue"]["pending"], 1)
        self.assertIn("signals/pending/SHADOW-ashare-000001.json", check.details["leaked_shadow_sample"])

    def test_signal_queue_isolation_passes_when_shadow_uses_shadow_subqueue(self) -> None:
        self._write_json("signals/shadow/pending/SHADOW-ashare-000001.json", {"capital_layer": "shadow", "order_id": "SHADOW-1"})

        check = market_health._check_signal_queues()

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["execution_queue"]["pending"], 0)
        self.assertEqual(check.details["shadow_queue"]["pending"], 1)

    def test_shadow_ledger_passes_when_no_shadow_trades_exist(self) -> None:
        check = market_health._check_shadow_ledger()

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["ashare_pnl"]["total_trades"], 0)
        self.assertEqual(check.details["ashare_pnl"]["valuation_source"], "shadow_broker_replay")

    def test_shadow_ledger_detects_invalid_ashare_codes_and_missing_pnl_fields(self) -> None:
        self._write_json("shared/logs/shadow/shadow_pnl.json", {"ashare_shadow": {"positions": {"200011.SZ": {}}}})
        self._write_json("shared/logs/shadow/shadow_positions.json", {"ashare_shadow": {"200011.SZ": {}}})
        path = self.root / "shared/logs/shadow/shadow_trades.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"ts_code":"200011.SZ"}\n', encoding="utf-8")

        check = market_health._check_shadow_ledger()

        self.assertEqual(check.status, "fail")
        self.assertGreater(check.details["invalid_ashare_code_matches"]["shared/logs/shadow/shadow_pnl.json"], 0)
        self.assertIn("total_pnl", check.details["missing_pnl_fields"])

    def test_shadow_ledger_passes_with_clean_pnl_fields(self) -> None:
        self._write_json(
            "shared/logs/shadow/shadow_pnl.json",
            {"ashare_shadow": {"realized_pnl": 0, "unrealized_pnl": 1, "market_value": 100, "total_pnl": 1, "valuation_source": "unit"}},
        )
        self._write_json("shared/logs/shadow/shadow_positions.json", {"ashare_shadow": {"000001.SZ": {}}})
        path = self.root / "shared/logs/shadow/shadow_trades.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"ts_code":"000001.SZ"}\n', encoding="utf-8")

        check = market_health._check_shadow_ledger()

        self.assertEqual(check.status, "pass")


if __name__ == "__main__":
    unittest.main()
