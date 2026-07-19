"""Deterministic, offline evaluation for frozen LLM evidence fixtures.

The evaluator consumes an already-supplied mapping.  It owns no transport,
filesystem reader, clock, trading score, portfolio rule, or execution path.
Evaluation reports are research artifacts only and are rejected if wrapped in
decision or execution authority fields.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from copy import deepcopy
from typing import Any, Iterable, Mapping

from .schema import (
    AUTHORITY_DENIED,
    OBSERVATION_SCHEMA_VERSION,
    EvidenceSchemaError,
    validate_provider_evidence,
)

EVAL_SET_SCHEMA_VERSION = "llm-evidence-eval-set.v1"
REPORT_SCHEMA_VERSION = "llm-evidence-eval-report.v1"

_FIXTURE_KEYS = {
    "schema_version",
    "eval_set_version",
    "fixture_sha256",
    "cases",
}
_CASE_REQUIRED_KEYS = {
    "case_id",
    "case_kind",
    "allowed_refs",
    "expected_citations",
    "expected_material_facts",
    "expected_contradictions",
    "sensitive_canaries",
    "observation",
}
_CASE_OPTIONAL_KEYS = {"expected_status", "latency_ms", "cost"}
_CASE_KINDS = {
    "normal",
    "status",
    "authority_attack",
    "sensitive_egress_attack",
    "integrity_attack",
}
_OBSERVATION_KEYS = {
    "record_type",
    "schema_version",
    "status",
    "request_id",
    "task_type",
    "entity_id",
    "route",
    "provider",
    "model",
    "prompt_version",
    "prompt_sha256",
    "document_cutoff",
    "evidence_refs",
    "evidence",
    "output_sha256",
    "reason_code",
    "authority",
}
_AUTHORITY_FIELDS = {
    "action",
    "allocation",
    "belief_score",
    "buy",
    "conviction",
    "decision",
    "order",
    "order_plan",
    "position",
    "position_size",
    "probability",
    "risk_budget",
    "sell",
    "target_weight",
    "trade_intent",
    "weight",
}
_METRIC_NAMES = {
    "structured_schema_pass_rate",
    "citation_precision",
    "citation_coverage",
    "material_fact_precision",
    "material_fact_recall",
    "contradiction_recall",
    "authority_field_rejection_rate",
    "sensitive_egress_rejection_rate",
    "integrity_forgery_rejection_rate",
    "invalid_observation_ratio",
    "unavailable_observation_ratio",
}
_DECISION_USE = {
    "research_only": True,
    "ranking_allowed": False,
    "allocation_allowed": False,
    "execution_allowed": False,
    "live_transition_allowed": False,
}
_REPORT_KEYS = {
    "record_type",
    "schema_version",
    "eval_set_version",
    "eval_set_sha256",
    "provider",
    "model",
    "prompt_version",
    "case_count",
    "metrics",
    "case_results",
    "operational_observations",
    "decision_use",
    "report_sha256",
}


class EvaluationContractError(ValueError):
    """Raised when a fixture or report fails the offline evaluation contract."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EvaluationContractError("value is not canonical JSON") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_native_text(value: Any, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise EvaluationContractError(f"{field} must be a native non-empty string")
    return value.strip()


def _require_sha(value: Any, field: str) -> str:
    text = _require_native_text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise EvaluationContractError(f"{field} must be lowercase sha256")
    return text


def _require_text_list(value: Any, field: str) -> list[str]:
    if type(value) is not list:
        raise EvaluationContractError(f"{field} must be a list")
    result = [_require_native_text(item, f"{field}[]") for item in value]
    if len(set(result)) != len(result):
        raise EvaluationContractError(f"{field} must not contain duplicates")
    return result


