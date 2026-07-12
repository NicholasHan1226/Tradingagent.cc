from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shared.capital import MarketCapitalFillCommitDecision
from shared.execution import local_sim_ledger, sim_account_epoch


class LocalSimLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name) / "ashare-sim-fresh-20260712-v1"
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
        verification_patcher = patch(
            "shared.capital.verify_market_capital_reservation",
            side_effect=lambda market, **kwargs: {
                "verified": True,
                "reason": "reservation_verified",
                "reservation_id": kwargs["reservation_id"],
                "reference_id": kwargs["reference_id"],
                "market": market,
                "authority_id": kwargs["authority_id"],
                "authority_generation": kwargs["authority_generation"],
                "execution_lineage_id": kwargs["execution_lineage_id"],
                "risk_unit_key": kwargs["risk_unit_key"],
                "event_id": kwargs["expected_event_id"],
                "remaining_amount_cny": 100_000.0,
                "real_trading_enabled": False,
            },
        )
        verification_patcher.start()
        self.addCleanup(verification_patcher.stop)
        local_sim_ledger.bootstrap_fresh_local_sim(
            root=base,
            lineage_started_at="2026-07-12T00:00:00+08:00",
            point_in_time_as_of="2026-07-12T00:00:00+08:00",
        )

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

    def _lineage_order(self, order: dict[str, object]) -> dict[str, object]:
        payload = dict(order)
        identity = str(
            payload.get("idempotency_key") or payload.get("order_id") or "test"
        )
        if local_sim_ledger._order_trade_date(payload, identity) < "2026-07-12":
            payload["trade_date"] = "20260713"
        effective_trade_date = local_sim_ledger._order_trade_date(payload, identity)
        payload.update(
            local_sim_ledger.build_execution_lineage(
                lineage_started_at="2026-07-12T00:00:00+08:00",
                point_in_time_as_of=f"{effective_trade_date}T10:00:00+08:00",
            )
        )
        return payload

    def _market_funded(self, order: dict[str, object]) -> dict[str, object]:
        payload = self._lineage_order(order)
        identity = str(
            payload.get("idempotency_key") or payload.get("order_id") or "test"
        )
        payload.update(
            {
                "capital_scope": "strategy",
                "market_capital_required": True,
                "market_capital_reference_id": f"ASHARE-CAP:{identity}",
                "market_capital_reservation_id": f"ares-{identity}",
                "market_capital_event_id": f"aevt-{identity}",
                "market_capital_expected_head_event_id": f"aevt-{identity}",
                "market_capital_expected_head_checksum": "a" * 64,
                "market_reserved_gross_cny": 100_000.0,
                "fill_price_source_class": "market_data",
                "fill_evidence": self._verified_fill_evidence(),
            }
        )
        return payload

    @staticmethod
    def _verified_fill_evidence() -> dict[str, object]:
        return {
            "execution_evidence_class": "verified_5min_market_data",
            "fill_price_source": "sharedsignals_api_realtime_5min",
            "fill_price_source_class": "market_data",
            "bar_time": "2026-07-13T10:00:00+08:00",
            "bar_volume": 100_000.0,
        }

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
        order = self._market_funded(order)
        with self._valid_session():
            first = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )
            second = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(first["status"], "filled")
        self.assertTrue(first["recorded"])
        self.assertEqual(second["status"], "duplicate")
        pnl = local_sim_ledger.get_local_sim_pnl("ashare_sim")
        self.assertEqual(pnl["positions"]["600000.SH"]["quantity"], 100)
        self.assertEqual(pnl["market_value"], 1000.0)
        snapshot = json.loads(
            local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot["positions"][0]["ts_code"], "600000.SH")
        self.assertEqual(snapshot["positions"][0]["account"], "ashare_sim")
        self.assertEqual(snapshot["pnl"]["ashare_sim"]["cash_available"], 48994.99)
        receipts = [
            json.loads(line)
            for line in local_sim_ledger.LOCAL_SIM_RECEIPTS.read_text(
                encoding="utf-8"
            ).splitlines()
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
            for line in local_sim_ledger.LOCAL_SIM_TRADES.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        self.assertEqual(trades[0]["candidate_pool_layer"], "candidate")
        self.assertEqual(trades[0]["execution_source"], "ashare_candidate_layer")

    def test_records_explicit_exploration_provenance_and_rejects_mismatch(self) -> None:
        order = self._market_funded(
            {
                "order_id": "SIM-EXPLORE-1",
                "idempotency_key": "SIM:ashare:acct:20260708:600001.SH:buy:explore",
                "ts_code": "600001.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "candidate_pool_layer": "exploration",
                "execution_source": "ashare_candidate_layer",
                "sample_intent": "exploration",
            }
        )
        with self._valid_session():
            accepted = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )
            rejected = local_sim_ledger.record_local_sim_order(
                {
                    **order,
                    "order_id": "SIM-EXPLORE-MISMATCH",
                    "idempotency_key": "SIM:ashare:acct:20260708:600002.SH:buy:mismatch",
                    "ts_code": "600002.SH",
                    "sample_intent": "exploitation",
                },
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(accepted["status"], "filled")
        self.assertEqual(accepted["sample_intent"], "exploration")
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn("sample_intent=exploration", rejected["reason"])
        trades = local_sim_ledger._load_trades_unlocked()
        self.assertEqual(trades[0]["sample_intent"], "exploration")
        position = local_sim_ledger.get_local_sim_pnl("ashare_sim")["positions"][
            "600001.SH"
        ]
        self.assertEqual(position["sample_intent"], "exploration")
        self.assertGreater(position["exploration_exposure_cny"], 0.0)

    def test_partial_receipt_records_only_filled_quantity(self) -> None:
        order = {
            "order_id": "SIM-PARTIAL-1",
            "idempotency_key": "SIM:ashare:acct:20260708:600000.SH:buy:partial",
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": 200,
            "price": 10.0,
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "fill_price_source_class": "signal_card_price",
        }
        receipt = SimpleNamespace(
            status="partial",
            filled_qty=100,
            avg_price=10.0,
            raw_response={},
        )
        order = self._market_funded(order)

        with self._valid_session():
            result = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
                receipt,
            )

        trades = [
            json.loads(line)
            for line in local_sim_ledger.LOCAL_SIM_TRADES.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["filled_qty"], 100)
        self.assertEqual(trades[0]["quantity"], 100)
        self.assertEqual(
            local_sim_ledger.get_local_sim_pnl("ashare_sim")["positions"]["600000.SH"][
                "quantity"
            ],
            100,
        )
        actions = local_sim_ledger.get_local_sim_market_capital_outbox()["actions"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "fill_commit")
        self.assertFalse(actions[0]["fill_commit_request"]["terminal"])

    def test_partial_exploration_counts_as_daily_position_and_persists_exposure(
        self,
    ) -> None:
        order = self._market_funded(
            {
                "order_id": "SIM-EXP-PARTIAL",
                "idempotency_key": "SIM:ashare:acct:20260708:600010.SH:buy:partial",
                "trade_date": "20260708",
                "ts_code": "600010.SH",
                "side": "buy",
                "quantity": 200,
                "price": 10.0,
                "candidate_pool_layer": "exploration",
                "execution_source": "ashare_candidate_layer",
                "sample_intent": "exploration",
            }
        )
        receipt = SimpleNamespace(
            status="partial",
            filled_qty=100,
            avg_price=10.0,
            raw_response={},
        )

        with self._valid_session():
            result = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
                receipt,
            )

        self.assertEqual(result["status"], "partial")
        state = local_sim_ledger.get_local_sim_exploration_state(
            "ashare_sim", trade_date="20260713"
        )
        self.assertEqual(state["new_position_count"], 1)
        self.assertGreater(state["open_exposure_cny"], 0.0)
        self.assertEqual(state["daily_realized_pnl_cny"], 0.0)

    def test_local_ledger_rejects_same_day_sell_even_with_direct_entry(self) -> None:
        buy = self._market_funded(
            {
                "order_id": "SIM-T1-BUY",
                "idempotency_key": "SIM:ashare:acct:20260708:600011.SH:buy",
                "trade_date": "20260708",
                "ts_code": "600011.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
                "sample_intent": "exploitation",
            }
        )
        sell = self._lineage_order(
            {
                "order_id": "SIM-T1-SELL",
                "idempotency_key": "SIM:ashare:acct:20260708:600011.SH:sell",
                "trade_date": "20260713",
                "ts_code": "600011.SH",
                "side": "sell",
                "quantity": 100,
                "price": 9.9,
                "candidate_pool_layer": "ashare_rebalance_sell",
                "execution_source": "ashare_rebalance_sell",
                "fill_price_source_class": "market_data",
                "fill_evidence": self._verified_fill_evidence(),
                "market_capital_required": True,
                "market_capital_expected_head_event_id": "aevt-roundtrip-sell-head",
                "market_capital_expected_head_checksum": "b" * 64,
            }
        )
        with self._valid_session():
            self.assertEqual(
                local_sim_ledger.record_local_sim_order(
                    buy,
                    "ashare",
                    {"account": "ashare_sim"},
                    {"local_sim_slippage_bps": 0},
                )["status"],
                "filled",
            )
            rejected = local_sim_ledger.record_local_sim_order(
                sell,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["reason"], "t_plus_one_sell_quantity_unavailable")

    def test_market_reserved_buy_rejects_underfunded_lineage_without_append(
        self,
    ) -> None:
        order = self._market_funded(
            {
                "order_id": "SIM-MARKET-UNDERFUNDED",
                "idempotency_key": "SIM:ashare:acct:20260713:600000.SH:buy:market-underfunded",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
            }
        )
        order["market_reserved_gross_cny"] = 1_000.0

        with self._valid_session():
            result = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "market_reservation_underfunded")
        self.assertEqual(result["required_cash"], 1_005.01)
        self.assertEqual(
            local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8"), ""
        )
        self.assertEqual(
            local_sim_ledger.LOCAL_SIM_RECEIPTS.read_text(encoding="utf-8"), ""
        )

    def test_strategy_buy_without_market_lineage_fails_closed(self) -> None:
        order = self._lineage_order(
            {
                "order_id": "SIM-MARKET-MISSING",
                "idempotency_key": "SIM:ashare:acct:20260713:600000.SH:buy:market-missing",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
                "fill_price_source_class": "signal_card_price",
                "capital_scope": "strategy",
                "fill_evidence": self._verified_fill_evidence(),
            }
        )

        with self._valid_session():
            result = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "market_capital_lineage_missing")

    def test_strategy_buy_requires_real_market_reservation(self) -> None:
        order = self._market_funded(
            {
                "order_id": "SIM-MASTER-UNKNOWN",
                "idempotency_key": "SIM:ashare:acct:20260708:600000.SH:buy:unknown",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
            }
        )
        with (
            patch(
                "shared.capital.verify_market_capital_reservation",
                return_value={
                    "verified": False,
                    "reason": "unknown_reservation",
                    "real_trading_enabled": False,
                },
            ),
            self._valid_session(),
        ):
            result = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "market_reservation_verification_failed")
        self.assertEqual(result["verification_reason"], "unknown_reservation")
        self.assertEqual(
            local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8"), ""
        )
        self.assertEqual(
            local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8"), ""
        )
        self.assertEqual(
            local_sim_ledger.LOCAL_SIM_RECEIPTS.read_text(encoding="utf-8"), ""
        )

    def test_strategy_fill_without_verified_market_evidence_fails_closed(self) -> None:
        order = self._market_funded(
            {
                "order_id": "SIM-EVIDENCE-MISSING",
                "idempotency_key": "SIM:ashare:acct:20260708:600000.SH:buy:evidence-missing",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
            }
        )
        order["fill_price_source_class"] = "signal_card_price"
        order["fill_evidence"] = {
            "execution_evidence_class": "weak_price_only",
            "fill_price_source": "signal_card.price",
        }

        with self._valid_session():
            result = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "execution_evidence_unverified")
        self.assertEqual(
            local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8"), ""
        )

    def test_market_reserved_buy_projects_one_atomic_fill_commit(self) -> None:
        order = self._market_funded(
            {
                "order_id": "SIM-MARKET-BUY",
                "idempotency_key": "SIM:ashare:acct:20260713:600000.SH:buy:market",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
            }
        )
        order.update(
            {
                "market_capital_reference_id": "ASHARE-CAP:buy",
                "market_capital_reservation_id": "ares-buy",
                "market_capital_event_id": "aevt-buy",
                "market_capital_expected_head_event_id": "aevt-buy",
                "market_reserved_gross_cny": 1_010.0,
            }
        )

        with self._valid_session():
            result = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(result["status"], "filled")
        self.assertEqual(result["market_capital_reservation_id"], "ares-buy")
        self.assertEqual(result["market_reserved_gross_cny"], 1_010.0)
        self.assertEqual(result["market_retained_gross_cny"], 0.0)
        self.assertEqual(result["commission"], 5.0)
        self.assertEqual(result["stamp_duty"], 0.0)
        self.assertEqual(result["transfer_fee"], 0.01)
        self.assertEqual(result["total_fee"], 5.01)
        self.assertEqual(result["slippage_bps"], 0.0)
        self.assertTrue(result["created_at"])
        trade = json.loads(
            local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8").splitlines()[
                0
            ]
        )
        self.assertEqual(trade["market_capital_event_id"], "aevt-buy")
        position = local_sim_ledger.get_local_sim_pnl("ashare_sim")["positions"][
            "600000.SH"
        ]
        self.assertEqual(position["market_reserved_cost_basis_cny"], 0.0)
        self.assertEqual(position["market_reservations"], [])
        recovered = local_sim_ledger.get_local_sim_trade_by_idempotency(
            order["idempotency_key"], account="ashare_sim"
        )
        self.assertEqual(recovered["trade_id"], result["trade_id"])
        actions = local_sim_ledger.list_local_sim_market_capital_actions(
            account="ashare_sim"
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "fill_commit")
        request = actions[0]["fill_commit_request"]
        self.assertEqual(
            request["reference_id"],
            f"MCAPFILL:1:ashare-sim-fresh-20260712-v1:ares-buy:{trade['trade_id']}",
        )
        self.assertEqual(request["reservation_id"], "ares-buy")
        self.assertEqual(request["reservation_event_id"], "aevt-buy")
        self.assertEqual(request["reservation_reference_id"], "ASHARE-CAP:buy")
        self.assertEqual(request["risk_unit_key"], "600000.SH")
        self.assertEqual(request["authority_id"], "ashare-capital-v1")
        self.assertEqual(request["authority_generation"], 1)
        self.assertEqual(
            request["execution_lineage_id"],
            "ashare-sim-fresh-20260712-v1",
        )
        self.assertEqual(request["lineage_sha256"], order["execution_lineage_sha256"])
        self.assertEqual(request["order_id"], order["order_id"])
        self.assertEqual(request["idempotency_key"], order["idempotency_key"])
        self.assertEqual(request["execution_fill_id"], trade["trade_id"])
        self.assertEqual(request["fill_sequence"], 1)
        self.assertEqual(request["status"], "filled")
        self.assertEqual(request["actual_filled_quantity"], 100)
        self.assertEqual(request["actual_fill_price"], 10.0)
        self.assertEqual(request["actual_cash_debit_cny"], 1_005.01)
        self.assertEqual(request["actual_exposure_cny"], 1_000.0)
        self.assertEqual(request["actual_margin_cny"], 0.0)
        self.assertEqual(request["actual_fee_cash_cny"], 5.01)
        self.assertTrue(request["terminal"])
        self.assertTrue(request["filled_at"])
        self.assertEqual(
            request["point_in_time_as_of"],
            order["point_in_time_as_of"],
        )
        self.assertEqual(
            request["source_sha256"], trade["market_capital_source_sha256"]
        )
        self.assertEqual(
            request["receipt_sha256"],
            trade["market_capital_receipt_sha256"],
        )
        self.assertEqual(request["local_trade_sha256"], trade["trade_sha256"])
        self.assertEqual(request["expected_ledger_event_id"], "aevt-buy")
        self.assertEqual(request["expected_ledger_checksum"], "a" * 64)
        for field in (
            "lineage_sha256",
            "source_sha256",
            "receipt_sha256",
            "local_trade_sha256",
            "expected_ledger_checksum",
        ):
            self.assertRegex(request[field], r"^[a-f0-9]{64}$")
        self.assertEqual(
            actions[0]["fill_commit_request_sha256"],
            local_sim_ledger._payload_sha256(request),
        )

    def test_sell_allocates_market_release_and_realized_pnl(self) -> None:
        buy = self._market_funded(
            {
                "order_id": "SIM-MARKET-ROUNDTRIP-BUY",
                "idempotency_key": "SIM:ashare:acct:20260713:600000.SH:buy:roundtrip",
                "trade_date": "20260713",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
            }
        )
        buy.update(
            {
                "market_capital_reference_id": "ASHARE-CAP:roundtrip-buy",
                "market_capital_reservation_id": "ares-roundtrip-buy",
                "market_capital_event_id": "aevt-roundtrip-buy",
                "market_reserved_gross_cny": 1_005.01,
            }
        )
        sell = self._lineage_order(
            {
                "order_id": "SIM-MARKET-ROUNDTRIP-SELL",
                "idempotency_key": "SIM:ashare:acct:20260714:600000.SH:sell:roundtrip",
                "trade_date": "20260714",
                "ts_code": "600000.SH",
                "side": "sell",
                "quantity": 100,
                "price": 11.0,
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_rebalance_sell",
                "capital_scope": "strategy",
                "fill_price_source_class": "market_data",
                "fill_evidence": self._verified_fill_evidence(),
                "market_capital_required": True,
                "market_capital_expected_head_event_id": "aevt-roundtrip-sell-head",
                "market_capital_expected_head_checksum": "b" * 64,
            }
        )

        with self._valid_session():
            bought = local_sim_ledger.record_local_sim_order(
                buy,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )
            sold = local_sim_ledger.record_local_sim_order(
                sell,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(bought["status"], "filled")
        self.assertEqual(sold["status"], "filled")
        self.assertEqual(sold["net_amount"], 1_094.44)
        self.assertEqual(sold["realized_pnl_cny"], 89.43)
        self.assertEqual(sold["gross_realized_pnl_cny"], 100.0)
        self.assertEqual(sold["released_principal_cost_basis_cny"], 1_000.0)
        self.assertEqual(sold["released_entry_fee_cny"], 5.01)
        self.assertEqual(sold["market_release_allocations"], [])
        pnl = local_sim_ledger.get_local_sim_pnl("ashare_sim")
        self.assertEqual(pnl["realized_pnl"], 89.43)
        self.assertEqual(pnl["positions"], {})
        actions = local_sim_ledger.list_local_sim_market_capital_actions(
            account="ashare_sim"
        )
        self.assertEqual(
            [(row["action"], row["amount_cny"]) for row in actions],
            [("fill_commit", 1_005.01), ("ashare_sell_commit", 89.43)],
        )
        self.assertIn("ashare-sim-fresh-20260712-v1", actions[0]["reference_id"])
        self.assertIn("ashare-sim-fresh-20260712-v1", actions[1]["reference_id"])
        request = actions[1]["ashare_sell_commit_request"]
        self.assertEqual(request["risk_unit_key"], "600000.SH")
        self.assertEqual(request["actual_closed_quantity"], 100)
        self.assertEqual(request["actual_fill_price"], 11.0)
        self.assertEqual(request["actual_gross_proceeds_cny"], 1_100.0)
        self.assertEqual(request["actual_fee_cash_cny"], 5.56)
        self.assertEqual(request["actual_net_cash_credit_cny"], 1_094.44)
        self.assertEqual(request["actual_gross_realized_pnl_cny"], 100.0)
        self.assertEqual(
            request["expected_ledger_event_id"], "aevt-roundtrip-sell-head"
        )
        self.assertEqual(request["expected_ledger_checksum"], "b" * 64)
        self.assertEqual(
            actions[1]["ashare_sell_commit_request_sha256"],
            local_sim_ledger._payload_sha256(request),
        )

    def test_new_fill_persists_complete_fresh_lineage_metadata(self) -> None:
        order = {
            "order_id": "SIM-EPOCH-2",
            "idempotency_key": "SIM:ashare:acct:20260711:600000.SH:buy:epoch2",
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": 100,
            "price": 10,
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "fill_price_source_class": "verified_5min_market_data",
        }
        with self._valid_session():
            result = local_sim_ledger.record_local_sim_order(
                self._market_funded(order),
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(result["status"], "filled")
        trade = json.loads(
            local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8").splitlines()[
                0
            ]
        )
        receipt = json.loads(
            local_sim_ledger.LOCAL_SIM_RECEIPTS.read_text(
                encoding="utf-8"
            ).splitlines()[0]
        )
        for payload in (trade, receipt, result):
            self.assertEqual(payload["capital_authority_id"], "ashare-capital-v1")
            self.assertEqual(payload["authority_generation"], 1)
            self.assertEqual(
                payload["execution_lineage_id"], "ashare-sim-fresh-20260712-v1"
            )
            self.assertNotIn("capital_epoch", payload)
            self.assertEqual(payload["capital_cny"], 50_000.0)

    def test_legacy_numeric_epoch_record_is_rejected_without_write(self) -> None:
        order = self._market_funded(
            {
                "order_id": "SIM-EPOCH-2-CORRUPT",
                "idempotency_key": "SIM:ashare:acct:20260711:600000.SH:buy:epoch2-corrupt",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10,
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
                "fill_price_source_class": "verified_5min_market_data",
            }
        )
        order["capital_epoch"] = 2

        with self._valid_session():
            result = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "legacy_numeric_epoch_forbidden")
        self.assertEqual(
            local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8"), ""
        )
        self.assertEqual(
            local_sim_ledger.LOCAL_SIM_RECEIPTS.read_text(encoding="utf-8"), ""
        )

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
            local_sim_ledger.record_local_sim_order(
                self._market_funded(order),
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        result = local_sim_ledger.refresh_local_sim_snapshot(
            mark_prices={"600000.SH": 11.0}
        )

        self.assertEqual(result["status"], "refreshed")
        pnl_payload = json.loads(
            local_sim_ledger.LOCAL_SIM_PNL.read_text(encoding="utf-8")
        )
        self.assertEqual(pnl_payload["ashare_sim"]["market_value"], 1100.0)
        self.assertEqual(pnl_payload["ashare_sim"]["total_pnl"], 94.99)
        snapshot = json.loads(
            local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot["positions"][0]["mark_price"], 11.0)
        self.assertEqual(snapshot["pnl"]["ashare_sim"]["total_pnl"], 94.99)

    def test_replay_exposes_single_open_order_id_for_read_only_opportunity_attribution(
        self,
    ) -> None:
        replay = local_sim_ledger._replay_account(
            [
                {
                    "account": "ashare_sim",
                    "status": "filled",
                    "ts_code": "600000.SH",
                    "side": "buy",
                    "quantity": 100,
                    "net_amount": 1000,
                    "filled_price": 10,
                    "order_id": "opp-ashare-001",
                },
            ],
            account="ashare_sim",
            mark_prices={"600000.SH": 11},
        )

        position = replay["positions"]["600000.SH"]
        self.assertEqual(position["order_id"], "opp-ashare-001")
        self.assertEqual(position["unrealized_pnl"], 100.0)

    def test_replay_omits_attribution_when_open_position_has_multiple_order_origins(
        self,
    ) -> None:
        replay = local_sim_ledger._replay_account(
            [
                {
                    "account": "ashare_sim",
                    "status": "filled",
                    "ts_code": "600000.SH",
                    "side": "buy",
                    "quantity": 100,
                    "net_amount": 1000,
                    "filled_price": 10,
                    "order_id": "opp-ashare-001",
                },
                {
                    "account": "ashare_sim",
                    "status": "filled",
                    "ts_code": "600000.SH",
                    "side": "buy",
                    "quantity": 100,
                    "net_amount": 1200,
                    "filled_price": 12,
                    "order_id": "opp-ashare-002",
                },
            ],
            account="ashare_sim",
            mark_prices={"600000.SH": 11},
        )

        self.assertNotIn("order_id", replay["positions"]["600000.SH"])

    def test_bootstrap_check_does_not_overwrite_existing_mark_to_market_snapshot(
        self,
    ) -> None:
        order = {
            "order_id": "SIM-MTM-BOOTSTRAP",
            "idempotency_key": "SIM:ashare:acct:20260701:600000.SH:buy:mtm-bootstrap",
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": 100,
            "price": 10,
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "fill_price_source_class": "signal_card_price",
        }
        with self._valid_session():
            local_sim_ledger.record_local_sim_order(
                self._market_funded(order),
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )
        local_sim_ledger.refresh_local_sim_snapshot(mark_prices={"600000.SH": 11.0})

        result = local_sim_ledger.ensure_local_sim_bootstrap_snapshot(
            starting_cash=50000
        )

        self.assertEqual(result["status"], "existing_trades")
        pnl_payload = json.loads(
            local_sim_ledger.LOCAL_SIM_PNL.read_text(encoding="utf-8")
        )
        snapshot = json.loads(
            local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8")
        )
        self.assertEqual(pnl_payload["ashare_sim"]["total_pnl"], 94.99)
        self.assertEqual(snapshot["positions"][0]["mark_price"], 11.0)

    def test_persists_retry_lineage_in_trade_and_signed_receipt(self) -> None:
        order = {
            "order_id": "SIM-RETRY-1",
            "idempotency_key": "SIM:ashare:acct:20260710:600000.SH:buy:retry1",
            "retry_of": "SIM-ORIGINAL",
            "retry_attempt": 1,
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": 100,
            "price": 10,
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "fill_price_source_class": "signal_card_price",
        }
        with self._valid_session():
            result = local_sim_ledger.record_local_sim_order(
                self._market_funded(order),
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(result["status"], "filled")
        trade = json.loads(
            local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8").splitlines()[
                0
            ]
        )
        receipt = json.loads(
            local_sim_ledger.LOCAL_SIM_RECEIPTS.read_text(
                encoding="utf-8"
            ).splitlines()[0]
        )
        self.assertEqual(trade["retry_of"], "SIM-ORIGINAL")
        self.assertEqual(trade["retry_attempt"], 1)
        self.assertEqual(receipt["retry_of"], "SIM-ORIGINAL")
        self.assertEqual(receipt["retry_attempt"], 1)
        self.assertEqual(
            receipt["receipt_sha256"],
            local_sim_ledger._payload_sha256(receipt, drop_checksums=True),
        )

    def test_rejects_buy_that_would_make_local_cash_negative(self) -> None:
        first = {
            "order_id": "SIM-CASH-1",
            "idempotency_key": "SIM:ashare:acct:20260701:600000.SH:buy:cash1",
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": 2000,
            "price": 20,
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "fill_price_source_class": "signal_card_price",
        }
        second = {
            "order_id": "SIM-CASH-2",
            "idempotency_key": "SIM:ashare:acct:20260701:600001.SH:buy:cash2",
            "ts_code": "600001.SH",
            "side": "buy",
            "quantity": 500,
            "price": 20,
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "fill_price_source_class": "signal_card_price",
        }

        with self._valid_session():
            filled = local_sim_ledger.record_local_sim_order(
                self._market_funded(first),
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )
            rejected = local_sim_ledger.record_local_sim_order(
                self._market_funded(second),
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(filled["status"], "filled")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["reason"], "insufficient_cash")
        trades = [
            json.loads(line)
            for line in local_sim_ledger.LOCAL_SIM_TRADES.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        self.assertEqual(len(trades), 1)
        pnl_payload = json.loads(
            local_sim_ledger.LOCAL_SIM_PNL.read_text(encoding="utf-8")
        )
        pnl = pnl_payload["ashare_sim"]
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
            "capital_scope": "validation",
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
                self._lineage_order(validation_order),
                "ashare",
                {"account": "ashare_sim"},
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
                self._market_funded(strategy_order),
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(validation["status"], "filled")
        self.assertEqual(strategy["status"], "filled")
        pnl = local_sim_ledger.get_local_sim_pnl("ashare_sim")
        self.assertEqual(set(pnl["positions"]), {"600001.SH"})
        self.assertEqual(pnl["cash_available"], 47994.98)
        audit_pnl = local_sim_ledger.get_local_sim_pnl(
            "ashare_sim", include_validation_samples=True
        )
        self.assertEqual(set(audit_pnl["positions"]), {"600000.SH", "600001.SH"})
        self.assertEqual(audit_pnl["cash_available"], 46989.97)
        snapshot = json.loads(
            local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot["account_view"], "strategy_samples_only")
        self.assertEqual(
            set(snapshot["positions_by_account"]["ashare_sim"]), {"600001.SH"}
        )
        self.assertEqual(
            set(snapshot["audit_positions_by_account"]["ashare_sim"]),
            {"600000.SH", "600001.SH"},
        )
        self.assertEqual(
            snapshot["audit_pnl"]["ashare_sim"]["cash_available"], 46989.97
        )

    def test_validation_scope_does_not_block_a_strategy_buy_with_strategy_cash(
        self,
    ) -> None:
        validation_order = {
            "order_id": "SIM-VALIDATION-CASH-SCOPE",
            "idempotency_key": "SIM:ashare:acct:20260701:000001.SZ:buy:validation-scope",
            "ts_code": "000001.SZ",
            "side": "buy",
            "quantity": 200,
            "price": 50,
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "fill_price_source_class": "signal_card_price",
            "capital_scope": "validation",
        }
        strategy_order = {
            "order_id": "SIM-STRATEGY-CASH-SCOPE",
            "idempotency_key": "SIM:ashare:acct:20260702:600001.SH:buy:strategy-scope",
            "ts_code": "600001.SH",
            "side": "buy",
            "quantity": 400,
            "price": 50,
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "fill_price_source_class": "signal_card_price",
        }
        overflow_validation_order = {
            **validation_order,
            "order_id": "SIM-VALIDATION-CASH-SCOPE-OVERFLOW",
            "idempotency_key": "SIM:ashare:acct:20260701:000002.SZ:buy:validation-scope-overflow",
            "ts_code": "000002.SZ",
            "quantity": 800,
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
                self._lineage_order(validation_order),
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )
            overflow_validation = local_sim_ledger.record_local_sim_order(
                self._lineage_order(overflow_validation_order),
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )
        with self._valid_session():
            strategy = local_sim_ledger.record_local_sim_order(
                self._market_funded(strategy_order),
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(validation["status"], "filled")
        self.assertEqual(overflow_validation["status"], "rejected")
        self.assertEqual(overflow_validation["reason"], "insufficient_cash")
        self.assertEqual(strategy["status"], "filled")
        strategy_pnl = local_sim_ledger.get_local_sim_pnl("ashare_sim")
        self.assertEqual(strategy_pnl["cash_available"], 29994.8)
        self.assertEqual(set(strategy_pnl["positions"]), {"600001.SH"})
        trades = [
            json.loads(line)
            for line in local_sim_ledger.LOCAL_SIM_TRADES.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        receipts = [
            json.loads(line)
            for line in local_sim_ledger.LOCAL_SIM_RECEIPTS.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        scopes = {trade["ts_code"]: trade["capital_scope"] for trade in trades}
        self.assertEqual(scopes, {"000001.SZ": "validation", "600001.SH": "strategy"})
        self.assertEqual(receipts[-1]["capital_scope"], "strategy")

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
            "capital_scope": "validation",
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
            result = local_sim_ledger.record_local_sim_order(
                self._lineage_order(order),
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(result["status"], "filled")
        trades = [
            json.loads(line)
            for line in local_sim_ledger.LOCAL_SIM_TRADES.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        self.assertEqual(trades[0]["trade_timestamp_bj"], "2026-07-07T16:26:00+08:00")
        self.assertFalse(trades[0]["ashare_session_valid"])
        self.assertEqual(
            trades[0]["ashare_session_rejection"],
            "outside_regular_session_09:30-11:30_13:00-14:57",
        )
        self.assertFalse(trades[0]["execution_eligible"])
        self.assertEqual(trades[0]["sample_layer"], "chain_validation")

    def test_strategy_and_exploration_fills_reject_outside_regular_session_without_write(
        self,
    ) -> None:
        for sample_intent, candidate_pool_layer in (
            ("exploitation", "candidate"),
            ("exploration", "exploration"),
        ):
            with self.subTest(sample_intent=sample_intent):
                order = self._market_funded(
                    {
                        "order_id": f"SIM-SESSION-{sample_intent}",
                        "idempotency_key": (
                            f"SIM:ashare:acct:20260713:600000.SH:buy:{sample_intent}"
                        ),
                        "trade_date": "20260713",
                        "ts_code": "600000.SH",
                        "side": "buy",
                        "quantity": 100,
                        "price": 10,
                        "candidate_pool_layer": candidate_pool_layer,
                        "execution_source": "ashare_candidate_layer",
                        "sample_intent": sample_intent,
                    }
                )
                with patch.object(
                    local_sim_ledger,
                    "_ashare_session_metadata",
                    return_value={
                        "trade_timestamp_bj": "2026-07-13T16:26:00+08:00",
                        "ashare_session_valid": False,
                        "ashare_session_rejection": (
                            "outside_regular_session_09:30-11:30_13:00-14:57"
                        ),
                    },
                ):
                    result = local_sim_ledger.record_local_sim_order(
                        order,
                        "ashare",
                        {"account": "ashare_sim"},
                        {"local_sim_slippage_bps": 0},
                    )

                self.assertEqual(result["status"], "rejected")
                self.assertEqual(result["reason"], "outside_ashare_regular_session")
        self.assertEqual(
            local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8"), ""
        )
        self.assertEqual(
            local_sim_ledger.LOCAL_SIM_RECEIPTS.read_text(encoding="utf-8"), ""
        )
        self.assertEqual(
            local_sim_ledger.get_local_sim_market_capital_outbox()["actions"],
            [],
        )

    def test_preserves_verified_5min_market_evidence_in_trade_and_receipt(self) -> None:
        order = {
            "order_id": "SIM-EVIDENCE",
            "idempotency_key": "SIM:ashare:acct:20260708:600000.SH:buy:evidence",
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": 100,
            "price": 10,
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
        }
        receipt = {
            "status": "filled",
            "avg_price": 10.02,
            "raw_response": {
                "fill_evidence": {
                    "execution_evidence_class": "verified_5min_market_data",
                    "fill_price_source": "sharedsignals_api_realtime_5min",
                    "fill_price_source_class": "market_data",
                    "bar_time": "2026-07-08T10:00:00+08:00",
                    "bar_volume": 123456.0,
                }
            },
        }

        with self._valid_session():
            result = local_sim_ledger.record_local_sim_order(
                self._market_funded(order),
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
                receipt,
            )

        self.assertEqual(result["status"], "filled")
        trades = [
            json.loads(line)
            for line in local_sim_ledger.LOCAL_SIM_TRADES.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        receipts = [
            json.loads(line)
            for line in local_sim_ledger.LOCAL_SIM_RECEIPTS.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        self.assertEqual(
            trades[0]["fill_evidence"]["execution_evidence_class"],
            "verified_5min_market_data",
        )
        self.assertEqual(
            receipts[0]["fill_evidence"]["execution_evidence_class"],
            "verified_5min_market_data",
        )

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
            {"account": "ashare_sim"},
            {"local_sim_slippage_bps": 0},
            {"status": "pending"},
        )

        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["recorded"])
        self.assertEqual(
            local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8"), ""
        )

    def test_default_starting_cash_is_ashare_50000(self) -> None:
        snapshot = local_sim_ledger.get_local_sim_account_snapshot("ashare_sim")

        self.assertEqual(snapshot["cash_available"], 50000.0)

    def test_corrupt_trade_log_blocks_replay_instead_of_minting_empty_account(
        self,
    ) -> None:
        local_sim_ledger.LOCAL_SIM_TRADES.write_text(
            '{"order_id":"truncated"',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            local_sim_ledger.LocalSimLedgerCorruption,
            "corrupt_local_sim_trade:1",
        ):
            local_sim_ledger.get_local_sim_account_snapshot("ashare_sim")

    def test_trade_checksum_detects_tampering_and_persists_sim_only_marker(
        self,
    ) -> None:
        order = self._market_funded(
            {
                "order_id": "SIM-CHECKSUM",
                "idempotency_key": "SIM:ashare:acct:20260708:600000.SH:buy:checksum",
                "trade_date": "20260708",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
            }
        )
        with self._valid_session():
            result = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )
        self.assertEqual(result["status"], "filled")

        row = json.loads(local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8"))
        self.assertFalse(row["real_trading_enabled"])
        self.assertEqual(
            row["trade_sha256"],
            local_sim_ledger._payload_sha256(row, drop_checksums=True),
        )
        row["net_amount"] = 1.0
        local_sim_ledger.LOCAL_SIM_TRADES.write_text(
            json.dumps(row, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            local_sim_ledger.LocalSimLedgerCorruption,
            "checksum_mismatch",
        ):
            local_sim_ledger.get_local_sim_account_snapshot("ashare_sim")

    def test_rejects_non_regular_ashare_code(self) -> None:
        result = local_sim_ledger.record_local_sim_order(
            {"ts_code": "200011.SZ", "side": "buy", "quantity": 100, "price": 1},
            "ashare",
        )
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["recorded"])

    def test_direct_local_entry_rejects_real_trading_flag_without_append(self) -> None:
        result = local_sim_ledger.record_local_sim_order(
            {
                "order_id": "REAL-FLAG",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "real_trading_enabled": True,
            },
            "ashare",
            {"account": "ashare_sim"},
            {},
        )

        self.assertEqual(result["status"], "rejected")
        self.assertIn("real/live execution is rejected", result["reason"])
        self.assertEqual(
            local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8"), ""
        )

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
            {"account": "ashare_sim"},
            {"local_sim_slippage_bps": 0},
        )

        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["recorded"])
        self.assertIn("candidate_pool_layer=candidate", result["reason"])
        self.assertEqual(
            local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8"), ""
        )

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
            {"account": "ashare_sim"},
            {"local_sim_slippage_bps": 0},
        )

        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["recorded"])
        self.assertIn("execution_source=ashare_rebalance_sell", result["reason"])
        self.assertEqual(
            local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8"), ""
        )

    def test_fresh_bootstrap_snapshot_is_stable_and_cannot_change_50k_cash(
        self,
    ) -> None:
        result = local_sim_ledger.ensure_local_sim_bootstrap_snapshot(
            starting_cash=50000, trade_date="20260706"
        )

        self.assertEqual(result["status"], "snapshot_exists")
        self.assertFalse(result["written"])
        snapshot = json.loads(
            local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot["cash_available"], 50000.0)
        self.assertEqual(snapshot["positions"], [])
        self.assertEqual(
            snapshot["execution_lineage_id"], "ashare-sim-fresh-20260712-v1"
        )

        again = local_sim_ledger.ensure_local_sim_bootstrap_snapshot(
            starting_cash=50000
        )
        self.assertEqual(again["status"], "snapshot_exists")
        snapshot_again = json.loads(
            local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot_again["cash_available"], 50000.0)

        updated = local_sim_ledger.ensure_local_sim_bootstrap_snapshot(
            starting_cash=30000
        )
        self.assertEqual(updated["status"], "rejected")
        self.assertEqual(updated["reason"], "fresh_initial_cash_mismatch")
        snapshot_updated = json.loads(
            local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot_updated["cash_available"], 50000.0)

    def test_fresh_ledger_never_creates_numeric_epoch_metadata(self) -> None:
        result = local_sim_ledger.ensure_local_sim_bootstrap_snapshot(
            starting_cash=50000, trade_date="20260710"
        )
        self.assertEqual(result["status"], "snapshot_exists")

        epoch_meta_path = local_sim_ledger.LOCAL_SIM_DIR / ".epoch_metadata.json"
        self.assertFalse(
            epoch_meta_path.exists(),
            "fresh execution must never create numeric epoch metadata",
        )

    def test_starting_cash_defaults_to_50000_cny(self) -> None:
        """The fresh A-share execution authority has exactly 50,000 CNY."""
        self.assertEqual(local_sim_ledger.ASHARE_SIM_DEFAULT_CASH, 50000.0)

    def test_bootstrap_refuses_parallel_ashare_account(self) -> None:
        result = local_sim_ledger.ensure_local_sim_bootstrap_snapshot(
            account={"account": "parallel_style_account"},
            starting_cash=50000,
        )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(
            result["reason"],
            "ashare_authoritative_account_must_be_ashare_sim",
        )

    def test_order_refuses_parallel_ashare_account_before_append(self) -> None:
        order = self._market_funded(
            {
                "order_id": "SIM-PARALLEL-ACCOUNT",
                "idempotency_key": "SIM:ashare:parallel:20260713:600000.SH:buy",
                "trade_date": "20260713",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
            }
        )

        with self._valid_session():
            result = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "parallel_style_account"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(
            result["reason"],
            "ashare_authoritative_account_must_be_ashare_sim",
        )
        self.assertEqual(
            local_sim_ledger.LOCAL_SIM_TRADES.read_text(encoding="utf-8"),
            "",
        )

    def test_explicit_fresh_projection_has_zero_positions(self) -> None:
        result = local_sim_ledger.ensure_local_sim_bootstrap_snapshot(
            starting_cash=50000,
            trade_date="20260712",
            force=True,
        )
        self.assertEqual(result["status"], "bootstrapped")

        snapshot = json.loads(
            local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT.read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot["positions"], [])
        self.assertEqual(snapshot["bootstrap_state"], "no_trades_yet")
        self.assertEqual(snapshot["cash_available"], 50000.0)
        self.assertEqual(snapshot["pnl"]["ashare_sim"]["total_trades"], 0)
        self.assertEqual(snapshot["pnl"]["ashare_sim"]["cash_available"], 50000.0)

    def test_snapshot_writers_reject_missing_fresh_manifest_before_write(self) -> None:
        old_snapshot = b'{"sentinel":"unchanged"}\n'
        local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT.write_bytes(old_snapshot)
        absent_dir = local_sim_ledger.LOCAL_SIM_DIR / "absent-ledger"

        with (
            patch.object(local_sim_ledger, "LOCAL_SIM_DIR", absent_dir),
            patch.object(
                local_sim_ledger, "LOCAL_SIM_LOCK", absent_dir / ".local_sim.lock"
            ),
        ):
            refreshed = local_sim_ledger.refresh_local_sim_snapshot()
            bootstrapped = local_sim_ledger.ensure_local_sim_bootstrap_snapshot()

        self.assertEqual(refreshed["status"], "rejected")
        self.assertEqual(bootstrapped["status"], "rejected")
        self.assertEqual(
            local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT.read_bytes(), old_snapshot
        )
        self.assertFalse(absent_dir.exists())


class FreshExecutionLineageContractTest(unittest.TestCase):
    @staticmethod
    def _regular_session():
        return patch.object(
            local_sim_ledger,
            "_ashare_session_metadata",
            return_value={
                "trade_timestamp_bj": "2026-07-13T10:00:00+08:00",
                "ashare_session_valid": True,
                "ashare_session_rejection": "",
            },
        )

    def _fresh_root(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "ashare-sim-fresh-20260712-v1"
        paths = {
            "LOCAL_SIM_DIR": root,
            "LOCAL_SIM_TRADES": root / "local_sim_trades.jsonl",
            "LOCAL_SIM_POSITIONS": root / "local_sim_positions.json",
            "LOCAL_SIM_PNL": root / "local_sim_pnl.json",
            "LOCAL_SIM_LOCK": root / ".local_sim.lock",
            "LOCAL_SIM_POSITIONS_SNAPSHOT": root / "simulated_ashare_positions.json",
            "LOCAL_SIM_RECEIPTS": root / "sim_execution_receipts.jsonl",
        }
        for name, value in paths.items():
            patcher = patch.object(local_sim_ledger, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        local_sim_ledger.bootstrap_fresh_local_sim(
            root=root,
            lineage_started_at="2026-07-12T00:00:00+08:00",
            point_in_time_as_of="2026-07-12T00:00:00+08:00",
        )
        return root

    @staticmethod
    def _lineage() -> dict[str, object]:
        lineage = local_sim_ledger.build_execution_lineage(
            lineage_started_at="2026-07-12T00:00:00+08:00",
            point_in_time_as_of="2026-07-13T10:00:00+08:00",
        )
        return {
            **lineage,
            "market_capital_expected_head_event_id": "market-head-1",
            "market_capital_expected_head_checksum": "b" * 64,
        }

    @staticmethod
    def _evidence() -> dict[str, object]:
        return {
            "execution_evidence_class": "verified_5min_market_data",
            "fill_price_source": "sharedsignals_api_realtime_5min",
            "fill_price_source_class": "market_data",
            "bar_time": "2026-07-13T10:00:00+08:00",
            "bar_volume": 100_000.0,
        }

    def test_explicit_bootstrap_creates_zero_import_50k_state_in_namespaced_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ashare-sim-fresh-20260712-v1"
            paths = {
                "LOCAL_SIM_DIR": root,
                "LOCAL_SIM_TRADES": root / "local_sim_trades.jsonl",
                "LOCAL_SIM_POSITIONS": root / "local_sim_positions.json",
                "LOCAL_SIM_PNL": root / "local_sim_pnl.json",
                "LOCAL_SIM_LOCK": root / ".local_sim.lock",
                "LOCAL_SIM_POSITIONS_SNAPSHOT": root
                / "simulated_ashare_positions.json",
                "LOCAL_SIM_RECEIPTS": root / "sim_execution_receipts.jsonl",
            }
            patchers = [
                patch.object(local_sim_ledger, name, value)
                for name, value in paths.items()
            ]
            for patcher in patchers:
                patcher.start()
                self.addCleanup(patcher.stop)

            before = local_sim_ledger.get_local_sim_account_snapshot("ashare_sim")
            self.assertEqual(before["status"], "execution_lineage_unavailable")
            self.assertFalse(
                root.exists(), "a read must never bootstrap the fresh authority"
            )

            result = local_sim_ledger.bootstrap_fresh_local_sim(
                root=root,
                lineage_started_at="2026-07-12T00:00:00+08:00",
                point_in_time_as_of="2026-07-12T00:00:00+08:00",
            )

            self.assertEqual(result["status"], "bootstrapped")
            self.assertEqual(result["capital_authority_id"], "ashare-capital-v1")
            self.assertEqual(result["authority_generation"], 1)
            self.assertEqual(result["execution_lineage_id"], root.name)
            self.assertEqual(result["imported_legacy_record_count"], 0)
            self.assertEqual(result["cash_available"], 50_000.0)
            self.assertEqual(result["positions"], {})
            self.assertEqual(result["realized_pnl"], 0.0)
            self.assertNotIn("capital_epoch", result)
            self.assertEqual(paths["LOCAL_SIM_TRADES"].read_text(encoding="utf-8"), "")
            manifest = local_sim_ledger.get_local_sim_execution_lineage_manifest()
            self.assertEqual(manifest["status"], "ready")
            self.assertEqual(manifest["execution_lineage_id"], root.name)
            order_lineage = local_sim_ledger.build_local_sim_order_lineage(
                point_in_time_as_of="2026-07-13T10:00:00+08:00"
            )
            self.assertEqual(
                order_lineage["point_in_time_as_of"],
                "2026-07-13T10:00:00+08:00",
            )

    def test_partial_fill_projects_nonterminal_atomic_fill_commit(self) -> None:
        root = self._fresh_root()
        order = {
            **self._lineage(),
            "order_id": "FRESH-PARTIAL-1",
            "idempotency_key": "FRESH:ashare:20260713:600000.SH:buy:1",
            "trade_date": "20260713",
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": 200,
            "price": 10.0,
            "capital_scope": "strategy",
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "sample_intent": "exploitation",
            "fill_price_source_class": "market_data",
            "fill_evidence": self._evidence(),
            "market_capital_required": True,
            "market_capital_reference_id": "ASHARE-CAP:FRESH-PARTIAL-1",
            "market_capital_reservation_id": "ares-FRESH-PARTIAL-1",
            "market_capital_event_id": "aevt-FRESH-PARTIAL-1",
            "market_reserved_gross_cny": 2_500.0,
        }
        receipt = SimpleNamespace(
            status="partial",
            filled_qty=100,
            avg_price=10.0,
            raw_response={"partial_terminal": False},
        )
        verification = {
            "verified": True,
            "reason": "reservation_verified",
            "reservation_id": "ares-FRESH-PARTIAL-1",
            "reference_id": "ASHARE-CAP:FRESH-PARTIAL-1",
            "market": "ashare",
            "authority_id": "ashare-capital-v1",
            "authority_generation": 1,
            "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
            "risk_unit_key": "600000.SH",
            "event_id": "aevt-FRESH-PARTIAL-1",
            "remaining_amount_cny": 2_500.0,
            "real_trading_enabled": False,
        }

        with (
            patch.object(
                local_sim_ledger,
                "_ashare_session_metadata",
                return_value={
                    "trade_timestamp_bj": "2026-07-13T10:00:00+08:00",
                    "ashare_session_valid": True,
                    "ashare_session_rejection": "",
                },
            ),
            patch(
                "shared.capital.verify_market_capital_reservation",
                return_value=verification,
            ),
        ):
            result = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
                receipt,
            )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["filled_qty"], 100)
        self.assertEqual(result["market_retained_gross_cny"], 0.0)
        trade = json.loads(
            (root / "local_sim_trades.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        self.assertEqual(trade["quantity"], 100)
        self.assertEqual(trade["capital_authority_id"], "ashare-capital-v1")
        self.assertEqual(trade["authority_generation"], 1)
        self.assertEqual(trade["execution_lineage_id"], root.name)
        self.assertEqual(trade["point_in_time_as_of"], "2026-07-13T10:00:00+08:00")
        self.assertNotIn("capital_epoch", trade)
        self.assertNotIn("master_capital_reservation_id", trade)
        outbox = local_sim_ledger.get_local_sim_market_capital_outbox()
        self.assertEqual(len(outbox["actions"]), 1)
        self.assertEqual(outbox["actions"][0]["action"], "fill_commit")
        self.assertFalse(outbox["actions"][0]["fill_commit_request"]["terminal"])
        pnl = local_sim_ledger.get_local_sim_pnl("ashare_sim")
        self.assertEqual(pnl["capital_authority_id"], "ashare-capital-v1")
        self.assertEqual(pnl["authority_generation"], 1)
        self.assertEqual(pnl["execution_lineage_id"], root.name)
        self.assertEqual(pnl["point_in_time_as_of"], "2026-07-13T10:00:00+08:00")
        snapshot = json.loads(
            (root / "simulated_ashare_positions.json").read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot["point_in_time_as_of"], "2026-07-13T10:00:00+08:00")

    def test_terminal_partial_queues_fill_commit_and_persists_retry_evidence(
        self,
    ) -> None:
        self._fresh_root()
        order = {
            **self._lineage(),
            "order_id": "FRESH-PARTIAL-TERMINAL",
            "idempotency_key": "FRESH:ashare:20260713:600001.SH:buy:terminal",
            "trade_date": "20260713",
            "ts_code": "600001.SH",
            "side": "buy",
            "quantity": 200,
            "price": 10.0,
            "capital_scope": "strategy",
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "sample_intent": "exploitation",
            "fill_price_source_class": "market_data",
            "fill_evidence": self._evidence(),
            "market_capital_required": True,
            "market_capital_reference_id": "ASHARE-CAP:FRESH-PARTIAL-TERMINAL",
            "market_capital_reservation_id": "ares-FRESH-PARTIAL-TERMINAL",
            "market_capital_event_id": "aevt-FRESH-PARTIAL-TERMINAL",
            "market_reserved_gross_cny": 2_500.0,
        }
        receipt = SimpleNamespace(
            status="partial",
            filled_qty=100,
            avg_price=10.0,
            raw_response={"partial_terminal": True},
        )
        verification = {
            "verified": True,
            "reason": "reservation_verified",
            "reservation_id": "ares-FRESH-PARTIAL-TERMINAL",
            "reference_id": "ASHARE-CAP:FRESH-PARTIAL-TERMINAL",
            "market": "ashare",
            "authority_id": "ashare-capital-v1",
            "authority_generation": 1,
            "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
            "risk_unit_key": "600001.SH",
            "event_id": "aevt-FRESH-PARTIAL-TERMINAL",
            "remaining_amount_cny": 2_500.0,
            "real_trading_enabled": False,
        }

        with (
            patch.object(
                local_sim_ledger,
                "_ashare_session_metadata",
                return_value={
                    "trade_timestamp_bj": "2026-07-13T10:00:00+08:00",
                    "ashare_session_valid": True,
                    "ashare_session_rejection": "",
                },
            ),
            patch(
                "shared.capital.verify_market_capital_reservation",
                return_value=verification,
            ),
        ):
            recorded = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
                receipt,
            )

        queued = local_sim_ledger.get_local_sim_market_capital_outbox()
        self.assertEqual(recorded["status"], "partial")
        self.assertTrue(recorded["partial_terminal"])
        self.assertEqual(len(queued["actions"]), 1)
        self.assertEqual(queued["actions"][0]["status"], "pending")
        self.assertEqual(queued["actions"][0]["action"], "fill_commit")
        self.assertTrue(queued["actions"][0]["fill_commit_request"]["terminal"])
        self.assertEqual(
            queued["actions"][0]["fill_commit_request"]["actual_cash_debit_cny"],
            1_005.01,
        )
        self.assertNotIn("capital_epoch", queued["actions"][0])

        with patch(
            "shared.capital.commit_market_capital_fill",
            side_effect=[
                MarketCapitalFillCommitDecision(
                    committed=False,
                    reason="market_capital_unavailable",
                ),
                MarketCapitalFillCommitDecision(
                    committed=True,
                    reason="fill_committed",
                    status="committed",
                    event_id="fill-event-1",
                ),
            ],
        ):
            first = local_sim_ledger.replay_local_sim_market_capital_outbox()
            second = local_sim_ledger.replay_local_sim_market_capital_outbox()

        self.assertEqual(first["pending_count"], 1)
        self.assertEqual(first["actions"][0]["status"], "error")
        self.assertEqual(first["actions"][0]["attempt_count"], 1)
        self.assertTrue(first["actions"][0]["last_error"])
        self.assertEqual(second["pending_count"], 0)
        self.assertEqual(second["actions"][0]["status"], "completed")
        self.assertEqual(second["actions"][0]["attempt_count"], 2)
        self.assertTrue(second["actions"][0]["completed_at"])
        pnl = local_sim_ledger.get_local_sim_pnl("ashare_sim")
        self.assertEqual(pnl["total_trades"], 1)
        self.assertEqual(pnl["sells"], 0)
        self.assertEqual(pnl["realized_pnl"], 0.0)

    def test_fresh_execution_rejects_non_lot_quantity_without_appending(self) -> None:
        root = self._fresh_root()
        order = {
            **self._lineage(),
            "order_id": "FRESH-ODD-LOT",
            "idempotency_key": "FRESH:ashare:20260713:600002.SH:buy:odd",
            "trade_date": "20260713",
            "ts_code": "600002.SH",
            "side": "buy",
            "quantity": 50,
            "price": 10.0,
            "capital_scope": "strategy",
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "sample_intent": "exploitation",
            "fill_price_source_class": "market_data",
            "fill_evidence": self._evidence(),
            "market_capital_required": True,
            "market_capital_reference_id": "ASHARE-CAP:FRESH-ODD-LOT",
            "market_capital_reservation_id": "ares-FRESH-ODD-LOT",
            "market_capital_event_id": "aevt-FRESH-ODD-LOT",
            "market_reserved_gross_cny": 1_000.0,
        }

        result = local_sim_ledger.record_local_sim_order(
            order,
            "ashare",
            {"account": "ashare_sim"},
            {"local_sim_slippage_bps": 0},
        )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "ashare_lot_size_invalid")
        self.assertEqual(
            (root / "local_sim_trades.jsonl").read_text(encoding="utf-8"), ""
        )

    def test_trade_reader_rejects_legacy_epoch_record_inside_fresh_root(self) -> None:
        root = self._fresh_root()
        legacy_row = {
            "order_id": "LEGACY-EPOCH-2",
            "capital_epoch": 2,
            "capital_cny": 50_000.0,
        }
        (root / "local_sim_trades.jsonl").write_text(
            json.dumps(legacy_row, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            local_sim_ledger.LocalSimLedgerCorruption,
            "legacy_numeric_epoch_forbidden",
        ):
            local_sim_ledger.get_local_sim_pnl("ashare_sim")

    def test_stale_initial_capital_cannot_override_fresh_50k_authority(self) -> None:
        self._fresh_root()
        snapshot = local_sim_ledger.get_local_sim_account_snapshot(
            "ashare_sim",
            starting_cash=200_000,
        )
        self.assertEqual(snapshot["status"], "rejected")
        self.assertEqual(snapshot["reason"], "fresh_initial_cash_mismatch")

        order = {
            **self._lineage(),
            "order_id": "FRESH-STALE-CAPITAL",
            "idempotency_key": "FRESH:ashare:20260713:600003.SH:buy:stale-capital",
            "trade_date": "20260713",
            "ts_code": "600003.SH",
            "side": "buy",
            "quantity": 100,
            "price": 600.0,
            "capital_scope": "strategy",
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "sample_intent": "exploitation",
            "fill_price_source_class": "market_data",
            "fill_evidence": self._evidence(),
            "market_capital_required": True,
            "market_capital_reference_id": "ASHARE-CAP:FRESH-STALE-CAPITAL",
            "market_capital_reservation_id": "ares-FRESH-STALE-CAPITAL",
            "market_capital_event_id": "aevt-FRESH-STALE-CAPITAL",
            "market_reserved_gross_cny": 100_000.0,
        }
        with self._regular_session():
            result = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim", "initial_capital": 200_000},
                {"starting_cash": 200_000, "local_sim_slippage_bps": 0},
            )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "fresh_initial_cash_mismatch")

    def test_strategy_buy_requires_reservation_from_same_execution_lineage(
        self,
    ) -> None:
        root = self._fresh_root()
        order = {
            **self._lineage(),
            "order_id": "FRESH-WRONG-RESERVATION-LINEAGE",
            "idempotency_key": "FRESH:ashare:20260713:600004.SH:buy:wrong-lineage",
            "trade_date": "20260713",
            "ts_code": "600004.SH",
            "side": "buy",
            "quantity": 100,
            "price": 10.0,
            "capital_scope": "strategy",
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "sample_intent": "exploitation",
            "fill_price_source_class": "market_data",
            "fill_evidence": self._evidence(),
            "market_capital_required": True,
            "market_capital_reference_id": "ASHARE-CAP:WRONG-LINEAGE",
            "market_capital_reservation_id": "ares-WRONG-LINEAGE",
            "market_capital_event_id": "aevt-WRONG-LINEAGE",
            "market_reserved_gross_cny": 1_500.0,
        }
        verification = {
            "verified": True,
            "reason": "reservation_verified",
            "reservation_id": "ares-WRONG-LINEAGE",
            "reference_id": "ASHARE-CAP:WRONG-LINEAGE",
            "market": "ashare",
            "authority_id": "ashare-capital-v1",
            "authority_generation": 1,
            "execution_lineage_id": "some-other-lineage",
            "risk_unit_key": "600004.SH",
            "event_id": "aevt-WRONG-LINEAGE",
            "remaining_amount_cny": 1_500.0,
            "real_trading_enabled": False,
        }

        with (
            self._regular_session(),
            patch(
                "shared.capital.verify_market_capital_reservation",
                return_value=verification,
            ) as verify,
        ):
            result = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "market_reservation_lineage_mismatch")
        self.assertEqual(
            (root / "local_sim_trades.jsonl").read_text(encoding="utf-8"), ""
        )
        self.assertEqual(
            verify.call_args.kwargs["execution_lineage_id"],
            "ashare-sim-fresh-20260712-v1",
        )
        self.assertEqual(verify.call_args.kwargs["authority_generation"], 1)
        self.assertEqual(verify.call_args.kwargs["risk_unit_key"], "600004.SH")
        self.assertEqual(
            verify.call_args.kwargs["expected_event_id"],
            "aevt-WRONG-LINEAGE",
        )

        wrong_risk_verification = {
            **verification,
            "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
            "risk_unit_key": "600999.SH",
        }
        with (
            self._regular_session(),
            patch(
                "shared.capital.verify_market_capital_reservation",
                return_value=wrong_risk_verification,
            ),
        ):
            wrong_risk = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )
        self.assertEqual(wrong_risk["status"], "rejected")
        self.assertEqual(
            wrong_risk["reason"],
            "market_reservation_lineage_mismatch",
        )

        with (
            self._regular_session(),
            patch(
                "shared.capital.verify_market_capital_reservation"
            ) as verify_wrong_order,
        ):
            wrong_order_risk = local_sim_ledger.record_local_sim_order(
                {
                    **order,
                    "risk_unit_key": "600999.SH",
                    "market_capital_risk_unit_key": "600999.SH",
                },
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )
        self.assertEqual(wrong_order_risk["status"], "rejected")
        self.assertEqual(
            wrong_order_risk["reason"],
            "market_capital_risk_unit_mismatch",
        )
        verify_wrong_order.assert_not_called()

    def test_fresh_lineage_rejects_backdated_execution_record(self) -> None:
        root = self._fresh_root()
        order = {
            **self._lineage(),
            "order_id": "FRESH-BACKDATED",
            "idempotency_key": "FRESH:ashare:20260710:600005.SH:buy:backdated",
            "trade_date": "20260710",
            "ts_code": "600005.SH",
            "side": "buy",
            "quantity": 100,
            "price": 10.0,
            "capital_scope": "strategy",
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "sample_intent": "exploitation",
            "fill_price_source_class": "market_data",
            "fill_evidence": self._evidence(),
            "market_capital_required": True,
            "market_capital_reference_id": "ASHARE-CAP:FRESH-BACKDATED",
            "market_capital_reservation_id": "ares-FRESH-BACKDATED",
            "market_capital_event_id": "aevt-FRESH-BACKDATED",
            "market_reserved_gross_cny": 1_500.0,
        }

        with self._regular_session():
            result = local_sim_ledger.record_local_sim_order(
                order,
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "trade_date_before_execution_lineage")
        self.assertEqual(
            (root / "local_sim_trades.jsonl").read_text(encoding="utf-8"), ""
        )

        with (
            self._regular_session(),
            patch("shared.capital.verify_market_capital_reservation") as verify,
        ):
            future_trade = local_sim_ledger.record_local_sim_order(
                {
                    **order,
                    "order_id": "FRESH-FUTURE-TRADE",
                    "idempotency_key": "FRESH:ashare:20260714:600005.SH:buy:future-trade",
                    "trade_date": "20260714",
                },
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )
            future_evidence = local_sim_ledger.record_local_sim_order(
                {
                    **order,
                    "order_id": "FRESH-FUTURE-EVIDENCE",
                    "idempotency_key": "FRESH:ashare:20260713:600005.SH:buy:future-evidence",
                    "trade_date": "20260713",
                    "fill_evidence": {
                        **self._evidence(),
                        "bar_time": "2026-07-14T10:00:00+08:00",
                    },
                },
                "ashare",
                {"account": "ashare_sim"},
                {"local_sim_slippage_bps": 0},
            )
        self.assertEqual(future_trade["reason"], "trade_date_after_point_in_time")
        self.assertEqual(
            future_evidence["reason"], "execution_evidence_after_point_in_time"
        )
        verify.assert_not_called()

    def test_legacy_epoch_module_only_verifies_freeze_without_mutation(self) -> None:
        root = self._fresh_root()
        with tempfile.TemporaryDirectory() as tmp:
            legacy_root = Path(tmp) / "local_sim"
            legacy_root.mkdir()
            legacy_trade = legacy_root / "local_sim_trades.jsonl"
            legacy_trade.write_bytes(b'{"capital_epoch":1,"quantity":100}\n')
            before = {
                path.relative_to(legacy_root).as_posix(): path.read_bytes()
                for path in legacy_root.rglob("*")
                if path.is_file()
            }

            result = sim_account_epoch.verify_legacy_execution_freeze(
                fresh_root=root,
                legacy_roots=(legacy_root,),
            )

            after = {
                path.relative_to(legacy_root).as_posix(): path.read_bytes()
                for path in legacy_root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(result["status"], "legacy_execution_frozen")
            self.assertTrue(result["fresh_zero_import_verified"])
            self.assertEqual(result["legacy_roots"][0]["record_count"], 1)
            self.assertEqual(after, before)
            with self.assertRaisesRegex(
                sim_account_epoch.LegacyExecutionFreezeError,
                "runtime_cutover_retired",
            ):
                sim_account_epoch.apply_cutover()

    def test_broken_symlinks_cannot_masquerade_as_unavailable_fresh_or_legacy_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fresh_root = base / "ashare-sim-fresh-20260712-v1"
            fresh_root.symlink_to(
                base / "missing-fresh-target", target_is_directory=True
            )
            with patch.object(local_sim_ledger, "LOCAL_SIM_DIR", fresh_root):
                with self.assertRaisesRegex(
                    local_sim_ledger.LocalSimLedgerCorruption,
                    "fresh_execution_symlink_not_allowed",
                ):
                    local_sim_ledger.get_local_sim_execution_lineage_manifest()

            legacy_root = base / "legacy-local-sim"
            legacy_root.symlink_to(
                base / "missing-legacy-target", target_is_directory=True
            )
            with self.assertRaisesRegex(
                sim_account_epoch.LegacyExecutionFreezeError,
                "legacy_evidence_symlink_forbidden",
            ):
                sim_account_epoch._tree_fingerprint(legacy_root)


if __name__ == "__main__":
    unittest.main()
