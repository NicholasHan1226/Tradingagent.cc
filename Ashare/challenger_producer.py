#!/usr/bin/env python3
"""Offline-learning Challenger producer for the A-share simulation domain.

The append-only SampleJournal KPI projection is the only evolution authority.
When an evolution decision is ``promotion_evidence_ready`` this module turns
the per-style, per-sample-intent KPI evidence into challenger candidate
descriptions that satisfy the :mod:`Ashare.promotion_executor` contract
(required text/sha256 fields plus an already verified, frozen
``ValidationPlan`` instance).  When the evidence does not clear the bars
below, an empty list is produced — a challenger is never fabricated and a
missing candidate stays an explicit ``no_op/no_qualified_challenger`` in the
promotion executor.

Evidence semantics and threshold rationale
------------------------------------------

A challenger style qualifies only on its own completed round-trip economics,
read from ``kpi["styles"][style_id]["performance_by_sample_intent"]`` with
``exploitation`` preferred over ``exploration`` (the same layering the
scientific gate uses — ``unclassified`` buckets never qualify).  The style
must simultaneously show:

- ``completed_round_trip_count >= 5``: half of the account-level promotion
  bar (``MIN_COMPLETED_ROUND_TRIPS = 10`` in
  :mod:`Ashare.evolution_controller`); a challenger may carry less evidence
  than the account-level gate it feeds, but never a trivial sample.
- ``expectancy_cny > 0``: mirrors the ``positive_expectancy`` requirement of
  the scientific evidence gate, applied to the challenger style itself
  instead of any-style.
- ``win_rate >= 0.5``: a strict-majority hit rate so a positive expectancy
  cannot rest on a single outlier win.
- ``trade_pnl_sequence_max_drawdown_cny <= 5_625``: 0.75 x the 7,500 CNY
  single-name exposure cap, mirroring the standing policy that a 5% drawdown
  only tightens the risk budget to 0.75x; a challenger whose own trade-PnL
  sequence already drawdowns beyond that bound is not promotion evidence.
  This is the auxiliary trade-PnL sequence drawdown, never an account MTM
  drawdown claim.

All four numbers must be present, finite and non-boolean in the KPI; missing
or malformed evidence disqualifies the style (fail closed).  Every candidate
binds its artifact and validation evidence to the content-addressed
``projection_input_sha256`` of the producing KPI projection, and ``created_at``
is bound to the promotion ``recorded_at`` so the executor's
``plan.frozen_at <= created_at <= recorded_at`` invariant holds by
construction.

Everything stays simulation-only: any decision that is not explicitly
simulation-only (``real_trading_enabled``/``live_transition_authorized``/
``automatic_risk_expansion_enabled`` all ``False``) raises
:class:`ChallengerProducerError` instead of producing candidates.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Any, Mapping, Optional, Tuple

from Ashare.style_samples import SCHEMA_VERSION, STYLE_DEFINITIONS
from shared.models.lifecycle import ValidationPlan


class ChallengerProducerError(RuntimeError):
    """Raised when a challenger production input cannot be trusted."""


FEATURE_CONTRACT_VERSION = SCHEMA_VERSION
STYLE_CATALOG_VERSION = "ashare-challenger-style-catalog-v1"
CREATED_BY = "ashare-challenger-producer"

MIN_CHALLENGER_COMPLETED_ROUND_TRIPS = 5
MIN_CHALLENGER_WIN_RATE = 0.5
MAX_CHALLENGER_TRADE_PNL_DRAWDOWN_CNY = 5_625.0
EVIDENCE_BUCKET_ORDER = ("exploitation", "exploration")

_DECISION_SAFETY_FIELDS = (
    "real_trading_enabled",
    "live_transition_authorized",
    "automatic_risk_expansion_enabled",
)
_REQUIRED_SCIENTIFIC_GATES = (
    "point_in_time_lineage_complete",
    "costs_evidence_complete",
    "fill_evidence_revalidated",
    "duplicate_cluster_control_passed",
    "calibration_evidence_sufficient",
)
_SHA256_HEX = frozenset("0123456789abcdef")


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ChallengerProducerError("challenger_evidence_not_canonical") from exc
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256_HEX for character in value)
    )


def _finite_number(value: Any, *, nonnegative: bool = False) -> Optional[float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    parsed = float(value)
    if not math.isfinite(parsed):
        return None
    if nonnegative and parsed < 0.0:
        return None
    return parsed


def _validate_decision(decision: Any) -> Mapping[str, Any]:
    if not isinstance(decision, Mapping):
        raise ChallengerProducerError("challenger_decision_invalid")
    for field_name in _DECISION_SAFETY_FIELDS:
        if decision.get(field_name) is not False:
            raise ChallengerProducerError("challenger_decision_not_simulation_only")
    return decision


def _decision_ready(decision: Mapping[str, Any]) -> bool:
    if not (
        decision.get("promotion_evidence_ready") is True
        and decision.get("recommended_action") == "execute_automatic_promotion"
    ):
        return False
    scientific = decision.get("scientific_evidence")
    scientific_map = scientific if isinstance(scientific, Mapping) else {}
    return all(
        scientific_map.get(gate) is True for gate in _REQUIRED_SCIENTIFIC_GATES
    )


def _qualified_style_evidence(
    style_row: Any,
) -> Optional[Tuple[str, Mapping[str, Any]]]:
    """Return the qualifying ``(sample_intent, metrics)`` bucket, or ``None``."""

    if not isinstance(style_row, Mapping):
        return None
    buckets = style_row.get("performance_by_sample_intent")
    if not isinstance(buckets, Mapping):
        return None
    for intent in EVIDENCE_BUCKET_ORDER:
        metrics = buckets.get(intent)
        if not isinstance(metrics, Mapping):
            continue
        completed = metrics.get("completed_round_trip_count")
        if (
            isinstance(completed, bool)
            or not isinstance(completed, (int, float))
            or int(completed) < MIN_CHALLENGER_COMPLETED_ROUND_TRIPS
        ):
            continue
        expectancy = _finite_number(metrics.get("expectancy_cny"))
        win_rate = _finite_number(metrics.get("win_rate"), nonnegative=True)
        drawdown = _finite_number(
            metrics.get("trade_pnl_sequence_max_drawdown_cny"), nonnegative=True
        )
        if expectancy is None or expectancy <= 0.0:
            continue
        if win_rate is None or win_rate < MIN_CHALLENGER_WIN_RATE:
            continue
        if drawdown is None or drawdown > MAX_CHALLENGER_TRADE_PNL_DRAWDOWN_CNY:
            continue
        return intent, metrics
    return None


def build_challenger_candidates(
    sample_kpi: Mapping[str, Any],
    *,
    decision: Mapping[str, Any],
    validation_plan: ValidationPlan,
    recorded_at: datetime,
) -> list[dict[str, Any]]:
    """Build promotion-executor-ready challenger candidates from KPI evidence.

    Returns one candidate description per qualified challenger style, ordered
    deterministically by ``style_id``.  Returns an empty list when the
    decision is not evidence-ready or no style clears the evidence bars.
    Unsafe inputs (non-simulation-only decision, unverified plan, naive
    ``recorded_at``) raise :class:`ChallengerProducerError` (fail closed).
    """

    decision = _validate_decision(decision)
    if not isinstance(validation_plan, ValidationPlan):
        raise ChallengerProducerError("challenger_validation_plan_invalid")
    if (
        not isinstance(recorded_at, datetime)
        or recorded_at.tzinfo is None
        or recorded_at.utcoffset() is None
    ):
        raise ChallengerProducerError("challenger_recorded_at_invalid")
    if not _decision_ready(decision):
        return []
    if validation_plan.frozen_at > recorded_at:
        # The frozen plan must predate candidate materialization; a plan
        # frozen after the promotion timestamp cannot bind this evidence.
        return []

    kpi = sample_kpi if isinstance(sample_kpi, Mapping) else {}
    projection_input_sha256 = kpi.get("projection_input_sha256")
    if not _is_sha256(projection_input_sha256):
        return []
    data_as_of = str(kpi.get("data_as_of") or "").strip()
    run_id = str(kpi.get("run_id") or "").strip()
    if not data_as_of or not run_id:
        return []
    journal_head_event_count = kpi.get("journal_head_event_count")
    if isinstance(journal_head_event_count, bool) or not isinstance(
        journal_head_event_count, int
    ):
        return []
    authority_scope = decision.get("authority_scope")
    authority = dict(authority_scope) if isinstance(authority_scope, Mapping) else {}
    scientific = decision.get("scientific_evidence")
    scientific_map = dict(scientific) if isinstance(scientific, Mapping) else {}
    trade_date = str(decision.get("trade_date") or "").strip()

    styles = kpi.get("styles")
    style_rows = styles if isinstance(styles, Mapping) else {}
    candidates: list[dict[str, Any]] = []
    for definition in sorted(STYLE_DEFINITIONS, key=lambda row: row["style_id"]):
        if definition.get("lifecycle_status") != "challenger":
            continue
        style_id = str(definition["style_id"])
        style_version = str(definition["style_version"])
        qualified = _qualified_style_evidence(style_rows.get(style_id))
        if qualified is None:
            continue
        intent, metrics = qualified
        artifact_sha256 = _canonical_sha256(
            {
                "authority_scope": authority,
                "challenger_id": style_id,
                "challenger_version": style_version,
                "data_as_of": data_as_of,
                "projection_input_sha256": projection_input_sha256,
            }
        )
        validation_evidence_sha256 = _canonical_sha256(
            {
                "challenger_id": style_id,
                "challenger_version": style_version,
                "evidence_bucket": intent,
                "metrics": {
                    "completed_round_trip_count": int(
                        metrics["completed_round_trip_count"]
                    ),
                    "win_rate": float(metrics["win_rate"]),
                    "expectancy_cny": float(metrics["expectancy_cny"]),
                    "post_cost_pnl_cny": float(metrics.get("post_cost_pnl_cny") or 0.0),
                    "trade_pnl_sequence_max_drawdown_cny": float(
                        metrics["trade_pnl_sequence_max_drawdown_cny"]
                    ),
                },
                "projection_input_sha256": projection_input_sha256,
                "scientific_evidence": scientific_map,
                "trade_date": trade_date,
            }
        )
        candidates.append(
            {
                "challenger_id": style_id,
                "challenger_version": style_version,
                "artifact_sha256": artifact_sha256,
                "training_data_version": "sample-journal-events-%d"
                % journal_head_event_count,
                "feature_contract_version": FEATURE_CONTRACT_VERSION,
                "research_snapshot_sha256": projection_input_sha256,
                "catalog_version": STYLE_CATALOG_VERSION,
                "validation_evidence_sha256": validation_evidence_sha256,
                "source_commit": run_id,
                "created_by": CREATED_BY,
                "created_at": recorded_at,
                "validation_plan": validation_plan,
            }
        )
    return candidates


__all__ = [
    "ChallengerProducerError",
    "CREATED_BY",
    "EVIDENCE_BUCKET_ORDER",
    "FEATURE_CONTRACT_VERSION",
    "MAX_CHALLENGER_TRADE_PNL_DRAWDOWN_CNY",
    "MIN_CHALLENGER_COMPLETED_ROUND_TRIPS",
    "MIN_CHALLENGER_WIN_RATE",
    "STYLE_CATALOG_VERSION",
    "build_challenger_candidates",
]
