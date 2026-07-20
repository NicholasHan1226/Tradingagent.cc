#!/usr/bin/env python3
# ruff: noqa: E402
"""Tests for API-backed simulated broker dispatch."""

from __future__ import annotations

import json
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
from Crypto.capital_policy import CRYPTO_CAPITAL_AUTHORITY_ID


ASHARE_CONTRACT = "tradingagent.ashare.paper_broker.v1"
ASHARE_AUTHORITY = "ashare-capital-v1"
CRYPTO_CONTRACT = "tradingagent.crypto.paper_broker.v1"
CRYPTO_AUTHORITY = CRYPTO_CAPITAL_AUTHORITY_ID


class SimBrokerV2Test(unittest.TestCase):
    def setUp(self) -> None:
        self._old_registry = dict(sim_executor_registry._SIM_EXECUTORS)
        sim_executor_registry._SIM_EXECUTORS.clear()
        self._market_verification = patch(
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
        self._market_verification.start()
        self._regular_session = patch.object(
            local_sim_ledger,
            "_ashare_session_metadata",
            return_value={
                "trade_timestamp_bj": "2026-07-13T10:00:00+08:00",
                "ashare_session_valid": True,
                "ashare_session_rejection": "",
            },
        )
        self._regular_session.start()

    def tearDown(self) -> None:
        self._regular_session.stop()
        self._market_verification.stop()
        sim_executor_registry._SIM_EXECUTORS.clear()
        sim_executor_registry._SIM_EXECUTORS.update(self._old_registry)

    def _ashare_account(self, **overrides: object) -> dict[str, object]:
        return {
            "account_id": "ashare_sim",
            "market": "ashare",
            "broker_contract": ASHARE_CONTRACT,
            "authority_id": ASHARE_AUTHORITY,
            "authority_generation": 1,
            **overrides,
        }

    def _crypto_account(self, **overrides: object) -> dict[str, object]:
        return {
            "account_id": "crypto_sim",
            "market": "crypto",
            "broker_contract": CRYPTO_CONTRACT,
            "authority_id": CRYPTO_AUTHORITY,
            "authority_generation": 1,
            **overrides,
        }

    def _market_funded(self, order: dict[str, object]) -> dict[str, object]:
        payload = dict(order)
        identity = str(payload.get("order_id") or "test")
        payload.update(
            {
                **local_sim_ledger.build_execution_lineage(
                    lineage_started_at="2026-07-12T00:00:00+08:00",
                    point_in_time_as_of="2026-07-13T10:00:00+08:00",
                ),
                "capital_scope": "strategy",
                "market_capital_required": True,
                "market_capital_reference_id": f"ASHARE-CAP:{identity}",
                "market_capital_reservation_id": f"ares-{identity}",
                "market_capital_event_id": f"aevt-{identity}",
                "market_capital_expected_head_event_id": f"ahead-{identity}",
                "market_capital_expected_head_checksum": "a" * 64,
                "market_reserved_gross_cny": 100_000.0,
                "real_trading_enabled": False,
                "fill_price_source_class": "market_data",
                "fill_evidence": {
                    "execution_evidence_class": "verified_5min_market_data",
                    "fill_price_source": "tradingdatas_fixture_5min",
                    "fill_price_source_class": "market_data",
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "bar_volume": 100_000.0,
                },
                "market_snapshot": {
                    "ask_price": float(payload.get("price") or 10.0),
                    "bid_price": float(payload.get("price") or 10.0),
                    "last_price": float(payload.get("price") or 10.0),
                    "bar_time": "2026-07-13T10:00:00+08:00",
                    "bar_volume": 100_000.0,
                },
            }
        )
        return payload

    def _bootstrap_fresh_ledger(self, tmp: str) -> Path:
        root = Path(tmp) / "ashare-sim-fresh-20260712-v1"
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

    def test_crypto_executor_cannot_be_registered_or_bypass_capital_ledger(
        self,
    ) -> None:
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
                order_id=str(order["order_id"]),
                market="crypto",
                raw_response={
                    "api_order_id": "stub-123",
                    "broker_contract": CRYPTO_CONTRACT,
                    "authority_id": CRYPTO_AUTHORITY,
                },
                broker_contract=CRYPTO_CONTRACT,
                authority_id=CRYPTO_AUTHORITY,
            )

        with self.assertRaisesRegex(ValueError, "registration disabled"):
            sim_executor_registry.register_sim_executor(
                "crypto",
                stub_executor,
                simulation_contract=CRYPTO_CONTRACT,
                authority_id=CRYPTO_AUTHORITY,
            )

        result = execute_sim_order(
            order={
                "order_id": "SIM-V2-1",
                "ts_code": "BTCUSDT",
                "side": "buy",
                "quantity": 2,
                "authority_generation": 1,
            },
            market="Crypto",
            account=self._crypto_account(),
            config={"venue": "unit"},
        )

        self.assertIsInstance(result, SimResult)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.filled_qty, 0)
        self.assertEqual(result.avg_price, 0)
        self.assertEqual(result.fee, 0)
        self.assertEqual(
            result.raw_response["reason"], "crypto_general_executor_retired"
        )
        self.assertEqual(result.capital_layer, "simulated")
        self.assertEqual(result.account_type, "simulated")
        self.assertEqual(result.order_id, "SIM-V2-1")
        self.assertEqual(result.market, "crypto")
        self.assertEqual(captured, {})

    def test_ashare_pending_order_does_not_record_server_local_backup_fill(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._bootstrap_fresh_ledger(tmp)

            def ashare_pending_executor(order, account, config) -> SimResult:
                return SimResult(
                    status="pending",
                    filled_qty=0,
                    avg_price=0.0,
                    fee=0.0,
                    message="A-share paper broker accepted a pending order",
                    order_id=str(order["order_id"]),
                    market="ashare",
                    raw_response={
                        "mode": "paper_broker_pending",
                        "broker_contract": ASHARE_CONTRACT,
                        "authority_id": ASHARE_AUTHORITY,
                    },
                    broker_contract=ASHARE_CONTRACT,
                    authority_id=ASHARE_AUTHORITY,
                )

            sim_executor_registry.register_sim_executor(
                "ashare",
                ashare_pending_executor,
                simulation_contract=ASHARE_CONTRACT,
                authority_id=ASHARE_AUTHORITY,
            )

            result = execute_sim_order(
                order=self._market_funded(
                    {
                        "order_id": "SIM-ASHARE-DUAL-1",
                        "idempotency_key": "SIM:ashare:acct:20260713:600000.SH:buy",
                        "trade_date": "20260713",
                        "ts_code": "600000.SH",
                        "side": "buy",
                        "quantity": 100,
                        "price": 10.0,
                        "candidate_pool_layer": "candidate",
                        "execution_source": "ashare_candidate_layer",
                    }
                ),
                market="ashare",
                account=self._ashare_account(),
                config={
                    "local_sim_slippage_bps": 0,
                    "market_session_now": "2026-07-13T10:00:00+08:00",
                },
            )

            self.assertEqual(result.status, "pending")
            backup = result.raw_response.get("local_sim_backup", {})
            self.assertEqual(backup, {})
            pnl = local_sim_ledger.get_local_sim_pnl("ashare_sim")
            self.assertEqual(pnl["total_trades"], 0)
            self.assertEqual(pnl["positions"], {})
            self.assertEqual(
                (root / "sim_execution_receipts.jsonl").read_text(encoding="utf-8"), ""
            )

    def test_ashare_builtin_executor_records_into_explicit_fresh_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._bootstrap_fresh_ledger(tmp)

            result = execute_sim_order(
                order=self._market_funded(
                    {
                        "order_id": "SIM-ASHARE-BUILTIN-1",
                        "trade_date": "20260713",
                        "ts_code": "600000.SH",
                        "side": "buy",
                        "quantity": 100,
                        "price": 10.0,
                        "candidate_pool_layer": "candidate",
                        "execution_source": "ashare_candidate_layer",
                    }
                ),
                market="ashare",
                account=self._ashare_account(),
                config={
                    "local_sim_slippage_bps": 0,
                    "market_session_now": "2026-07-13T10:00:00+08:00",
                },
            )

            self.assertEqual(result.status, "filled", result.raw_response)
            self.assertEqual(result.raw_response.get("mode"), "server_local_sim_engine")
            self.assertTrue(
                result.raw_response.get("local_sim_backup", {}).get("recorded")
            )
            self.assertTrue((root / "local_sim_trades.jsonl").exists())
            self.assertTrue((root / "simulated_ashare_positions.json").exists())
            self.assertTrue((root / "sim_execution_receipts.jsonl").exists())
            backup = result.raw_response["local_sim_backup"]
            self.assertEqual(backup["capital_authority_id"], "ashare-capital-v1")
            self.assertEqual(backup["authority_generation"], 1)
            self.assertEqual(
                backup["execution_lineage_id"],
                "ashare-sim-fresh-20260712-v1",
            )
            self.assertEqual(backup["market_capital_risk_unit_key"], "600000.SH")
            self.assertNotIn("capital_epoch", backup)

    def test_ashare_builtin_executor_rejects_unbound_string_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._bootstrap_fresh_ledger(tmp)

            result = execute_sim_order(
                order=self._market_funded(
                    {
                        "order_id": "SIM-ASHARE-STRING-ACCOUNT",
                        "trade_date": "20260713",
                        "ts_code": "600000.SH",
                        "side": "buy",
                        "quantity": 100,
                        "price": 10.0,
                        "candidate_pool_layer": "candidate",
                        "execution_source": "ashare_candidate_layer",
                    }
                ),
                market="ashare",
                account="ashare_sim",
                config={
                    "local_sim_slippage_bps": 0,
                    "market_session_now": "2026-07-13T10:00:00+08:00",
                },
            )

            self.assertEqual(result.status, "failed")
            self.assertEqual(
                result.raw_response.get("reason"), "sim_account_binding_invalid"
            )

    def test_ashare_builtin_executor_rejects_second_fill_when_ledger_cash_is_exhausted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._bootstrap_fresh_ledger(tmp)

            first = execute_sim_order(
                order=self._market_funded(
                    {
                        "order_id": "SIM-ASHARE-CASH-1",
                        "trade_date": "20260713",
                        "ts_code": "600000.SH",
                        "side": "buy",
                        "quantity": 4000,
                        "price": 10.0,
                        "candidate_pool_layer": "candidate",
                        "execution_source": "ashare_candidate_layer",
                    }
                ),
                market="ashare",
                account=self._ashare_account(cash_available=50000.0),
                config={
                    "local_sim_slippage_bps": 0,
                    "market_session_now": "2026-07-13T10:00:00+08:00",
                },
            )
            second = execute_sim_order(
                order=self._market_funded(
                    {
                        "order_id": "SIM-ASHARE-CASH-2",
                        "trade_date": "20260713",
                        "ts_code": "600001.SH",
                        "side": "buy",
                        "quantity": 1000,
                        "price": 10.0,
                        "candidate_pool_layer": "candidate",
                        "execution_source": "ashare_candidate_layer",
                    }
                ),
                market="ashare",
                account=self._ashare_account(cash_available=50000.0),
                config={
                    "local_sim_slippage_bps": 0,
                    "market_session_now": "2026-07-13T10:00:00+08:00",
                },
            )

            self.assertEqual(first.status, "filled")
            self.assertEqual(second.status, "rejected")
            self.assertEqual(
                second.raw_response["local_sim_backup"]["reason"], "insufficient_cash"
            )
            trades = [
                json.loads(line)
                for line in local_sim_ledger.LOCAL_SIM_TRADES.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            self.assertEqual(len(trades), 1)

    def test_ashare_builtin_executor_rejects_buy_without_candidate_provenance(
        self,
    ) -> None:
        calls: list[object] = []

        def ashare_executor(order, account, config) -> SimResult:
            calls.append((order, account, config))
            return SimResult(
                status="filled",
                filled_qty=100,
                avg_price=10.0,
                order_id=str(order["order_id"]),
                market="ashare",
                broker_contract=ASHARE_CONTRACT,
                authority_id=ASHARE_AUTHORITY,
            )

        sim_executor_registry.register_sim_executor(
            "ashare",
            ashare_executor,
            simulation_contract=ASHARE_CONTRACT,
            authority_id=ASHARE_AUTHORITY,
        )

        result = execute_sim_order(
            order={
                "order_id": "SIM-ASHARE-NO-PROVENANCE",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
            },
            market="ashare",
            account=self._ashare_account(),
            config={"local_sim_slippage_bps": 0},
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("candidate_pool_layer=candidate", result.message)
        self.assertEqual(calls, [])

    def test_execute_sim_order_rejects_real_payload_before_sanitizing(self) -> None:
        calls: list[object] = []

        result = execute_sim_order(
            order={"order_id": "SIM-V2-REAL", "ts_code": "BTCUSDT", "quantity": 1},
            market="Crypto",
            account=self._crypto_account(account_type="real"),
            config={},
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("real/live execution is rejected", result.message)
        self.assertEqual(calls, [])

    def test_execute_sim_order_rejects_real_trading_enabled_before_executor(
        self,
    ) -> None:
        calls: list[object] = []

        result = execute_sim_order(
            order={
                "order_id": "SIM-V2-REAL-FLAG",
                "ts_code": "BTCUSDT",
                "quantity": 1,
                "real_trading_enabled": True,
            },
            market="Crypto",
            account=self._crypto_account(),
            config={},
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("real/live execution is rejected", result.message)
        self.assertEqual(calls, [])

    def test_current_executor_cannot_return_real_receipt_markers(self) -> None:
        def real_receipt(order, account, config):
            return {
                "status": "filled",
                "filled_qty": 100,
                "avg_price": 10.0,
                "capital_layer": "real",
                "account_type": "real",
                "market": "ashare",
                "broker_contract": ASHARE_CONTRACT,
                "authority_id": ASHARE_AUTHORITY,
            }

        sim_executor_registry.register_sim_executor(
            "ashare",
            real_receipt,
            simulation_contract=ASHARE_CONTRACT,
            authority_id=ASHARE_AUTHORITY,
        )

        result = execute_sim_order(
            order=self._market_funded(
                {
                    "order_id": "SIM-ASHARE-REAL-RECEIPT",
                    "ts_code": "600000.SH",
                    "side": "buy",
                    "quantity": 100,
                    "price": 10.0,
                    "candidate_pool_layer": "candidate",
                    "execution_source": "ashare_candidate_layer",
                }
            ),
            market="ashare",
            account=self._ashare_account(),
            config={"local_sim_slippage_bps": 0},
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("real/live execution is rejected", result.message)

    def test_current_executor_receipt_must_match_registered_binding(self) -> None:
        def wrong_binding(order, account, config) -> SimResult:
            variants = {
                "market": {
                    "market": "cn_futures",
                    "broker_contract": ASHARE_CONTRACT,
                    "authority_id": ASHARE_AUTHORITY,
                },
                "contract": {
                    "market": "ashare",
                    "broker_contract": "wrong.contract.v1",
                    "authority_id": ASHARE_AUTHORITY,
                },
                "authority": {
                    "market": "ashare",
                    "broker_contract": ASHARE_CONTRACT,
                    "authority_id": "wrong-authority",
                },
            }
            binding = variants[str(order["receipt_variant"])]
            return SimResult(
                status="filled",
                filled_qty=100,
                avg_price=10.0,
                order_id=str(order["order_id"]),
                **binding,
            )

        sim_executor_registry.register_sim_executor(
            "ashare",
            wrong_binding,
            simulation_contract=ASHARE_CONTRACT,
            authority_id=ASHARE_AUTHORITY,
        )

        for variant in ("market", "contract", "authority"):
            with self.subTest(variant=variant):
                result = execute_sim_order(
                    order=self._market_funded(
                        {
                            "order_id": f"SIM-ASHARE-WRONG-{variant.upper()}",
                            "ts_code": "600000.SH",
                            "side": "buy",
                            "quantity": 100,
                            "price": 10.0,
                            "candidate_pool_layer": "candidate",
                            "execution_source": "ashare_candidate_layer",
                            "receipt_variant": variant,
                        }
                    ),
                    market="ashare",
                    account=self._ashare_account(),
                    config={"local_sim_slippage_bps": 0},
                )

                self.assertEqual(result.status, "failed")
                self.assertEqual(
                    result.raw_response["reason"], "sim_receipt_binding_mismatch"
                )

    def test_registered_crypto_receipt_factory_is_never_invoked(self) -> None:
        calls: list[object] = []

        def filled_receipt(order, account, config) -> SimResult:
            calls.append((order, account, config))
            return SimResult(
                status="filled",
                filled_qty=1,
                avg_price=1.0,
                market="crypto",
                broker_contract=CRYPTO_CONTRACT,
                authority_id=CRYPTO_AUTHORITY,
            )

        with self.assertRaisesRegex(ValueError, "registration disabled"):
            sim_executor_registry.register_sim_executor(
                "crypto",
                filled_receipt,
                simulation_contract=CRYPTO_CONTRACT,
                authority_id=CRYPTO_AUTHORITY,
            )
        result = execute_sim_order(
            order={
                "order_id": "SIM-NO-GENERAL-CRYPTO",
                "symbol": "BTCUSDT",
                "quantity": 1,
                "authority_generation": 1,
            },
            market="crypto",
            account=self._crypto_account(),
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.raw_response["reason"], "crypto_general_executor_retired"
        )
        self.assertEqual(calls, [])

    def test_registry_rejects_wrong_contract_and_silent_override(self) -> None:
        def first(order, account, config):
            return {}

        def second(order, account, config):
            return {}

        with self.assertRaisesRegex(ValueError, "requires simulation_contract"):
            sim_executor_registry.register_sim_executor(
                "ashare",
                first,
                simulation_contract=CRYPTO_CONTRACT,
                authority_id=ASHARE_AUTHORITY,
            )
        sim_executor_registry.register_sim_executor(
            "ashare",
            first,
            simulation_contract=ASHARE_CONTRACT,
            authority_id=ASHARE_AUTHORITY,
        )
        with self.assertRaisesRegex(ValueError, "already registered"):
            sim_executor_registry.register_sim_executor(
                "ashare",
                second,
                simulation_contract=ASHARE_CONTRACT,
                authority_id=ASHARE_AUTHORITY,
            )

    def test_any_local_ledger_rejection_overrides_preledger_filled_status(self) -> None:
        def ashare_executor(order, account, config) -> SimResult:
            return SimResult(
                status="filled",
                filled_qty=100,
                avg_price=10.0,
                order_id=str(order["order_id"]),
                market="ashare",
                broker_contract=ASHARE_CONTRACT,
                authority_id=ASHARE_AUTHORITY,
            )

        sim_executor_registry.register_sim_executor(
            "ashare",
            ashare_executor,
            simulation_contract=ASHARE_CONTRACT,
            authority_id=ASHARE_AUTHORITY,
        )
        with patch.object(
            local_sim_ledger,
            "record_local_sim_order",
            return_value={
                "status": "rejected",
                "recorded": False,
                "reason": "market_reservation_underfunded",
            },
        ):
            result = execute_sim_order(
                order={
                    "order_id": "SIM-ASHARE-LEDGER-REJECT",
                    "ts_code": "600000.SH",
                    "side": "buy",
                    "quantity": 100,
                    "price": 10.0,
                    "candidate_pool_layer": "candidate",
                    "execution_source": "ashare_candidate_layer",
                    "authority_generation": 1,
                },
                market="ashare",
                account=self._ashare_account(),
                config={"local_sim_slippage_bps": 0},
            )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.filled_qty, 0)
        self.assertEqual(
            result.raw_response["local_sim_backup"]["reason"],
            "market_reservation_underfunded",
        )

    def test_local_ledger_failure_cannot_leave_preledger_fill_successful(self) -> None:
        def ashare_executor(order, account, config) -> SimResult:
            return SimResult(
                status="filled",
                filled_qty=100,
                avg_price=10.0,
                order_id=str(order["order_id"]),
                market="ashare",
                broker_contract=ASHARE_CONTRACT,
                authority_id=ASHARE_AUTHORITY,
            )

        sim_executor_registry.register_sim_executor(
            "ashare",
            ashare_executor,
            simulation_contract=ASHARE_CONTRACT,
            authority_id=ASHARE_AUTHORITY,
        )
        with patch.object(
            local_sim_ledger,
            "record_local_sim_order",
            side_effect=local_sim_ledger.LocalSimLedgerCorruption(
                "corrupt_local_sim_trade:1"
            ),
        ):
            result = execute_sim_order(
                order=self._market_funded(
                    {
                        "order_id": "SIM-ASHARE-LEDGER-FAIL",
                        "ts_code": "600000.SH",
                        "side": "buy",
                        "quantity": 100,
                        "price": 10.0,
                        "candidate_pool_layer": "candidate",
                        "execution_source": "ashare_candidate_layer",
                    }
                ),
                market="ashare",
                account=self._ashare_account(),
                config={"local_sim_slippage_bps": 0},
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.filled_qty, 0)
        self.assertIn("corrupt_local_sim_trade", result.message)

    def test_sim_result_rejects_invalid_numeric_or_status_combinations(self) -> None:
        with self.assertRaisesRegex(ValueError, "avg_price"):
            SimResult(status="filled", filled_qty=1, avg_price=float("inf"))
        with self.assertRaisesRegex(ValueError, "requires positive"):
            SimResult(status="filled", filled_qty=0, avg_price=0)
        with self.assertRaisesRegex(ValueError, "cannot carry fill"):
            SimResult(status="rejected", filled_qty=1, avg_price=1)

    def test_sim_result_scans_preconstructed_raw_response_for_live_markers(
        self,
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "real/live execution"):
            SimResult(
                status="filled",
                filled_qty=1,
                avg_price=1,
                raw_response={"broker_mode": "live"},
            )

    def test_dry_run_status_never_normalizes_to_a_fill(self) -> None:
        result = SimResult(status="dry_run_ok")

        self.assertEqual(result.status, "pending")


if __name__ == "__main__":
    unittest.main()
