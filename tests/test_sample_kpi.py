from __future__ import annotations

from copy import deepcopy

import pytest

from shared.review.sample_kpi import (
    SAMPLE_LAYERS,
    build_opportunity_capture_evidence,
    build_sample_kpi,
    classify_sample_layers,
)


def test_explicit_sample_layers_cover_every_required_bucket_without_collapsing_them():
    assert SAMPLE_LAYERS == (
        "observation_counterfactual",
        "exploration_fill",
        "exploitation_fill",
        "completed_round_trip",
        "exit_stop",
        "risk_reject",
        "chain_validation",
        "shadow_research",
    )

    assert classify_sample_layers({"record_type": "prediction"}) == (
        "observation_counterfactual",
    )
    assert classify_sample_layers(
        {"record_type": "fill", "sample_intent": "exploration"}
    ) == ("exploration_fill",)
    assert classify_sample_layers(
        {"record_type": "fill", "sample_intent": "exploitation"}
    ) == ("exploitation_fill",)
    assert classify_sample_layers(
        {"record_type": "round_trip", "exit_reason": "stop_loss"}
    ) == (
        "completed_round_trip",
        "exit_stop",
    )
    assert classify_sample_layers({"record_type": "risk_reject"}) == ("risk_reject",)
    assert classify_sample_layers({"sample_classification": "chain_validation"}) == (
        "chain_validation",
    )
    assert classify_sample_layers({"record_type": "shadow_research"}) == (
        "shadow_research",
    )
    assert classify_sample_layers(
        {"record_type": "shadow_research", "sample_layers": ["shadow_research"]}
    ) == ("shadow_research",)


def test_fixture_records_are_excluded_from_all_kpi_and_promotion_evidence():
    fixture_records = [
        {
            "record_type": "prediction",
            "style": "fixture_champion",
            "source_class": "fixture",
            "promotion_eligible": False,
            "decision_cluster_id": "fixture-cluster-1",
            "trade_date": "20260716",
            "primary_label_horizon": "1d",
            "probability_model_state": "frozen_out_of_sample_calibrated",
            "calibration_role": "primary",
            "calibrated_probability": 0.99,
            "labels": {
                "1d": {"status": "ready", "net_return_after_costs": 0.10},
            },
        },
        {
            "record_type": "fill",
            "primary_style": "fixture_champion",
            "source_class": "fixture",
            "promotion_eligible": False,
            "sample_intent": "exploitation",
            "status": "filled",
        },
        {
            "record_type": "completed_round_trip",
            "primary_style": "fixture_champion",
            "source_class": "fixture",
            "promotion_eligible": False,
            "sample_intent": "exploitation",
            "gross_pnl_cny": 1_000.0,
            "fee_cny": 1.0,
            "slippage_cny": 1.0,
            "net_pnl_cny": 998.0,
        },
    ]

    result = build_sample_kpi(fixture_records)

    assert result["styles"] == {}
    assert result["sample_layer_totals"] == {layer: 0 for layer in SAMPLE_LAYERS}
    assert result["excluded_source_class_counts"] == {"fixture": 3}
    assert result["sample_size_evidence"]["raw_N"] == 0
    assert result["sample_size_evidence"]["N_eff"] == 0.0
    assert result["calibration_evidence"]["sufficient"] is False
    assert result["calibration_evidence"]["independent_sample_count"] == 0


def _records():
    return [
        {
            "record_type": "candidate",
            "style": "trend_breakout",
            "timestamp": "2026-07-13T01:29:00+00:00",
        },
        {
            "record_type": "prediction",
            "style": "trend_breakout",
            "sample_layer": "observation_counterfactual",
            "timestamp": "2026-07-13T01:30:00+00:00",
            "labels": {
                "m30": {"status": "ready", "net_return_after_costs": 0.01},
                "m60": {"status": "pending_not_due"},
                "close": {"status": "missing_exit_evidence"},
                "1d": {"status": "ready", "net_return_after_costs": -0.002},
            },
        },
        {
            "record_type": "fill",
            "primary_style": "trend_breakout",
            "supporting_styles": ["event_catalyst"],
            "sample_intent": "exploration",
            "timestamp": "2026-07-13T02:00:00+00:00",
        },
        {
            "record_type": "round_trip",
            "primary_style": "trend_breakout",
            "sample_intent": "exploration",
            "timestamp": "2026-07-13T06:30:00+00:00",
            "gross_pnl_cny": 120.0,
            "fee_cny": 8.0,
            "slippage_cny": 2.0,
            "net_pnl_cny": 110.0,
        },
        {
            "record_type": "round_trip",
            "primary_style": "trend_breakout",
            "sample_intent": "exploitation",
            "timestamp": "2026-07-14T06:30:00+00:00",
            "gross_pnl_cny": -45.0,
            "fee_cny": 4.0,
            "slippage_cny": 1.0,
            "net_pnl_cny": -50.0,
            "exit_reason": "stop_loss",
        },
        {
            "record_type": "risk_reject",
            "style": "trend_breakout",
            "timestamp": "2026-07-14T02:00:00+00:00",
            "reject_reason": "single_name_exposure",
        },
        {
            "record_type": "risk_reject",
            "style": "trend_breakout",
            "timestamp": "2026-07-14T02:01:00+00:00",
            "reject_reason": "single_name_exposure",
        },
        {
            "record_type": "chain_validation",
            "style": "event_catalyst",
            "timestamp": "2026-07-14T02:02:00+00:00",
            "evidence_status": "missing_fill_evidence",
        },
    ]


