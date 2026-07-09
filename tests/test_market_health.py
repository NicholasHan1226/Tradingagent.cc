from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
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

    def test_ashare_session_uses_real_trading_day_calendar(self) -> None:
        with patch.object(market_health, "_is_ashare_trading_day", return_value=False):
            state = market_health._market_session_state(
                "ashare",
                now=datetime.fromisoformat("2026-10-01T10:00:00+08:00"),
            )

        self.assertFalse(state["in_session"])
        self.assertFalse(state["samples_expected_today"])

    def test_cn_futures_session_treats_lunch_break_as_not_in_session(self) -> None:
        state = market_health._market_session_state(
            "cn_futures",
            now=datetime.fromisoformat("2026-07-09T11:55:00+08:00"),
        )

        self.assertFalse(state["in_session"])
        self.assertEqual(state["session"], "lunch_break")
        self.assertTrue(state["samples_expected_today"])

    def test_cn_futures_session_detects_afternoon_trading(self) -> None:
        state = market_health._market_session_state(
            "cn_futures",
            now=datetime.fromisoformat("2026-07-09T13:05:00+08:00"),
        )

        self.assertTrue(state["in_session"])
        self.assertEqual(state["session"], "day_afternoon")

    def test_signal_queue_isolation_fails_when_shadow_leaks_into_execution_pending(self) -> None:
        self._write_json("signals/pending/SHADOW-ashare-000001.json", {"capital_layer": "shadow", "order_id": "SHADOW-1"})

        check = market_health._check_signal_queues()

        self.assertEqual(check.status, "fail")
        self.assertEqual(check.details["execution_queue"]["pending"], 1)
        self.assertIn("signals/pending/SHADOW-ashare-000001.json", check.details["leaked_shadow_sample"])

    def test_signal_queue_isolation_reports_expired_execution_pending(self) -> None:
        self._write_json(
            "signals/pending/SIM-OLD.json",
            {"capital_layer": "simulated", "order_id": "SIM-OLD", "valid_until": "2026-01-01"},
        )

        check = market_health._check_signal_queues()

        self.assertEqual(check.status, "fail")
        self.assertEqual(check.details["stale_execution_sample"][0]["reason"], "valid_until_expired")

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

    def test_sim_position_sync_passes_before_first_local_trade(self) -> None:
        check = market_health._check_simulated_position_sync()

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["bootstrap_state"], "no_trades_yet")

    def test_sim_position_sync_reads_bootstrap_snapshot_state(self) -> None:
        self._write_json(
            "signals/positions/simulated_ashare_positions.json",
            {"positions": [], "bootstrap_state": "no_trades_yet"},
        )

        check = market_health._check_simulated_position_sync()

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["bootstrap_state"], "no_trades_yet")

    def test_failure_receipts_pass_before_first_failure_or_trade(self) -> None:
        check = market_health._check_failure_receipts()

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["bootstrap_state"], "no_receipts_expected_yet")

    def test_failure_receipts_warn_when_failed_signal_has_no_receipt(self) -> None:
        self._write_json("signals/failed/ORDER-1.json", {"order_id": "ORDER-1", "status": "failed"})

        check = market_health._check_failure_receipts()

        self.assertEqual(check.status, "warn")
        self.assertEqual(check.details["failed_count"], 1)

    def test_failure_receipts_passes_when_only_after_hours_validation_samples_exist(self) -> None:
        trades = self.root / "shared/logs/local_sim/local_sim_trades.jsonl"
        trades.parent.mkdir(parents=True, exist_ok=True)
        trades.write_text(
            json.dumps(
                {
                    "ts_code": "000001.SZ",
                    "market": "ashare",
                    "side": "buy",
                    "status": "filled",
                    "candidate_pool_layer": "candidate",
                    "execution_source": "ashare_candidate_layer",
                    "created_at": "2026-07-07T08:26:30+00:00",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        check = market_health._check_failure_receipts()

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.severity, "info")
        self.assertTrue(check.details["advisory"])
        self.assertEqual(check.details["sample_quality"]["by_reason"], {"outside_ashare_regular_session": 1})

    def test_failure_receipts_warns_when_trade_provenance_is_missing(self) -> None:
        trades = self.root / "shared/logs/local_sim/local_sim_trades.jsonl"
        trades.parent.mkdir(parents=True, exist_ok=True)
        trades.write_text(
            json.dumps(
                {
                    "ts_code": "000001.SZ",
                    "market": "ashare",
                    "side": "buy",
                    "status": "filled",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        check = market_health._check_failure_receipts()

        self.assertEqual(check.status, "warn")
        self.assertFalse(check.details["advisory"])

    def test_local_sim_ledger_checks_code_field_not_raw_line_text(self) -> None:
        from shared.execution import local_sim_ledger

        trades_path = self.root / "shared/logs/local_sim/local_sim_trades.jsonl"
        positions_path = self.root / "shared/logs/local_sim/local_sim_positions.json"
        pnl_path = self.root / "shared/logs/local_sim/local_sim_pnl.json"
        snapshot_path = self.root / "signals/positions/simulated_ashare_positions.json"
        trades_path.parent.mkdir(parents=True, exist_ok=True)
        trades_path.write_text(
            json.dumps({"ts_code": "000001.SZ", "net_amount": 2000.0}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        positions_path.write_text(json.dumps({"ashare_sim": {}}, ensure_ascii=False), encoding="utf-8")
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(json.dumps({"positions": [], "pnl": {"ashare_sim": {"cash_available": 200000}}}, ensure_ascii=False), encoding="utf-8")
        pnl_path.write_text(json.dumps({"ashare_sim": {"cash_available": 200000, "positions": {}}}, ensure_ascii=False), encoding="utf-8")

        with patch.object(local_sim_ledger, "LOCAL_SIM_TRADES", trades_path):
            with patch.object(local_sim_ledger, "LOCAL_SIM_POSITIONS", positions_path):
                with patch.object(local_sim_ledger, "LOCAL_SIM_PNL", pnl_path):
                    with patch.object(local_sim_ledger, "LOCAL_SIM_POSITIONS_SNAPSHOT", snapshot_path):
                        check = market_health._check_local_sim_ledger()

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["invalid_code_matches"], 0)

    def test_local_sim_ledger_fails_when_snapshot_and_pnl_disagree(self) -> None:
        from shared.execution import local_sim_ledger

        local_dir = self.root / "shared/logs/local_sim"
        local_dir.mkdir(parents=True, exist_ok=True)
        trades_path = local_dir / "local_sim_trades.jsonl"
        positions_path = local_dir / "local_sim_positions.json"
        pnl_path = local_dir / "local_sim_pnl.json"
        snapshot_path = self.root / "signals/positions/simulated_ashare_positions.json"
        trades_path.write_text(json.dumps({"ts_code": "000001.SZ"}, ensure_ascii=False) + "\n", encoding="utf-8")
        positions_path.write_text(json.dumps({"ashare_sim": {"000001.SZ": {"quantity": 100}}}, ensure_ascii=False), encoding="utf-8")
        pnl_path.write_text(json.dumps({"ashare_sim": {"cash_available": 190000, "positions": {"000001.SZ": {}}}}, ensure_ascii=False), encoding="utf-8")
        self._write_json("signals/positions/simulated_ashare_positions.json", {"positions": [], "pnl": {"ashare_sim": {"cash_available": 190000}}})

        with patch.object(local_sim_ledger, "LOCAL_SIM_TRADES", trades_path):
            with patch.object(local_sim_ledger, "LOCAL_SIM_POSITIONS", positions_path):
                with patch.object(local_sim_ledger, "LOCAL_SIM_PNL", pnl_path):
                    with patch.object(local_sim_ledger, "LOCAL_SIM_POSITIONS_SNAPSHOT", snapshot_path):
                        check = market_health._check_local_sim_ledger()

        self.assertEqual(check.status, "fail")
        self.assertIn("position_count_mismatch", check.details["consistency_errors"])

    def test_local_sim_ledger_passes_when_after_hours_trade_is_isolated_validation_sample(self) -> None:
        from shared.execution import local_sim_ledger

        local_dir = self.root / "shared/logs/local_sim"
        local_dir.mkdir(parents=True, exist_ok=True)
        trades_path = local_dir / "local_sim_trades.jsonl"
        positions_path = local_dir / "local_sim_positions.json"
        pnl_path = local_dir / "local_sim_pnl.json"
        snapshot_path = self.root / "signals/positions/simulated_ashare_positions.json"
        trades_path.write_text(
            json.dumps(
                {
                    "ts_code": "000001.SZ",
                    "market": "ashare",
                    "side": "buy",
                    "status": "filled",
                    "candidate_pool_layer": "candidate",
                    "execution_source": "ashare_candidate_layer",
                    "created_at": "2026-07-07T08:26:30+00:00",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        positions_path.write_text(json.dumps({"ashare_sim": {"000001.SZ": {"quantity": 100}}}, ensure_ascii=False), encoding="utf-8")
        pnl_path.write_text(json.dumps({"ashare_sim": {"cash_available": 190000, "positions": {"000001.SZ": {}}}}, ensure_ascii=False), encoding="utf-8")
        self._write_json(
            "signals/positions/simulated_ashare_positions.json",
            {"positions": [{"ts_code": "000001.SZ"}], "pnl": {"ashare_sim": {"cash_available": 190000}}},
        )

        with patch.object(local_sim_ledger, "LOCAL_SIM_TRADES", trades_path):
            with patch.object(local_sim_ledger, "LOCAL_SIM_POSITIONS", positions_path):
                with patch.object(local_sim_ledger, "LOCAL_SIM_PNL", pnl_path):
                    with patch.object(local_sim_ledger, "LOCAL_SIM_POSITIONS_SNAPSHOT", snapshot_path):
                        check = market_health._check_local_sim_ledger()

        self.assertEqual(check.status, "pass")
        self.assertTrue(check.details["advisory"])
        self.assertEqual(check.details["sample_quality"]["by_reason"], {"outside_ashare_regular_session": 1})
        self.assertEqual(check.details["sample_quality"]["strategy_sample_valid_count"], 0)

    def test_local_sim_ledger_warns_when_provenance_is_missing(self) -> None:
        from shared.execution import local_sim_ledger

        local_dir = self.root / "shared/logs/local_sim"
        local_dir.mkdir(parents=True, exist_ok=True)
        trades_path = local_dir / "local_sim_trades.jsonl"
        positions_path = local_dir / "local_sim_positions.json"
        pnl_path = local_dir / "local_sim_pnl.json"
        snapshot_path = self.root / "signals/positions/simulated_ashare_positions.json"
        trades_path.write_text(
            json.dumps(
                {
                    "ts_code": "000001.SZ",
                    "market": "ashare",
                    "side": "buy",
                    "status": "filled",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        positions_path.write_text(json.dumps({"ashare_sim": {"000001.SZ": {"quantity": 100}}}, ensure_ascii=False), encoding="utf-8")
        pnl_path.write_text(json.dumps({"ashare_sim": {"cash_available": 190000, "positions": {"000001.SZ": {}}}}, ensure_ascii=False), encoding="utf-8")
        self._write_json(
            "signals/positions/simulated_ashare_positions.json",
            {"positions": [{"ts_code": "000001.SZ"}], "pnl": {"ashare_sim": {"cash_available": 190000}}},
        )

        with patch.object(local_sim_ledger, "LOCAL_SIM_TRADES", trades_path):
            with patch.object(local_sim_ledger, "LOCAL_SIM_POSITIONS", positions_path):
                with patch.object(local_sim_ledger, "LOCAL_SIM_PNL", pnl_path):
                    with patch.object(local_sim_ledger, "LOCAL_SIM_POSITIONS_SNAPSHOT", snapshot_path):
                        check = market_health._check_local_sim_ledger()

        self.assertEqual(check.status, "warn")
        self.assertFalse(check.details["advisory"])
        self.assertEqual(check.details["sample_quality"]["by_reason"], {"missing_ashare_candidate_provenance": 1})

    def test_capital_plan_alignment_fails_when_plan_misses_snapshot_positions(self) -> None:
        trades = self.root / "shared/logs/local_sim/local_sim_trades.jsonl"
        trades.parent.mkdir(parents=True, exist_ok=True)
        trades.write_text('{"ts_code":"000001.SZ"}\n', encoding="utf-8")
        self._write_json(
            "signals/positions/simulated_ashare_positions.json",
            {"positions": [{"ts_code": "000001.SZ"}, {"ts_code": "000002.SZ"}]},
        )
        plan = self.root / "shared/review/ashare/capital_plan_20260706.jsonl"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(
            json.dumps(
                {
                    "capital_plan": {"existing_position_count": 0, "cash_source": "account_snapshot"},
                    "rebalance": {"existing_position_count": 0},
                    "generated_at": "2026-07-06T01:00:00+00:00",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        check = market_health._check_ashare_capital_plan_alignment()

        self.assertEqual(check.status, "fail")
        self.assertEqual(check.details["snapshot_position_count"], 2)
        self.assertEqual(check.details["capital_plan_position_count"], 0)

    def test_capital_plan_alignment_warns_when_plan_is_older_than_snapshot(self) -> None:
        trades = self.root / "shared/logs/local_sim/local_sim_trades.jsonl"
        trades.parent.mkdir(parents=True, exist_ok=True)
        trades.write_text('{"ts_code":"000001.SZ"}\n', encoding="utf-8")
        self._write_json(
            "signals/positions/simulated_ashare_positions.json",
            {
                "synced_at": "2026-07-06T02:00:00+00:00",
                "positions": [{"ts_code": "000001.SZ"}, {"ts_code": "000002.SZ"}],
            },
        )
        plan = self.root / "shared/review/ashare/capital_plan_20260706.jsonl"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(
            json.dumps(
                {
                    "capital_plan": {"existing_position_count": 0, "cash_source": "account_snapshot"},
                    "generated_at": "2026-07-06T01:00:00+00:00",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        check = market_health._check_ashare_capital_plan_alignment()

        self.assertEqual(check.status, "warn")
        self.assertTrue(check.details["plan_older_than_snapshot"])

    def test_capital_plan_alignment_passes_when_stale_plan_only_reflects_validation_samples(self) -> None:
        trades = self.root / "shared/logs/local_sim/local_sim_trades.jsonl"
        trades.parent.mkdir(parents=True, exist_ok=True)
        trades.write_text(
            json.dumps(
                {
                    "ts_code": "000001.SZ",
                    "market": "ashare",
                    "side": "buy",
                    "status": "filled",
                    "candidate_pool_layer": "candidate",
                    "execution_source": "ashare_candidate_layer",
                    "created_at": "2026-07-07T08:26:30+00:00",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        self._write_json(
            "signals/positions/simulated_ashare_positions.json",
            {
                "synced_at": "2026-07-07T08:26:30+00:00",
                "positions": [{"ts_code": "000001.SZ"}, {"ts_code": "000002.SZ"}],
            },
        )
        plan = self.root / "shared/review/ashare/capital_plan_20260707.jsonl"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(
            json.dumps(
                {
                    "capital_plan": {"existing_position_count": 0, "cash_source": "account_snapshot"},
                    "generated_at": "2026-07-07T06:56:24+00:00",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        check = market_health._check_ashare_capital_plan_alignment()

        self.assertEqual(check.status, "pass")
        self.assertTrue(check.details["advisory"])
        self.assertEqual(check.details["sample_quality"]["by_reason"], {"outside_ashare_regular_session": 1})

    def test_optional_mini_health_does_not_block_server_local_sim_by_default(self) -> None:
        with patch.dict("os.environ", {"ASHARE_SIM_HERMES_ENABLED": "0"}):
            check = market_health._check_optional_mini_health("http://127.0.0.1:1/health")

        self.assertEqual(check.status, "pass")
        self.assertFalse(check.details["enabled"])
        self.assertEqual(check.details["primary_path"], "server_local_sim")

    def test_optional_mini_health_warns_only_when_explicitly_enabled(self) -> None:
        with patch.dict("os.environ", {"ASHARE_SIM_HERMES_ENABLED": "1"}):
            with patch.object(
                market_health,
                "_check_mini_health",
                return_value=market_health.Check("mini_hermes_health", "fail", "down", {"error": "boom"}),
            ):
                check = market_health._check_optional_mini_health("http://127.0.0.1:1/health")

        self.assertEqual(check.status, "warn")
        self.assertTrue(check.details["enabled"])
        self.assertEqual(check.details["raw_status"], "fail")

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

    def test_cn_futures_sim_market_loop_reads_governed_wrapper_log(self) -> None:
        log = self.root / "shared/logs/cron/job_cn_futures_sim.log"
        log.parent.mkdir(parents=True)
        log.write_text('noise\n{"market":"cn_futures","state":"ok","hold_count":3}\n', encoding="utf-8")

        with patch.object(market_health, "_probe_market_data", return_value={"status": "ok", "priced_signal_count": 5}):
            with patch.object(market_health, "_market_session_state", return_value={"in_session": True, "samples_expected_today": True}):
                check = market_health._check_sim_market_loop("cn_futures", "job_cn_futures_sim.sh")

        self.assertEqual(check.details["latest_cron_result"]["path"], "shared/logs/cron/job_cn_futures_sim.log")
        self.assertEqual(check.details["latest_cron_result"]["payload"]["hold_count"], 3)

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

    def test_pm_sim_market_loop_warns_when_data_feed_has_no_market_rows(self) -> None:
        with patch.object(
            market_health,
            "_probe_market_data",
            return_value={"status": "warn", "reason": "pm_market_rows_empty", "priced_signal_count": 0},
        ):
            check = market_health._check_sim_market_loop("pm", "job_pm_sim.sh")

        self.assertEqual(check.status, "warn")
        self.assertEqual(check.details["fail_reasons"], [])
        self.assertIn("pm_waiting_for_market_data", check.details["warn_reasons"])
        self.assertEqual(check.details["diagnostic_class"], "market_data_wait")
        self.assertEqual(check.details["execution_fault"], False)

    def test_pm_sim_market_loop_observes_when_model_probability_is_missing(self) -> None:
        with patch.object(
            market_health,
            "_probe_market_data",
            return_value={"status": "warn", "reason": "pm_model_probability_missing", "priced_signal_count": 10, "modeled_signal_count": 0},
        ):
            check = market_health._check_sim_market_loop("pm", "job_pm_sim.sh")

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["fail_reasons"], [])
        self.assertIn("pm_waiting_for_marketgraph_probability", check.details["warn_reasons"])
        self.assertNotIn("market_data_degraded", check.details["warn_reasons"])
        self.assertEqual(check.details["diagnostic_class"], "strategy_wait")
        self.assertEqual(check.details["execution_fault"], False)

    def test_pm_sim_market_loop_observes_when_model_edge_is_below_threshold(self) -> None:
        with patch.object(
            market_health,
            "_probe_market_data",
            return_value={"status": "warn", "reason": "pm_model_edge_below_threshold", "priced_signal_count": 10, "modeled_signal_count": 10},
        ):
            check = market_health._check_sim_market_loop("pm", "job_pm_sim.sh")

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["fail_reasons"], [])
        self.assertIn("pm_waiting_for_model_edge", check.details["warn_reasons"])
        self.assertNotIn("market_data_degraded", check.details["warn_reasons"])
        self.assertEqual(check.details["diagnostic_class"], "strategy_wait")
        self.assertEqual(check.details["execution_fault"], False)

    def test_crypto_sim_market_loop_observes_when_momentum_threshold_not_met(self) -> None:
        with patch.object(
            market_health,
            "_probe_market_data",
            return_value={
                "status": "warn",
                "reason": "crypto_momentum_threshold_not_met",
                "priced_signal_count": 5,
                "strategy_candidate_count": 0,
            },
        ):
            check = market_health._check_sim_market_loop("crypto", "job_crypto_sim.sh")

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["fail_reasons"], [])
        self.assertIn("crypto_waiting_for_momentum_signal", check.details["warn_reasons"])
        self.assertNotIn("market_data_degraded", check.details["warn_reasons"])
        self.assertEqual(check.details["diagnostic_class"], "strategy_wait")
        self.assertEqual(check.details["execution_fault"], False)

    def test_crypto_sim_market_loop_marks_strategy_wait_even_with_existing_ledger(self) -> None:
        ledger = self.root / "shared/logs/sim_ledger/crypto/aggressive/trade_journal.jsonl"
        ledger.parent.mkdir(parents=True)
        ledger.write_text('{"symbol":"BTCUSDT","side":"buy","fill_price":60000,"fill_qty":0.01}\n', encoding="utf-8")

        with patch.object(
            market_health,
            "_probe_market_data",
            return_value={
                "status": "warn",
                "reason": "crypto_momentum_threshold_not_met",
                "priced_signal_count": 5,
                "strategy_candidate_count": 0,
            },
        ):
            check = market_health._check_sim_market_loop("crypto", "job_crypto_sim.sh")

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["ledger"]["trade_rows"], 1)
        self.assertIn("crypto_waiting_for_momentum_signal", check.details["warn_reasons"])
        self.assertNotIn("market_data_degraded", check.details["warn_reasons"])
        self.assertEqual(check.details["diagnostic_class"], "strategy_wait")
        self.assertEqual(check.details["execution_fault"], False)

    def test_crypto_sim_market_loop_fails_when_candidate_exists_but_no_ledger(self) -> None:
        with patch.object(
            market_health,
            "_probe_market_data",
            return_value={"status": "ok", "priced_signal_count": 5, "strategy_candidate_count": 1},
        ):
            check = market_health._check_sim_market_loop("crypto", "job_crypto_sim.sh")

        self.assertEqual(check.status, "fail")
        self.assertIn("sim_trade_ledger_empty", check.details["fail_reasons"])

    def test_ashare_sim_loop_warns_without_production_trade_sample(self) -> None:
        with patch.object(market_health, "_probe_market_data", return_value={"status": "ok", "asset_count": 10}):
            with patch.object(market_health, "_market_session_state", return_value={"in_session": True, "samples_expected_today": True}):
                check = market_health._check_sim_market_loop("ashare", "job_ashare_sim_exec.sh")

        self.assertEqual(check.status, "warn")
        self.assertIn("server_local_sim_has_no_production_trades_yet", check.details["warn_reasons"])

    def test_ashare_sim_loop_observes_when_no_portfolio_orders_explains_no_trade(self) -> None:
        log_path = self.root / "shared/logs/ashare_no_trade_explanations.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps(
                {
                    "generated_at": "2026-07-08T10:25:00+08:00",
                    "no_trade_explanation": {
                        "category": "no_portfolio_orders",
                        "action": "continue_monitoring_capital_plan",
                        "counts": {"candidates": 3, "orders": 0},
                        "candidate_decision_trace": [{"symbol": "AAA", "drop_reason": "capital_plan_capacity_zero"}],
                        "capital_plan_decision": {"position_capacity": 0},
                        "portfolio_decision": {"allowed_buy_count": 0},
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        with patch.object(market_health, "_probe_market_data", return_value={"status": "ok", "asset_count": 10}):
            with patch.object(market_health, "_market_session_state", return_value={"in_session": True, "samples_expected_today": True}):
                check = market_health._check_sim_market_loop("ashare", "job_ashare_sim_exec.sh")

        self.assertEqual(check.status, "pass")
        self.assertIn("ashare_waiting_for_portfolio_or_strategy_signal", check.details["warn_reasons"])
        self.assertEqual(check.details["diagnostic_class"], "strategy_wait")
        self.assertEqual(check.details["no_trade_explanation"]["category"], "no_portfolio_orders")

    def test_ashare_sim_loop_warns_when_no_portfolio_orders_lacks_trace_evidence(self) -> None:
        log_path = self.root / "shared/logs/ashare_no_trade_explanations.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps(
                {
                    "generated_at": "2026-07-08T10:25:00+08:00",
                    "no_trade_explanation": {
                        "category": "no_portfolio_orders",
                        "counts": {"candidates": 3, "orders": 0},
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        with patch.object(market_health, "_probe_market_data", return_value={"status": "ok", "asset_count": 10}):
            with patch.object(market_health, "_market_session_state", return_value={"in_session": True, "samples_expected_today": True}):
                check = market_health._check_sim_market_loop("ashare", "job_ashare_sim_exec.sh")

        self.assertEqual(check.status, "warn")
        self.assertIn("server_local_sim_has_no_production_trades_yet", check.details["warn_reasons"])
        self.assertNotIn("ashare_waiting_for_portfolio_or_strategy_signal", check.details["warn_reasons"])
        self.assertEqual(check.details["no_trade_explanation"]["evidence_status"], "incomplete")
        self.assertIn("candidate_decision_trace_missing", check.details["no_trade_explanation"]["evidence_gaps"])

    def test_ashare_sim_loop_warns_when_empty_candidate_pool_lacks_plan_evidence(self) -> None:
        log_path = self.root / "shared/logs/ashare_no_trade_explanations.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps(
                {
                    "generated_at": "2026-07-08T10:25:00+08:00",
                    "no_trade_explanation": {
                        "category": "no_candidates",
                        "counts": {"candidates": 0, "orders": 0},
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        with patch.object(market_health, "_probe_market_data", return_value={"status": "ok", "asset_count": 10}):
            with patch.object(market_health, "_market_session_state", return_value={"in_session": True, "samples_expected_today": True}):
                check = market_health._check_sim_market_loop("ashare", "job_ashare_sim_exec.sh")

        self.assertEqual(check.status, "warn")
        self.assertIn("server_local_sim_has_no_production_trades_yet", check.details["warn_reasons"])
        self.assertEqual(check.details["no_trade_explanation"]["evidence_status"], "incomplete")
        self.assertEqual(
            check.details["no_trade_explanation"]["evidence_gaps"],
            [
                "candidate_decision_trace_missing",
                "capital_plan_decision_missing",
                "portfolio_decision_missing",
            ],
        )

    def test_ashare_sim_loop_accepts_empty_candidate_pool_with_plan_evidence(self) -> None:
        log_path = self.root / "shared/logs/ashare_no_trade_explanations.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps(
                {
                    "generated_at": "2026-07-08T10:25:00+08:00",
                    "no_trade_explanation": {
                        "category": "no_candidates",
                        "counts": {"candidates": 0, "orders": 0},
                        "candidate_decision_trace": [],
                        "capital_plan_decision": {"risk_mode": "defensive", "position_capacity": 0},
                        "portfolio_decision": {"allowed_buy_count": 0},
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        with patch.object(market_health, "_probe_market_data", return_value={"status": "ok", "asset_count": 10}):
            with patch.object(market_health, "_market_session_state", return_value={"in_session": True, "samples_expected_today": True}):
                check = market_health._check_sim_market_loop("ashare", "job_ashare_sim_exec.sh")

        self.assertEqual(check.status, "pass")
        self.assertIn("ashare_waiting_for_portfolio_or_strategy_signal", check.details["warn_reasons"])
        self.assertEqual(check.details["no_trade_explanation"]["evidence_status"], "ready")

    def test_ashare_sim_loop_warns_when_risk_rejection_gap_lacks_trace_evidence(self) -> None:
        log_path = self.root / "shared/logs/ashare_no_trade_explanations.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps(
                {
                    "generated_at": "2026-07-08T10:25:00+08:00",
                    "no_trade_explanation": {
                        "category": "all_rejected_by_risk",
                        "counts": {"candidates": 3, "orders": 0, "risk_rejections": 3},
                        "sample_risk_rejections": [{"symbol": "AAA", "reasons": ["unit risk"]}],
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        with patch.object(market_health, "_probe_market_data", return_value={"status": "ok", "asset_count": 10}):
            with patch.object(market_health, "_market_session_state", return_value={"in_session": True, "samples_expected_today": True}):
                check = market_health._check_sim_market_loop("ashare", "job_ashare_sim_exec.sh")

        self.assertEqual(check.status, "warn")
        self.assertIn("server_local_sim_has_no_production_trades_yet", check.details["warn_reasons"])
        self.assertNotIn("ashare_waiting_for_portfolio_or_strategy_signal", check.details["warn_reasons"])
        self.assertEqual(check.details["no_trade_explanation"]["evidence_status"], "incomplete")
        self.assertIn("candidate_decision_trace_missing", check.details["no_trade_explanation"]["evidence_gaps"])

    def test_ashare_sim_loop_uses_today_trade_rows_not_historical_total(self) -> None:
        local_sim = self.root / "shared/logs/local_sim/local_sim_trades.jsonl"
        local_sim.parent.mkdir(parents=True, exist_ok=True)
        local_sim.write_text(
            json.dumps({"trade_date": "20260707", "ts_code": "600000.SH", "status": "filled"}) + "\n",
            encoding="utf-8",
        )
        log_path = self.root / "shared/logs/ashare_no_trade_explanations.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps(
                {
                    "date": "20260708",
                    "generated_at": "2026-07-08T14:57:00+08:00",
                    "no_trade_explanation": {
                        "category": "no_portfolio_orders",
                        "counts": {"candidates": 3, "orders": 0},
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        with patch.object(market_health, "_probe_market_data", return_value={"status": "ok", "asset_count": 10}):
            with patch.object(
                market_health,
                "_market_session_state",
                return_value={"in_session": False, "samples_expected_today": True, "local_time": "2026-07-08T15:30:00+08:00"},
            ):
                check = market_health._check_sim_market_loop("ashare", "job_ashare_sim_exec.sh")

        self.assertEqual(check.details["ledger"]["trade_rows"], 1)
        self.assertEqual(check.details["ledger"]["today_trade_rows"], 0)
        self.assertEqual(check.status, "warn")
        self.assertIn("server_local_sim_has_no_production_trades_yet", check.details["warn_reasons"])
        self.assertEqual(check.details["no_trade_explanation"]["evidence_status"], "incomplete")

    def test_ashare_sim_loop_ignores_previous_day_no_trade_evidence(self) -> None:
        log_path = self.root / "shared/logs/ashare_no_trade_explanations.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps(
                {
                    "date": "20260707",
                    "generated_at": "2026-07-07T14:57:00+08:00",
                    "no_trade_explanation": {
                        "category": "no_portfolio_orders",
                        "counts": {"candidates": 3, "orders": 0},
                        "candidate_decision_trace": [{"symbol": "600000.SH", "decision": "skip"}],
                        "capital_plan_decision": {"target_new_positions": 0},
                        "portfolio_decision": {"orders": []},
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        with patch.object(market_health, "_probe_market_data", return_value={"status": "ok", "asset_count": 10}):
            with patch.object(
                market_health,
                "_market_session_state",
                return_value={"in_session": True, "samples_expected_today": True, "local_time": "2026-07-08T10:30:00+08:00"},
            ):
                check = market_health._check_sim_market_loop("ashare", "job_ashare_sim_exec.sh")

        self.assertEqual(check.details["no_trade_explanation"], {})
        self.assertEqual(check.status, "warn")
        self.assertIn("server_local_sim_has_no_production_trades_yet", check.details["warn_reasons"])
        self.assertNotIn("ashare_waiting_for_portfolio_or_strategy_signal", check.details["warn_reasons"])

    def test_ashare_sim_loop_passes_without_sample_before_session(self) -> None:
        with patch.object(market_health, "_probe_market_data", return_value={"status": "ok", "asset_count": 10}):
            with patch.object(market_health, "_market_session_state", return_value={"in_session": False, "samples_expected_today": False}):
                check = market_health._check_sim_market_loop("ashare", "job_ashare_sim_exec.sh")

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["warn_reasons"], [])

    def test_default_sim_market_health_excludes_deferred_hk(self) -> None:
        def fake_check(market: str, crontab_text: str = "", crontab_error: str = "") -> market_health.Check:
            return market_health.Check(f"{market}_sim_loop", "pass", f"{market} ok")

        with patch.object(market_health, "_installed_crontab_text", return_value=("", "")):
            with patch.object(market_health, "_check_sim_market_loop", side_effect=fake_check):
                result = market_health.run_sim_market_health()

        names = [check["name"] for check in result["checks"]]
        self.assertEqual(names, ["ashare_sim_loop", "crypto_sim_loop", "pm_sim_loop", "us_sim_loop", "cn_futures_sim_loop"])
        self.assertNotIn("hk_sim_loop", names)

    def test_cn_futures_sim_loop_warns_without_live_samples(self) -> None:
        with patch.object(market_health, "_probe_market_data", return_value={"status": "fail", "reason": "futures_universe_missing"}):
            with patch.object(market_health, "_market_session_state", return_value={"in_session": True, "samples_expected_today": True}):
                check = market_health._check_sim_market_loop("cn_futures", "job_cn_futures_sim.sh")

        self.assertEqual(check.status, "warn")
        self.assertIn("futures_market_data_not_ready", check.details["warn_reasons"])
        self.assertIn("cn_futures_review_has_no_samples_yet", check.details["warn_reasons"])

    def test_cn_futures_sim_loop_waits_without_sample_before_session(self) -> None:
        with patch.object(market_health, "_probe_market_data", return_value={"status": "ok", "reason": "futures_intraday_waiting_for_next_session"}):
            with patch.object(market_health, "_market_session_state", return_value={"in_session": False, "samples_expected_today": False}):
                check = market_health._check_sim_market_loop("cn_futures", "job_cn_futures_sim.sh")

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["warn_reasons"], [])

    def test_cn_futures_sim_loop_reads_append_only_review_as_ledger(self) -> None:
        review = self.root / "shared/review/data/cn_futures_sim_reviews.jsonl"
        review.parent.mkdir(parents=True)
        review.write_text(
            json.dumps(
                {
                    "state": "ok",
                    "filled_count": 2,
                    "error_count": 0,
                    "error_summary": {"total": 0},
                    "style_health": {"trend": {"status": "active_sample"}},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with patch.object(market_health, "_probe_market_data", return_value={"status": "warn", "priced_signal_count": 0}):
            with patch.object(market_health, "_market_session_state", return_value={"in_session": True, "samples_expected_today": True}):
                check = market_health._check_sim_market_loop("cn_futures", "job_cn_futures_sim.sh")

        self.assertEqual(check.status, "warn")
        self.assertEqual(check.details["ledger"]["trade_rows"], 2)
        self.assertEqual(check.details["ledger"]["latest_style_health"]["trend"]["status"], "active_sample")

    def test_cn_futures_market_health_entrypoint_is_read_only(self) -> None:
        payload = {
            "market": "cn_futures",
            "overall_status": "pass",
            "summary": {"pass": 1, "warn": 0, "fail": 0},
            "checks": [],
            "real_trading_enabled": False,
        }
        with patch("shared.runtime_test.cn_futures_live_check.run_live_check", return_value=payload):
            result = market_health.run_cn_futures_health()

        self.assertEqual(result["market"], "cn_futures")
        self.assertFalse(result["real_trading_enabled"])


if __name__ == "__main__":
    unittest.main()
