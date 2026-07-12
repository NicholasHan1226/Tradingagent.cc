from __future__ import annotations

import importlib

import pytest


STYLE_IDS = {
    "trend_breakout_strength_continuation",
    "pullback_or_short_reversal",
    "event_catalyst_with_price_confirmation",
    "defensive_low_volatility_abstain",
}


def _candidate(**feature_overrides: float) -> dict:
    features = {
        "breakout_strength": 0.84,
        "trend_strength": 0.78,
        "volume_confirmation": 0.76,
        "pullback_quality": 0.34,
        "reversal_confirmation": 0.30,
        "overextension_risk": 0.20,
        "event_catalyst_score": 0.72,
        "price_confirmation": 0.75,
        "realized_volatility": 0.32,
        "downside_resilience": 0.68,
        "liquidity_score": 0.90,
    }
    features.update(feature_overrides)
    return {
        "symbol": "600000.SH",
        "trade_date": "2026-07-13",
        "candidate_id": "candidate-600000-20260713",
        "data_quality": {"qualified": True, "source": "SharedSignals"},
        "features": features,
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
    }


def _module():
    return importlib.import_module("Ashare.style_samples")


# -- v2 schema tests -----------------------------------------------------------


def test_schema_is_v2():
    assert _module().SCHEMA_VERSION == "ashare-style-samples-v2"


def test_predictions_use_raw_style_score_not_probability():
    contract = _module().build_style_sample_contract(
        _candidate(),
        sample_intent="observation",
    )
    for row in contract["style_predictions"]:
        pred = row["prediction"]
        assert "probability" not in pred
        assert isinstance(pred["raw_style_score"], float)
        assert pred["score_semantics"] == "uncalibrated_heuristic"
        assert pred["calibrated_probability"] is None
        assert pred["probability_model_state"] == "not_calibrated"


def test_expected_return_distribution_replaced_by_uncalibrated_return_prior():
    contract = _module().build_style_sample_contract(
        _candidate(),
        sample_intent="observation",
    )
    for row in contract["style_predictions"]:
        assert "expected_return_distribution" not in row
        prior = row["uncalibrated_return_prior"]
        assert prior["model_state"] == "uncalibrated_research_prior"
        assert prior["decision_eligible"] is False
        assert set(prior) >= {
            "unit",
            "p10",
            "p50",
            "p90",
            "model_state",
            "decision_eligible",
        }


def test_abstain_direction_has_zero_return_prior():
    candidate = _candidate()
    candidate["data_quality"] = {"qualified": False, "reason": "unreliable"}
    contract = _module().build_style_sample_contract(candidate)
    for row in contract["style_predictions"]:
        if row["prediction"]["direction"] == "abstain":
            prior = row["uncalibrated_return_prior"]
            assert prior["p10"] == 0.0
            assert prior["p50"] == 0.0
            assert prior["p90"] == 0.0
            assert prior["model_state"] == "uncalibrated_research_prior"
            assert prior["decision_eligible"] is False


# -- v1 migration compatibility tests -----------------------------------------


def test_migrate_v1_prediction_renames_probability_to_legacy():
    v1 = {
        "style_id": "trend_breakout_strength_continuation",
        "style_version": "1.0.0",
        "prediction": {
            "direction": "long_bias",
            "probability": 0.72,
            "score": 0.72,
        },
        "expected_return_distribution": {
            "unit": "decimal_return",
            "p10": 0.0,
            "p50": 0.01,
            "p90": 0.06,
            "model_state": "uncalibrated_research_prior",
        },
    }
    v2 = _module().migrate_v1_prediction_to_v2(v1)

    pred = v2["prediction"]
    assert "probability" not in pred
    assert pred["legacy_uncalibrated_probability"] == 0.72
    assert pred["raw_style_score"] == 0.72
    assert pred["calibration_eligible"] is False
    assert pred["promotion_eligible"] is False
    assert pred["calibrated_probability"] is None
    assert pred["probability_model_state"] == "not_calibrated"

    assert "expected_return_distribution" not in v2
    prior = v2["uncalibrated_return_prior"]
    assert prior["model_state"] == "legacy_v1_uncalibrated"
    assert prior["decision_eligible"] is False


def test_migrate_v1_without_prior_fields_returns_defaults():
    v1 = {
        "style_id": "trend_breakout",
        "prediction": {"direction": "abstain"},
    }
    v2 = _module().migrate_v1_prediction_to_v2(v1)
    pred = v2["prediction"]
    assert pred["calibrated_probability"] is None
    assert pred["calibration_eligible"] is False
    assert pred["promotion_eligible"] is False
    prior = v2["uncalibrated_return_prior"]
    assert prior["model_state"] == "legacy_v1_uncalibrated"
    assert prior["decision_eligible"] is False


# -- cost model tests ---------------------------------------------------------