def test_style_kpis_keep_predictions_fills_round_trips_labels_and_rejections_separate():
    result = build_sample_kpi(_records())
    trend = result["styles"]["trend_breakout"]

    assert trend["candidate_count"] == 1
    assert trend["prediction_count"] == 1
    assert trend["observation_counterfactual_count"] == 1
    assert trend["exploration_fill_count"] == 1
    assert trend["exploitation_fill_count"] == 0
    assert trend["completed_round_trip_count"] == 2
    assert trend["exit_stop_count"] == 1
    assert trend["risk_reject_count"] == 2
    assert trend["chain_validation_count"] == 0
    assert trend["forward_label_counts"]["m30"] == {"ready": 1}
    assert trend["forward_label_counts"]["m60"] == {"pending_not_due": 1}
    assert trend["forward_label_counts"]["close"] == {"missing_exit_evidence": 1}
    assert trend["forward_label_counts"]["1d"] == {"ready": 1}
    assert trend["forward_label_counts"]["3d"] == {}
    assert trend["forward_label_counts"]["5d"] == {}
    assert trend["rejection_reason_distribution"] == {"single_name_exposure": 2}


def test_completed_round_trip_metrics_use_post_cost_pnl_and_report_drawdown():
    trend = build_sample_kpi(_records())["styles"]["trend_breakout"]

    assert trend["performance_scope"] == "separated_by_sample_intent"
    assert trend["win_rate"] is None
    assert trend["expectancy_cny"] is None
    exploration = trend["performance_by_sample_intent"]["exploration"]
    exploitation = trend["performance_by_sample_intent"]["exploitation"]
    assert exploration["completed_round_trip_count"] == 1
    assert exploration["win_rate"] == pytest.approx(1.0)
    assert exploration["expectancy_cny"] == pytest.approx(110.0)
    assert exploration["post_cost_pnl_cny"] == pytest.approx(110.0)
    assert exploration["trade_pnl_sequence_max_drawdown_cny"] == pytest.approx(0.0)
    assert exploitation["completed_round_trip_count"] == 1
    assert exploitation["win_rate"] == pytest.approx(0.0)
    assert exploitation["expectancy_cny"] == pytest.approx(-50.0)
    assert exploitation["post_cost_pnl_cny"] == pytest.approx(-50.0)
    assert exploitation["trade_pnl_sequence_max_drawdown_cny"] == pytest.approx(50.0)


def test_one_execution_is_attributed_only_to_primary_style_not_supporting_styles():
    result = build_sample_kpi(_records())

    assert result["styles"]["trend_breakout"]["exploration_fill_count"] == 1
    assert result["styles"]["event_catalyst"]["exploration_fill_count"] == 0
    assert result["styles"]["event_catalyst"]["chain_validation_count"] == 1


def test_portfolio_risk_comes_only_from_authoritative_account_snapshot():
    records = _records() + [
        {
            "record_type": "prediction",
            "style": "trend_breakout",
            "shadow_capital_cny": 50_000,
        },
        {
            "record_type": "prediction",
            "style": "event_catalyst",
            "shadow_capital_cny": 50_000,
        },
    ]
    result = build_sample_kpi(
        records,
        portfolio_snapshot={
            "source": "master_capital_ledger",
            "account_equity_cny": 50_000,
            "total_risk_cny": 1_250,
            "gross_exposure_cny": 12_000,
            "as_of": "2026-07-14T07:00:00+00:00",
        },
    )

    assert result["portfolio"] == {
        "status": "available",
        "source": "master_capital_ledger",
        "as_of": "2026-07-14T07:00:00+00:00",
        "account_equity_cny": 50_000.0,
        "total_risk_cny": 1_250.0,
        "gross_exposure_cny": 12_000.0,
        "shadow_capital_included": False,
        "real_trading_enabled": False,
    }