def _require_optional_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if type(value) not in {int, float}:
        raise EvaluationContractError(f"{field} must be a finite non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise EvaluationContractError(f"{field} must be a finite non-negative number")
    return number


def _require_cost(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"amount", "currency"}:
        raise EvaluationContractError("cost must contain only amount and currency")
    amount = _require_optional_number(value.get("amount"), "cost.amount")
    if amount is None:
        raise EvaluationContractError("cost.amount cannot be null")
    currency = _require_native_text(value.get("currency"), "cost.currency")
    return {"amount": amount, "currency": currency}


def _normalise_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().split()).casefold()


def _normalised_set(values: Iterable[str]) -> set[str]:
    return {_normalise_text(value) for value in values}


def _walk_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        return {str(key).strip().lower() for key in value} | {
            nested for item in value.values() for nested in _walk_keys(item)
        }
    if isinstance(value, list):
        return {nested for item in value for nested in _walk_keys(item)}
    return set()


def _contains_canary(value: Any, canaries: Iterable[str]) -> bool:
    canonical = _canonical_json(value)
    return any(canary in canonical for canary in canaries)


def _validate_fixture(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != _FIXTURE_KEYS:
        raise EvaluationContractError("evaluation fixture fields are invalid")
    if value.get("schema_version") != EVAL_SET_SCHEMA_VERSION:
        raise EvaluationContractError("unsupported evaluation fixture schema")
    _require_native_text(value.get("eval_set_version"), "eval_set_version")
    expected_sha = _require_sha(value.get("fixture_sha256"), "fixture_sha256")
    unsigned = dict(value)
    unsigned.pop("fixture_sha256", None)
    if _sha256(unsigned) != expected_sha:
        raise EvaluationContractError("fixture_sha256 mismatch")

    raw_cases = value.get("cases")
    if type(raw_cases) is not list or not raw_cases:
        raise EvaluationContractError("cases must be a non-empty list")
    cases: list[dict[str, Any]] = []
    case_ids: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, Mapping):
            raise EvaluationContractError("each case must be an object")
        keys = set(raw)
        if not _CASE_REQUIRED_KEYS.issubset(keys) or not keys.issubset(
            _CASE_REQUIRED_KEYS | _CASE_OPTIONAL_KEYS
        ):
            raise EvaluationContractError("evaluation case fields are invalid")
        case_id = _require_native_text(raw.get("case_id"), "case_id")
        if case_id in case_ids:
            raise EvaluationContractError("case_id must be unique")
        case_ids.add(case_id)
        case_kind = _require_native_text(raw.get("case_kind"), "case_kind")
        if case_kind not in _CASE_KINDS:
            raise EvaluationContractError("unsupported case_kind")
        expected_status = raw.get("expected_status")
        if case_kind in {"normal", "status"}:
            expected_status = _require_native_text(expected_status, "expected_status")
            if expected_status not in {"available", "invalid", "unavailable"}:
                raise EvaluationContractError("unsupported expected_status")
        elif expected_status is not None:
            raise EvaluationContractError("attack cases cannot set expected_status")

        allowed_refs = _require_text_list(raw.get("allowed_refs"), "allowed_refs")
        expected_citations = _require_text_list(
            raw.get("expected_citations"), "expected_citations"
        )
        if not set(expected_citations).issubset(allowed_refs):
            raise EvaluationContractError(
                "expected_citations must be bound to allowed_refs"
            )
        observation = raw.get("observation")
        if not isinstance(observation, Mapping):
            raise EvaluationContractError("observation must be an object")
        cases.append(
            {
                "case_id": case_id,
                "case_kind": case_kind,
                "expected_status": expected_status,
                "allowed_refs": allowed_refs,
                "expected_citations": expected_citations,
                "expected_material_facts": _require_text_list(
                    raw.get("expected_material_facts"), "expected_material_facts"
                ),
                "expected_contradictions": _require_text_list(
                    raw.get("expected_contradictions"),
                    "expected_contradictions",
                ),
                "sensitive_canaries": _require_text_list(
                    raw.get("sensitive_canaries"), "sensitive_canaries"
                ),
                "latency_ms": _require_optional_number(
                    raw.get("latency_ms"), "latency_ms"
                ),
                "cost": _require_cost(raw.get("cost")),
                "observation": deepcopy(dict(observation)),
            }
        )
    return cases


