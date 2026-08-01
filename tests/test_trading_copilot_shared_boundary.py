from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = (
    ROOT
    / "TradingCopilot"
    / "contracts"
    / "shared_capability_boundary.v1.json"
)


def _load_contract() -> dict[str, object]:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_shared_capability_boundary_has_disjoint_ownership() -> None:
    contract = _load_contract()
    assert contract["contract_id"] == (
        "tradingagent.trading_copilot_shared_capability_boundary.v1"
    )
    assert contract["market"] == "ashare"
    assert contract["principle"] == "shared_evidence_separated_authority"

    groups = [
        contract["shared_read_only_bottom"],
        contract["quant_core_only"],
        contract["copilot_only"],
    ]
    ids = [item["id"] for group in groups for item in group]
    assert len(ids) == len(set(ids))


def test_shared_bottom_is_read_only_evidence_for_copilot() -> None:
    contract = _load_contract()
    shared = contract["shared_read_only_bottom"]
    assert {item["id"] for item in shared} == {
        "market_data_and_reference",
        "market_rules_and_costs",
        "point_in_time_observations_and_features",
        "event_news_sentiment_evidence",
        "forecast_models_and_evaluation",
        "market_regime_and_risk_context",
        "symbol_intelligence_projection",
    }
    assert all(item["copilot_access"] == "read_only_projection" for item in shared)
    assert all(item["authority"] == "evidence_only" for item in shared)


def test_mutable_authorities_and_personal_state_never_cross() -> None:
    contract = _load_contract()
    quant = contract["quant_core_only"]
    copilot = contract["copilot_only"]

    assert all(item["owner"] == "quant_core" for item in quant)
    assert all(item["copilot_access"] != "write" for item in quant)
    assert all(item["owner"] == "trading_copilot" for item in copilot)
    assert all(
        item["write_namespace"] == "runtime/tradingcopilot/state-events.jsonl"
        for item in copilot
    )
    assert all(item["excluded_from_quant_learning"] is True for item in copilot)
    assert set(contract["prohibited_flows"]) == {
        "copilot_state_to_quant_capital_or_execution",
        "copilot_state_to_quant_sample_or_model_promotion",
        "quant_simulated_account_to_user_declared_account",
        "copilot_frontend_to_provider_or_broker",
        "frontend_generated_forecast_to_formal_predictive_evidence",
    }
