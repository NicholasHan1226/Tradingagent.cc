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
            ("LOCAL_SIM_RECEIPTS", base / "sim_execution_receipts.jsonl"),
        ):
            patcher = patch.object(local_sim_ledger, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _valid_session(self):
        return patch.object(
            local_sim_ledger,
            "_ashare_session_metadata",
            return_value={
                "trade_timestamp_bj": "2026-07-08T10:00:00+08:00",
                "ashare_session_valid": True,
                "ashare_session_rejection": "",
            },
        )

    def test_records_ashare_backup_fill_once_by_idempotency_key(self) -> None:
        order = {
            "order_id": "SIM-1",
            "idempotency_key": "SIM:ashare:acct:20260701:600000.SH:buy",
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": 100,
            "price": 10,
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "fill_price_source_class": "signal_card_price",
        }
        with self._valid_session():
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
        self.assertEqual(snapshot["pnl"]["acct"]["cash_available"], 198995.0)
        receipts = [
            json.loads(line)
            for line in local_sim_ledger.LOCAL_SIM_RECEIPTS.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "filled")
        self.assertEqual(receipts[0]["candidate_pool_layer"], "candidate")
        self.assertEqual(receipts[0]["execution_source"], "ashare_candidate_layer")
        self.assertEqual(
            receipts[0]["receipt_sha256"],
            local_sim_ledger._payload_sha256(receipts[0], drop_checksums=True),
        )
        trades = [
            json.loads(line)
            for line in local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(trades[0]["candidate_pool_layer"], "candidate")
        self.assertEqual(trades[0]["execution_source"], "ashare_candidate_layer")

    def test_refresh_local_sim_snapshot_persists_mark_to_market_pnl(self) -> None:
        order = {
            "order_id": "SIM-MTM",
            "idempotency_key": "SIM:ashare:acct:20260701:600000.SH:buy:mtm",
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": 100,
            "price": 10,
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "fill_price_source_class": "signal_card_price",
        }
        with self._valid_session():
            local_sim_ledger.record_local_sim_order(order, "ashare", {"account": "acct"}, {"local_sim_slippage_bps": 0})

        result = local_sim_ledger.refresh_local_sim_snapshot(mark_prices={"600000.SH": 11.0})

        self.assertEqual(result["status"], "refreshed")
        pnl_payload = json.loads(local_sim_ledger.LOCAL_SIM_PNL.read_text(encoding="utf-8"))
        self.assertEqual(pnl_payload["acct"]["market_value"], 1100.0)
        self.assertEqual(pnl_payload["acct"]["total_pnl"], 95.0)
        snapshot = json.loads(local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["positions"][0]["mark_price"], 11.0)
        self.assertEqual(snapshot["pnl"]["acct"]["total_pnl"], 95.0)

    def test_rejects_buy_that_would_make_local_cash_negative(self) -> None:
        first = {
            "order_id": "SIM-CASH-1",
            "idempotency_key": "SIM:ashare:acct:20260701:600000.SH:buy:cash1",
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": 15000,
            "price": 10,
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "fill_price_source_class": "signal_card_price",
        }
        second = {
            "order_id": "SIM-CASH-2",
            "idempotency_key": "SIM:ashare:acct:20260701:600001.SH:buy:cash2",
            "ts_code": "600001.SH",
            "side": "buy",
            "quantity": 5000,
            "price": 10,
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "fill_price_source_class": "signal_card_price",
        }

        with self._valid_session():
            filled = local_sim_ledger.record_local_sim_order(first, "ashare", {"account": "acct"}, {"local_sim_slippage_bps": 0})
            rejected = local_sim_ledger.record_local_sim_order(second, "ashare", {"account": "acct"}, {"local_sim_slippage_bps": 0})

        self.assertEqual(filled["status"], "filled")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["reason"], "insufficient_cash")
        trades = [
            json.loads(line)
            for line in local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(trades), 1)
        pnl_payload = json.loads(local_sim_ledger.LOCAL_SIM_PNL.read_text(encoding="utf-8"))
        pnl = pnl_payload["acct"]
        self.assertGreaterEqual(pnl["cash_available"], 0)

    def test_validation_samples_do_not_consume_strategy_account_cash(self) -> None:
        validation_order = {
            "order_id": "SIM-VALIDATION",
            "idempotency_key": "SIM:ashare:acct:20260701:600000.SH:buy:validation",
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": 100,
            "price": 10,
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
        }
        strategy_order = {
            "order_id": "SIM-STRATEGY",
            "idempotency_key": "SIM:ashare:acct:20260702:600001.SH:buy:strategy",
            "ts_code": "600001.SH",
            "side": "buy",
            "quantity": 100,
            "price": 20,
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "fill_price_source_class": "signal_card_price",
        }

        with patch.object(
            local_sim_ledger,
            "_ashare_session_metadata",
            return_value={
                "trade_timestamp_bj": "2026-07-07T16:26:00+08:00",
                "ashare_session_valid": False,
                "ashare_session_rejection": "outside_regular_session_09:30-11:30_13:00-14:57",
            },
        ):
            validation = local_sim_ledger.record_local_sim_order(
                validation_order,
                "ashare",
                {"account": "acct"},
                {"local_sim_slippage_bps": 0},
            )
        with patch.object(
            local_sim_ledger,
            "_ashare_session_metadata",
            return_value={
                "trade_timestamp_bj": "2026-07-08T10:00:00+08:00",
                "ashare_session_valid": True,
                "ashare_session_rejection": "",
            },
        ):
            strategy = local_sim_ledger.record_local_sim_order(
                strategy_order,
                "ashare",
                {"account": "acct"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(validation["status"], "filled")
        self.assertEqual(strategy["status"], "filled")
        pnl = local_sim_ledger.get_local_sim_pnl("acct")
        self.assertEqual(set(pnl["positions"]), {"600001.SH"})
        self.assertEqual(pnl["cash_available"], 197995.0)
        audit_pnl = local_sim_ledger.get_local_sim_pnl("acct", include_validation_samples=True)
        self.assertEqual(set(audit_pnl["positions"]), {"600000.SH", "600001.SH"})
        self.assertEqual(audit_pnl["cash_available"], 196990.0)
        snapshot = json.loads(local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["account_view"], "strategy_samples_only")
        self.assertEqual(set(snapshot["positions_by_account"]["acct"]), {"600001.SH"})
        self.assertEqual(set(snapshot["audit_positions_by_account"]["acct"]), {"600000.SH", "600001.SH"})
        self.assertEqual(snapshot["audit_pnl"]["acct"]["cash_available"], 196990.0)

    def test_records_ashare_session_metadata_on_trade(self) -> None:
        order = {
            "order_id": "SIM-SESSION",
            "idempotency_key": "SIM:ashare:acct:20260701:600000.SH:buy:session",
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": 100,
            "price": 10,
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
        }
        with patch.object(
            local_sim_ledger,
            "_ashare_session_metadata",
            return_value={
                "trade_timestamp_bj": "2026-07-07T16:26:00+08:00",
                "ashare_session_valid": False,
                "ashare_session_rejection": "outside_regular_session_09:30-11:30_13:00-14:57",
            },
        ):
            result = local_sim_ledger.record_local_sim_order(order, "ashare", {"account": "acct"}, {"local_sim_slippage_bps": 0})

        self.assertEqual(result["status"], "filled")
        trades = [
            json.loads(line)
            for line in local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(trades[0]["trade_timestamp_bj"], "2026-07-07T16:26:00+08:00")
        self.assertFalse(trades[0]["ashare_session_valid"])
        self.assertEqual(trades[0]["ashare_session_rejection"], "outside_regular_session_09:30-11:30_13:00-14:57")

    def test_pending_receipt_does_not_record_local_fill(self) -> None:
        order = {
            "order_id": "SIM-PENDING",
            "idempotency_key": "SIM:ashare:acct:20260701:600000.SH:buy",
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": 100,
            "price": 10,
        }

        result = local_sim_ledger.record_local_sim_order(
            order,
            "ashare",
            {"account": "acct"},
            {"local_sim_slippage_bps": 0},
            {"status": "pending"},
        )

        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["recorded"])
        self.assertFalse(local_sim_ledger.LOCAL_SIM_TRADES.exists())

    def test_default_starting_cash_is_ashare_200000(self) -> None:
        snapshot = local_sim_ledger.get_local_sim_account_snapshot("acct")

        self.assertEqual(snapshot["cash_available"], 200000.0)

    def test_rejects_non_regular_ashare_code(self) -> None:
        result = local_sim_ledger.record_local_sim_order({"ts_code": "200011.SZ", "side": "buy", "quantity": 100, "price": 1}, "ashare")
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["recorded"])

    def test_rejects_ashare_buy_without_candidate_provenance(self) -> None:
        result = local_sim_ledger.record_local_sim_order(
            {
                "order_id": "SIM-NO-PROVENANCE",
                "idempotency_key": "SIM:ashare:acct:20260701:600000.SH:buy",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10,
            },
            "ashare",
            {"account": "acct"},
            {"local_sim_slippage_bps": 0},
        )

        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["recorded"])
        self.assertIn("candidate_pool_layer=candidate", result["reason"])
        self.assertFalse(local_sim_ledger.LOCAL_SIM_TRADES.exists())

    def test_rejects_ashare_sell_without_rebalance_provenance(self) -> None:
        result = local_sim_ledger.record_local_sim_order(
            {
                "order_id": "SIM-SELL-NO-PROVENANCE",
                "idempotency_key": "SIM:ashare:acct:20260701:600000.SH:sell",
                "ts_code": "600000.SH",
                "side": "sell",
                "quantity": 100,
                "price": 10,
            },
            "ashare",
            {"account": "acct"},
            {"local_sim_slippage_bps": 0},
        )

        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["recorded"])
        self.assertIn("execution_source=ashare_rebalance_sell", result["reason"])
        self.assertFalse(local_sim_ledger.LOCAL_SIM_TRADES.exists())

    def test_bootstrap_snapshot_creates_empty_sim_state(self) -> None:
        result = local_sim_ledger.ensure_local_sim_bootstrap_snapshot(starting_cash=200000, trade_date="20260706")

        self.assertEqual(result["status"], "bootstrapped")
        self.assertTrue(result["written"])
        snapshot = json.loads(local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["bootstrap_state"], "no_trades_yet")
        self.assertEqual(snapshot["cash_available"], 200000.0)
        self.assertEqual(snapshot["positions"], [])
        self.assertEqual(snapshot["pnl"]["ashare_sim"]["cash_available"], 200000.0)

        again = local_sim_ledger.ensure_local_sim_bootstrap_snapshot(starting_cash=200000)
        self.assertEqual(again["status"], "snapshot_exists")
        snapshot_again = json.loads(local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(snapshot_again["cash_available"], 200000.0)

        updated = local_sim_ledger.ensure_local_sim_bootstrap_snapshot(starting_cash=30000)
        self.assertEqual(updated["status"], "bootstrapped")
        snapshot_updated = json.loads(local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(snapshot_updated["cash_available"], 30000.0)


if __name__ == "__main__":
    unittest.main()