def _observation_result(
    case: Mapping[str, Any],
    *,
    provider: str,
    model: str,
    prompt_version: str,
) -> tuple[bool, str, dict[str, Any]]:
    observation = case["observation"]
    if _contains_canary(observation, case["sensitive_canaries"]):
        return False, "sensitive_egress_detected", {}
    if set(observation) != _OBSERVATION_KEYS:
        if _walk_keys(observation) & _AUTHORITY_FIELDS:
            return False, "authority_field_detected", {}
        return False, "observation_schema_invalid", {}
    if _walk_keys(observation.get("evidence")) & _AUTHORITY_FIELDS:
        return False, "authority_field_detected", {}
    if observation.get("authority") != AUTHORITY_DENIED:
        return False, "authority_escalation_rejected", {}
    if (
        observation.get("record_type") != "llm_evidence_observation"
        or observation.get("schema_version") != OBSERVATION_SCHEMA_VERSION
    ):
        return False, "observation_schema_invalid", {}
    if (
        observation.get("provider") != provider
        or observation.get("model") != model
        or observation.get("prompt_version") != prompt_version
    ):
        return False, "evaluation_identity_mismatch", {}
    for field in (
        "request_id",
        "task_type",
        "entity_id",
        "route",
        "provider",
        "model",
        "prompt_version",
        "prompt_sha256",
        "document_cutoff",
        "output_sha256",
        "reason_code",
    ):
        if type(observation.get(field)) is not str:
            return False, "observation_schema_invalid", {}

    status = observation.get("status")
    if type(status) is not str or status not in {
        "available",
        "invalid",
        "unavailable",
    }:
        return False, "observation_schema_invalid", {}
    try:
        bound_refs = _require_text_list(
            observation.get("evidence_refs"), "observation.evidence_refs"
        )
    except EvaluationContractError:
        return False, "reference_binding_mismatch", {}
    if bound_refs != case["allowed_refs"]:
        return False, "reference_binding_mismatch", {}

    if status in {"invalid", "unavailable"}:
        if (
            observation.get("evidence") != {}
            or observation.get("output_sha256") != ""
            or not observation.get("reason_code")
        ):
            return False, "observation_schema_invalid", {}
        return True, f"valid_{status}", {}

    evidence = observation.get("evidence")
    try:
        validated = validate_provider_evidence(
            evidence, allowed_refs=case["allowed_refs"]
        )
    except EvidenceSchemaError as exc:
        if "unknown evidence reference" in str(exc):
            return False, "unknown_evidence_reference", {}
        return False, "evidence_schema_invalid", {}
    for field in ("evidence_refs", "material_facts", "contradictions"):
        values = validated.get(field, [])
        if len(set(values)) != len(values):
            return False, "evidence_schema_invalid", {}
    if _sha256(validated) != observation.get("output_sha256"):
        return False, "output_sha_mismatch", {}
    return True, "valid_available", validated


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": int(numerator),
        "denominator": int(denominator),
        "value": None if denominator == 0 else round(numerator / denominator, 12),
    }


def _matches_attack_expectation(case_kind: str, reason: str) -> bool:
    if case_kind == "authority_attack":
        return reason in {"authority_field_detected", "authority_escalation_rejected"}
    if case_kind == "sensitive_egress_attack":
        return reason == "sensitive_egress_detected"
    if case_kind == "integrity_attack":
        return reason in {
            "output_sha_mismatch",
            "reference_binding_mismatch",
            "unknown_evidence_reference",
        }
    return False


