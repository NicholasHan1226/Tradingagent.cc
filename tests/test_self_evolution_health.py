from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from CNFutures.sample_maturity import (
    canonical_futures_maturity_projection_sha256,
)
from shared.runtime_test.self_evolution_health import evaluate_self_evolution_health
from shared.review.projection_generation import (
    CURRENT_MANIFEST,
    publish_projection_generation,
)


def _seal_maturity(payload: dict[str, object]) -> dict[str, object]:
    sealed = dict(payload)
    sealed["projection_sha256"] = canonical_futures_maturity_projection_sha256(sealed)
    return sealed


class SelfEvolutionHealthTest(unittest.TestCase):
    def _write_jsonl(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _write_current_ashare_projections(self, root: Path) -> None:
        review = root / "ashare"
        review.mkdir(parents=True, exist_ok=True)
        authority = {
            "capital_authority_id": "ashare-capital-v1",
            "authority_generation": 1,
            "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
        }
        (review / "sample_kpi_latest.json").write_text(
            json.dumps(
                {
                    "report_type": "sample_journal_kpi",
                    "evidence_source": "sample_journal_kpi",
                    "generated_at": "2026-07-13T08:00:00+00:00",
                    "trade_date": "20260713",
                    "authority_scope": authority,
                    "journal_event_count": 8,
                    "sample_layer_totals": {
                        "observation_counterfactual": 4,
                        "exploration_fill": 1,
                        "exploitation_fill": 0,
                        "completed_round_trip": 2,
                    },
                    "styles": {
                        "trend_breakout": {
                            "prediction_count": 4,
                            "completed_round_trip_count": 2,
                            "post_cost_pnl_cny": 12.5,
                        }
                    },
                    "automatic_promotion_enabled": False,
                    "automatic_risk_expansion_enabled": False,
                    "real_trading_enabled": False,
                    "live_execution_enabled": False,
                }
            ),
            encoding="utf-8",
        )
        (review / "evolution_decision_latest.json").write_text(
            json.dumps(
                {
                    "report_type": "ashare_evolution_decision_v2",
                    "evidence_source": "sample_journal_kpi",
                    "generated_at": "2026-07-13T08:00:01+00:00",
                    "state": "evidence_pending",
                    "recommended_action": "observe_and_label_candidates",
                    "authority_scope": authority,
                    "automatic_promotion_enabled": False,
                    "automatic_risk_expansion_enabled": False,
                    "live_transition_authorized": False,
                    "real_trading_enabled": False,
                    "live_execution_enabled": False,
                }
            ),
            encoding="utf-8",
        )
        (review / "market_maturity_latest.json").write_text(
            json.dumps(
                {
                    "report_type": "ashare_market_maturity_v1",
                    "evidence_source": "sample_journal_kpi",
                    "generated_at": "2026-07-13T08:00:02+00:00",
                    "stage": "stage_collecting",
                    "authority_scope": authority,
                    "automatic_promotion_enabled": False,
                    "automatic_risk_expansion_enabled": False,
                    "live_transition_authorized": False,
                    "real_trading_enabled": False,
                    "live_execution_enabled": False,
                }
            ),
            encoding="utf-8",
        )

    def _write_current_cn_futures_maturity(self, root: Path) -> None:
        review = root / "cn_futures"
        review.mkdir(parents=True, exist_ok=True)
        (review / "market_maturity_latest.json").write_text(
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
                        "source_review_sha256": "c" * 64,
                        "sample_counts": {
                            "valid_sample_count": 4,
                            "completed_round_trip_count": 2,
                            "forward_label_count": 8,
                        },
                        "performance": {
                            "completed_round_trip_count": 2,
                            "post_cost_pnl_cny": 12.0,
                        },
                        "sample_kpi_projection": {
                            "styles": {"trend": {"prediction_count": 4}}
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

    def _publish_current_ashare_generation(self, root: Path) -> dict[str, object]:
        self._write_current_ashare_projections(root)
        review = (root / "ashare").resolve()
        input_sha = "a" * 64
        projections: dict[str, dict[str, object]] = {}
        for filename in (
            "sample_kpi_latest.json",
            "evolution_decision_latest.json",
            "market_maturity_latest.json",
        ):
            payload = json.loads((review / filename).read_text(encoding="utf-8"))
            payload["projection_input_sha256"] = input_sha
            payload["H0"] = {"event_count": 1, "sha256": "b" * 64}
            payload["H1"] = {
                "event_count": 2,
                "sha256": "c" * 64,
                "task_owned_delta_event_count": 1,
            }
            projections[filename] = payload
        return publish_projection_generation(
            review_dir=review,
            projections=projections,
            projection_input_sha256=input_sha,
            run_id="self-evolution-health-test",
            generated_at="2026-07-13T08:00:03+00:00",
        )

    def test_flags_strategy_samples_missing_from_current_sample_projection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            report = evaluate_self_evolution_health(
                review_root=root,
                markets=["ashare"],
                pnl_summary={
                    "ashare": {
                        "total_pnl": -149.13,
                        "sample_quality": {"strategy_sample_valid_count": 2},
                    }
                },
            )

            self.assertEqual(report["overall_status"], "warn")
            self.assertIn(
                {"market": "ashare", "issue": "missing_current_sample_kpi"},
                report["issues"],
            )
            self.assertIn(
                {
                    "market": "ashare",
                    "issue": "strategy_samples_not_seen_by_current_projection",
                },
                report["issues"],
            )

    def test_current_sample_projection_satisfies_strategy_sample_visibility(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_current_ashare_projections(root)

            report = evaluate_self_evolution_health(
                review_root=root,
                markets=["ashare"],
                pnl_summary={
                    "ashare": {
                        "total_pnl": 12.5,
                        "sample_quality": {"strategy_sample_valid_count": 2},
                    }
                },
            )

            self.assertEqual(report["overall_status"], "warn")
            self.assertIn(
                "legacy_projection_mode_degraded", report["markets"][0]["issues"]
            )
            self.assertEqual(
                report["markets"][0]["current_projection"]["projection_mode"],
                "legacy_compatibility_degraded",
            )
            self.assertEqual(
                report["markets"][0]["evolution_source"], "sample_journal_kpi"
            )
            self.assertEqual(report["markets"][0]["ranking_trade_sum"], 2)
            self.assertEqual(
                report["markets"][0]["current_projection"]["prediction_count"], 4
            )
            self.assertEqual(
                report["markets"][0]["current_projection"]["maturity_stage"],
                "legacy_degraded",
            )
            self.assertFalse(
                report["markets"][0]["current_projection"]["promotion_evidence_ready"]
            )
            self.assertFalse(report["markets"][0]["positive_evolution_proven"])

    def test_legacy_mature_mirrors_cannot_expose_green_maturity_or_promotion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_current_ashare_projections(root)
            review = root / "ashare"
            kpi_path = review / "sample_kpi_latest.json"
            kpi = json.loads(kpi_path.read_text(encoding="utf-8"))
            kpi["scientific_evidence"] = {"promotion_evidence_ready": True}
            kpi_path.write_text(json.dumps(kpi), encoding="utf-8")
            maturity_path = review / "market_maturity_latest.json"
            maturity = json.loads(maturity_path.read_text(encoding="utf-8"))
            maturity["stage"] = "stage_mature"
            maturity["promotion_evidence_ready"] = True
            maturity_path.write_text(json.dumps(maturity), encoding="utf-8")

            report = evaluate_self_evolution_health(
                review_root=root,
                markets=["ashare"],
                pnl_summary={
                    "ashare": {"sample_quality": {"strategy_sample_valid_count": 2}}
                },
            )

            projection = report["markets"][0]["current_projection"]
            self.assertEqual(report["overall_status"], "warn")
            self.assertEqual(
                projection["projection_mode"], "legacy_compatibility_degraded"
            )
            self.assertEqual(projection["maturity_stage"], "legacy_degraded")
            self.assertFalse(projection["maturity_evidence_trusted"])
            self.assertFalse(projection["promotion_evidence_ready"])

    def test_canonical_generation_is_accepted_but_missing_pointer_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self._publish_current_ashare_generation(root)

            current = evaluate_self_evolution_health(
                review_root=root,
                markets=["ashare"],
                pnl_summary={
                    "ashare": {"sample_quality": {"strategy_sample_valid_count": 2}}
                },
            )
            self.assertEqual(current["overall_status"], "pass")
            self.assertEqual(
                current["markets"][0]["current_projection"]["projection_mode"],
                "canonical_generation",
            )

            (root / "ashare" / CURRENT_MANIFEST).unlink()
            missing = evaluate_self_evolution_health(
                review_root=root,
                markets=["ashare"],
                pnl_summary={
                    "ashare": {"sample_quality": {"strategy_sample_valid_count": 2}}
                },
            )
            market = missing["markets"][0]
            self.assertEqual(missing["overall_status"], "warn")
            self.assertIn("missing_current_projection_manifest", market["issues"])
            self.assertEqual(
                market["current_projection"]["projection_mode"],
                "canonical_generation_missing_current",
            )
            self.assertEqual(market["ranking_trade_sum"], 0)
            self.assertEqual(market["current_projection"]["maturity_stage"], "missing")

    def test_forged_legacy_portfolio_evolution_cannot_replace_current_projection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_current_ashare_projections(root)
            (root / "ashare" / "portfolio_evolution_latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2099-01-01T00:00:00+00:00",
                        "state": "expanded",
                        "actions": [{"action": "expand_risk"}],
                        "rankings": [{"trades": 999, "pnl": 9_999_999}],
                        "strategy_sample_count": 999,
                        "automatic_promotion_enabled": True,
                        "automatic_risk_expansion_enabled": True,
                    }
                ),
                encoding="utf-8",
            )

            report = evaluate_self_evolution_health(
                review_root=root,
                markets=["ashare"],
                pnl_summary={
                    "ashare": {
                        "total_pnl": 12.5,
                        "sample_quality": {"strategy_sample_valid_count": 2},
                    }
                },
            )

            self.assertEqual(report["overall_status"], "warn")
            self.assertIn(
                "legacy_projection_mode_degraded", report["markets"][0]["issues"]
            )
            self.assertEqual(
                report["markets"][0]["evolution_source"], "sample_journal_kpi"
            )
            self.assertEqual(report["markets"][0]["ranking_trade_sum"], 2)
            self.assertNotIn("portfolio_evolution", report["markets"][0])
            self.assertFalse(report["markets"][0]["positive_evolution_proven"])

    def test_invalid_current_filename_payload_cannot_supply_state_or_action(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_current_ashare_projections(root)
            (root / "ashare" / "evolution_decision_latest.json").write_text(
                json.dumps(
                    {
                        "report_type": "portfolio_evolution",
                        "evidence_source": "legacy_history_only",
                        "generated_at": "2099-01-01T00:00:00+00:00",
                        "state": "expanded",
                        "recommended_action": "expand_risk",
                        "authority_scope": {
                            "capital_authority_id": "ashare-capital-v1",
                            "authority_generation": 1,
                            "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
                        },
                        "automatic_risk_expansion_enabled": True,
                        "real_trading_enabled": False,
                    }
                ),
                encoding="utf-8",
            )

            report = evaluate_self_evolution_health(
                review_root=root,
                markets=["ashare"],
                pnl_summary={"ashare": {"sample_quality": {}}},
            )
            market = report["markets"][0]

            self.assertEqual(report["overall_status"], "warn")
            self.assertIn("invalid_current_evolution_decision", market["issues"])
            self.assertIn("unsafe_current_projection_policy", market["issues"])
            self.assertEqual(market["latest_evolution_state"], "evidence_pending")
            self.assertEqual(
                market["current_projection"]["recommended_action"],
                "observe_and_label_candidates",
            )

    def test_legacy_cn_futures_weight_mismatch_is_ignored_without_current_maturity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_jsonl(
                root / "cn_futures" / "evolution_log.jsonl",
                {
                    "generated_at": "2026-07-09T07:46:00+00:00",
                    "state": "observed",
                    "actions": [
                        {
                            "style_name": "trend",
                            "action": "observe",
                            "after": {"status": "active", "weight": 0.05},
                        }
                    ],
                    "rankings": [{"style_name": "trend", "trades": 0, "pnl": 0}],
                    "weights": {"trend": {"status": "active", "weight": 1.0}},
                },
            )

            report = evaluate_self_evolution_health(
                review_root=root,
                markets=["cn_futures"],
                pnl_summary={"cn_futures": {"total_pnl": 0.0}},
            )

            self.assertEqual(report["overall_status"], "warn")
            self.assertEqual(
                report["markets"][0]["issues"],
                ["missing_current_market_maturity"],
            )
            self.assertEqual(report["markets"][0]["weight_mismatches"], [])
            self.assertEqual(report["markets"][0]["style_weight_count"], 0)

    def test_cn_futures_uses_only_exact_maturity_and_ignores_legacy_evolution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_current_cn_futures_maturity(root)
            self._write_jsonl(
                root / "cn_futures" / "evolution_log.jsonl",
                {
                    "generated_at": "2099-01-01T00:00:00+00:00",
                    "state": "expanded",
                    "actions": [{"action": "expand_risk"}],
                    "rankings": [{"trades": 999, "pnl": 9_999_999}],
                    "weights": {"rogue": {"weight": 1.0}},
                },
            )
            (root / "cn_futures" / "style_weights.json").write_text(
                json.dumps({"styles": {"rogue": {"weight": 1.0}}}),
                encoding="utf-8",
            )

            report = evaluate_self_evolution_health(
                review_root=root,
                markets=["cn_futures"],
                pnl_summary={
                    "cn_futures": {
                        "total_pnl": 12.0,
                        "sample_quality": {"strategy_sample_valid_count": 2},
                    }
                },
            )

            market = report["markets"][0]
            self.assertEqual(report["overall_status"], "pass")
            self.assertEqual(
                market["evolution_source"],
                "cn_futures_review_journal+sample_kpi",
            )
            self.assertEqual(market["latest_evolution_state"], "stage_initial_samples")
            self.assertEqual(market["ranking_trade_sum"], 2)
            self.assertEqual(market["ranking_pnl_sum"], 12.0)
            self.assertEqual(market["style_weight_count"], 0)
            self.assertEqual(market["action_count"], 0)
            self.assertEqual(market["generated_variant_count"], 0)
            self.assertEqual(market["weight_mismatches"], [])
            self.assertFalse(market["positive_evolution_proven"])

            maturity_path = root / "cn_futures" / "market_maturity_latest.json"
            tampered = json.loads(maturity_path.read_text(encoding="utf-8"))
            tampered["performance"]["post_cost_pnl_cny"] = 9_999_999
            maturity_path.write_text(json.dumps(tampered), encoding="utf-8")
            rejected = evaluate_self_evolution_health(
                review_root=root,
                markets=["cn_futures"],
                pnl_summary={"cn_futures": {"sample_quality": {}}},
            )
            rejected_market = rejected["markets"][0]
            self.assertEqual(rejected["overall_status"], "warn")
            self.assertIn(
                "invalid_current_market_maturity:projection_sha256_invalid",
                rejected_market["issues"],
            )
            self.assertEqual(rejected_market["ranking_pnl_sum"], 0.0)
            self.assertFalse(rejected_market["positive_evolution_proven"])

    def test_passes_when_samples_and_evolution_are_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_jsonl(
                root / "us" / "evolution_log.jsonl",
                {
                    "generated_at": "2026-07-09T01:00:00+00:00",
                    "state": "adjusted",
                    "actions": [
                        {
                            "style_name": "trend",
                            "action": "promote",
                            "after": {"weight": 1.0},
                        }
                    ],
                    "rankings": [{"style_name": "trend", "trades": 3, "pnl": 1.5}],
                    "weights": {"trend": {"status": "active", "weight": 1.0}},
                },
            )

            report = evaluate_self_evolution_health(
                review_root=root,
                markets=["us"],
                pnl_summary={"us": {"total_pnl": 1.5}},
            )

            self.assertEqual(report["overall_status"], "pass")
            self.assertTrue(report["markets"][0]["positive_evolution_proven"])


if __name__ == "__main__":
    unittest.main()