def test_conservative_cost_model_at_10_yuan_100_lot():
    costs = _module().compute_ashare_conservative_costs(10.0)
    # notional = 1000
    # buy_comm = max(1000 * 0.00025, 5) = 5
    # sell_comm = max(1000 * 0.00025, 5) = 5
    # stamp = 1000 * 0.0005 = 0.5
    # transfer fee = 0.01 each side; total fee = 10.52 CNY; bps = 105.2
    # slippage per side = 1000 * 5/10000 = 0.5; round trip = 1.0 CNY; bps = 10
    assert costs["cost_model_version"] == "ashare-execution-reality-20260706-v1"
    assert costs["cost_basis_notional_cny"] == pytest.approx(1000.0)
    assert costs["round_trip_fee_bps"] == pytest.approx(105.2)
    assert costs["round_trip_slippage_bps"] == pytest.approx(10.0)
    assert costs["buy_commission_cny"] == pytest.approx(5.0)
    assert costs["sell_commission_cny"] == pytest.approx(5.0)
    assert costs["stamp_duty_cny"] == pytest.approx(0.5)
    assert costs["buy_transfer_fee_cny"] == pytest.approx(0.01)
    assert costs["sell_transfer_fee_cny"] == pytest.approx(0.01)
    assert costs["commission_schedule_status"] == "provisional_pending_broker_contract"


def test_conservative_cost_model_at_high_price_uses_rate():
    costs = _module().compute_ashare_conservative_costs(200.0)
    # notional = 20000
    # buy_comm = max(20000*0.00025, 5) = max(5, 5) = 5
    # sell_comm = 5
    # stamp = 20000*0.0005 = 10
    # transfer fee = 0.2 each side; total fee = 20.4 CNY; bps = 10.2
    # slippage per side = 20000*5/10000 = 10; round trip = 20; bps = 10
    assert costs["cost_basis_notional_cny"] == pytest.approx(20000.0)
    assert costs["round_trip_fee_bps"] == pytest.approx(10.2)
    assert costs["round_trip_slippage_bps"] == pytest.approx(10.0)
    assert costs["buy_commission_cny"] == pytest.approx(5.0)
    assert costs["sell_commission_cny"] == pytest.approx(5.0)
    assert costs["stamp_duty_cny"] == pytest.approx(10.0)
    assert costs["buy_transfer_fee_cny"] == pytest.approx(0.2)
    assert costs["sell_transfer_fee_cny"] == pytest.approx(0.2)


def test_conservative_cost_model_at_mid_price_min_commission_still_applies():
    costs = _module().compute_ashare_conservative_costs(50.0)
    # notional = 5000
    # buy_comm = max(5000*0.00025, 5) = max(1.25, 5) = 5
    # sell_comm = 5
    # stamp = 2.5
    # transfer fee = 0.05 each side; total fee = 12.6 CNY; bps = 25.2
    assert costs["round_trip_fee_bps"] == pytest.approx(25.2)
    assert costs["round_trip_slippage_bps"] == pytest.approx(10.0)


def test_cost_model_rejects_missing_or_invalid_price():
    with pytest.raises(ValueError, match="reference_price"):
        _module().compute_ashare_conservative_costs(None)
    with pytest.raises(ValueError, match="reference_price"):
        _module().compute_ashare_conservative_costs(0)
    with pytest.raises(ValueError, match="reference_price"):
        _module().compute_ashare_conservative_costs(-10)


# -- existing tests adapted for v2 --------------------------------------------


def test_shared_candidate_emits_complete_predictions_for_four_orthogonal_styles():
    contract = _module().build_style_sample_contract(
        _candidate(),
        sample_intent="observation",
        mg_enabled=True,
    )

    predictions = contract["style_predictions"]
    assert {row["style_id"] for row in predictions} == STYLE_IDS
    assert len({row["hypothesis_family"] for row in predictions}) == 4
    for row in predictions:
        assert set(row) >= {
            "style_id",
            "style_version",
            "lifecycle_status",
            "hypothesis_family",
            "prediction",
            "entry_thesis",
            "exit_thesis",
            "holding_horizon",
            "uncalibrated_return_prior",
            "risk_budget_request",
            "abstain_reason",
            "reject_reason",
            "marketgraph",
        }
        assert row["risk_budget_request"]["request_only"] is True
        assert row["risk_budget_request"]["allocated_capital_cny"] is None
        assert row["risk_budget_request"]["single_market_portfolio_required"] is True
        assert "shared_portfolio_required" not in row["risk_budget_request"]
        assert row["marketgraph"]["enabled"] is True
        assert row["marketgraph"]["ablation_group"] == "mg_on"


def _all_mapping_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(str(key) for key in value)
        for child in value.values():
            keys.update(_all_mapping_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_all_mapping_keys(child))
    return keys


