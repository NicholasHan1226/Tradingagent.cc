#!/usr/bin/env python3
"""Tests for API-backed simulated broker dispatch."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.execution import local_sim_ledger, sim_executor_registry
from shared.execution.sim_broker import SimResult, execute_sim_order


class SimBrokerV2Test(unittest.TestCase):
    def setUp(self) -> None:
        self._old_registry = dict(sim_executor_registry._SIM_EXECUTORS)
        sim_executor_registry._SIM_EXECUTORS.clear()

    def tearDown(self) -> None:
        sim_executor_registry._SIM_EXECUTORS.clear()
        sim_executor_registry._SIM_EXECUTORS.update(self._old_registry)

    def test_registered_executor_returns_sim_result_with_simulated_layer(self) -> None:
        captured: dict[str, dict[str, object]] = {}

        def stub_executor(
            order: dict[str, object],
            account: dict[str, object],
            config: dict[str, object],
        ) -> SimResult:
            captured["order"] = order
            captured["account"] = account
            captured["config"] = config
            return SimResult(
                status="filled",
                filled_qty=2,
                avg_price=101.25,
                fee=0.35,
                message="stub sim api receipt",
                capital_layer="real",
                account_type="real",
                order_id=str(order["order_id"]),
                market="crypto",
                raw_response={"api_order_id": "stub-123"},
            )

        sim_executor_registry.register_sim_executor("crypto", stub_executor)

        result = execute_sim_order(
            order={
                "order_id": "SIM-V2-1",
                "ts_code": "BTCUSDT",
                "side": "buy",
                "quantity": 2,
            },
            market="Crypto",
            account={"account_id": "sim-crypto"},
            config={"venue": "unit"},
        )

        self.assertIsInstance(result, SimResult)
        self.assertEqual(result.status, "filled")
        self.assertEqual(result.filled_qty, 2)
        self.assertEqual(result.avg_price, 101.25)
        self.assertEqual(result.fee, 0.35)
        self.assertEqual(result.message, "stub sim api receipt")
        self.assertEqual(result.capital_layer, "simulated")
        self.assertEqual(result.account_type, "simulated")
        self.assertEqual(result.order_id, "SIM-V2-1")
        self.assertEqual(result.market, "crypto")
        self.assertEqual(captured["order"]["capital_layer"], "simulated")
        self.assertEqual(captured["order"]["account_type"], "simulated")
        self.assertEqual(captured["account"]["account_type"], "simulated")
        self.assertEqual(captured["account"]["capital_layer"], "simulated")
        self.assertEqual(captured["config"]["account_type"], "simulated")
        self.assertEqual(captured["config"]["capital_layer"], "simulated")


    def test_ashare_pending_order_does_not_record_server_local_backup_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            patches = [
                patch.object(local_sim_ledger, "LOCAL_SIM_DIR", base),
                patch.object(local_sim_ledger, "LOCAL_SIM_TRADES", base / "local_sim_trades.jsonl"),
                patch.object(local_sim_ledger, "LOCAL_SIM_POSITIONS", base / "local_sim_positions.json"),
                patch.object(local_sim_ledger, "LOCAL_SIM_PNL", base / "local_sim_pnl.json"),
                patch.object(local_sim_ledger, "LOCAL_SIM_LOCK", base / ".local_sim.lock"),
                patch.object(local_sim_ledger, "LOCAL_SIM_POSITIONS_SNAPSHOT", base / "simulated_ashare_positions.json"),
                patch.object(local_sim_ledger, "LOCAL_SIM_RECEIPTS", base / "sim_execution_receipts.jsonl"),
            ]
            for p in patches:
                p.start()
                self.addCleanup(p.stop)

            def ashare_bridge_executor(order, account, config) -> SimResult:
                return SimResult(
                    status="pending",
                    filled_qty=0,
                    avg_price=0.0,
                    fee=0.0,
                    message="queued for Mini/Hermes simulated execution",
                    order_id=str(order["order_id"]),
                    market="ashare",
                    raw_response={"mode": "mini_bridge_pending"},
                )

            sim_executor_registry.register_sim_executor("ashare", ashare_bridge_executor)

            result = execute_sim_order(
                order={
                    "order_id": "SIM-ASHARE-DUAL-1",
                    "idempotency_key": "SIM:ashare:acct:20260704:600000.SH:buy",
                    "ts_code": "600000.SH",
                    "side": "buy",
                    "quantity": 100,
                    "price": 10.0,
                },
                market="ashare",
                account={"account": "ashare_sim"},
                config={"local_sim_slippage_bps": 0},
            )

            self.assertEqual(result.status, "pending")
            backup = result.raw_response.get("local_sim_backup", {})
            self.assertEqual(backup, {})
            pnl = local_sim_ledger.get_local_sim_pnl("ashare_sim")
            self.assertEqual(pnl["total_trades"], 0)
            self.assertEqual(pnl["positions"], {})
            self.assertFalse((base / "sim_execution_receipts.jsonl").exists())


    def test_ashare_builtin_executor_loads_without_prior_import_and_records_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            patches = [
                patch.object(local_sim_ledger, "LOCAL_SIM_DIR", base),
                patch.object(local_sim_ledger, "LOCAL_SIM_TRADES", base / "local_sim_trades.jsonl"),
                patch.object(local_sim_ledger, "LOCAL_SIM_POSITIONS", base / "local_sim_positions.json"),
                patch.object(local_sim_ledger, "LOCAL_SIM_PNL", base / "local_sim_pnl.json"),
                patch.object(local_sim_ledger, "LOCAL_SIM_LOCK", base / ".local_sim.lock"),
                patch.object(local_sim_ledger, "LOCAL_SIM_POSITIONS_SNAPSHOT", base / "simulated_ashare_positions.json"),
                patch.object(local_sim_ledger, "LOCAL_SIM_RECEIPTS", base / "sim_execution_receipts.jsonl"),
            ]
            for p in patches:
                p.start()
                self.addCleanup(p.stop)

            result = execute_sim_order(
                order={
                    "order_id": "SIM-ASHARE-BUILTIN-1",
                    "ts_code": "600000.SH",
                    "side": "buy",
                    "quantity": 100,
                    "price": 10.0,
                },
                market="ashare",
                account={"account": "ashare_sim"},
                config={"local_sim_slippage_bps": 0},
            )

            self.assertEqual(result.status, "filled")
            self.assertEqual(result.raw_response.get("mode"), "server_local_sim_engine")
            self.assertTrue(result.raw_response.get("local_sim_backup", {}).get("recorded"))
            self.assertTrue((base / "local_sim_trades.jsonl").exists())
            self.assertTrue((base / "simulated_ashare_positions.json").exists())
            self.assertTrue((base / "sim_execution_receipts.jsonl").exists())

    def test_ashare_builtin_executor_accepts_string_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            patches = [
                patch.object(local_sim_ledger, "LOCAL_SIM_DIR", base),
                patch.object(local_sim_ledger, "LOCAL_SIM_TRADES", base / "local_sim_trades.jsonl"),
                patch.object(local_sim_ledger, "LOCAL_SIM_POSITIONS", base / "local_sim_positions.json"),
                patch.object(local_sim_ledger, "LOCAL_SIM_PNL", base / "local_sim_pnl.json"),
                patch.object(local_sim_ledger, "LOCAL_SIM_LOCK", base / ".local_sim.lock"),
                patch.object(local_sim_ledger, "LOCAL_SIM_POSITIONS_SNAPSHOT", base / "simulated_ashare_positions.json"),
                patch.object(local_sim_ledger, "LOCAL_SIM_RECEIPTS", base / "sim_execution_receipts.jsonl"),
            ]
            for p in patches:
                p.start()
                self.addCleanup(p.stop)

            result = execute_sim_order(
                order={
                    "order_id": "SIM-ASHARE-STRING-ACCOUNT",
                    "ts_code": "600000.SH",
                    "side": "buy",
                    "quantity": 100,
                    "price": 10.0,
                },
                market="ashare",
                account="ashare_sim",
                config={"local_sim_slippage_bps": 0},
            )

            self.assertEqual(result.status, "filled")
            backup = result.raw_response.get("local_sim_backup", {})
            self.assertTrue(backup.get("recorded"), backup)
            self.assertEqual(backup.get("account"), "ashare_sim")
            snapshot = local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8")
            self.assertIn("600000.SH", snapshot)

    def test_execute_sim_order_rejects_real_payload_before_sanitizing(self) -> None:
        calls: list[object] = []

        def stub_executor(order, account, config) -> SimResult:
            calls.append((order, account, config))
            return SimResult(status="filled", filled_qty=1, avg_price=1.0)

        sim_executor_registry.register_sim_executor("crypto", stub_executor)

        result = execute_sim_order(
            order={"order_id": "SIM-V2-REAL", "ts_code": "BTCUSDT", "quantity": 1},
            market="Crypto",
            account={"account_id": "sim-crypto", "account_type": "real"},
            config={},
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("real/live execution is rejected", result.message)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
