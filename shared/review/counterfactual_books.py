"""Return-only counterfactual views derived from an outcome evaluation report."""

from __future__ import annotations

from copy import deepcopy
from statistics import mean
from typing import Any, Callable, Mapping, Sequence

from shared.models.lifecycle import ValidationPlan
from shared.review.outcome_evaluation import (
    OutcomeEvaluationError,
    OutcomeMarketTruthVerifier,
    ValidationPlanProvenanceVerifier,
    canonical_sha256,
    verify_outcome_evaluation_against_source,
)


COUNTERFACTUAL_BOOKS_SCHEMA_VERSION = "ashare-counterfactual-books.v1"
_BOOK_AUTHORITY = {
    "research_only": True,
    "capital_authority": False,
    "position_authority": False,
    "order_authority": False,
    "automatic_promotion_enabled": False,
    "automatic_risk_expansion_enabled": False,
    "live_transition_authorized": False,
    "real_trading_enabled": False,
}


class CounterfactualBooksError(ValueError):
    """Raised when a counterfactual report is malformed or claims authority."""


def _book(
    rows: Sequence[Mapping[str, Any]],
    predicate: Callable[[Mapping[str, Any]], bool],
) -> dict[str, Any]:
    selected = [row for row in rows if predicate(row)]
    clusters: dict[str, list[Mapping[str, Any]]] = {}
    for row in selected:
        cluster_id = str(row.get("decision_cluster_id") or "").strip()
        if cluster_id:
            clusters.setdefault(cluster_id, []).append(row)

    ready: list[float] = []
    raw_ready_count = 0
    ambiguous_cluster_count = 0
    for cluster_rows in clusters.values():
        cluster_ready: list[tuple[str, float]] = []
        for row in cluster_rows:
            label = row.get("label")
            if (
                row.get("eligible_for_statistical_learning") is not True
                or not isinstance(label, Mapping)
                or label.get("status") not in {"ready", "labeled"}
                or not isinstance(label.get("net_return_after_costs"), (int, float))
                or isinstance(label.get("net_return_after_costs"), bool)
            ):
                continue
            raw_ready_count += 1
            cluster_ready.append(
                (canonical_sha256(label), float(label["net_return_after_costs"]))
            )
        if not cluster_ready:
            continue
        identities = {identity for identity, _ in cluster_ready}
        if len(identities) != 1:
            ambiguous_cluster_count += 1
            continue
        ready.append(cluster_ready[0][1])
    return {
        "status": "available" if ready else "unavailable_no_ready_outcomes",
        "constituent_outcome_ids": sorted(
            str(row.get("outcome_id") or "") for row in selected
        ),
        "observation_count": len(selected),
        "unique_decision_cluster_count": len(clusters),
        "raw_ready_outcome_count": raw_ready_count,
        "ready_unique_decision_cluster_count": len(ready),
        "ambiguous_decision_cluster_count": ambiguous_cluster_count,
        "ready_outcome_count": len(ready),
        "mean_net_return_after_costs": round(mean(ready), 12) if ready else None,
        "unit_of_analysis": "unique_decision_cluster",
        "descriptive_slice_only": True,
        "causal_interpretation_permitted": False,
        "synthetic_counterfactual": True,
        "capital_authority": False,
        "position_authority": False,
        "order_authority": False,
        "pnl_cny": None,
    }


