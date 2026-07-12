from __future__ import annotations

from Ashare.sample_pipeline import build_hypothesis_id, build_research_hypothesis


def test_active_hypothesis_id_is_stable_and_human_readable() -> None:
    hypothesis_id = build_hypothesis_id(
        trade_date="20260710",
        symbol="600584.SH",
        side="buy",
        execution_source="ashare_candidate_layer",
        candidate_pool_layer="candidate",
        score=0.6118,
    )

    assert hypothesis_id == "ashare-20260710-buy-600584.SH-candidate-s061"


def test_active_orchestrator_hypothesis_builder_remains_available() -> None:
    hypothesis = build_research_hypothesis(
        trade_date="20260713",
        symbol="000001.SZ",
        side="buy",
        execution_source="ashare_candidate_layer",
        candidate_pool_layer="candidate",
        score_snapshot={"combined": 0.62, "technical": 0.7},
        sample_intent="exploration",
        capital_plan={"risk_mode": "normal"},
    )

    assert hypothesis["hypothesis_id"] == "ashare-20260713-buy-000001.SZ-candidate-s062"
    assert hypothesis["sample_intent"] == "exploration"
    assert hypothesis["factor_snapshot"] == {"combined": 0.62, "technical": 0.7}
    assert hypothesis["capital_plan_risk_mode"] == "normal"
    assert hypothesis["expected_validation_horizon"] == [
        "m30",
        "m60",
        "close",
        "1d",
        "3d",
        "5d",
    ]
