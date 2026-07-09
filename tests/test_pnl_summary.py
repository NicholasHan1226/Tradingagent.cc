from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.execution import local_sim_ledger
from shared.review import pnl_summary


class PnlSummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger_root = self.root / "ledger"
        self.local_sim = self.root / "local_sim" / "local_sim_trades.jsonl"

    def test_style_ledger_pnl_aggregates_realized_and_unrealized(self) -> None:
        journal = self.ledger_root / "crypto" / "balanced" / "trade_journal.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(
            json.dumps(
                {
                    "timestamp": "2026-07-04T00:00:00+00:00",
                    "order_id": "B1",
                    "fill_id": "F1",
                    "symbol": "BTCUSDT",
                    "side": "buy",
                    "fill_qty": 1.0,
                    "fill_price": 100.0,
                    "realized_pnl": 0.0,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = pnl_summary.sim_ledger_pnl_summary(
            markets=("crypto",),
            ledger_root=self.ledger_root,
            local_trades_path=self.local_sim,
        )

        crypto = result["crypto"]
        self.assertEqual(crypto["pnl_source"], "sim_ledger_mark_to_market")
        self.assertEqual(crypto["realized_pnl"], 0.0)
        self.assertEqual(crypto["unrealized_pnl"], 0.0)
        self.assertEqual(crypto["total_pnl"], 0.0)
        self.assertEqual(crypto["open_position_count"], 1)
        # The latest journal fill price is used as the mark, so no missing mark.
        self.assertEqual(crypto["missing_mark_count"], 0)

    def test_ashare_local_sim_uses_mark_prices_when_provided(self) -> None:
        base = self.root / "local_sim"
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

        local_sim_ledger.record_local_sim_order(
            {
                "order_id": "SIM-1",
                "idempotency_key": "idem-1",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
            },
            "ashare",
            {"account": "acct"},
            {"local_sim_slippage_bps": 0},
        )

        result = pnl_summary.sim_ledger_pnl_summary(
            markets=("ashare",),
            ledger_root=self.ledger_root,
            local_trades_path=local_sim_ledger.LOCAL_SIM_TRADES,
            ashare_mark_prices={"600000.SH": 12.0},
        )

        ashare = result["ashare"]
        self.assertEqual(ashare["pnl_source"], "ashare_local_sim_mark_to_market")
        # cost_basis includes commission (max(1000*0.00025, 5.0) = 5.0), so
        # unrealized = 100 * 12.0 - (100 * 10.0 + 5.0) = 195.0.
        self.assertEqual(ashare["realized_pnl"], 0.0)
        self.assertEqual(ashare["unrealized_pnl"], 195.0)
        self.assertEqual(ashare["total_pnl"], 195.0)
        self.assertEqual(ashare["open_position_count"], 1)
        self.assertEqual(ashare["missing_mark_count"], 0)

    def test_ashare_local_sim_auto_loads_mark_prices_from_sharedsignals(self) -> None:
        base = self.root / "local_sim"
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

        local_sim_ledger.record_local_sim_order(
            {
                "order_id": "SIM-1",
                "idempotency_key": "idem-1",
                "ts_code": "600000.SH",
                "side": "buy",
                "quantity": 100,
                "price": 10.0,
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
            },
            "ashare",
            {"account": "acct"},
            {"local_sim_slippage_bps": 0},
        )

        with patch("shared.review.pnl_summary.load_mark_prices_for_positions", return_value={"600000.SH": 12.0}) as loader:
            result = pnl_summary.sim_ledger_pnl_summary(
                markets=("ashare",),
                ledger_root=self.ledger_root,
                local_trades_path=local_sim_ledger.LOCAL_SIM_TRADES,
            )

        ashare = result["ashare"]
        loader.assert_called_once()
        self.assertEqual(ashare["pnl_source"], "ashare_local_sim_mark_to_market")
        self.assertEqual(ashare["unrealized_pnl"], 195.0)
        self.assertEqual(ashare["missing_mark_count"], 0)

    def test_empty_markets_return_zero_pnl(self) -> None:
        result = pnl_summary.sim_ledger_pnl_summary(
            markets=("crypto", "ashare"),
            ledger_root=self.ledger_root,
            local_trades_path=self.local_sim,
        )
        self.assertEqual(result["crypto"]["total_pnl"], 0.0)
        self.assertEqual(result["ashare"]["total_pnl"], 0.0)

    def test_ashare_missing_provenance_is_validation_sample_not_strategy_pnl(self) -> None:
        self.local_sim.parent.mkdir(parents=True)
        self.local_sim.write_text(
            json.dumps(
                {
                    "trade_id": "LSIM-VALIDATION",
                    "order_id": "SIM-OLD",
                    "idempotency_key": "old",
                    "market": "ashare",
                    "account": "ashare_server_sim",
                    "trade_date": "2026-07-06",
                    "ts_code": "600000.SH",
                    "side": "buy",
                    "quantity": 100,
                    "requested_price": 10.0,
                    "filled_price": 10.0,
                    "amount": 1000.0,
                    "commission": 5.0,
                    "stamp_duty": 0.0,
                    "net_amount": 1005.0,
                    "status": "filled",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = pnl_summary.sim_ledger_pnl_summary(
            markets=("ashare",),
            ledger_root=self.ledger_root,
            local_trades_path=self.local_sim,
            ashare_mark_prices={"600000.SH": 10.0},
        )

        ashare = result["ashare"]
        self.assertEqual(ashare["total_pnl"], -5.0)
        self.assertEqual(ashare["market_value"], 1000.0)
        self.assertEqual(ashare["open_position_count"], 1)
        self.assertEqual(ashare["strategy_total_pnl"], 0.0)
        self.assertEqual(ashare["strategy_market_value"], 0.0)
        self.assertEqual(ashare["strategy_open_position_count"], 0)
        self.assertEqual(ashare["sample_quality"]["validation_sample_count"], 1)
        self.assertEqual(ashare["sample_quality"]["strategy_sample_valid_count"], 0)

    def test_ashare_candidate_provenance_counts_as_strategy_pnl(self) -> None:
        self.local_sim.parent.mkdir(parents=True)
        self.local_sim.write_text(
            json.dumps(
                {
                    "trade_id": "LSIM-STRATEGY",
                    "order_id": "SIM-NEW",
                    "idempotency_key": "new",
                    "market": "ashare",
                    "account": "ashare_server_sim",
                    "trade_date": "2026-07-06",
                    "ts_code": "600000.SH",
                    "side": "buy",
                    "quantity": 100,
                    "requested_price": 10.0,
                    "filled_price": 10.0,
                    "amount": 1000.0,
                    "commission": 5.0,
                    "stamp_duty": 0.0,
                    "net_amount": 1005.0,
                    "status": "filled",
                    "candidate_pool_layer": "candidate",
                    "execution_source": "ashare_candidate_layer",
                    "fill_price_source": "order.market_snapshot.ask_price",
                    "fill_price_source_class": "market_data",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = pnl_summary.sim_ledger_pnl_summary(
            markets=("ashare",),
            ledger_root=self.ledger_root,
            local_trades_path=self.local_sim,
            ashare_mark_prices={"600000.SH": 10.0},
        )

        ashare = result["ashare"]
        self.assertEqual(ashare["total_pnl"], -5.0)
        self.assertEqual(ashare["strategy_total_pnl"], -5.0)
        self.assertEqual(ashare["sample_quality"]["validation_sample_count"], 0)
        self.assertEqual(ashare["sample_quality"]["strategy_sample_valid_count"], 1)

    def test_ashare_regular_session_signal_card_price_counts_as_strategy_pnl(self) -> None:
        self.local_sim.parent.mkdir(parents=True)
        self.local_sim.write_text(
            json.dumps(
                {
                    "trade_id": "LSIM-SIGNAL-CARD",
                    "order_id": "SIM-SIGNAL-CARD",
                    "idempotency_key": "signal-card",
                    "market": "ashare",
                    "account": "ashare_server_sim",
                    "trade_date": "2026-07-07",
                    "ts_code": "600000.SH",
                    "side": "buy",
                    "quantity": 100,
                    "requested_price": 10.0,
                    "filled_price": 10.0,
                    "amount": 1000.0,
                    "commission": 5.0,
                    "stamp_duty": 0.0,
                    "net_amount": 1005.0,
                    "status": "filled",
                    "candidate_pool_layer": "candidate",
                    "execution_source": "ashare_candidate_layer",
                    "fill_price_source": "signal_card.price",
                    "fill_price_source_class": "signal_card_price",
                    "created_at": "2026-07-07T02:00:00+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = pnl_summary.sim_ledger_pnl_summary(
            markets=("ashare",),
            ledger_root=self.ledger_root,
            local_trades_path=self.local_sim,
            ashare_mark_prices={"600000.SH": 10.0},
        )

        ashare = result["ashare"]
        self.assertEqual(ashare["strategy_total_pnl"], -5.0)
        self.assertEqual(ashare["sample_quality"]["validation_sample_count"], 0)
        self.assertEqual(ashare["sample_quality"]["strategy_sample_valid_count"], 1)

    def test_ashare_candidate_trade_without_price_provenance_is_validation_sample(self) -> None:
        self.local_sim.parent.mkdir(parents=True)
        self.local_sim.write_text(
            json.dumps(
                {
                    "trade_id": "LSIM-NO-PRICE-PROVENANCE",
                    "order_id": "SIM-NO-PRICE-PROVENANCE",
                    "idempotency_key": "no-price-provenance",
                    "market": "ashare",
                    "account": "ashare_server_sim",
                    "trade_date": "2026-07-06",
                    "ts_code": "600000.SH",
                    "side": "buy",
                    "quantity": 100,
                    "requested_price": 10.0,
                    "filled_price": 10.0,
                    "amount": 1000.0,
                    "commission": 5.0,
                    "stamp_duty": 0.0,
                    "net_amount": 1005.0,
                    "status": "filled",
                    "candidate_pool_layer": "candidate",
                    "execution_source": "ashare_candidate_layer",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = pnl_summary.sim_ledger_pnl_summary(
            markets=("ashare",),
            ledger_root=self.ledger_root,
            local_trades_path=self.local_sim,
            ashare_mark_prices={"600000.SH": 10.0},
        )

        ashare = result["ashare"]
        self.assertEqual(ashare["strategy_total_pnl"], 0.0)
        self.assertEqual(ashare["sample_quality"]["validation_sample_count"], 1)
        self.assertEqual(ashare["sample_quality"]["strategy_sample_valid_count"], 0)
        self.assertEqual(ashare["sample_quality"]["by_reason"], {"missing_fill_price_provenance": 1})

    def test_ashare_after_hours_trade_is_validation_sample_not_strategy_pnl(self) -> None:
        self.local_sim.parent.mkdir(parents=True)
        self.local_sim.write_text(
            json.dumps(
                {
                    "trade_id": "LSIM-AFTER-HOURS",
                    "order_id": "SIM-AFTER-HOURS",
                    "idempotency_key": "after-hours",
                    "market": "ashare",
                    "account": "ashare_server_sim",
                    "trade_date": "2026-07-07",
                    "ts_code": "600000.SH",
                    "side": "buy",
                    "quantity": 100,
                    "requested_price": 10.0,
                    "filled_price": 10.0,
                    "amount": 1000.0,
                    "commission": 5.0,
                    "stamp_duty": 0.0,
                    "net_amount": 1005.0,
                    "status": "filled",
                    "candidate_pool_layer": "candidate",
                    "execution_source": "ashare_candidate_layer",
                    "created_at": "2026-07-07T08:26:30+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = pnl_summary.sim_ledger_pnl_summary(
            markets=("ashare",),
            ledger_root=self.ledger_root,
            local_trades_path=self.local_sim,
            ashare_mark_prices={"600000.SH": 10.0},
        )

        ashare = result["ashare"]
        self.assertEqual(ashare["total_pnl"], -5.0)
        self.assertEqual(ashare["strategy_total_pnl"], 0.0)
        self.assertEqual(ashare["sample_quality"]["validation_sample_count"], 1)
        self.assertEqual(ashare["sample_quality"]["strategy_sample_valid_count"], 0)
        self.assertEqual(ashare["sample_quality"]["by_reason"], {"outside_ashare_regular_session": 1})


if __name__ == "__main__":
    unittest.main()
