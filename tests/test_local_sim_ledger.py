from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.execution import local_sim_ledger


class LocalSimLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        for name, value in (
            ("LOCAL_SIM_DIR", base),
            ("LOCAL_SIM_TRADES", base / "local_sim_trades.jsonl"),
            ("LOCAL_SIM_POSITIONS", base / "local_sim_positions.json"),
            ("LOCAL_SIM_PNL", base / "local_sim_pnl.json"),
            ("LOCAL_SIM_LOCK", base / ".local_sim.lock"),
            ("LOCAL_SIM_POSITIONS_SNAPSHOT", base / "simulated_ashare_positions.json"),
        ):
            patcher = patch.object(local_sim_ledger, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_records_ashare_backup_fill_once_by_idempotency_key(self) -> None:
        order = {
            "order_id": "SIM-1",
            "idempotency_key": "SIM:ashare:acct:20260701:600000.SH:buy",
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": 100,
            "price": 10,
        }
        first = local_sim_ledger.record_local_sim_order(order, "ashare", {"account": "acct"}, {"local_sim_slippage_bps": 0})
        second = local_sim_ledger.record_local_sim_order(order, "ashare", {"account": "acct"}, {"local_sim_slippage_bps": 0})

        self.assertEqual(first["status"], "filled")
        self.assertTrue(first["recorded"])
        self.assertEqual(second["status"], "duplicate")
        pnl = local_sim_ledger.get_local_sim_pnl("acct")
        self.assertEqual(pnl["positions"]["600000.SH"]["quantity"], 100)
        self.assertEqual(pnl["market_value"], 1000.0)
        snapshot = json.loads(local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["positions"][0]["ts_code"], "600000.SH")
        self.assertEqual(snapshot["positions"][0]["account"], "acct")

    def test_rejects_non_regular_ashare_code(self) -> None:
        result = local_sim_ledger.record_local_sim_order({"ts_code": "200011.SZ", "side": "buy", "quantity": 100, "price": 1}, "ashare")
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["recorded"])


if __name__ == "__main__":
    unittest.main()
