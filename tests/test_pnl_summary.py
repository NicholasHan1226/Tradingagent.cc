from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.execution import local_sim_ledger
from shared.review import pnl_summary, sim_ledger_reader


class PnlSummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger_root = self.root / "ledger"
        self.canonical_local_sim_trades = local_sim_ledger.LOCAL_SIM_TRADES
        self.execution_root = self.root / local_sim_ledger.ASHARE_EXECUTION_LINEAGE_ID
        self.local_sim = self.execution_root / "local_sim_trades.jsonl"
        for name, value in (
            ("LOCAL_SIM_DIR", self.execution_root),
            ("LOCAL_SIM_TRADES", self.local_sim),
            ("LOCAL_SIM_POSITIONS", self.execution_root / "local_sim_positions.json"),
            ("LOCAL_SIM_PNL", self.execution_root / "local_sim_pnl.json"),
            ("LOCAL_SIM_LOCK", self.execution_root / ".local_sim.lock"),
            (
                "LOCAL_SIM_POSITIONS_SNAPSHOT",
                self.execution_root / "simulated_ashare_positions.json",
            ),
            (
                "LOCAL_SIM_RECEIPTS",
                self.execution_root / "sim_execution_receipts.jsonl",
            ),
        ):
            patcher = patch.object(local_sim_ledger, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.lineage_started_at = "2026-07-12T00:00:00+08:00"
        local_sim_ledger.bootstrap_fresh_local_sim(
            root=self.execution_root,
            lineage_started_at=self.lineage_started_at,
            point_in_time_as_of=self.lineage_started_at,
        )

    def _write_current_trade(self, row: dict[str, object]) -> None:
        payload = dict(row)
        point_in_time_as_of = str(
            payload.get("trade_timestamp_bj")
            or payload.get("created_at")
            or f"{payload['trade_date']}T10:00:00+08:00"
        )
        payload.update(
            local_sim_ledger.build_execution_lineage(
                lineage_started_at=self.lineage_started_at,
                point_in_time_as_of=point_in_time_as_of,
            )
        )
        payload.setdefault("capital_layer", "simulated")
        payload.setdefault("account_type", "simulated")
        payload.setdefault("real_trading_enabled", False)
        payload["trade_sha256"] = local_sim_ledger._payload_sha256(
            payload,
            drop_checksums=True,
        )
        self.local_sim.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_ashare_default_trade_path_uses_current_fresh_execution_lineage(
        self,
    ) -> None:
        self.assertEqual(
            pnl_summary.DEFAULT_LOCAL_SIM_TRADES,
            self.canonical_local_sim_trades,
        )
        self.assertEqual(
            sim_ledger_reader.DEFAULT_LOCAL_SIM_TRADES,
            self.canonical_local_sim_trades,
        )

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
        self._write_current_trade(
            {
                "trade_id": "LSIM-STRATEGY",
                "order_id": "SIM-1",
                "idempotency_key": "idem-1",
                "market": "ashare",
                "account": "ashare_sim",
                "trade_date": "2026-07-13",
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
                "trade_timestamp_bj": "2026-07-13T10:00:00+08:00",
            }
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
        self._write_current_trade(
            {
                "trade_id": "LSIM-STRATEGY",
                "order_id": "SIM-1",
                "idempotency_key": "idem-1",
                "market": "ashare",
                "account": "ashare_sim",
                "trade_date": "2026-07-13",
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
                "trade_timestamp_bj": "2026-07-13T10:00:00+08:00",
            }
        )

        with patch(
            "shared.review.pnl_summary.load_mark_prices_for_positions",
            return_value={"600000.SH": 12.0},
        ) as loader:
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

    def test_ashare_missing_provenance_is_validation_sample_not_strategy_pnl(
        self,
    ) -> None:
        self._write_current_trade(
            {
                "trade_id": "LSIM-VALIDATION",
                "order_id": "SIM-OLD",
                "idempotency_key": "old",
                "market": "ashare",
                "account": "ashare_sim",
                "trade_date": "2026-07-13",
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

        result = pnl_summary.sim_ledger_pnl_summary(
            markets=("ashare",),
            ledger_root=self.ledger_root,
            local_trades_path=self.local_sim,
            ashare_mark_prices={"600000.SH": 10.0},
        )

        ashare = result["ashare"]
        self.assertEqual(ashare["total_pnl"], 0.0)
        self.assertEqual(ashare["market_value"], 0.0)
        self.assertEqual(ashare["open_position_count"], 0)
        self.assertEqual(ashare["strategy_total_pnl"], 0.0)
        self.assertEqual(ashare["strategy_market_value"], 0.0)
        self.assertEqual(ashare["strategy_open_position_count"], 0)
        self.assertEqual(ashare["audit_total_pnl"], -5.0)
        self.assertEqual(ashare["audit_market_value"], 1000.0)
        self.assertEqual(ashare["audit_open_position_count"], 1)
        self.assertEqual(ashare["sample_quality"]["validation_sample_count"], 1)
        self.assertEqual(ashare["sample_quality"]["strategy_sample_valid_count"], 0)

    def test_ashare_candidate_provenance_counts_as_strategy_pnl(self) -> None:
        self._write_current_trade(
            {
                "trade_id": "LSIM-STRATEGY",
                "order_id": "SIM-NEW",
                "idempotency_key": "new",
                "market": "ashare",
                "account": "ashare_sim",
                "trade_date": "2026-07-13",
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

        result = pnl_summary.sim_ledger_pnl_summary(
            markets=("ashare",),
            ledger_root=self.ledger_root,
            local_trades_path=self.local_sim,
            ashare_mark_prices={"600000.SH": 10.0},
        )

        ashare = result["ashare"]
        self.assertEqual(ashare["total_pnl"], -5.0)
        self.assertEqual(ashare["strategy_total_pnl"], -5.0)
        self.assertEqual(ashare["audit_total_pnl"], -5.0)
        self.assertEqual(ashare["sample_quality"]["validation_sample_count"], 0)
        self.assertEqual(ashare["sample_quality"]["strategy_sample_valid_count"], 1)

    def test_ashare_regular_session_signal_card_price_counts_as_strategy_pnl(
        self,
    ) -> None:
        self._write_current_trade(
            {
                "trade_id": "LSIM-SIGNAL-CARD",
                "order_id": "SIM-SIGNAL-CARD",
                "idempotency_key": "signal-card",
                "market": "ashare",
                "account": "ashare_sim",
                "trade_date": "2026-07-14",
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
                "created_at": "2026-07-14T02:00:00+00:00",
            }
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

    def test_ashare_candidate_trade_without_price_provenance_is_validation_sample(
        self,
    ) -> None:
        self._write_current_trade(
            {
                "trade_id": "LSIM-NO-PRICE-PROVENANCE",
                "order_id": "SIM-NO-PRICE-PROVENANCE",
                "idempotency_key": "no-price-provenance",
                "market": "ashare",
                "account": "ashare_sim",
                "trade_date": "2026-07-13",
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

        result = pnl_summary.sim_ledger_pnl_summary(
            markets=("ashare",),
            ledger_root=self.ledger_root,
            local_trades_path=self.local_sim,
            ashare_mark_prices={"600000.SH": 10.0},
        )

        ashare = result["ashare"]
        self.assertEqual(ashare["strategy_total_pnl"], 0.0)
        self.assertEqual(ashare["audit_total_pnl"], -5.0)
        self.assertEqual(ashare["sample_quality"]["validation_sample_count"], 1)
        self.assertEqual(ashare["sample_quality"]["strategy_sample_valid_count"], 0)
        self.assertEqual(
            ashare["sample_quality"]["by_reason"], {"missing_fill_price_provenance": 1}
        )

    def test_ashare_after_hours_trade_is_validation_sample_not_strategy_pnl(
        self,
    ) -> None:
        self._write_current_trade(
            {
                "trade_id": "LSIM-AFTER-HOURS",
                "order_id": "SIM-AFTER-HOURS",
                "idempotency_key": "after-hours",
                "market": "ashare",
                "account": "ashare_sim",
                "trade_date": "2026-07-14",
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
                "created_at": "2026-07-14T08:26:30+00:00",
            }
        )

        result = pnl_summary.sim_ledger_pnl_summary(
            markets=("ashare",),
            ledger_root=self.ledger_root,
            local_trades_path=self.local_sim,
            ashare_mark_prices={"600000.SH": 10.0},
        )

        ashare = result["ashare"]
        self.assertEqual(ashare["total_pnl"], 0.0)
        self.assertEqual(ashare["strategy_total_pnl"], 0.0)
        self.assertEqual(ashare["audit_total_pnl"], -5.0)
        self.assertEqual(ashare["sample_quality"]["validation_sample_count"], 1)
        self.assertEqual(ashare["sample_quality"]["strategy_sample_valid_count"], 0)
        self.assertEqual(
            ashare["sample_quality"]["by_reason"], {"outside_ashare_regular_session": 1}
        )


if __name__ == "__main__":
    unittest.main()