def test_missing_authoritative_portfolio_evidence_is_explicit_and_never_sums_shadow_ledgers():
    result = build_sample_kpi(
        [
            {
                "record_type": "prediction",
                "style": "trend_breakout",
                "shadow_capital_cny": 50_000,
            },
            {
                "record_type": "prediction",
                "style": "event_catalyst",
                "shadow_capital_cny": 50_000,
            },
        ]
    )

    assert result["portfolio"]["status"] == "missing_authoritative_portfolio_snapshot"
    assert result["portfolio"]["account_equity_cny"] is None
    assert result["portfolio"]["total_risk_cny"] is None
    assert result["portfolio"]["shadow_capital_included"] is False
    assert result["real_trading_enabled"] is False


def test_kpi_input_is_immutable_and_missing_evidence_is_counted_explicitly():
    rows = _records()
    before = deepcopy(rows)

    result = build_sample_kpi(rows)

    assert rows == before
    assert result["evidence_status_counts"] == {"missing_fill_evidence": 1}
    assert result["missing_evidence_count"] == 1
    assert result["real_trading_enabled"] is False


def test_completed_round_trip_without_explicit_gross_and_net_is_not_performance_evidence():
    result = build_sample_kpi(
        [
            {
                "record_type": "round_trip",
                "style": "pullback_reversal",
                "timestamp": "2026-07-14T06:30:00+00:00",
                "gross_pnl_cny": 100,
                "fee_cny": 7,
                "slippage_cny": 3,
            }
        ]
    )

    style = result["styles"]["pullback_reversal"]
    assert style["completed_round_trip_count"] == 0
    assert style["post_cost_pnl_cny"] == 0.0
    assert style["expectancy_cny"] is None
    assert result["invalid_completed_round_trip_count"] == 1


def test_sample_size_uses_prespecified_primary_horizon_and_one_decision_cluster():
    rows = []
    for style in ("trend", "pullback", "event", "defensive"):
        rows.append(
            {
                "record_type": "prediction",
                "style": style,
                "symbol": "600000.SH",
                "trade_date": "20260713",
                "decision_cluster_id": "decision:one",
                "primary_label_horizon": "1d",
                "maturity_weight": 1.0,
                "labels": {
                    horizon: {
                        "status": "ready",
                        "net_return_after_costs": 0.01,
                    }
                    for horizon in ("m30", "m60", "close", "1d", "3d", "5d")
                },
            }
        )

    evidence = build_sample_kpi(rows)["sample_size_evidence"]

    assert evidence["ready_label_cell_count"] == 24
    assert evidence["raw_N"] == 4
    assert evidence["unique_decision_cluster_count"] == 1
    assert evidence["independent_trading_day_count"] == 1
    assert evidence["N_eff"] == pytest.approx(1.0)
    assert evidence["primary_horizon_policy_version"] == "ashare-primary-horizon-v1"
    assert build_sample_kpi(rows)["sample_science_contract_version"] == (
        "ashare-sample-science-v1"
    )


def test_calibration_is_computed_from_independent_primary_cluster_outcomes():
    rows = []
    for index, (probability, outcome) in enumerate(
        ((0.8, 1), (0.7, 1), (0.3, 0), (0.2, 0))
    ):
        rows.append(
            {
                "record_type": "prediction",
                "style": "trend",
                "symbol": "%06d.SH" % (600000 + index),
                "trade_date": "202607%d" % (13 + index),
                "decision_cluster_id": "decision:%d" % index,
                "primary_label_horizon": "1d",
                "calibration_role": "primary",
                "calibrated_probability": probability,
                "probability_model_state": "out_of_sample_calibrated",
                "labels": {
                    "1d": {
                        "status": "ready",
                        "net_return_after_costs": 0.01 if outcome else -0.01,
                    }
                },
            }
        )

    calibration = build_sample_kpi(rows)["calibration_evidence"]

    assert calibration["status"] == "insufficient_independent_samples"
    assert calibration["independent_sample_count"] == 4
    assert calibration["brier_score"] == pytest.approx(0.065)
    assert calibration["log_loss"] == pytest.approx(0.289909, abs=1e-6)
    assert calibration["base_rate"] == pytest.approx(0.5)
    assert calibration["base_rate_brier_score"] == pytest.approx(0.25)
    assert calibration["brier_skill_score"] == pytest.approx(0.74)
    assert calibration["reliability_ece"] == pytest.approx(0.25)
    assert calibration["sufficient"] is False


