# ruff: noqa: E402
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Ashare.adapter import AshareAdapter
from Ashare.sim_executor import (
    ashare_sim_execute,
    build_test_only_ashare_mock_config,
)
from shared.execution import local_sim_ledger
from shared.execution.sim_executor_registry import get_sim_executor


def _fresh_market_snapshot(
    *,
    price: float = 10.5,
    side: str = "buy",
    bar_volume: int = 100_000,
) -> dict[str, object]:
    quote_field = "ask_price" if side == "buy" else "bid_price"
    return {
        quote_field: price,
        "last_price": price,
        "bar_time": "2026-07-07 09:55:00",
        "bar_volume": bar_volume,
        "provider": "tradingdatas_fixture_5min",
    }


class AshareSimExecutorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp_path = Path(self.tmpdir.name)
        now_patcher = patch(
            "Ashare.sim_executor._now_cn",
            return_value=datetime(2026, 7, 7, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        now_patcher.start()
        self.addCleanup(now_patcher.stop)

    def _patch_local_sim_paths(self) -> None:
        base = self.tmp_path / local_sim_ledger.ASHARE_EXECUTION_LINEAGE_ID
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
        local_sim_ledger.bootstrap_fresh_local_sim(
            root=base,
            lineage_started_at="2026-07-07T09:00:00+08:00",
            point_in_time_as_of="2026-07-07T09:00:00+08:00",
        )

    def test_adapter_exposes_ashare_sim_account_without_breaking_shadow_account(
        self,
    ) -> None:
        adapter = AshareAdapter(reader=object())

        self.assertEqual(adapter.get_shadow_account(), "ashare_shadow")
        self.assertEqual(adapter.get_sim_account()["account"], "ashare_sim")

    def test_retired_mini_bridge_config_fails_closed(self) -> None:
        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-1",
                "ts_code": "600000.SH",
                "quantity": 100,
                "price": 10.5,
                "side": "buy",
                "market_snapshot": _fresh_market_snapshot(),
            },
            account={"account_id": "ashare_sim"},
            config={"hermes_enabled": True},
        )

        self.assertEqual(result.status, "rejected")
        self.assertIn("mini_hermes_bridge_retired", result.message)
        self.assertEqual(
            result.raw_response["broker_contract"],
            "tradingagent.ashare.paper_broker.v1",
        )
        self.assertEqual(
            result.raw_response["retired_inputs"], ["config:hermes_enabled"]
        )

    def test_retired_mini_bridge_environment_fails_closed(self) -> None:
        with patch.dict("os.environ", {"ASHARE_SIM_WEBHOOK_ENABLED": "1"}):
            result = ashare_sim_execute(
                order={
                    "order_id": "SIM-ASHARE-ENV-BRIDGE",
                    "ts_code": "600000.SH",
                    "quantity": 100,
                    "price": 10.5,
                    "side": "buy",
                },
                account={"account_id": "ashare_sim"},
            )

        self.assertEqual(result.status, "rejected")
        self.assertIn("mini_hermes_bridge_retired", result.message)
        self.assertEqual(
            result.raw_response["retired_inputs"],
            ["env:ASHARE_SIM_WEBHOOK_ENABLED"],
        )

    def test_ashare_sim_execute_rejects_non_a_share(self) -> None:
        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-BSHARE",
                "ts_code": "200521.SZ",
                "quantity": 100,
                "price": 10.5,
                "side": "buy",
                "market_snapshot": _fresh_market_snapshot(),
            },
            account={"account_id": "ashare_sim"},
        )

        self.assertEqual(result.status, "rejected")

    def test_ashare_sim_execute_uses_market_specific_server_local_broker(
        self,
    ) -> None:
        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-LOCAL",
                "ts_code": "600000.SH",
                "quantity": 100,
                "price": 10.5,
                "side": "buy",
                "market_snapshot": _fresh_market_snapshot(),
            },
            account={"account_id": "ashare_sim"},
        )

        self.assertEqual(result.status, "filled")
        self.assertEqual(result.filled_qty, 100)
        self.assertGreaterEqual(result.avg_price, 10.5)
        self.assertEqual(result.capital_layer, "simulated")
        self.assertEqual(result.account_type, "simulated")
        self.assertEqual(result.raw_response["mode"], "server_local_sim_engine")
        self.assertEqual(
            result.raw_response["broker_contract"],
            "tradingagent.ashare.paper_broker.v1",
        )
        self.assertEqual(result.raw_response["engine_record"]["state"], "filled")

    def test_ashare_sim_execute_rejects_outside_regular_session_before_any_execution(
        self,
    ) -> None:
        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-AFTER-CLOSE",
                "ts_code": "600000.SH",
                "quantity": 100,
                "price": 10.5,
                "side": "buy",
            },
            account={"account_id": "ashare_sim"},
            config={"market_session_now": "2026-07-07T15:01:00+08:00"},
        )

        self.assertEqual(result.status, "rejected")
        self.assertIn("market_closed", result.message)

    def test_ashare_sim_execute_classifies_closing_auction_as_explicitly_unsupported(
        self,
    ) -> None:
        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-CLOSING-AUCTION",
                "ts_code": "600000.SH",
                "quantity": 100,
                "price": 10.5,
                "side": "buy",
                "order_type": "limit",
            },
            account={"account_id": "ashare_sim"},
            config={"market_session_now": "2026-07-07T14:58:00+08:00"},
        )

        self.assertEqual(result.status, "rejected")
        self.assertIn("closing_auction_batch_match_not_implemented", result.message)
        self.assertEqual(result.raw_response["market_session"], "closing_auction")
        self.assertEqual(
            result.raw_response["execution_reality_model_version"],
            "ashare-execution-reality-20260706-v1",
        )

    def test_ashare_sim_execute_classifies_after_hours_fixed_price_as_unsupported(
        self,
    ) -> None:
        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-AFTER-HOURS",
                "ts_code": "600000.SH",
                "quantity": 100,
                "price": 10.5,
                "side": "buy",
                "order_type": "after_hours_fixed_price",
            },
            account={"account_id": "ashare_sim"},
            config={"market_session_now": "2026-07-07T15:10:00+08:00"},
        )

        self.assertEqual(result.status, "rejected")
        self.assertIn("after_hours_fixed_price_match_not_implemented", result.message)
        self.assertEqual(
            result.raw_response["market_session"], "after_hours_fixed_price"
        )
        self.assertEqual(
            result.raw_response["required_order_type"],
            "after_hours_fixed_price",
        )

    def test_ashare_sim_execute_rejects_holiday_even_during_regular_clock_time(
        self,
    ) -> None:
        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-HOLIDAY",
                "ts_code": "600000.SH",
                "quantity": 100,
                "price": 10.5,
                "side": "buy",
            },
            account={"account_id": "ashare_sim"},
            config={"market_session_now": "2026-10-01T10:00:00+08:00"},
        )

        self.assertEqual(result.status, "rejected")
        self.assertIn("market_closed", result.message)

    def test_ashare_server_local_fill_rejects_non_lot_buy(self) -> None:
        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-NONLOT",
                "ts_code": "600000.SH",
                "quantity": 120,
                "price": 10.5,
                "side": "buy",
                "market_snapshot": _fresh_market_snapshot(),
            },
            account={"account_id": "ashare_sim", "cash_available": 50_000},
        )

        self.assertEqual(result.status, "rejected")
        self.assertIn("buy_quantity_not_lot_aligned", result.message)
        self.assertEqual(
            result.raw_response["engine_record"]["reason"],
            "buy_quantity_not_lot_aligned",
        )

    def test_ashare_server_local_fill_uses_bar_volume_when_book_size_missing(
        self,
    ) -> None:
        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-BARVOL",
                "ts_code": "600000.SH",
                "quantity": 300,
                "price": 10.5,
                "side": "buy",
                "bar_volume": 1500,
                "market_snapshot": _fresh_market_snapshot(bar_volume=1500),
            },
            account={"account_id": "ashare_sim", "cash_available": 50_000},
        )

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.filled_qty, 100)
        self.assertEqual(result.raw_response["engine_record"]["state"], "partial")

    def test_ashare_server_local_fill_marks_verified_5min_market_evidence(self) -> None:
        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-5MIN-EVIDENCE",
                "ts_code": "600000.SH",
                "quantity": 100,
                "price": 10.5,
                "side": "buy",
                "market_snapshot": {
                    "ask_price": 10.5,
                    "last_price": 10.5,
                    "bar_time": "2026-07-07 09:55:00",
                    "bar_volume": 1500,
                    "provider": "tradingdatas_fixture_5min",
                },
            },
            account={"account_id": "ashare_sim", "cash_available": 50_000},
        )

        evidence = result.raw_response["fill_evidence"]
        self.assertEqual(result.status, "filled")
        self.assertEqual(evidence["bar_time"], "2026-07-07 09:55:00")
        self.assertEqual(evidence["bar_volume"], 1500)
        self.assertEqual(
            evidence["execution_evidence_class"], "verified_5min_market_data"
        )

    def test_ashare_server_local_fill_downgrades_stale_5min_market_evidence(
        self,
    ) -> None:
        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-STALE-5MIN",
                "ts_code": "600000.SH",
                "quantity": 100,
                "price": 10.5,
                "side": "buy",
                "market_snapshot": {
                    "ask_price": 10.5,
                    "last_price": 10.5,
                    "bar_time": "2026-07-07 09:35:00",
                    "bar_volume": 1500,
                    "provider": "tradingdatas_fixture_5min",
                },
            },
            account={"account_id": "ashare_sim", "cash_available": 50_000},
        )

        evidence = result.raw_response["fill_evidence"]
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.message, "unverified_execution_evidence")
        self.assertEqual(
            result.raw_response["reason_code"],
            "verified_fresh_5min_market_data_required",
        )
        self.assertEqual(evidence["execution_evidence_class"], "weak_price_only")
        self.assertEqual(evidence["evidence_reason"], "stale_or_future_5min_bar")

    def test_ashare_server_local_fill_rejects_same_day_t1_sell_from_ledger(
        self,
    ) -> None:
        self._patch_local_sim_paths()
        local_sim_ledger.record_local_sim_order(
            {
                "order_id": "SEED-BUY",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "trade_date": "2026-07-03",
            },
            "ashare",
            "ashare_sim",
            {"local_sim_slippage_bps": 0},
        )

        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-T1-SELL",
                "ts_code": "600000.SH",
                "quantity": 100,
                "price": 10.0,
                "side": "sell",
                "trade_date": "2026-07-03",
                "market_snapshot": _fresh_market_snapshot(
                    price=10.0, side="sell"
                ),
            },
            account="ashare_sim",
        )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(
            result.raw_response["engine_record"]["reason"],
            "insufficient_sellable_qty_t1",
        )

    def test_ashare_server_local_fill_rejects_insufficient_default_cash(self) -> None:
        self._patch_local_sim_paths()

        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-CASH",
                "ts_code": "600000.SH",
                "quantity": 20000,
                "price": 10.0,
                "side": "buy",
                "market_snapshot": _fresh_market_snapshot(price=10.0),
            },
            account="ashare_sim",
        )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(
            result.raw_response["engine_record"]["reason"], "insufficient_cash"
        )

    def test_ashare_sim_execute_supports_local_mock_fill(self) -> None:
        result = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-MOCK",
                "ts_code": "600519.SH",
                "quantity": 200,
                "price": 123.45,
                "side": "buy",
            },
            account="ashare_sim",
            config=build_test_only_ashare_mock_config(mock_fee=1.23),
        )

        self.assertEqual(result.status, "filled")
        self.assertEqual(result.filled_qty, 200)
        self.assertEqual(result.avg_price, 123.45)
        self.assertEqual(result.fee, 1.23)
        self.assertEqual(result.capital_layer, "simulated")
        self.assertEqual(result.account_type, "simulated")
        self.assertEqual(result.raw_response["mode"], "mock_filled")

    def test_registered_executor_is_ashare_specific(self) -> None:
        executor = get_sim_executor("ashare")
        self.assertIs(executor, ashare_sim_execute)

    def test_direct_ashare_executor_rejects_real_markers_before_mock_fill(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            ashare_sim_execute(
                order={
                    "order_id": "SIM-ASHARE-REAL",
                    "ts_code": "600000.SH",
                    "quantity": 100,
                    "price": 10.0,
                    "capital_layer": "real",
                },
                account={"account_id": "ashare_sim"},
                config={"mock_filled": True},
            )

    def test_serialized_mock_flag_and_fractional_lot_fail_closed(self) -> None:
        serialized_mock = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-SERIALIZED-MOCK",
                "ts_code": "600000.SH",
                "quantity": 100,
                "price": 10.0,
                "side": "buy",
            },
            account="ashare_sim",
            config={"mock_filled": True},
        )
        fractional_lot = ashare_sim_execute(
            order={
                "order_id": "SIM-ASHARE-FRACTIONAL",
                "ts_code": "600000.SH",
                "quantity": 100.5,
                "price": 10.0,
                "side": "buy",
            },
            account="ashare_sim",
            config=build_test_only_ashare_mock_config(),
        )

        self.assertEqual(serialized_mock.status, "rejected")
        self.assertEqual(
            serialized_mock.raw_response["reason_code"],
            "test_only_mock_token_required",
        )
        self.assertEqual(fractional_lot.status, "rejected")
        self.assertIn("non-positive quantity", fractional_lot.message)


if __name__ == "__main__":
    unittest.main()