def build_counterfactual_books(
    outcome_report: Mapping[str, Any],
    *,
    events: Sequence[Mapping[str, Any]],
    expected_as_of: str,
    expected_authority_scope: Mapping[str, Any],
    validation_plan: ValidationPlan | None = None,
    validation_plan_provenance: Mapping[str, Any] | None = None,
    validation_plan_provenance_verifier: ValidationPlanProvenanceVerifier | None = None,
    market_truth_verifier: OutcomeMarketTruthVerifier | None = None,
) -> dict[str, Any]:
    try:
        verify_outcome_evaluation_against_source(
            outcome_report,
            events=events,
            expected_as_of=expected_as_of,
            expected_authority_scope=expected_authority_scope,
            validation_plan=validation_plan,
            validation_plan_provenance=validation_plan_provenance,
            validation_plan_provenance_verifier=validation_plan_provenance_verifier,
            market_truth_verifier=market_truth_verifier,
        )
    except OutcomeEvaluationError as exc:
        raise CounterfactualBooksError(str(exc)) from exc
    rows = deepcopy(list(outcome_report["outcomes"]))
    books = {
        "all_observations": _book(rows, lambda _: True),
        "champion_selected": _book(
            rows,
            lambda row: (
                bool(str(row.get("decision_id") or "").strip())
                and (
                    str(row.get("sample_intent") or "").strip().lower()
                    in {"champion", "exploitation", "mature"}
                    or str(row.get("style") or "").strip().lower() == "champion"
                )
            ),
        ),
        "post_risk_accepted": _book(
            rows,
            lambda row: row.get("disposition") in {"paper_filled", "paper_not_filled"},
        ),
        "paper_filled": _book(
            rows, lambda row: row.get("disposition") == "paper_filled"
        ),
        "paper_not_filled": _book(
            rows, lambda row: row.get("disposition") == "paper_not_filled"
        ),
        "rejected": _book(rows, lambda row: row.get("disposition") == "rejected"),
    }
    report: dict[str, Any] = {
        "record_type": "ashare_counterfactual_books",
        "schema_version": COUNTERFACTUAL_BOOKS_SCHEMA_VERSION,
        "source_outcome_report_sha256": outcome_report["report_sha256"],
        "as_of": outcome_report["as_of"],
        "books": books,
        "authority": deepcopy(_BOOK_AUTHORITY),
    }
    report["report_sha256"] = canonical_sha256(report)
    _verify_counterfactual_books_structure(report)
    return report


def _verify_counterfactual_books_structure(value: Any) -> bool:
    if not isinstance(value, Mapping):
        raise CounterfactualBooksError("counterfactual_report_invalid")
    if value.get("schema_version") != COUNTERFACTUAL_BOOKS_SCHEMA_VERSION:
        raise CounterfactualBooksError("counterfactual_report_schema_invalid")
    if value.get("authority") != _BOOK_AUTHORITY:
        raise CounterfactualBooksError("counterfactual_report_authority_invalid")
    books = value.get("books")
    expected = {
        "all_observations",
        "champion_selected",
        "post_risk_accepted",
        "paper_filled",
        "paper_not_filled",
        "rejected",
    }
    if not isinstance(books, Mapping) or set(books) != expected:
        raise CounterfactualBooksError("counterfactual_books_invalid")
    if any(
        not isinstance(book, Mapping)
        or book.get("capital_authority") is not False
        or book.get("position_authority") is not False
        or book.get("order_authority") is not False
        or book.get("pnl_cny") is not None
        or book.get("unit_of_analysis") != "unique_decision_cluster"
        or book.get("descriptive_slice_only") is not True
        or book.get("causal_interpretation_permitted") is not False
        or book.get("ready_outcome_count")
        != book.get("ready_unique_decision_cluster_count")
        for book in books.values()
    ):
        raise CounterfactualBooksError("counterfactual_book_authority_invalid")
    unsigned = deepcopy(dict(value))
    supplied = unsigned.pop("report_sha256", None)
    if supplied != canonical_sha256(unsigned):
        raise CounterfactualBooksError("counterfactual_report_sha256_mismatch")
    return True


def verify_counterfactual_books(
    value: Any,
    *,
    outcome_report: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    expected_as_of: str,
    expected_authority_scope: Mapping[str, Any],
    validation_plan: ValidationPlan | None = None,
    validation_plan_provenance: Mapping[str, Any] | None = None,
    validation_plan_provenance_verifier: ValidationPlanProvenanceVerifier | None = None,
    market_truth_verifier: OutcomeMarketTruthVerifier | None = None,
) -> bool:
    """Rebuild books from the exact outcome source instead of trusting a hash."""

    _verify_counterfactual_books_structure(value)
    expected = build_counterfactual_books(
        outcome_report,
        events=events,
        expected_as_of=expected_as_of,
        expected_authority_scope=expected_authority_scope,
        validation_plan=validation_plan,
        validation_plan_provenance=validation_plan_provenance,
        validation_plan_provenance_verifier=validation_plan_provenance_verifier,
        market_truth_verifier=market_truth_verifier,
    )
    if dict(value) != expected:
        raise CounterfactualBooksError(
            "counterfactual_report_does_not_match_exact_sources"
        )
    return True


__all__ = [
    "COUNTERFACTUAL_BOOKS_SCHEMA_VERSION",
    "CounterfactualBooksError",
    "build_counterfactual_books",
    "verify_counterfactual_books",
]
