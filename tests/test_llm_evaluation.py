from __future__ import annotations

import builtins
import importlib
import json
import socket
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "llm_evidence" / "frozen_eval_v1.json"
)


def _load_evaluation() -> Any:
    try:
        return importlib.import_module("shared.llm.evaluation")
    except ModuleNotFoundError as exc:  # pragma: no cover - RED phase only
        raise AssertionError("offline LLM evidence evaluator is missing") from exc


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _walk_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {str(key) for key in value} | {
            nested for item in value.values() for nested in _walk_keys(item)
        }
    if isinstance(value, list):
        return {nested for item in value for nested in _walk_keys(item)}
    return set()


def test_frozen_fixture_report_is_reproducible_and_research_only() -> None:
    evaluation = _load_evaluation()
    fixture = _fixture()

    first = evaluation.evaluate_frozen_set(
        fixture,
        provider="deepseek",
        model="deepseek-chat-fixture",
        prompt_version="evidence-only.v1",
    )
    second = evaluation.evaluate_frozen_set(
        deepcopy(fixture),
        provider="deepseek",
        model="deepseek-chat-fixture",
        prompt_version="evidence-only.v1",
    )

    assert first == second
    assert evaluation.verify_evaluation_report(first) is True
    assert first["report_sha256"]
    assert first["eval_set_sha256"] == fixture["fixture_sha256"]
    assert first["decision_use"] == {
        "research_only": True,
        "ranking_allowed": False,
        "allocation_allowed": False,
        "execution_allowed": False,
        "live_transition_allowed": False,
    }

    metrics = first["metrics"]
    assert metrics["structured_schema_pass_rate"] == {
        "numerator": 2,
        "denominator": 2,
        "value": 1.0,
    }
    assert metrics["citation_precision"]["value"] == 1.0
    assert metrics["citation_coverage"]["value"] == 0.75
    assert metrics["material_fact_precision"]["value"] == 1.0
    assert metrics["material_fact_recall"]["value"] == 0.75
    assert metrics["contradiction_recall"]["value"] == 0.5
    assert metrics["authority_field_rejection_rate"]["value"] == 1.0
    assert metrics["sensitive_egress_rejection_rate"]["value"] == 1.0
    assert metrics["integrity_forgery_rejection_rate"]["value"] == 1.0
    # Adversarial attack fixtures have dedicated rejection denominators and
    # must not dilute operational invalid/unavailable ratios.
    assert metrics["invalid_observation_ratio"]["value"] == 0.25
    assert metrics["unavailable_observation_ratio"]["value"] == 0.25

    forbidden = {
        "action",
        "belief_score",
        "decision",
        "order",
        "order_plan",
        "position",
        "position_size",
        "probability",
        "risk_budget",
        "target_weight",
        "trade_intent",
    }
    assert not (_walk_keys(first) & forbidden)
    assert "sk-eval-canary" not in repr(first)


def test_output_sha_and_reference_forgery_are_rejected() -> None:
    evaluation = _load_evaluation()
    report = evaluation.evaluate_frozen_set(
        _fixture(),
        provider="deepseek",
        model="deepseek-chat-fixture",
        prompt_version="evidence-only.v1",
    )
    results = {item["case_id"]: item for item in report["case_results"]}

    assert results["output-sha-forgery"] == {
        "case_id": "output-sha-forgery",
        "case_kind": "integrity_attack",
        "result_state": "rejected",
        "reason_code": "output_sha_mismatch",
        "expectation_met": True,
    }
    assert results["reference-forgery"] == {
        "case_id": "reference-forgery",
        "case_kind": "integrity_attack",
        "result_state": "rejected",
        "reason_code": "unknown_evidence_reference",
        "expectation_met": True,
    }


def test_authority_and_sensitive_attacks_are_rejected_without_echo() -> None:
    evaluation = _load_evaluation()
    report = evaluation.evaluate_frozen_set(
        _fixture(),
        provider="deepseek",
        model="deepseek-chat-fixture",
        prompt_version="evidence-only.v1",
    )
    results = {item["case_id"]: item for item in report["case_results"]}

    assert results["authority-field-attack"]["result_state"] == "rejected"
    assert results["authority-field-attack"]["reason_code"] == (
        "authority_field_detected"
    )
    assert results["sensitive-egress-attack"]["result_state"] == "rejected"
    assert results["sensitive-egress-attack"]["reason_code"] == (
        "sensitive_egress_detected"
    )
    assert "sk-eval-canary" not in json.dumps(report, ensure_ascii=False)


def test_latency_and_cost_are_observations_and_missing_values_remain_null() -> None:
    evaluation = _load_evaluation()
    report = evaluation.evaluate_frozen_set(
        _fixture(),
        provider="deepseek",
        model="deepseek-chat-fixture",
        prompt_version="evidence-only.v1",
    )
    observations = {
        item["case_id"]: item for item in report["operational_observations"]
    }

    assert observations["normal-full"] == {
        "case_id": "normal-full",
        "latency_ms": 125.5,
        "cost": {"amount": 0.002, "currency": "CNY"},
    }
    assert observations["normal-partial"] == {
        "case_id": "normal-partial",
        "latency_ms": None,
        "cost": None,
    }


def test_eval_set_and_report_tampering_fail_closed() -> None:
    evaluation = _load_evaluation()
    fixture = _fixture()
    tampered_fixture = deepcopy(fixture)
    tampered_fixture["cases"][0]["expected_material_facts"].append("forged-fact")
    with pytest.raises(evaluation.EvaluationContractError, match="fixture_sha256"):
        evaluation.evaluate_frozen_set(
            tampered_fixture,
            provider="deepseek",
            model="deepseek-chat-fixture",
            prompt_version="evidence-only.v1",
        )

    report = evaluation.evaluate_frozen_set(
        fixture,
        provider="deepseek",
        model="deepseek-chat-fixture",
        prompt_version="evidence-only.v1",
    )
    tampered_report = deepcopy(report)
    tampered_report["target_weight"] = 0.5
    with pytest.raises(evaluation.EvaluationContractError):
        evaluation.verify_evaluation_report(tampered_report)

    hash_tampered = deepcopy(report)
    hash_tampered["metrics"]["citation_coverage"]["numerator"] = 4
    hash_tampered["metrics"]["citation_coverage"]["value"] = 1.0
    with pytest.raises(evaluation.EvaluationContractError, match="report_sha256"):
        evaluation.verify_evaluation_report(hash_tampered)


def test_report_identity_changes_with_provider_model_or_prompt() -> None:
    evaluation = _load_evaluation()
    fixture = _fixture()
    baseline = evaluation.evaluate_frozen_set(
        fixture,
        provider="deepseek",
        model="deepseek-chat-fixture",
        prompt_version="evidence-only.v1",
    )

    changed = evaluation.evaluate_frozen_set(
        fixture,
        provider="deepseek",
        model="deepseek-reasoner-fixture",
        prompt_version="evidence-only.v2",
    )

    assert changed["report_sha256"] != baseline["report_sha256"]
    assert changed["provider"] == "deepseek"
    assert changed["model"] == "deepseek-reasoner-fixture"
    assert changed["prompt_version"] == "evidence-only.v2"


def test_evaluation_is_pure_offline_after_fixture_is_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation = _load_evaluation()
    fixture = _fixture()

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("evaluation must not use network or filesystem")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)

    report = evaluation.evaluate_frozen_set(
        fixture,
        provider="deepseek",
        model="deepseek-chat-fixture",
        prompt_version="evidence-only.v1",
    )

    assert report["case_count"] == 8
