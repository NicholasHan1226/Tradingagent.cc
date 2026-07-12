from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from CNFutures import observation_report
from CNFutures.sample_maturity import (
    canonical_futures_maturity_projection_sha256,
)


def _seal_maturity(payload: dict[str, object]) -> dict[str, object]:
    sealed = dict(payload)
    sealed["projection_sha256"] = canonical_futures_maturity_projection_sha256(sealed)
    return sealed


class CNFuturesObservationReportTest(unittest.TestCase):
    def test_legacy_weight_and_evolution_files_cannot_influence_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review_root = root / "review"
            current_dir = review_root / "cn_futures"
            current_dir.mkdir(parents=True)
            (current_dir / "market_maturity_latest.json").write_text(
                json.dumps(
                    _seal_maturity(
                        {
                            "report_type": "cn_futures_market_maturity_v1",
                            "evidence_source": "cn_futures_review_journal+sample_kpi",
                            "market": "cnfutures",
                            "capital_layer": "simulated",
                            "account_type": "simulated",
                            "trade_date": "20260713",
                            "generated_at": "2026-07-13T15:10:00+08:00",
                            "stage": "stage_initial_samples",
                            "authority_scope": {
                                "capital_authority_id": "cn-futures-capital-v1",
                                "authority_generation": 1,
                                "execution_lineage_id": "cn-futures-sim-fresh-20260712-v1",
                            },
                            "pool_cny": 50_000,
                            "margin_utilization_limit_cny": 25_000,
                            "source_review_sha256": "b" * 64,
                            "sample_counts": {
                                "valid_sample_count": 3,
                                "completed_round_trip_count": 1,
                                "forward_label_count": 6,
                                "pending_forward_label_count": 2,
                            },
                            "performance": {"post_cost_pnl_cny": 8.5},
                            "blocking_reasons": [
                                "missing_independent_stability_evidence"
                            ],
                            "sample_kpi_projection": {
                                "styles": {
                                    "trend": {
                                        "prediction_count": 3,
                                        "completed_round_trip_count": 1,
                                        "post_cost_pnl_cny": 8.5,
                                    }
                                }
                            },
                            "promotion_evidence_ready": False,
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
            (current_dir / "style_weights.json").write_text(
                json.dumps(
                    {
                        "real_trading_enabled": True,
                        "styles": {"rogue": {"weight": 1.0, "enabled": True}},
                    }
                ),
                encoding="utf-8",
            )
            (current_dir / "evolution_plan.json").write_text(
                json.dumps(
                    {
                        "state": "expanded",
                        "actions": [{"action": "expand_risk"}],
                        "generated_variants": [{"style_name": "rogue"}],
                    }
                ),
                encoding="utf-8",
            )
            live_report = {
                "generated_at": "2026-07-13T08:00:00+00:00",
                "overall_status": "pass",
                "observation_phase": "ready_to_observe",
                "alerts": [],
                "checks": [
                    {
                        "name": "cn_futures_review",
                        "details": {"current_trade_date": "20260713"},
                    }
                ],
            }

            with patch.object(
                observation_report, "run_live_check", return_value=live_report
            ):
                report = observation_report.build_observation_report(
                    review_root=review_root,
                    review_path=review_root / "data/cn_futures_sim_reviews.jsonl",
                )

            self.assertEqual(report["maturity"]["status"], "current")
            self.assertEqual(
                report["maturity"]["authority_scope"]["capital_authority_id"],
                "cn-futures-capital-v1",
            )
            self.assertEqual(report["styles"]["source"], "market_maturity_sample_kpi")
            self.assertEqual(
                [row["style_name"] for row in report["styles"]["ranked"]], ["trend"]
            )
            self.assertEqual(report["styles"]["weights"], {})
            self.assertEqual(report["evolution"]["state"], "manual_review_only")
            self.assertEqual(report["evolution"]["action_count"], 0)
            self.assertEqual(report["evolution"]["generated_variants"], [])
            self.assertFalse(report["evolution"]["automatic_promotion_enabled"])
            self.assertNotIn(
                "real_trading_enabled_forced_false", report["config_warnings"]
            )

            maturity_path = current_dir / "market_maturity_latest.json"
            tampered = json.loads(maturity_path.read_text(encoding="utf-8"))
            tampered["sample_counts"]["valid_sample_count"] = 999
            maturity_path.write_text(json.dumps(tampered), encoding="utf-8")
            with patch.object(
                observation_report, "run_live_check", return_value=live_report
            ):
                rejected = observation_report.build_observation_report(
                    review_root=review_root,
                    review_path=review_root / "data/cn_futures_sim_reviews.jsonl",
                )
            self.assertEqual(rejected["maturity"]["status"], "invalid")
            self.assertIn("projection_sha256_invalid", rejected["maturity"]["issues"])
            self.assertEqual(rejected["styles"]["ranked"], [])
            self.assertFalse(rejected["evolution"]["promotion_evidence_ready"])

    def test_non_authoritative_or_counterfactual_affordability_cannot_claim_capacity(
        self,
    ) -> None:
        cases = (
            (
                "non_authoritative_account",
                {
                    "authoritative": False,
                    "counterfactual_only": True,
                    "source": "cn_futures_local_sim_account_state",
                },
                None,
                False,
            ),
            (
                "counterfactual_affordability",
                {
                    "authoritative": True,
                    "counterfactual_only": False,
                    "source": "master_capital_ledger",
                },
                None,
                True,
            ),
            (
                "non_authoritative_affordability",
                {
                    "authoritative": True,
                    "counterfactual_only": False,
                    "source": "master_capital_ledger",
                },
                False,
                False,
            ),
        )
        for (
            case_name,
            account_state,
            affordability_authoritative,
            counterfactual_only,
        ) in cases:
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                review_root = root / "review"
                review_path = root / "cn_futures_sim_reviews.jsonl"
                review_path.with_name(
                    "cn_futures_affordability_latest.json"
                ).write_text(
                    json.dumps(
                        {
                            "date": "20260711",
                            "account_state": account_state,
                            "authoritative": affordability_authoritative,
                            "counterfactual_only": counterfactual_only,
                            "real_trading_enabled": True,
                            "raw_distinct_products": ["rb"],
                            "affordable_distinct_products": ["rb"],
                            "affordable_distinct_product_count": 1,
                            "contracts": [
                                {
                                    "style": "trend",
                                    "product": "rb",
                                    "eligible": True,
                                    "counterfactual_only": False,
                                    "execution_class": "new_position",
                                    "real_trading_enabled": True,
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                cn_dir = review_root / "cn_futures"
                cn_dir.mkdir(parents=True)
                (cn_dir / "style_comparison.json").write_text(
                    json.dumps(
                        {
                            "style_comparison": [
                                {
                                    "style_name": "trend",
                                    "win_rate": 0.5,
                                    "real_trading_enabled": True,
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                (cn_dir / "style_weights.json").write_text(
                    json.dumps(
                        {
                            "real_trading_enabled": True,
                            "styles": {
                                "trend": {
                                    "weight": 1.0,
                                    "real_trading_enabled": True,
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                live_report = {
                    "overall_status": "pass",
                    "observation_phase": "ready_to_observe",
                    "checks": [
                        {
                            "name": "cn_futures_review",
                            "details": {"current_trade_date": "20260711"},
                        }
                    ],
                    "alerts": [],
                }

                with patch.object(
                    observation_report, "run_live_check", return_value=live_report
                ):
                    report = observation_report.build_observation_report(
                        review_root=review_root,
                        review_path=review_path,
                    )

                affordability = report["simulation"]["affordability"]
                self.assertEqual(affordability["affordable_distinct_products"], [])
                self.assertEqual(affordability["affordable_distinct_product_count"], 0)
                self.assertTrue(affordability["counterfactual_only"])
                self.assertFalse(affordability["real_trading_enabled"])
                self.assertFalse(affordability["contracts"][0]["real_trading_enabled"])
                self.assertFalse(report["styles"]["real_trading_enabled"])
                self.assertEqual(report["styles"]["ranked"], [])
                self.assertEqual(report["styles"]["weights"], {})
                self.assertEqual(report["maturity"]["status"], "missing")
                self.assertIn(
                    "affordability_non_authoritative_capacity_ignored",
                    report["config_warnings"],
                )
                self.assertIn(
                    "real_trading_enabled_forced_false",
                    report["config_warnings"],
                )

    def test_build_observation_report_summarizes_data_simulation_and_evolution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review_root = root / "review"
            review_path = review_root / "data/cn_futures_sim_reviews.jsonl"
            review_path.parent.mkdir(parents=True)
            review_path.write_text(
                json.dumps(
                    {
                        "date": "20260706",
                        "state": "ok",
                        "record_count": 2,
                        "filled_count": 1,
                        "hold_count": 3,
                        "hold_reason_summary": {"by_reason": {"below_threshold": 3}},
                        "forward_label_summary": {
                            "styles": {
                                "index_intraday_directional": {
                                    "labeled": 4,
                                    "pending": 1,
                                    "win_rate": 0.75,
                                }
                            }
                        },
                        "dynamic_threshold_candidates": [
                            {
                                "style_name": "index_intraday_directional",
                                "action": "observe",
                            }
                        ],
                        "error_count": 0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cn_dir = review_root / "cn_futures"
            cn_dir.mkdir(parents=True)
            (cn_dir / "market_maturity_latest.json").write_text(
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
                            "source_review_sha256": "d" * 64,
                            "sample_counts": {
                                "valid_sample_count": 5,
                                "completed_round_trip_count": 1,
                                "forward_label_count": 4,
                                "pending_forward_label_count": 1,
                            },
                            "performance": {"post_cost_pnl_cny": 3.0},
                            "sample_kpi_projection": {
                                "styles": {
                                    "index_intraday_directional": {
                                        "prediction_count": 4,
                                        "completed_round_trip_count": 1,
                                        "post_cost_pnl_cny": 3.0,
                                    },
                                    "trend": {
                                        "prediction_count": 1,
                                        "completed_round_trip_count": 0,
                                        "post_cost_pnl_cny": 2.0,
                                    },
                                }
                            },
                            "blocking_reasons": [
                                "missing_independent_stability_evidence"
                            ],
                            "promotion_evidence_ready": False,
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
            live_report = {
                "generated_at": "2026-07-06T01:05:00+00:00",
                "overall_status": "pass",
                "observation_phase": "ready_to_observe",
                "alerts": [],
                "checks": [
                    {
                        "name": "sharedsignals_5min_freshness",
                        "details": {
                            "report": {
                                "status": "fresh",
                                "latest_bar_time": "2026-07-06 09:35:00",
                                "symbol_count": 8,
                                "total_bars": 160,
                                "session": {"current": "day", "in_session": True},
                            }
                        },
                    }
                ],
            }

            with patch.object(
                observation_report, "run_live_check", return_value=live_report
            ):
                report = observation_report.build_observation_report(
                    review_root=review_root, review_path=review_path
                )

            self.assertEqual(report["observation_phase"], "ready_to_observe")
            self.assertEqual(report["data"]["freshness_status"], "fresh")
            self.assertEqual(report["simulation"]["filled_count"], 1)
            self.assertEqual(report["simulation"]["hold_count"], 3)
            self.assertEqual(
                report["simulation"]["forward_label_summary"]["styles"][
                    "index_intraday_directional"
                ]["labeled"],
                4,
            )
            self.assertEqual(report["dashboard"]["forward_labeled_count"], 4)
            self.assertEqual(report["dashboard"]["forward_pending_count"], 1)
            self.assertEqual(
                report["dashboard"]["dynamic_threshold_candidate_count"], 1
            )
            self.assertEqual(report["dashboard"]["top_hold_reason"], "below_threshold")
            self.assertEqual(
                report["styles"]["ranked"][0]["style_name"],
                "index_intraday_directional",
            )
            self.assertEqual(report["evolution"]["state"], "manual_review_only")
            self.assertEqual(report["evolution"]["action_count"], 0)
            self.assertEqual(report["styles"]["weights"], {})
            self.assertEqual(report["dashboard"]["readiness"], "ready_to_observe")
            self.assertEqual(
                report["dashboard"]["primary_next_step"], "continue_observation"
            )
            self.assertEqual(
                report["simulation"]["affordability"]["affordable_distinct_products"],
                [],
            )
            self.assertEqual(
                report["simulation"]["affordability"][
                    "affordable_distinct_product_count"
                ],
                0,
            )
            self.assertFalse(report["simulation"]["affordability"]["authoritative"])
            self.assertFalse(
                report["simulation"]["affordability"]["real_trading_enabled"]
            )
            self.assertFalse(report["real_trading_enabled"])

    def test_affordability_excludes_counterfactual_contracts_and_retains_reasons(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review_path = root / "cn_futures_sim_reviews.jsonl"
            affordability_path = root / "cn_futures_affordability_latest.json"
            affordability_path.write_text(
                json.dumps(
                    {
                        "date": "20260711",
                        "account_state": {
                            "authoritative": True,
                            "counterfactual_only": False,
                            "source": "master_capital_ledger",
                        },
                        "counterfactual_only": False,
                        "raw_distinct_products": ["cu", "m", "rb"],
                        "affordable_distinct_products": ["m", "rb"],
                        "contracts": [
                            {
                                "style": "trend",
                                "symbol": "RB2610.SHF",
                                "product": "rb",
                                "eligible": True,
                                "counterfactual_only": True,
                                "reason": "account_state_unavailable",
                            },
                            {
                                "style": "trend",
                                "symbol": "CU2610.SHF",
                                "product": "cu",
                                "eligible": False,
                                "counterfactual_only": False,
                                "reason": "minimum_contract_exceeds_risk_budget",
                            },
                            {
                                "style": "trend",
                                "symbol": "M2610.DCE",
                                "product": "m",
                                "eligible": True,
                                "counterfactual_only": False,
                                "reason": "eligible",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            live_report = {
                "generated_at": "2026-07-11T01:36:00+00:00",
                "overall_status": "pass",
                "observation_phase": "ready_to_observe",
                "alerts": [],
                "checks": [
                    {
                        "name": "cn_futures_review",
                        "details": {"current_trade_date": "20260711"},
                    }
                ],
            }

            with patch.object(
                observation_report, "run_live_check", return_value=live_report
            ):
                report = observation_report.build_observation_report(
                    review_root=root / "review",
                    review_path=review_path,
                )

            affordability = report["simulation"]["affordability"]
            self.assertEqual(affordability["raw_distinct_products"], ["cu", "m", "rb"])
            self.assertEqual(affordability["affordable_distinct_products"], ["m"])
            self.assertEqual(affordability["affordable_distinct_product_count"], 1)
            self.assertEqual(
                affordability["contracts"][0]["reason"], "account_state_unavailable"
            )
            self.assertEqual(report["dashboard"]["raw_distinct_product_count"], 3)
            self.assertEqual(
                report["dashboard"]["affordable_distinct_product_count"], 1
            )

    def test_previous_trade_date_affordability_is_stale_and_cannot_claim_capacity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review_path = root / "cn_futures_sim_reviews.jsonl"
            review_path.write_text(
                json.dumps({"date": "20260711", "hold_count": 1}) + "\n",
                encoding="utf-8",
            )
            review_path.with_name("cn_futures_affordability_latest.json").write_text(
                json.dumps(
                    {
                        "date": "20260711",
                        "account_state": {
                            "authoritative": True,
                            "counterfactual_only": False,
                            "source": "master_capital_ledger",
                        },
                        "counterfactual_only": False,
                        "raw_distinct_products": ["rb"],
                        "contracts": [
                            {
                                "product": "rb",
                                "eligible": True,
                                "counterfactual_only": False,
                                "reason": "eligible",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            live_report = {
                "overall_status": "pass",
                "observation_phase": "ready_to_observe",
                "checks": [
                    {
                        "name": "cn_futures_review",
                        "details": {"current_trade_date": "20260712"},
                    }
                ],
                "alerts": [],
            }

            with patch.object(
                observation_report, "run_live_check", return_value=live_report
            ):
                report = observation_report.build_observation_report(
                    review_root=root / "review",
                    review_path=review_path,
                )

            affordability = report["simulation"]["affordability"]
            self.assertEqual(affordability["state"], "stale")
            self.assertEqual(affordability["affordable_distinct_products"], [])
            self.assertEqual(
                report["dashboard"]["affordable_distinct_product_count"], 0
            )
            self.assertEqual(affordability["contracts"][0]["reason"], "eligible")

    def test_night_bar_uses_live_futures_trade_date_not_calendar_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review_path = root / "cn_futures_sim_reviews.jsonl"
            review_path.write_text(
                json.dumps({"date": "20260713", "hold_count": 1}) + "\n",
                encoding="utf-8",
            )
            review_path.with_name("cn_futures_affordability_latest.json").write_text(
                json.dumps(
                    {
                        "date": "20260713",
                        "account_state": {
                            "authoritative": True,
                            "counterfactual_only": False,
                            "source": "master_capital_ledger",
                        },
                        "counterfactual_only": False,
                        "raw_distinct_products": ["rb"],
                        "contracts": [
                            {
                                "product": "rb",
                                "eligible": True,
                                "counterfactual_only": False,
                                "reason": "eligible",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            live_report = {
                "overall_status": "pass",
                "observation_phase": "ready_to_observe",
                "checks": [
                    {
                        "name": "cn_futures_review",
                        "details": {
                            "current_trade_date": "20260713",
                            "latest_bar_time": "2026-07-12 21:05:00",
                        },
                    },
                    {
                        "name": "sharedsignals_5min_freshness",
                        "details": {
                            "report": {"latest_bar_time": "2026-07-12 21:05:00"}
                        },
                    },
                ],
                "alerts": [],
            }

            with patch.object(
                observation_report, "run_live_check", return_value=live_report
            ):
                report = observation_report.build_observation_report(
                    review_root=root / "review", review_path=review_path
                )

            self.assertEqual(report["simulation"]["affordability"]["state"], "current")
            self.assertEqual(
                report["dashboard"]["affordable_distinct_product_count"], 1
            )


if __name__ == "__main__":
    unittest.main()