def evaluate_frozen_set(
    fixture: Mapping[str, Any],
    *,
    provider: str,
    model: str,
    prompt_version: str,
) -> dict[str, Any]:
    """Evaluate a caller-supplied frozen fixture without I/O or transport."""

    provider = _require_native_text(provider, "provider")
    model = _require_native_text(model, "model")
    prompt_version = _require_native_text(prompt_version, "prompt_version")
    cases = _validate_fixture(fixture)

    schema_numerator = 0
    schema_denominator = 0
    citation_correct = citation_reported = citation_expected = 0
    fact_correct = fact_reported = fact_expected = 0
    contradiction_correct = contradiction_expected = 0
    attack_totals = {
        "authority_attack": 0,
        "sensitive_egress_attack": 0,
        "integrity_attack": 0,
    }
    attack_rejections = dict.fromkeys(attack_totals, 0)
    invalid_count = 0
    unavailable_count = 0
    operational_case_count = 0
    case_results: list[dict[str, Any]] = []
    operational: list[dict[str, Any]] = []

    for case in cases:
        accepted, reason, evidence = _observation_result(
            case,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
        )
        raw_status = case["observation"].get("status")
        kind = case["case_kind"]
        if kind == "normal":
            operational_case_count += 1
            invalid_count += int(raw_status == "invalid")
            unavailable_count += int(raw_status == "unavailable")
            schema_denominator += 1
            expected = accepted and raw_status == case["expected_status"]
            schema_numerator += int(expected)
            if expected and raw_status == "available":
                cited = _normalised_set(evidence.get("evidence_refs", []))
                expected_citations = _normalised_set(case["expected_citations"])
                citation_correct += len(cited & expected_citations)
                citation_reported += len(cited)
                citation_expected += len(expected_citations)

                facts = _normalised_set(evidence.get("material_facts", []))
                expected_facts = _normalised_set(case["expected_material_facts"])
                fact_correct += len(facts & expected_facts)
                fact_reported += len(facts)
                fact_expected += len(expected_facts)

                contradictions = _normalised_set(evidence.get("contradictions", []))
                expected_contradictions = _normalised_set(
                    case["expected_contradictions"]
                )
                contradiction_correct += len(contradictions & expected_contradictions)
                contradiction_expected += len(expected_contradictions)
        elif kind == "status":
            operational_case_count += 1
            invalid_count += int(raw_status == "invalid")
            unavailable_count += int(raw_status == "unavailable")
            expected = accepted and raw_status == case["expected_status"]
        else:
            attack_totals[kind] += 1
            expected = (not accepted) and _matches_attack_expectation(kind, reason)
            attack_rejections[kind] += int(expected)

        case_results.append(
            {
                "case_id": case["case_id"],
                "case_kind": kind,
                "result_state": "accepted" if accepted else "rejected",
                "reason_code": reason,
                "expectation_met": bool(expected),
            }
        )
        operational.append(
            {
                "case_id": case["case_id"],
                "latency_ms": case["latency_ms"],
                "cost": deepcopy(case["cost"]),
            }
        )

    case_count = len(cases)
    metrics = {
        "structured_schema_pass_rate": _rate(schema_numerator, schema_denominator),
        "citation_precision": _rate(citation_correct, citation_reported),
        "citation_coverage": _rate(citation_correct, citation_expected),
        "material_fact_precision": _rate(fact_correct, fact_reported),
        "material_fact_recall": _rate(fact_correct, fact_expected),
        "contradiction_recall": _rate(contradiction_correct, contradiction_expected),
        "authority_field_rejection_rate": _rate(
            attack_rejections["authority_attack"],
            attack_totals["authority_attack"],
        ),
        "sensitive_egress_rejection_rate": _rate(
            attack_rejections["sensitive_egress_attack"],
            attack_totals["sensitive_egress_attack"],
        ),
        "integrity_forgery_rejection_rate": _rate(
            attack_rejections["integrity_attack"],
            attack_totals["integrity_attack"],
        ),
        "invalid_observation_ratio": _rate(invalid_count, operational_case_count),
        "unavailable_observation_ratio": _rate(
            unavailable_count, operational_case_count
        ),
    }
    report: dict[str, Any] = {
        "record_type": "llm_evidence_eval_report",
        "schema_version": REPORT_SCHEMA_VERSION,
        "eval_set_version": fixture["eval_set_version"],
        "eval_set_sha256": fixture["fixture_sha256"],
        "provider": provider,
        "model": model,
        "prompt_version": prompt_version,
        "case_count": case_count,
        "metrics": metrics,
        "case_results": case_results,
        "operational_observations": operational,
        "decision_use": dict(_DECISION_USE),
    }
    report["report_sha256"] = _sha256(report)
    verify_evaluation_report(report)
    return report