def test_contract_retains_style_disagreement_without_orders_or_virtual_capital():
    contract = _module().build_style_sample_contract(
        _candidate(
            breakout_strength=0.92,
            trend_strength=0.88,
            volume_confirmation=0.86,
            pullback_quality=0.12,
            reversal_confirmation=0.10,
            event_catalyst_score=0.15,
            price_confirmation=0.80,
            realized_volatility=0.82,
            downside_resilience=0.18,
        ),
        sample_intent="observation",
        mg_enabled=False,
    )

    disagreement = contract["style_disagreement"]
    assert disagreement["has_disagreement"] is True
    assert disagreement["direction_vote_counts"]["long_bias"] >= 1
    assert disagreement["direction_vote_counts"]["abstain"] >= 1
    assert set(disagreement["style_directions"]) == STYLE_IDS

    intent = contract["portfolio_intent"]
    assert isinstance(intent, dict)
    assert intent["action"] == "observe"
    assert intent["primary_style"] == "trend_breakout_strength_continuation"
    assert set(intent["style_scores"]) == STYLE_IDS
    assert set(intent["style_versions"]) == STYLE_IDS
    assert intent["creates_order"] is False
    assert intent["execution_authority"] == "none_research_only"

    assert contract["capital_authority"] == {
        "model": "single_ashare_execution_account",
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
        "execution_account_count": 1,
        "style_ledgers_allowed": False,
        "style_capital_summing_allowed": False,
    }
    assert not {
        "order",
        "orders",
        "order_id",
        "quantity",
        "style_ledger",
        "style_capital_cny",
    }.intersection(_all_mapping_keys(contract))


def test_sample_channels_keep_all_counterfactuals_but_gate_champion_and_challenger():
    candidate = _candidate(
        breakout_strength=0.64,
        trend_strength=0.64,
        volume_confirmation=0.64,
        pullback_quality=0.95,
        reversal_confirmation=0.92,
        overextension_risk=0.10,
        event_catalyst_score=0.20,
        price_confirmation=0.20,
    )
    states = {
        "trend_breakout_strength_continuation": "champion",
        "pullback_or_short_reversal": "challenger",
        "event_catalyst_with_price_confirmation": "paused",
        "defensive_low_volatility_abstain": "baseline",
    }
    module = _module()

    observation = module.build_style_sample_contract(
        candidate,
        sample_intent="observation",
        style_states=states,
    )
    exploration = module.build_style_sample_contract(
        candidate,
        sample_intent="exploration",
        style_states=states,
    )
    exploitation = module.build_style_sample_contract(
        candidate,
        sample_intent="exploitation",
        style_states=states,
    )

    assert observation["sample_channel"] == {
        "name": "observation",
        "performance_bucket": "observation_counterfactual",
        "may_request_simulated_fill": False,
        "fill_authority": "external_single_ashare_account_only",
    }
    assert observation["portfolio_intent"]["action"] == "observe"
    assert (
        exploration["sample_channel"]["performance_bucket"] == "exploration_simulated"
    )
    assert exploration["portfolio_intent"]["action"] == "exploration_candidate"
    assert (
        exploration["portfolio_intent"]["primary_style"] == "pullback_or_short_reversal"
    )
    assert exploration["portfolio_intent"]["eligible_styles"] == [
        "trend_breakout_strength_continuation",
        "pullback_or_short_reversal",
    ]
    assert (
        exploitation["sample_channel"]["performance_bucket"] == "exploitation_simulated"
    )
    assert exploitation["portfolio_intent"]["action"] == "exploitation_candidate"
    assert exploitation["portfolio_intent"]["primary_style"] == (
        "trend_breakout_strength_continuation"
    )
    assert exploitation["portfolio_intent"]["eligible_styles"] == [
        "trend_breakout_strength_continuation"
    ]

    for contract in (observation, exploration, exploitation):
        assert {row["style_id"] for row in contract["style_predictions"]} == STYLE_IDS
        paused = next(
            row
            for row in contract["style_predictions"]
            if row["style_id"] == "event_catalyst_with_price_confirmation"
        )
        assert paused["reject_reason"] == "style_paused"
        assert paused["channel_eligibility"]["observation"] is True
        assert paused["channel_eligibility"]["exploration"] is False
        assert paused["channel_eligibility"]["exploitation"] is False


def test_portfolio_intent_is_deterministically_idempotent_by_symbol_and_trade_date():
    first_candidate = _candidate()
    first_candidate["symbol"] = " 600000.sh "
    first_candidate["trade_date"] = "20260713"
    second_candidate = _candidate(breakout_strength=0.99)
    second_candidate["candidate_id"] = "new-upstream-revision"
    module = _module()

    first = module.build_style_sample_contract(first_candidate)
    second = module.build_style_sample_contract(second_candidate)
    next_day = module.build_style_sample_contract(
        {**_candidate(), "trade_date": "2026-07-14"}
    )

    assert first["symbol"] == "600000.SH"
    assert first["trade_date"] == "2026-07-13"
    assert first["idempotency_key"] == "ashare:2026-07-13:600000.SH"
    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["portfolio_intent"]["idempotency_key"] == first["idempotency_key"]
    assert next_day["idempotency_key"] != first["idempotency_key"]
    assert isinstance(first["portfolio_intent"], dict)
    assert "portfolio_intents" not in first


