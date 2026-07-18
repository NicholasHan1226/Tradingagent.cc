from __future__ import annotations

from copy import deepcopy

import pytest

from shared.review.counterfactual_books import (
    CounterfactualBooksError,
    build_counterfactual_books,
    verify_counterfactual_books,
)
from shared.review.outcome_evaluation import canonical_sha256

from tests.test_outcome_evaluation import (
    AUTHORITY,
    MARKET_TRUTH_VERIFIER,
    PLAN_PROVENANCE_VERIFIER,
    TRUSTED_PLAN_PROVENANCE,
    VALIDATION_PLAN,
    build_outcome_evaluation,
    decision,
    forward_label_update,
    prediction,
    ready_events,
)


def _build_books(report, events):
    return build_counterfactual_books(
        report,
        events=events,
        expected_as_of=report["as_of"],
        expected_authority_scope=AUTHORITY,
        validation_plan=VALIDATION_PLAN,
        validation_plan_provenance=TRUSTED_PLAN_PROVENANCE,
        validation_plan_provenance_verifier=PLAN_PROVENANCE_VERIFIER,
        market_truth_verifier=MARKET_TRUTH_VERIFIER,
    )


def _verify_books(report, outcome_report, events):
    return verify_counterfactual_books(
        report,
        outcome_report=outcome_report,
        events=events,
        expected_as_of=outcome_report["as_of"],
        expected_authority_scope=AUTHORITY,
        validation_plan=VALIDATION_PLAN,
        validation_plan_provenance=TRUSTED_PLAN_PROVENANCE,
        validation_plan_provenance_verifier=PLAN_PROVENANCE_VERIFIER,
        market_truth_verifier=MARKET_TRUTH_VERIFIER,
    )


def test_counterfactual_books_are_return_only_and_do_not_invent_capital() -> None:
    events = [*ready_events(), decision()]
    report = build_outcome_evaluation(
        events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    books = _build_books(report, events)

    assert _verify_books(books, report, events) is True
    assert set(books["books"]) == {
        "all_observations",
        "champion_selected",
        "post_risk_accepted",
        "paper_filled",
        "paper_not_filled",
        "rejected",
    }
    filled = books["books"]["paper_filled"]
    assert filled["ready_outcome_count"] == 1
    assert filled["ready_unique_decision_cluster_count"] == 1
    assert filled["unit_of_analysis"] == "unique_decision_cluster"
    assert filled["mean_net_return_after_costs"] == 0.02
    assert filled["pnl_cny"] is None
    assert filled["synthetic_counterfactual"] is True
    assert filled["causal_interpretation_permitted"] is False
    assert books["authority"]["capital_authority"] is False
    assert "best_policy" not in repr(books)


def test_missing_outcomes_remain_null_instead_of_zero() -> None:
    events = [prediction(net_return=None)]
    report = build_outcome_evaluation(
        events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    books = _build_books(report, events)
    observed = books["books"]["all_observations"]
    assert observed["ready_outcome_count"] == 0
    assert observed["mean_net_return_after_costs"] is None
    assert observed["status"] == "unavailable_no_ready_outcomes"


def test_champion_slice_uses_explicit_fields_not_model_id_substrings() -> None:
    not_champion = prediction(net_return=None)
    not_champion["style"] = "candidate"
    not_champion["sample_intent"] = "observation"
    not_champion["model_id"] = "definitely-not-champion"
    matching_decision = decision()
    matching_decision["decision_exposure"]["model_id"] = "definitely-not-champion"
    events = [not_champion, forward_label_update(not_champion), matching_decision]
    report = build_outcome_evaluation(
        events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )

    books = _build_books(report, events)
    champion = books["books"]["champion_selected"]
    assert champion["observation_count"] == 0
    assert champion["unique_decision_cluster_count"] == 0


def test_duplicate_rows_in_one_decision_cluster_are_not_double_counted() -> None:
    first = prediction(net_return=None)
    duplicate = prediction(snapshot_id="prediction:duplicate", net_return=None)
    events = [
        first,
        forward_label_update(first),
        duplicate,
        forward_label_update(duplicate),
    ]
    report = build_outcome_evaluation(
        events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )

    books = _build_books(report, events)
    observed = books["books"]["all_observations"]
    assert observed["observation_count"] == 2
    assert observed["raw_ready_outcome_count"] == 2
    assert observed["unique_decision_cluster_count"] == 1
    assert observed["ready_unique_decision_cluster_count"] == 1
    assert observed["ready_outcome_count"] == 1
    assert observed["mean_net_return_after_costs"] == 0.02


def test_rehashed_outcome_content_tamper_is_rejected_against_source_events() -> None:
    events = [*ready_events(), decision()]
    report = build_outcome_evaluation(
        events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    tampered = deepcopy(report)
    tampered["outcomes"][0]["label"]["net_return_after_costs"] = 0.99
    unsigned = deepcopy(tampered)
    unsigned.pop("report_sha256")
    tampered["report_sha256"] = canonical_sha256(unsigned)

    with pytest.raises(
        CounterfactualBooksError,
        match="outcome_report_does_not_match_source_events",
    ):
        build_counterfactual_books(
            tampered,
            events=events,
            expected_as_of=report["as_of"],
            expected_authority_scope=AUTHORITY,
            validation_plan=VALIDATION_PLAN,
            validation_plan_provenance=TRUSTED_PLAN_PROVENANCE,
            validation_plan_provenance_verifier=PLAN_PROVENANCE_VERIFIER,
            market_truth_verifier=MARKET_TRUTH_VERIFIER,
        )


def test_rehashed_counterfactual_tamper_is_rejected_against_exact_sources() -> None:
    events = [*ready_events(), decision()]
    outcome_report = build_outcome_evaluation(
        events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    books = _build_books(outcome_report, events)
    tampered = deepcopy(books)
    tampered["books"]["paper_filled"]["mean_net_return_after_costs"] = 0.99
    unsigned = deepcopy(tampered)
    unsigned.pop("report_sha256")
    tampered["report_sha256"] = canonical_sha256(unsigned)

    with pytest.raises(
        CounterfactualBooksError,
        match="counterfactual_report_does_not_match_exact_sources",
    ):
        _verify_books(tampered, outcome_report, events)