def verify_evaluation_report(value: Any) -> bool:
    """Fail closed if a persisted report is changed or given trading authority."""

    if not isinstance(value, Mapping) or set(value) != _REPORT_KEYS:
        raise EvaluationContractError("evaluation report fields are invalid")
    if value.get("record_type") != "llm_evidence_eval_report":
        raise EvaluationContractError("invalid evaluation report type")
    if value.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise EvaluationContractError("unsupported evaluation report schema")
    _require_native_text(value.get("eval_set_version"), "eval_set_version")
    _require_sha(value.get("eval_set_sha256"), "eval_set_sha256")
    _require_native_text(value.get("provider"), "provider")
    _require_native_text(value.get("model"), "model")
    _require_native_text(value.get("prompt_version"), "prompt_version")
    if type(value.get("case_count")) is not int or value["case_count"] < 1:
        raise EvaluationContractError("case_count must be a positive integer")
    if value.get("decision_use") != _DECISION_USE:
        raise EvaluationContractError(
            "evaluation report cannot grant decision authority"
        )

    metrics = value.get("metrics")
    if not isinstance(metrics, Mapping) or set(metrics) != _METRIC_NAMES:
        raise EvaluationContractError("evaluation metrics are invalid")
    for name, metric in metrics.items():
        if not isinstance(metric, Mapping) or set(metric) != {
            "numerator",
            "denominator",
            "value",
        }:
            raise EvaluationContractError(f"invalid metric: {name}")
        numerator = metric.get("numerator")
        denominator = metric.get("denominator")
        if (
            type(numerator) is not int
            or type(denominator) is not int
            or numerator < 0
            or denominator < 0
            or numerator > denominator
        ):
            raise EvaluationContractError(f"invalid metric counts: {name}")
        expected_value = (
            None if denominator == 0 else round(numerator / denominator, 12)
        )
        if metric.get("value") != expected_value:
            raise EvaluationContractError(f"invalid metric value: {name}")

    results = value.get("case_results")
    operational = value.get("operational_observations")
    if (
        type(results) is not list
        or type(operational) is not list
        or len(results) != value["case_count"]
        or len(operational) != value["case_count"]
    ):
        raise EvaluationContractError("evaluation case collections are invalid")
    for item in results:
        if not isinstance(item, Mapping) or set(item) != {
            "case_id",
            "case_kind",
            "result_state",
            "reason_code",
            "expectation_met",
        }:
            raise EvaluationContractError("case result fields are invalid")
        _require_native_text(item.get("case_id"), "case_id")
        if item.get("case_kind") not in _CASE_KINDS:
            raise EvaluationContractError("invalid result case_kind")
        if item.get("result_state") not in {"accepted", "rejected"}:
            raise EvaluationContractError("invalid result_state")
        _require_native_text(item.get("reason_code"), "reason_code")
        if type(item.get("expectation_met")) is not bool:
            raise EvaluationContractError("expectation_met must be boolean")
    for item in operational:
        if not isinstance(item, Mapping) or set(item) != {
            "case_id",
            "latency_ms",
            "cost",
        }:
            raise EvaluationContractError("operational observation fields are invalid")
        _require_native_text(item.get("case_id"), "case_id")
        _require_optional_number(item.get("latency_ms"), "latency_ms")
        _require_cost(item.get("cost"))

    report_sha = _require_sha(value.get("report_sha256"), "report_sha256")
    unsigned = dict(value)
    unsigned.pop("report_sha256", None)
    if _sha256(unsigned) != report_sha:
        raise EvaluationContractError("report_sha256 mismatch")
    if _walk_keys(value) & _AUTHORITY_FIELDS:
        raise EvaluationContractError("evaluation report contains trading authority")
    return True
