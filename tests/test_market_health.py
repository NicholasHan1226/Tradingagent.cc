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

    def test_sim_market_loop_passes_with_cron_data_and_ledger(self) -> None:
        ledger = self.root / "shared/logs/sim_ledger/crypto/grid/trade_journal.jsonl"
        ledger.parent.mkdir(parents=True)
        ledger.write_text('{"order_id":"1"}\n', encoding="utf-8")
        log = self.root / "shared/logs/cron/crypto_sim.log"
        log.parent.mkdir(parents=True)
        log.write_text('noise\n{"market":"crypto","status":"ok","signals":5}\n', encoding="utf-8")

        with patch.object(market_health, "_probe_market_data", return_value={"status": "ok", "priced_signal_count": 5}):
            check = market_health._check_sim_market_loop("crypto", "job_crypto_sim.sh")

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["ledger"]["trade_rows"], 1)
        self.assertEqual(check.details["latest_cron_result"]["payload"]["signals"], 5)

    def test_sim_market_loop_fails_when_hk_has_no_data(self) -> None:
        with patch.object(market_health, "_probe_market_data", return_value={"status": "fail", "priced_signal_count": 0}):
            check = market_health._check_sim_market_loop("hk", "job_hk_sim.sh")

        self.assertEqual(check.status, "fail")
        self.assertIn("market_data_missing", check.details["fail_reasons"])

    def test_hk_sim_market_loop_warns_when_using_proxy_with_ledger(self) -> None:
        ledger = self.root / "shared/logs/sim_ledger/hk/grid/trade_journal.jsonl"
        ledger.parent.mkdir(parents=True)
        ledger.write_text('{"order_id":"1","symbol":"HSI"}\n', encoding="utf-8")

        with patch.object(
            market_health,
            "_probe_market_data",
            return_value={"status": "warn", "proxy": "HSI", "proxy_priced_signal_count": 1},
        ):
            check = market_health._check_sim_market_loop("hk", "job_hk_sim.sh")

        self.assertEqual(check.status, "warn")
        self.assertEqual(check.details["ledger"]["trade_rows"], 1)
        self.assertIn("market_data_degraded", check.details["warn_reasons"])

    def test_ashare_sim_loop_warns_without_production_trade_sample(self) -> None:
        with patch.object(market_health, "_probe_market_data", return_value={"status": "ok", "asset_count": 10}):
            check = market_health._check_sim_market_loop("ashare", "job_ashare_sim_exec.sh")

        self.assertEqual(check.status, "warn")
        self.assertIn("server_local_sim_has_no_production_trades_yet", check.details["warn_reasons"])


if __name__ == "__main__":
    unittest.main()