def test_uncalibrated_rank_score_cannot_enter_calibration_metrics():
    result = build_sample_kpi(
        [
            {
                "record_type": "prediction",
                "style": "trend",
                "symbol": "600000.SH",
                "trade_date": "20260713",
                "decision_cluster_id": "decision:1",
                "primary_label_horizon": "1d",
                "rank_score": 0.9,
                "score_semantics": "uncalibrated_rank_score",
                "calibrated_probability": None,
                "probability_model_state": "not_calibrated",
                "labels": {"1d": {"status": "ready", "net_return_after_costs": 0.01}},
            }
        ]
    )

    assert (
        result["calibration_evidence"]["status"]
        == "unavailable_no_calibrated_predictions"
    )
    assert result["calibration_evidence"]["independent_sample_count"] == 0


def test_account_drawdown_uses_daily_mtm_equity_and_trade_drawdown_is_auxiliary():
    result = build_sample_kpi(
        [
            {
                "record_type": "completed_round_trip",
                "style": "trend",
                "sample_intent": "exploitation",
                "gross_pnl_cny": -90.0,
                "fee_cny": 5.0,
                "slippage_cny": 5.0,
                "net_pnl_cny": -100.0,
                "timestamp": "2026-07-13T15:00:00+08:00",
            },
            {
                "record_type": "chain_validation",
                "sample_layer": "chain_validation",
                "evidence_type": "account_daily_mtm_equity",
                "trade_date": "20260713",
                "account_equity_cny": 50_500.0,
                "equity_source": "ashare_market_capital_reconcile",
            },
            {
                "record_type": "chain_validation",
                "sample_layer": "chain_validation",
                "evidence_type": "account_daily_mtm_equity",
                "trade_date": "20260714",
                "account_equity_cny": 49_000.0,
                "equity_source": "ashare_market_capital_reconcile",
            },
        ]
    )

    drawdown = result["account_drawdown_evidence"]
    assert drawdown["status"] == "available"
    assert drawdown["source"] == "account_daily_mtm_equity"
    assert drawdown["equity_source"] == "ashare_market_capital_reconcile"
    assert drawdown["observation_count"] == 2
    assert drawdown["independent_trading_day_count"] == 2
    assert drawdown["max_drawdown_cny"] == pytest.approx(1500.0)
    assert drawdown["max_drawdown_ratio"] == pytest.approx(1500.0 / 50_500.0)
    assert drawdown["peak_equity_cny"] == pytest.approx(50_500.0)
    assert drawdown["trough_equity_cny"] == pytest.approx(49_000.0)
    performance = result["styles"]["trend"]["performance_by_sample_intent"][
        "exploitation"
    ]
    assert performance["trade_pnl_sequence_max_drawdown_cny"] == pytest.approx(100.0)
    assert "max_drawdown_cny" not in performance


def test_opportunity_capture_never_calls_scanned_recall_full_market_recall():
    evidence = build_opportunity_capture_evidence(
        full_eligible_symbols=None,
        scanned_symbols=["A", "B", "C"],
        top_k_symbols=["A"],
        realized_opportunity_symbols=["A", "C"],
        full_eligible_universe_complete=False,
    )

    assert evidence["full_eligible_universe_recall"] is None
    assert (
        evidence["full_eligible_universe_status"] == "unavailable_incomplete_universe"
    )
    assert evidence["scanned_universe_recall"] == pytest.approx(0.5)
    assert evidence["top_k_precision"] == pytest.approx(1.0)
    assert evidence["claim_scope"] == "scanned_universe_only"


def test_full_universe_recall_is_only_available_with_complete_eligible_set():
    evidence = build_opportunity_capture_evidence(
        full_eligible_symbols=["A", "B", "C", "D"],
        scanned_symbols=["A", "B", "C"],
        top_k_symbols=["A"],
        realized_opportunity_symbols=["A", "C", "D"],
        full_eligible_universe_complete=True,
    )

    assert evidence["claim_scope"] == "full_eligible_universe"
    assert evidence["full_eligible_universe_status"] == "available"
    assert evidence["full_eligible_universe_recall"] == pytest.approx(1 / 3)
    assert evidence["scanned_universe_recall"] == pytest.approx(1 / 2)