def test_unreliable_data_keeps_predictions_but_rejects_counterfactual_labels():
    candidate = _candidate()
    candidate["data_quality"] = {
        "qualified": False,
        "reason": "stale_or_unreliable_source",
    }

    contract = _module().build_style_sample_contract(candidate)

    assert len(contract["style_predictions"]) == 4
    for prediction in contract["style_predictions"]:
        assert prediction["prediction"]["direction"] == "abstain"
        assert prediction["reject_reason"] == "stale_or_unreliable_source"
        assert prediction["forward_label_request"] == {
            "request_only": True,
            "eligible": False,
            "horizons": ["m30", "m60", "close", "next_day", "3d", "5d"],
            "rejection_reason": "stale_or_unreliable_source",
        }
    assert contract["portfolio_intent"]["action"] == "observe"
    assert contract["portfolio_intent"]["eligible_styles"] == []
    assert contract["portfolio_intent"]["primary_style"] is None
    assert contract["portfolio_intent"]["supporting_styles"] == []


def test_default_challengers_cannot_become_exploitation_intent():
    contract = _module().build_style_sample_contract(
        _candidate(),
        sample_intent="exploitation",
    )

    assert contract["portfolio_intent"]["action"] == "abstain"
    assert contract["portfolio_intent"]["eligible_styles"] == []
    assert contract["portfolio_intent"]["primary_style"] is None


@pytest.mark.parametrize("sample_intent", ["fill", "live", "", "paper_order"])
def test_invalid_sample_intent_is_rejected(sample_intent: str):
    with pytest.raises(ValueError, match="sample_intent"):
        _module().build_style_sample_contract(
            _candidate(),
            sample_intent=sample_intent,
        )


def test_unknown_or_invalid_style_state_is_rejected():
    module = _module()
    with pytest.raises(ValueError, match="unknown style"):
        module.build_style_sample_contract(
            _candidate(),
            style_states={"made_up_style": "champion"},
        )
    with pytest.raises(ValueError, match="lifecycle status"):
        module.build_style_sample_contract(
            _candidate(),
            style_states={"trend_breakout_strength_continuation": "production_money"},
        )


def test_candidate_snapshot_is_preserved_for_counterfactual_training_without_aliasing():
    candidate = _candidate()
    contract = _module().build_style_sample_contract(candidate, mg_enabled=True)

    assert contract["record_type"] == "ashare_multi_style_counterfactual"
    assert contract["candidate_snapshot"] == {
        "candidate_id": "candidate-600000-20260713",
        "data_quality": {"qualified": True, "source": "SharedSignals"},
        "features": candidate["features"],
        "marketgraph_ablation_group": "mg_on",
    }
    candidate["features"]["breakout_strength"] = 0.01
    candidate["data_quality"]["qualified"] = False
    assert contract["candidate_snapshot"]["features"]["breakout_strength"] == 0.84
    assert contract["candidate_snapshot"]["data_quality"]["qualified"] is True


def test_paused_style_remains_labelled_and_in_disagreement_but_is_not_selected():
    states = {
        "trend_breakout_strength_continuation": "champion",
        "pullback_or_short_reversal": "challenger",
        "event_catalyst_with_price_confirmation": "paused",
        "defensive_low_volatility_abstain": "baseline",
    }
    contract = _module().build_style_sample_contract(
        _candidate(
            breakout_strength=0.70,
            trend_strength=0.70,
            volume_confirmation=0.70,
            event_catalyst_score=1.0,
            price_confirmation=1.0,
        ),
        style_states=states,
    )

    event_prediction = next(
        row
        for row in contract["style_predictions"]
        if row["style_id"] == "event_catalyst_with_price_confirmation"
    )
    assert event_prediction["prediction"]["direction"] == "long_bias"
    assert event_prediction["reject_reason"] == "style_paused"
    assert event_prediction["forward_label_request"]["eligible"] is True
    assert contract["portfolio_intent"]["primary_style"] == (
        "trend_breakout_strength_continuation"
    )
    conflict_pairs = {
        frozenset(pair)
        for pair in contract["style_disagreement"]["conflicting_style_pairs"]
    }
    assert (
        frozenset(
            {
                "event_catalyst_with_price_confirmation",
                "defensive_low_volatility_abstain",
            }
        )
        in conflict_pairs
    )
