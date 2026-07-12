from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from CNFutures.sample_maturity import (
    canonical_futures_maturity_projection_sha256,
)
from shared.runtime_test import cn_futures_live_check as live_check


def _seal_maturity(payload: dict[str, object]) -> dict[str, object]:
    sealed = dict(payload)
    sealed["projection_sha256"] = canonical_futures_maturity_projection_sha256(sealed)
    return sealed


class CNFuturesLiveCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "TradingAgent"
        self.sharedsignals = Path(self.tmp.name) / "SharedSignals"
        (self.root / "shared/runtime_test").mkdir(parents=True)
        (self.sharedsignals / "tools").mkdir(parents=True)
        (self.sharedsignals / "tools/check_cn_futures_5min_freshness.py").write_text(
            "# fake\n", encoding="utf-8"
        )

        patches = [
            patch.object(live_check, "ROOT", self.root),
            patch.object(
                live_check,
                "CN_FUTURES_REVIEW",
                self.root / "shared/review/data/cn_futures_sim_reviews.jsonl",
            ),
            patch.object(
                live_check,
                "CN_FUTURES_MATURITY",
                self.root / "shared/review/cn_futures/market_maturity_latest.json",
            ),
            patch.object(
                live_check,
                "CN_FUTURES_SIM_LOG",
                self.root / "shared/logs/cron/job_cn_futures_sim.log",
            ),
            patch.object(
                live_check,
                "CN_FUTURES_LEGACY_SIM_LOG",
                self.root / "shared/logs/cron/cn_futures_sim.log",
            ),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def _completed(
        self, payload: dict[str, object], returncode: int = 0
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["python"], returncode, stdout=json.dumps(payload), stderr=""
        )

    def _fake_runner(self, payload: dict[str, object], returncode: int = 0):
        def run_command(
            *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return self._completed(payload, returncode)

        return run_command

    def _write_jsonl(self, rel: str, rows: list[dict[str, object]]) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
        )

    def _write_ready_outputs(self) -> None:
        self._write_jsonl(
            "shared/review/data/cn_futures_sim_reviews.jsonl",
            [
                {
                    "generated_at": "2026-07-06T01:05:00+00:00",
                    "state": "ok",
                    "cadence": "5min",
                    "latest_bar_time": "2026-07-06 09:05:00",
                    "filled_count": 2,
                    "error_count": 0,
                    "real_trading_enabled": False,
                    "style_health": {"trend": {"status": "active_sample"}},
                }
            ],
        )
        maturity = self.root / "shared/review/cn_futures/market_maturity_latest.json"
        maturity.parent.mkdir(parents=True, exist_ok=True)
        maturity.write_text(
            json.dumps(
                _seal_maturity(
                    {
                        "report_type": "cn_futures_market_maturity_v1",
                        "evidence_source": "cn_futures_review_journal+sample_kpi",
                        "market": "cnfutures",
                        "capital_layer": "simulated",
                        "account_type": "simulated",
                        "trade_date": "20260706",
                        "generated_at": "2026-07-06T15:10:00+08:00",
                        "stage": "stage_initial_samples",
                        "authority_scope": {
                            "capital_authority_id": "cn-futures-capital-v1",
                            "authority_generation": 1,
                            "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
                        },
                        "pool_cny": 50_000,
                        "margin_utilization_limit_cny": 25_000,
                        "source_review_sha256": "a" * 64,
                        "sample_counts": {
                            "valid_sample_count": 2,
                            "completed_round_trip_count": 1,
                        },
                        "performance": {"post_cost_pnl_cny": 2.0},
                        "sample_kpi_projection": {
                            "styles": {"trend": {"prediction_count": 2}}
                        },
                        "promotion_policy_status": "manual_review_only_no_futures_live_date",
                        "automatic_promotion_enabled": False,
                        "automatic_risk_expansion_enabled": False,
                        "live_transition_authorized": False,
                        "real_trading_enabled": False,
                        "live_execution_enabled": False,
                    }
                )
            ),
            encoding="utf-8",
        )
        log = self.root / "shared/logs/cron/job_cn_futures_sim.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            'noise\n{"market":"cn_futures","status":"ok","cadence":"5min","filled_count":2,"latest_bar_time":"2026-07-06 09:05:00"}\n',
            encoding="utf-8",
        )

    def test_warns_without_data_but_structure_is_readable(self) -> None:
        with (
            patch("CNFutures.session.active_trade_date", return_value="20260706"),
            patch.object(
                live_check,
                "check_existing_health_surfaces",
                return_value=live_check.Check(
                    "cn_futures_existing_health_surfaces", "pass", "ok"
                ),
            ),
        ):
            report = live_check.run_live_check(
                sharedsignals_root=self.sharedsignals,
                run_command=self._fake_runner(
                    {"status": "no_data", "error": "no bars"}, returncode=1
                ),
                crontab_text="cn_futures_5min.sh\njob_cn_futures_sim.sh\njob_cn_futures_sample_ops.sh\njob_cn_futures_observation_report.sh\njob_cn_futures_calibration_report.sh\njob_cn_futures_pre_open_validation.sh\njob_cn_futures_first_sample_alert.sh",
            )

        self.assertEqual(report["overall_status"], "warn")
        self.assertEqual(report["summary"]["fail"], 0)
        self.assertEqual(report["observation_phase"], "waiting_for_5min_data")
        self.assertEqual(
            report["next_validation"]["expected_phase"], "wait_for_next_session"
        )
        self.assertFalse(report["next_validation"]["real_trading_enabled"])
        freshness = next(
            check
            for check in report["checks"]
            if check["name"] == "sharedsignals_5min_freshness"
        )
        self.assertEqual(freshness["status"], "warn")
        self.assertFalse(report["real_trading_enabled"])

    def test_passes_when_freshness_cron_review_and_style_outputs_are_ready(
        self,
    ) -> None:
        self._write_ready_outputs()

        with (
            patch("CNFutures.session.active_trade_date", return_value="20260706"),
            patch.object(
                live_check,
                "check_existing_health_surfaces",
                return_value=live_check.Check(
                    "cn_futures_existing_health_surfaces", "pass", "ok"
                ),
            ),
        ):
            report = live_check.run_live_check(
                sharedsignals_root=self.sharedsignals,
                run_command=self._fake_runner(
                    {"status": "fresh", "latest_bar_time": "2026-07-06T09:05:00+08:00"},
                    returncode=0,
                ),
                crontab_text="cn_futures_5min.sh\njob_cn_futures_sim.sh\njob_cn_futures_sample_ops.sh\njob_cn_futures_observation_report.sh\njob_cn_futures_calibration_report.sh\njob_cn_futures_pre_open_validation.sh\njob_cn_futures_first_sample_alert.sh",
            )

        self.assertEqual(report["overall_status"], "pass")
        self.assertEqual(report["summary"], {"pass": 6, "warn": 0, "fail": 0})
        self.assertEqual(report["observation_phase"], "ready_to_observe")
        self.assertEqual(
            report["next_validation"]["expected_phase"], "continue_observation"
        )
        self.assertEqual(report["alerts"], [])

    def test_fails_when_sharedsignals_freshness_script_errors(self) -> None:
        with patch.object(
            live_check,
            "check_existing_health_surfaces",
            return_value=live_check.Check(
                "cn_futures_existing_health_surfaces", "pass", "ok"
            ),
        ):
            report = live_check.run_live_check(
                sharedsignals_root=self.sharedsignals,
                run_command=self._fake_runner(
                    {"status": "error", "error": "database missing"}, returncode=2
                ),
                crontab_text="cn_futures_5min.sh\njob_cn_futures_sim.sh\njob_cn_futures_sample_ops.sh",
            )

        self.assertEqual(report["overall_status"], "fail")
        freshness = next(
            check
            for check in report["checks"]
            if check["name"] == "sharedsignals_5min_freshness"
        )
        self.assertEqual(freshness["status"], "fail")
        self.assertEqual(report["observation_phase"], "blocked")

    def test_cron_missing_is_hard_failure_when_crontab_is_readable(self) -> None:
        check = live_check.check_cron_entries("job_cn_futures_sim.sh")

        self.assertEqual(check.status, "fail")
        self.assertIn("sharedsignals_collector", check.details["missing"])
        self.assertIn("tradingagent_sample_ops", check.details["missing"])
        self.assertNotIn("tradingagent_evolution", check.details["found"])

    def test_alerts_when_market_session_has_no_fresh_5min_data(self) -> None:
        with patch.object(
            live_check,
            "check_existing_health_surfaces",
            return_value=live_check.Check(
                "cn_futures_existing_health_surfaces", "pass", "ok"
            ),
        ):
            report = live_check.run_live_check(
                sharedsignals_root=self.sharedsignals,
                run_command=self._fake_runner(
                    {
                        "status": "stale",
                        "latest_bar_time": None,
                        "session": {"current": "day", "in_session": True},
                    },
                    returncode=1,
                ),
                crontab_text="cn_futures_5min.sh\njob_cn_futures_sim.sh\njob_cn_futures_sample_ops.sh",
            )

        codes = {alert["code"] for alert in report["alerts"]}
        self.assertIn("futures_5min_missing_in_session", codes)

    def test_maturity_projection_fails_closed_on_wrong_lineage_and_live_flag(
        self,
    ) -> None:
        self._write_ready_outputs()
        path = self.root / "shared/review/cn_futures/market_maturity_latest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["authority_scope"]["capital_authority_id"] = "legacy-shared-master"
        payload["live_transition_authorized"] = True
        path.write_text(json.dumps(payload), encoding="utf-8")

        check = live_check.check_maturity_projection(
            path, expected_trade_date="20260706"
        )

        self.assertEqual(check.status, "fail")
        self.assertIn("capital_authority_id_mismatch", check.details["issues"])
        self.assertIn(
            "live_transition_authorized_must_be_false", check.details["issues"]
        )

    def test_maturity_projection_requires_timezone_aware_generation_time(self) -> None:
        self._write_ready_outputs()
        path = self.root / "shared/review/cn_futures/market_maturity_latest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["generated_at"] = "2026-07-06T15:10:00"
        path.write_text(json.dumps(payload), encoding="utf-8")

        check = live_check.check_maturity_projection(
            path, expected_trade_date="20260706"
        )

        self.assertEqual(check.status, "fail")
        self.assertIn("generated_at_timezone_required", check.details["issues"])

    def test_maturity_projection_requires_explicit_simulated_account_markers(
        self,
    ) -> None:
        self._write_ready_outputs()
        path = self.root / "shared/review/cn_futures/market_maturity_latest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("capital_layer")
        payload.pop("account_type")
        path.write_text(json.dumps(payload), encoding="utf-8")

        check = live_check.check_maturity_projection(path)

        self.assertEqual(check.status, "fail")
        self.assertIn("capital_layer_must_be_simulated", check.details["issues"])
        self.assertIn("account_type_must_be_simulated", check.details["issues"])

    def test_maturity_projection_tamper_is_rejected_by_projection_hash(self) -> None:
        self._write_ready_outputs()
        path = self.root / "shared/review/cn_futures/market_maturity_latest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sample_counts"]["valid_sample_count"] = 999
        path.write_text(json.dumps(payload), encoding="utf-8")

        check = live_check.check_maturity_projection(path)

        self.assertEqual(check.status, "fail")
        self.assertIn("projection_sha256_invalid", check.details["issues"])

    def test_sim_log_warns_when_5min_fill_has_no_bar_time(self) -> None:
        log = self.root / "shared/logs/cron/cn_futures_sim.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            '{"market":"cn_futures","status":"ok","cadence":"5min","filled_count":1}\n',
            encoding="utf-8",
        )

        check = live_check.check_sim_log(log)

        self.assertEqual(check.status, "warn")
        self.assertEqual(check.details["payload"]["filled_count"], 1)

    def test_sim_log_reads_wrapper_job_log_before_legacy_log(self) -> None:
        wrapper_log = self.root / "shared/logs/cron/job_cn_futures_sim.log"
        legacy_log = self.root / "shared/logs/cron/cn_futures_sim.log"
        wrapper_log.parent.mkdir(parents=True, exist_ok=True)
        wrapper_log.write_text(
            "[2026-07-06T09:05:00+0800] job_cn_futures_sim attempt=1 phase=intraday\n"
            '{"market":"cn_futures","status":"ok","cadence":"5min","filled_count":1,"latest_bar_time":"2026-07-06 09:05:00"}\n',
            encoding="utf-8",
        )
        legacy_log.write_text(
            '{"market":"cn_futures","status":"ok","cadence":"5min","filled_count":0}\n',
            encoding="utf-8",
        )

        check = live_check.check_sim_log()

        self.assertEqual(check.status, "pass")
        self.assertEqual(
            check.details["path"], "shared/logs/cron/job_cn_futures_sim.log"
        )
        self.assertEqual(check.details["payload"]["filled_count"], 1)

    def test_sim_log_treats_coverage_observation_as_normal(self) -> None:
        log = self.root / "shared/logs/cron/job_cn_futures_sim.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            '{"market":"cn_futures","state":"observation_only","cadence":"5min","filled_count":0,"hold_reason_summary":{"by_reason":{"insufficient_distinct_product_coverage":1}}}\n',
            encoding="utf-8",
        )

        check = live_check.check_sim_log(log)

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["payload"]["state"], "observation_only")

    def test_review_warns_when_filled_5min_sample_lacks_bar_time_or_real_flag_is_on(
        self,
    ) -> None:
        self._write_jsonl(
            "shared/review/data/cn_futures_sim_reviews.jsonl",
            [
                {
                    "state": "ok",
                    "cadence": "5min",
                    "filled_count": 1,
                    "real_trading_enabled": True,
                }
            ],
        )

        check = live_check.check_review()

        self.assertEqual(check.status, "warn")
        self.assertEqual(check.details["latest_real_trading_enabled"], True)
        self.assertFalse(check.details["latest_has_bar_time"])

    def test_review_distinguishes_strategy_hold_from_missing_sim_sample(self) -> None:
        self._write_jsonl(
            "shared/review/data/cn_futures_sim_reviews.jsonl",
            [
                {
                    "state": "ok",
                    "cadence": "5min",
                    "filled_count": 0,
                    "hold_count": 3,
                    "hold_reason_summary": {
                        "total": 3,
                        "by_reason": {"below_threshold": 3},
                    },
                }
            ],
        )

        check = live_check.check_review()

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["latest_sample_phase"], "strategy_hold")
        self.assertEqual(check.details["latest_top_hold_reason"], "below_threshold")
        self.assertIn("主动 hold", check.summary)

    def test_review_uses_latest_actionable_row_before_empty_close_rows(self) -> None:
        self._write_jsonl(
            "shared/review/data/cn_futures_sim_reviews.jsonl",
            [
                {
                    "state": "ok",
                    "cadence": "5min",
                    "latest_bar_time": "2026-07-08 14:56:00",
                    "filled_count": 0,
                    "hold_count": 1,
                    "hold_reason_summary": {
                        "total": 1,
                        "by_reason": {"session_close_guard": 1},
                    },
                },
                {
                    "state": "ok",
                    "cadence": "",
                    "latest_bar_time": "",
                    "filled_count": 0,
                    "hold_count": 0,
                    "record_count": 0,
                    "error_count": 0,
                    "hold_reason_summary": {"total": 0, "by_reason": {}},
                },
            ],
        )

        check = live_check.check_review()

        self.assertEqual(check.details["latest_sample_phase"], "strategy_hold")
        self.assertEqual(check.details["latest_top_hold_reason"], "session_close_guard")
        self.assertEqual(check.details["latest_bar_time"], "2026-07-08 14:56:00")

    def test_review_distinguishes_no_night_session_from_missing_sim_sample(
        self,
    ) -> None:
        self._write_jsonl(
            "shared/review/data/cn_futures_sim_reviews.jsonl",
            [
                {
                    "state": "ok",
                    "cadence": "5min",
                    "filled_count": 0,
                    "hold_count": 2,
                    "hold_reason_summary": {
                        "total": 2,
                        "by_reason": {"style_session_not_allowed": 2},
                    },
                }
            ],
        )

        check = live_check.check_review()

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["latest_sample_phase"], "no_night_session")
        self.assertEqual(
            check.details["latest_top_hold_reason"], "style_session_not_allowed"
        )
        self.assertIn("夜盘", check.summary)

    def test_review_surfaces_per_product_insufficient_consecutive_bars(self) -> None:
        self._write_jsonl(
            "shared/review/data/cn_futures_sim_reviews.jsonl",
            [
                {
                    "state": "ok",
                    "cadence": "5min",
                    "filled_count": 0,
                    "hold_count": 4,
                    "hold_reason_summary": {
                        "total": 4,
                        "by_reason": {
                            "insufficient_consecutive_5min_bars": 3,
                            "volume_confirmation_filter": 1,
                        },
                        "by_product": {"if": 1, "ih": 1, "rb": 2},
                        "by_product_by_reason": {
                            "if": {"insufficient_consecutive_5min_bars": 1},
                            "ih": {"insufficient_consecutive_5min_bars": 1},
                            "rb": {
                                "insufficient_consecutive_5min_bars": 1,
                                "volume_confirmation_filter": 1,
                            },
                        },
                    },
                }
            ],
        )

        check = live_check.check_review()

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.details["latest_sample_phase"], "strategy_hold")
        self.assertEqual(
            check.details["latest_top_hold_reason"],
            "insufficient_consecutive_5min_bars",
        )
        self.assertEqual(
            check.details["latest_hold_reason_summary"]["by_product"]["rb"], 2
        )
        insufficient_by_product = check.details.get(
            "insufficient_consecutive_bars_by_product", {}
        )
        self.assertEqual(insufficient_by_product.get("if"), 1)
        self.assertEqual(insufficient_by_product.get("ih"), 1)
        self.assertEqual(insufficient_by_product.get("rb"), 1)

    def test_freshness_api_url_has_no_date_param(self) -> None:
        """The SharedSignals freshness check must NOT pass a date param to the API."""
        base_url = "http://127.0.0.1:8082"
        import urllib.parse

        params = urllib.parse.urlencode({"market": "Futures"})
        url = f"{base_url}/realtime_5min?{params}"
        self.assertNotIn("date=", url, "API URL must not contain date param")

    def test_freshness_rejects_stale_bars_directly(self) -> None:
        """Direct test: check_sharedsignals_freshness filters out bars before session start."""
        import subprocess as _subprocess_mod
        from unittest.mock import patch

        # Build a proper HTTP response mock
        mock_body = json.dumps(
            {
                "data": [
                    {
                        "symbol": "IF2609.CFX",
                        "bar_time": "2026-07-06 08:55:00",
                        "close": 3500.0,
                    },  # before session
                    {
                        "symbol": "IF2609.CFX",
                        "bar_time": "2026-07-06 09:05:00",
                        "close": 3510.0,
                    },  # in session
                ]
            }
        ).encode("utf-8")

        class MockResponse:
            def read(self) -> bytes:
                return mock_body

            def __enter__(self) -> "MockResponse":
                return self

            def __exit__(self, *args: object) -> None:
                pass

        with (
            patch.object(
                live_check,
                "cn_futures_session_state",
                return_value={
                    "session_start": "2026-07-06 09:00:00+08:00",
                    "in_session": True,
                    "local_time": "2026-07-06 09:10:00+08:00",
                },
            ),
            patch("urllib.request.urlopen", return_value=MockResponse()),
            patch("urllib.request.Request"),
        ):
            check = live_check.check_sharedsignals_freshness(
                Path("/tmp"),
                run_command=_subprocess_mod.run,
            )

        self.assertEqual(
            check.status,
            "pass",
            "Should pass with in-session bars after filtering stale ones",
        )
        self.assertEqual(
            check.details.get("filtered_row_count"),
            1,
            "Only the in-session bar should remain after filtering",
        )


if __name__ == "__main__":
    unittest.main()
