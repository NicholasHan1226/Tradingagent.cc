"""Offline-only learning projections for completed delayed-paper sessions.

This module is intentionally downstream of the fixture bundle.  It neither
queries market data nor touches the shared SampleJournal, capital, broker, or
promotion paths.  Its small append-only journal is A-share-local evidence for
later human review only.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterator, Mapping

from .minute_canary import (
    MinuteCanaryConfigurationError,
    snapshot_from_canary_receipt,
)
from .minute_data import (
    MinuteBarSnapshot,
    MinuteDatasetProfile,
    SHANGHAI,
)
from .minute_research import (
    MODEL_ID,
    MODEL_VERSION,
    MinuteFeatureVector,
    MinuteResearchUniverse,
    rank_minute_candidates,
)
from .style_samples import compute_ashare_conservative_costs
from .minute_day_report import MinuteDayReportError, build_minute_day_report
from shared.governance.evidence_readiness import load_evidence_readiness_contract


JOURNAL_NAME = "minute_fixture_learning_journal.jsonl"
LATEST_NAME = "minute_fixture_learning_latest.json"
SCHEMA = "tradingagent.ashare.minute_fixture_learning.v1"
OBSERVATION_OUTCOME_JOURNAL_NAME = "minute_observation_outcomes.jsonl"
OBSERVATION_OUTCOME_LATEST_NAME = "minute_observation_outcome_latest.json"
OBSERVATION_OUTCOME_SCHEMA = "tradingagent.ashare.minute_observation_outcome.v1"
FORWARD_LABEL_HORIZONS = ("m30", "m60", "close", "1d", "3d", "5d")
FORWARD_LABEL_SCHEMA = "tradingagent.ashare.minute_forward_label.v1"
_SHA256_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class LocalContiguousLearningProfile:
    """Pre-registered feature/label geometry for local offline review only."""

    profile_id: str
    feature_slots: int
    label_horizon_slots: int
    label_kind: str

    @property
    def minimum_slots(self) -> int:
        return self.feature_slots + self.label_horizon_slots


LOCAL_CONTIGUOUS_LEARNING_PROFILE = LocalContiguousLearningProfile(
    profile_id="ashare.minute.local_contiguous.next_bar_return.v1",
    feature_slots=2,
    label_horizon_slots=1,
    label_kind="next_completed_bar_return",
)


class MinuteOfflineLearningError(ValueError):
    """Raised when fixture learning evidence is unsafe to project."""


def _single_symbol_bar(
    snapshot: MinuteBarSnapshot, symbol: str, reason: str
) -> Any:
    bars = tuple(bar for bar in snapshot.bars if bar.symbol == symbol)
    if len(bars) != 1:
        raise MinuteOfflineLearningError(reason)
    return bars[0]


def build_minute_forward_label(
    *,
    source_snapshot: MinuteBarSnapshot,
    future_snapshot: MinuteBarSnapshot,
    symbol: str,
    target_slot: datetime | str,
    decision_as_of: datetime | str,
    research_universe: MinuteResearchUniverse,
    horizon: str = "m60",
) -> dict[str, Any]:
    """Resolve one PIT-safe forward label from already validated snapshots.

    This is deliberately per-symbol: a capability-local TD failure for other
    symbols does not erase a validated row/proof pair.  The snapshots are the
    existing TA query boundary, so no provider or database access occurs here.
    """

    if (
        not isinstance(source_snapshot, MinuteBarSnapshot)
        or not isinstance(future_snapshot, MinuteBarSnapshot)
        or not isinstance(research_universe, MinuteResearchUniverse)
        or horizon not in FORWARD_LABEL_HORIZONS
        or horizon != "m60"
    ):
        raise MinuteOfflineLearningError("minute_forward_label_input_invalid")
    if not isinstance(symbol, str) or not symbol.strip():
        raise MinuteOfflineLearningError("minute_forward_label_symbol_invalid")
    source_bar = _single_symbol_bar(source_snapshot, symbol, "minute_forward_label_source_missing")
    future_bar = _single_symbol_bar(future_snapshot, symbol, "minute_forward_label_future_missing")
    source_proof = source_snapshot.validated_proof_summary
    future_proof = future_snapshot.validated_proof_summary
    if source_proof is None or future_proof is None:
        raise MinuteOfflineLearningError("minute_forward_label_proof_missing")
    if (
        source_bar.receipt_id not in source_proof.receipt_ids
        or future_bar.receipt_id not in future_proof.receipt_ids
        or not all(
            isinstance(value, str)
            and len(value) == 64
            and all(character in _SHA256_HEX for character in value)
            for value in (
                source_bar.source_lineage_sha256,
                future_bar.source_lineage_sha256,
                source_bar.source_row_sha256,
                future_bar.source_row_sha256,
            )
        )
        or not source_bar.receipt_id.strip()
        or not future_bar.receipt_id.strip()
    ):
        raise MinuteOfflineLearningError("minute_forward_label_proof_binding_invalid")
    if (
        source_proof.dataset_id != future_proof.dataset_id
        or source_proof.provider != future_proof.provider
        or source_proof.config_hash != future_proof.config_hash
        or source_snapshot.profile.dataset_id != future_snapshot.profile.dataset_id
        or source_proof.dataset_id != source_snapshot.profile.dataset_id
    ):
        raise MinuteOfflineLearningError("minute_forward_label_cohort_mismatch")
    target = _observation_stamp(target_slot, "minute_forward_label_target_slot_invalid")
    as_of = _observation_stamp(decision_as_of, "minute_forward_label_decision_as_of_invalid")
    source_proof_through = _observation_stamp(
        source_proof.data_through, "minute_forward_label_proof_data_through_invalid"
    )
    future_proof_through = _observation_stamp(
        future_proof.data_through, "minute_forward_label_proof_data_through_invalid"
    )
    if (
        source_proof_through != source_bar.data_through
        or future_proof_through != future_bar.data_through
        or target != future_bar.bar_end
        or future_bar.data_through != target
    ):
        raise MinuteOfflineLearningError("minute_forward_label_target_mismatch")
    if (
        source_bar.bar_end.astimezone(SHANGHAI).date()
        != future_bar.bar_end.astimezone(SHANGHAI).date()
        or source_bar.market_session != future_bar.market_session
        or future_bar.bar_end - source_bar.bar_end != timedelta(minutes=60)
        or not source_bar.bar_end < future_bar.bar_end
        or source_bar.receipt_id == future_bar.receipt_id
        or source_bar.data_through >= future_bar.data_through
        or source_bar.observed_at > as_of
        or future_bar.observed_at > as_of
        or source_bar.available_at > as_of
        or future_bar.available_at > as_of
    ):
        raise MinuteOfflineLearningError("minute_forward_label_pit_invalid")
    if source_proof_through >= future_proof_through:
        raise MinuteOfflineLearningError("minute_forward_label_slot_identity_invalid")

    # One transparent, uncalibrated factor: source close vs reference close.
    close_return = source_bar.close_cny / source_bar.previous_close_cny - 1.0
    intrabar_return = 0.0
    range_ratio = 0.0
    feature = MinuteFeatureVector(
        symbol=symbol,
        bar_end=source_bar.bar_end,
        previous_bar_sha256=source_bar.reference_evidence_sha256,
        current_bar_sha256=source_bar.source_row_sha256,
        close_to_close_return=round(close_return, 10),
        intrabar_return=round(intrabar_return, 10),
        range_ratio=round(range_ratio, 10),
        volume_change=0.0,
        amount_change=0.0,
        context_adjustment=0.0,
        raw_rank_score=round(100.0 * close_return, 10),
    )
    candidates = rank_minute_candidates(
        universe=research_universe,
        features=(feature,),
        trade_date=source_bar.bar_end.astimezone(SHANGHAI).date(),
        minimum_raw_score=-1_000_000_000.0,
    )
    if len(candidates) != 1 or not candidates[0].eligible:
        raise MinuteOfflineLearningError("minute_forward_label_factor_ineligible")
    costs = compute_ashare_conservative_costs(source_bar.close_cny)
    sample_key = _observation_sha(
        {
            "symbol": symbol,
            "horizon": horizon,
            "model_id": MODEL_ID,
            "model_version": MODEL_VERSION,
            "costs": costs,
            "source": {
                "dataset_id": source_proof.dataset_id,
                "provider": source_proof.provider,
                "execution_id": source_proof.execution_id,
                "config_hash": source_proof.config_hash,
                "data_through": source_proof_through.isoformat(),
                "receipt_id": source_bar.receipt_id,
                "lineage": source_bar.source_lineage_sha256,
                "row": source_bar.source_row_sha256,
            },
            "future": {
                "execution_id": future_proof.execution_id,
                "data_through": future_proof_through.isoformat(),
                "receipt_id": future_bar.receipt_id,
                "lineage": future_bar.source_lineage_sha256,
                "row": future_bar.source_row_sha256,
            },
        }
    )
    gross_return = future_bar.close_cny / source_bar.close_cny - 1.0
    cost_return = (costs["round_trip_fee_bps"] + costs["round_trip_slippage_bps"]) / 10_000.0
    net_return = gross_return - cost_return
    label = {
        "symbol": symbol,
        "horizon": horizon,
        "source_receipt_id": source_bar.receipt_id,
        "future_receipt_id": future_bar.receipt_id,
        "source_lineage_sha256": source_bar.source_lineage_sha256,
        "future_lineage_sha256": future_bar.source_lineage_sha256,
        "source_row_sha256": source_bar.source_row_sha256,
        "future_row_sha256": future_bar.source_row_sha256,
        "source_data_through": source_bar.data_through.astimezone(SHANGHAI).isoformat(),
        "future_data_through": future_bar.data_through.astimezone(SHANGHAI).isoformat(),
        "future_observed_at": future_bar.observed_at.astimezone(SHANGHAI).isoformat(),
        "gross_return": round(gross_return, 10),
        "net_return": round(net_return, 10),
        "costs": costs,
        "feature_sha256": feature.sha256,
        "strategy_id": MODEL_ID,
        "strategy_version": MODEL_VERSION,
        "baseline_net_return": 0.0,
        "net_return_delta_vs_no_trade": round(net_return, 10),
    }
    result = {
        "schema": FORWARD_LABEL_SCHEMA,
        "observation_key": sample_key,
        "horizon": horizon,
        "target_slot": target.astimezone(SHANGHAI).isoformat(),
        "requested_symbols": [symbol],
        "resolved_symbols": [symbol],
        "missing_symbols": [],
        "sample_count": 1,
        "resolved_count": 1,
        "pending": 0,
        "excluded": 0,
        "evaluated_status": "exploratory_insufficient_edge",
        "hit": net_return > 0,
        "abstention": False,
        "status": "usable_degraded",
        "source": {
            "dataset_id": source_proof.dataset_id,
            "provider": source_proof.provider,
            "execution_id": source_proof.execution_id,
            "config_hash": source_proof.config_hash,
            "receipt_ids": [source_bar.receipt_id, future_bar.receipt_id],
        },
        "labels": [label],
        "outcome": {
            "sample_count": 1,
            "resolved_count": 1,
            "pending": 0,
            "excluded": 0,
            "evaluated_status": "exploratory_insufficient_edge",
            "hit": net_return > 0,
            "abstention": False,
            "training_sample_count": 0,
            "training_eligible": False,
        },
        "shadow_suggestion": {
            "action": "retain_for_more_evidence" if net_return > 0 else "downweight",
            "reason": "single_symbol_exploratory_label",
            "risk_authority": False,
            "promotion_authority": False,
            "execution_authority": False,
            "real_trading_enabled": False,
        },
        "pit": {
            "decision_as_of": as_of.astimezone(SHANGHAI).isoformat(),
            "segment_id": (
                f"{source_bar.bar_end.astimezone(SHANGHAI).date().isoformat()}"
                f":{source_bar.market_session}:{source_bar.bar_end.astimezone(SHANGHAI).isoformat()}"
            ),
        },
    }
    result["artifact_sha256"] = _observation_sha(result)
    return result


def _observation_stamp(value: object, reason: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SHANGHAI)
        return parsed
    if not isinstance(value, str) or not value.strip():
        raise MinuteOfflineLearningError(reason)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MinuteOfflineLearningError(reason) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed


def _observation_sha(value: object) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise MinuteOfflineLearningError("minute_learning_payload_invalid") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_bundle_bytes(path: Path) -> bytes:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise MinuteOfflineLearningError("minute_learning_bundle_invalid")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise MinuteOfflineLearningError("minute_learning_bundle_invalid") from exc


def _load_bundle_mapping(path: Path) -> Mapping[str, Any]:
    try:
        raw = json.loads(_load_bundle_bytes(path))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinuteOfflineLearningError("minute_learning_bundle_invalid") from exc
    if not isinstance(raw, Mapping):
        raise MinuteOfflineLearningError("minute_learning_bundle_invalid")
    return raw


def _readiness_policy() -> Mapping[str, Any]:
    try:
        policy = load_evidence_readiness_contract().market_policies["ashare"]
        local = policy["local_contiguous_learning"]
    except (KeyError, ValueError) as exc:
        raise MinuteOfflineLearningError(
            "minute_learning_readiness_contract_invalid"
        ) from exc
    if (
        not isinstance(local, Mapping)
        or local.get("allowed") is not True
        or local.get("minimum_slots_source")
        != "preregistered_feature_and_label_profile"
        or local.get("gap_crossing_allowed") is not False
        or local.get("full_session_required") is not False
    ):
        raise MinuteOfflineLearningError("minute_learning_readiness_contract_invalid")
    return local


def _contiguous_segments(
    *, expected_slots: list[str], observed_slots: list[str]
) -> tuple[tuple[str, ...], ...]:
    observed = set(observed_slots)
    segments: list[tuple[str, ...]] = []
    current: list[str] = []
    previous: datetime | None = None
    for slot in expected_slots:
        if slot in observed:
            try:
                stamp = datetime.strptime(slot, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=SHANGHAI
                )
            except ValueError as exc:
                raise MinuteOfflineLearningError(
                    "minute_learning_coverage_invalid"
                ) from exc
            if (
                current
                and previous is not None
                and stamp - previous != timedelta(minutes=5)
            ):
                segments.append(tuple(current))
                current = []
            current.append(slot)
            previous = stamp
        elif current:
            segments.append(tuple(current))
            current = []
            previous = None
    if current:
        segments.append(tuple(current))
    return tuple(segments)


def _labelled_slots(bundle: Mapping[str, Any]) -> tuple[str, ...]:
    """Accept only an explicit fixture label receipt; absence stays blocked."""

    raw = bundle.get("local_contiguous_label_evidence")
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise MinuteOfflineLearningError("minute_learning_label_evidence_invalid")
    receipt = raw.get("receipt_sha256")
    slots = raw.get("labelled_bar_ends")
    if (
        raw.get("profile_id") != LOCAL_CONTIGUOUS_LEARNING_PROFILE.profile_id
        or raw.get("status") != "complete_fixture_label_evidence"
        or not isinstance(receipt, str)
        or len(receipt) != 64
        or any(value not in _SHA256_HEX for value in receipt)
        or not isinstance(slots, list)
        or not slots
        or any(not isinstance(slot, str) or not slot.strip() for slot in slots)
        or len(slots) != len(set(slots))
    ):
        raise MinuteOfflineLearningError("minute_learning_label_evidence_invalid")
    return tuple(slots)


def _local_contiguous_learning(
    *, bundle: Mapping[str, Any], report: Mapping[str, Any]
) -> dict[str, Any]:
    _readiness_policy()
    expected = report.get("expected_bar_slots")
    observed = report.get("observed_bar_slots")
    if (
        not isinstance(expected, list)
        or not isinstance(observed, list)
        or any(not isinstance(slot, str) for slot in (*expected, *observed))
    ):
        raise MinuteOfflineLearningError("minute_learning_coverage_invalid")
    labelled = set(_labelled_slots(bundle))
    segments = _contiguous_segments(expected_slots=expected, observed_slots=observed)
    eligible_ends = [
        segment[-1]
        for segment in segments
        if len(segment) >= LOCAL_CONTIGUOUS_LEARNING_PROFILE.minimum_slots
        and segment[-1] in labelled
    ]
    blockers: list[str] = []
    if not any(
        len(segment) >= LOCAL_CONTIGUOUS_LEARNING_PROFILE.minimum_slots
        for segment in segments
    ):
        blockers.append("local_contiguous_window_too_short")
    if not labelled:
        blockers.append("local_preregistered_label_evidence_missing")
    elif not eligible_ends:
        blockers.append("local_preregistered_label_evidence_incomplete")
    evidence = report.get("evidence")
    reconciliation = report.get("reconciliation_status")
    if (
        not isinstance(evidence, Mapping)
        or evidence.get("rejected_count") != 0
        or evidence.get("receipt_history_complete") is not True
    ):
        blockers.append("local_fixture_evidence_incomplete")
    if not isinstance(reconciliation, Mapping) or any(
        value != "fixture_reconciled" for value in reconciliation.values()
    ):
        blockers.append("local_fixture_reconciliation_incomplete")
    return {
        "status": "eligible_for_offline_review"
        if eligible_ends and not blockers
        else "blocked",
        "local_learning_eligible": bool(eligible_ends and not blockers),
        "profile_id": LOCAL_CONTIGUOUS_LEARNING_PROFILE.profile_id,
        "label_kind": LOCAL_CONTIGUOUS_LEARNING_PROFILE.label_kind,
        "feature_slots": LOCAL_CONTIGUOUS_LEARNING_PROFILE.feature_slots,
        "label_horizon_slots": LOCAL_CONTIGUOUS_LEARNING_PROFILE.label_horizon_slots,
        "minimum_slots": LOCAL_CONTIGUOUS_LEARNING_PROFILE.minimum_slots,
        "gap_crossing_allowed": False,
        "full_session_required": False,
        "contiguous_segment_lengths": [len(segment) for segment in segments],
        "eligible_window_end_slots": eligible_ends,
        "blockers": blockers,
    }


def build_minute_observation_outcome(
    *,
    canary_receipt: Mapping[str, Any],
    profile: MinuteDatasetProfile,
    decision_as_of: datetime | str,
) -> dict[str, Any]:
    """Build one durable, non-training observation from an exact canary receipt.

    The canary artifact is the only input authority here.  Its immutable
    per-row receipt proofs are reconstructed before any fields are persisted;
    no forward label is inferred when the target horizon is still open.
    """

    if not isinstance(canary_receipt, Mapping) or not isinstance(
        profile, MinuteDatasetProfile
    ):
        raise MinuteOfflineLearningError("minute_observation_input_invalid")
    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise MinuteOfflineLearningError("real_trading_must_remain_disabled")
    try:
        snapshot = snapshot_from_canary_receipt(canary_receipt, profile=profile)
    except (MinuteCanaryConfigurationError, ValueError) as exc:
        raise MinuteOfflineLearningError("minute_observation_receipt_invalid") from exc

    receipt = canary_receipt
    if (
        receipt.get("requested_count") != 30
        or receipt.get("accepted_count") != 30
        or receipt.get("missing_count") != 0
        or receipt.get("row_count") != 30
        or receipt.get("quality_status") != "usable"
        or receipt.get("lineage_complete") is not True
        or receipt.get("authority_tier") != "observation_only"
        or receipt.get("real_trading_enabled") is not False
    ):
        raise MinuteOfflineLearningError("minute_observation_exact_30_required")
    requested = receipt.get("requested_symbols")
    accepted = receipt.get("accepted_symbols")
    if (
        not isinstance(requested, list)
        or not isinstance(accepted, list)
        or len(requested) != 30
        or len(accepted) != 30
        or len(set(requested)) != 30
        or len(set(accepted)) != 30
        or set(requested) != set(accepted)
        or set(accepted) != {bar.symbol for bar in snapshot.bars}
    ):
        raise MinuteOfflineLearningError("minute_observation_universe_mismatch")

    as_of = (
        decision_as_of
        if isinstance(decision_as_of, datetime)
        else _observation_stamp(decision_as_of, "minute_observation_decision_as_of_invalid")
    )
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=SHANGHAI)
    bars = snapshot.bars
    if len(bars) != 30:
        raise MinuteOfflineLearningError("minute_observation_exact_30_required")
    decision_times = {bar.decision_time for bar in bars}
    bar_ends = {bar.bar_end for bar in bars}
    data_through = {bar.data_through for bar in bars}
    sessions = {bar.market_session for bar in bars}
    if (
        len(decision_times) != 1
        or len(bar_ends) != 1
        or len(data_through) != 1
        or len(sessions) != 1
        or any(bar.observed_at > as_of for bar in bars)
        or any(bar.data_through > bar.observed_at for bar in bars)
        or any(bar.decision_time > as_of for bar in bars)
    ):
        raise MinuteOfflineLearningError("minute_observation_pit_or_segment_invalid")
    proof_summary = snapshot.validated_proof_summary
    if proof_summary is None or set(proof_summary.receipt_ids) != {
        bar.receipt_id for bar in bars
    }:
        raise MinuteOfflineLearningError("minute_observation_proof_binding_invalid")
    if len(proof_summary.receipt_ids) < 1:
        raise MinuteOfflineLearningError("minute_observation_proof_binding_invalid")

    decision_time = next(iter(decision_times))
    bar_end = next(iter(bar_ends))
    data_through_stamp = next(iter(data_through))
    segment_id = (
        f"{bar_end.astimezone(SHANGHAI).date().isoformat()}"
        f":{next(iter(sessions))}:{bar_end.astimezone(SHANGHAI).isoformat()}"
    )
    rows = [
        {
            "symbol": bar.symbol,
            "bar_end": bar.bar_end.astimezone(SHANGHAI).isoformat(),
            "decision_time": bar.decision_time.astimezone(SHANGHAI).isoformat(),
            "receipt_id": bar.receipt_id,
            "data_through": bar.data_through.astimezone(SHANGHAI).isoformat(),
            "observed_at": bar.observed_at.astimezone(SHANGHAI).isoformat(),
            "source_lineage_sha256": bar.source_lineage_sha256,
            "envelope_proof_sha256": bar.envelope_proof_sha256,
            "source_row_sha256": bar.source_row_sha256,
            "bar_sha256": bar.sha256,
        }
        for bar in bars
    ]
    source = {
        "dataset_id": proof_summary.dataset_id,
        "provider": proof_summary.provider,
        "execution_id": proof_summary.execution_id,
        "config_hash": proof_summary.config_hash,
        "data_through": data_through_stamp.astimezone(SHANGHAI).isoformat(),
        "receipt_ids": list(proof_summary.receipt_ids),
        "content_sha256": proof_summary.content_sha256,
    }
    observation = {
        "schema": OBSERVATION_OUTCOME_SCHEMA,
        "observation_key": "",
        "trading_date": bar_end.astimezone(SHANGHAI).date().isoformat(),
        "segment_id": segment_id,
        "decision_time": decision_time.astimezone(SHANGHAI).isoformat(),
        "decision_as_of": as_of.astimezone(SHANGHAI).isoformat(),
        "evidence_use": bars[0].evidence_use.value,
        "observation": {
            "requested_count": 30,
            "accepted_count": 30,
            "row_count": 30,
            "lineage_complete": True,
            "snapshot_sha256": receipt.get("snapshot_sha256"),
            "pagination_trace_sha256": snapshot.pagination_trace_sha256,
            "first_semantic_sha256": snapshot.first_semantic_sha256,
            "replay_semantic_sha256": snapshot.replay_semantic_sha256,
            "proof": source,
            "rows": rows,
        },
        "outcome": {
            "status": "pending_forward_labels",
            "forward_label_state": "blocked_missing_authoritative_forward_labels",
            "planned_horizons": list(FORWARD_LABEL_HORIZONS),
            "training_sample_count": 0,
            "training_eligible": False,
            "labels_appended": 0,
        },
        "authority": {
            "observation_authority": False,
            "durable_observation": True,
            "training_authority": False,
            "promotion_authority": False,
            "execution_authority": False,
            "real_trading_enabled": False,
        },
    }
    digest_payload = dict(observation)
    digest_payload["observation_key"] = None
    observation["observation_key"] = _observation_sha(digest_payload)
    return observation


def build_minute_offline_learning_projection(
    *, state_bundle: Path | str
) -> dict[str, Any]:
    """Build a secret-free, non-authoritative daily learning projection."""

    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise MinuteOfflineLearningError("real_trading_must_remain_disabled")
    path = Path(state_bundle)
    bundle_bytes = _load_bundle_bytes(path)
    bundle_sha256 = _sha256_bytes(bundle_bytes)
    bundle = _load_bundle_mapping(path)
    try:
        report = build_minute_day_report(state_bundle=path)
    except MinuteDayReportError as exc:
        raise MinuteOfflineLearningError("minute_learning_report_invalid") from exc
    authority = report.get("authority")
    if not isinstance(authority, Mapping) or any(
        authority.get(key) is not False
        for key in (
            "execution_authority",
            "training_authority",
            "promotion_authority",
            "real_trading_enabled",
        )
    ):
        raise MinuteOfflineLearningError("minute_learning_authority_invalid")
    missing = report.get("missing_bar_slots")
    if not isinstance(missing, list) or any(
        not isinstance(item, str) for item in missing
    ):
        raise MinuteOfflineLearningError("minute_learning_coverage_invalid")
    evidence = report.get("evidence")
    if not isinstance(evidence, Mapping):
        raise MinuteOfflineLearningError("minute_learning_evidence_invalid")
    rejected_count = evidence.get("rejected_count")
    receipt_history_complete = evidence.get("receipt_history_complete")
    if (
        isinstance(rejected_count, bool)
        or not isinstance(rejected_count, int)
        or rejected_count < 0
        or not isinstance(receipt_history_complete, bool)
    ):
        raise MinuteOfflineLearningError("minute_learning_evidence_invalid")
    reconciliation = report.get("reconciliation_status")
    if not isinstance(reconciliation, Mapping) or not reconciliation:
        raise MinuteOfflineLearningError("minute_learning_reconciliation_invalid")
    reconciliation_complete = all(
        value == "fixture_reconciled" for value in reconciliation.values()
    )
    differences = report.get("shadow_book_differences")
    if not isinstance(differences, Mapping):
        raise MinuteOfflineLearningError("minute_learning_counterfactual_invalid")
    learning_key = f"{report['trading_date']}:{bundle_sha256}"
    blockers: list[str] = []
    if missing:
        blockers.append("fixture_session_incomplete")
    if rejected_count:
        blockers.append("fixture_evidence_rejected")
    if not receipt_history_complete:
        blockers.append("fixture_receipt_history_incomplete")
    if not reconciliation_complete:
        blockers.append("fixture_reconciliation_incomplete")
    complete = not blockers
    local_learning = _local_contiguous_learning(bundle=bundle, report=report)
    return {
        "schema": SCHEMA,
        "learning_key": learning_key,
        "trading_date": report["trading_date"],
        "capital_layer": "simulated",
        "account_type": "simulated",
        "source": {
            "state_bundle_sha256": bundle_sha256,
            "authority_tier": "non_production_fixture",
            "report_contract": "Ashare.minute_day_report",
        },
        "status": "complete_fixture_projection" if complete else "blocked",
        "blockers": blockers,
        "coverage": {
            "expected_bar_count": len(report["expected_bar_slots"]),
            "observed_bar_count": len(report["observed_bar_slots"]),
            "missing_bar_count": len(missing),
        },
        "sample_summary": {
            "fixture_observation_count": len(report["observed_bar_slots"]),
            "candidate_count": report["candidate_and_rejections"]["candidate_count"],
            "simulated_fill_count": report["simulated_execution"]["simulated_fills"],
            "simulated_not_filled_count": report["simulated_execution"][
                "simulated_not_filled"
            ],
            "training_sample_count": 0,
            "training_eligible": False,
            "promotion_eligible": False,
        },
        "kpi": {
            "fees_cny": report["simulated_execution"]["fees_cny"],
            "reconciliation_status": dict(reconciliation),
            "rejection_reason_counts": dict(
                report["candidate_and_rejections"]["rejection_reason_counts"]
            ),
        },
        "calibration": {
            "status": "blocked_missing_forward_labels",
            "calibrated_probability": None,
            "expected_return_bps": None,
            "reason": "fixture_delayed_paper_has_no_forward_label_authority",
        },
        "forward_label_state": {
            "status": "blocked_missing_authoritative_daily_receipt",
            "planned_horizons": ["m30", "m60", "close", "1d", "3d", "5d"],
            "fixture_candidate_count": report["candidate_and_rejections"][
                "candidate_count"
            ],
            "labels_appended": 0,
            "authoritative_market_data_consumed": False,
        },
        "local_contiguous_learning": local_learning,
        "missed_opportunities": {
            "status": "counterfactual_only",
            "by_sleeve": {name: dict(value) for name, value in differences.items()},
        },
        "challenger": {
            "recommendation": "observe_only",
            "challenger_eligible": False,
            "reason": "no_calibration_or_oos_label_authority",
        },
        "authority": {
            "capital_authority": False,
            "execution_authority": False,
            "training_authority": False,
            "promotion_authority": False,
            "durable": False,
            "automatic_model_change_enabled": False,
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
            "real_trading_enabled": False,
        },
    }


def _validate_root(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise MinuteOfflineLearningError("minute_learning_root_invalid")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as exc:
        raise MinuteOfflineLearningError("minute_learning_root_invalid") from exc


def _existing_keys(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise MinuteOfflineLearningError("minute_learning_journal_invalid")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            value = json.loads(line)
            if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
                raise MinuteOfflineLearningError("minute_learning_journal_invalid")
            key = value.get("learning_key")
            source = value.get("source")
            sha = (
                source.get("state_bundle_sha256")
                if isinstance(source, Mapping)
                else None
            )
            if not isinstance(key, str) or not isinstance(sha, str):
                raise MinuteOfflineLearningError("minute_learning_journal_invalid")
            if key in values and values[key] != sha:
                raise MinuteOfflineLearningError("minute_learning_journal_conflict")
            values[key] = sha
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinuteOfflineLearningError("minute_learning_journal_invalid") from exc
    return values


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = (_canonical_json(dict(value)) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise MinuteOfflineLearningError(
            "minute_learning_projection_persist_failed"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _exclusive_lock(root: Path) -> Iterator[None]:
    lock_path = root / ".minute-fixture-learning.lock"
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise MinuteOfflineLearningError("minute_learning_lock_failed") from exc
    try:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MinuteOfflineLearningError("minute_learning_already_running") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def write_minute_offline_learning_projection(
    *, state_bundle: Path | str, learning_root: Path | str
) -> dict[str, Any]:
    """Append one immutable projection and refresh a rebuildable latest view."""

    projection = build_minute_offline_learning_projection(state_bundle=state_bundle)
    root = Path(learning_root)
    _validate_root(root)
    journal = root / JOURNAL_NAME
    with _exclusive_lock(root):
        existing = _existing_keys(journal)
        key = projection["learning_key"]
        source_sha = projection["source"]["state_bundle_sha256"]
        if key in existing:
            if existing[key] != source_sha:
                raise MinuteOfflineLearningError("minute_learning_journal_conflict")
            _atomic_json(root / LATEST_NAME, projection)
            return {"appended": False, "projection": projection}
        encoded = (_canonical_json(projection) + "\n").encode("utf-8")
        try:
            descriptor = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "ab") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(journal, 0o600)
        except OSError as exc:
            raise MinuteOfflineLearningError(
                "minute_learning_journal_persist_failed"
            ) from exc
        _atomic_json(root / LATEST_NAME, projection)
    return {"appended": True, "projection": projection}


def _existing_observation_keys(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise MinuteOfflineLearningError("minute_observation_journal_invalid")
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if (
                not isinstance(value, Mapping)
                or value.get("schema") != OBSERVATION_OUTCOME_SCHEMA
                or not isinstance(value.get("observation_key"), str)
            ):
                raise MinuteOfflineLearningError("minute_observation_journal_invalid")
            key = value["observation_key"]
            digest = _observation_sha({**dict(value), "observation_key": None})
            if key != digest:
                raise MinuteOfflineLearningError("minute_observation_journal_invalid")
            if key in values and values[key] != digest:
                raise MinuteOfflineLearningError("minute_observation_journal_conflict")
            values[key] = digest
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinuteOfflineLearningError("minute_observation_journal_invalid") from exc
    return values


def write_minute_observation_outcome(
    *,
    canary_receipt: Mapping[str, Any],
    profile: MinuteDatasetProfile,
    decision_as_of: datetime | str,
    learning_root: Path | str,
) -> dict[str, Any]:
    """Append one validated exact observation and refresh its latest view."""

    observation = build_minute_observation_outcome(
        canary_receipt=canary_receipt,
        profile=profile,
        decision_as_of=decision_as_of,
    )
    root = Path(learning_root)
    _validate_root(root)
    journal = root / OBSERVATION_OUTCOME_JOURNAL_NAME
    with _exclusive_lock(root):
        existing = _existing_observation_keys(journal)
        key = observation["observation_key"]
        digest = _observation_sha({**observation, "observation_key": None})
        if key in existing:
            if existing[key] != digest:
                raise MinuteOfflineLearningError("minute_observation_journal_conflict")
            _atomic_json(root / OBSERVATION_OUTCOME_LATEST_NAME, observation)
            return {"appended": False, "observation": observation}
        encoded = (_canonical_json(observation) + "\n").encode("utf-8")
        try:
            descriptor = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "ab") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(journal, 0o600)
        except OSError as exc:
            raise MinuteOfflineLearningError(
                "minute_observation_journal_persist_failed"
            ) from exc
        _atomic_json(root / OBSERVATION_OUTCOME_LATEST_NAME, observation)
    return {"appended": True, "observation": observation}


append_minute_observation_outcome = write_minute_observation_outcome


def state_bundle_for_current_session(*, state_root: Path | str) -> Path:
    """Resolve today's bundle without scanning or modifying a state root."""

    root = Path(state_root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise MinuteOfflineLearningError("minute_learning_state_root_invalid")
    day_root = root / datetime.now(tz=SHANGHAI).strftime("%Y%m%d")
    if day_root.is_symlink() or not day_root.is_dir():
        raise MinuteOfflineLearningError("minute_learning_bundle_invalid")
    bundle = day_root / "state-bundle.json"
    if bundle.is_symlink() or not bundle.is_file():
        raise MinuteOfflineLearningError("minute_learning_bundle_invalid")
    return bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Project one A-share fixture session offline"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--state-bundle", type=Path)
    source.add_argument("--state-root", type=Path)
    parser.add_argument("--learning-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        bundle = (
            args.state_bundle
            if args.state_bundle is not None
            else state_bundle_for_current_session(state_root=args.state_root)
        )
        result = write_minute_offline_learning_projection(
            state_bundle=bundle, learning_root=args.learning_root
        )
    except MinuteOfflineLearningError:
        print("minute offline learning failed closed", file=sys.stderr)
        return 2
    print(_canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "JOURNAL_NAME",
    "LATEST_NAME",
    "OBSERVATION_OUTCOME_JOURNAL_NAME",
    "OBSERVATION_OUTCOME_LATEST_NAME",
    "OBSERVATION_OUTCOME_SCHEMA",
    "FORWARD_LABEL_SCHEMA",
    "MinuteOfflineLearningError",
    "build_minute_forward_label",
    "build_minute_observation_outcome",
    "build_minute_offline_learning_projection",
    "state_bundle_for_current_session",
    "write_minute_observation_outcome",
    "append_minute_observation_outcome",
    "write_minute_offline_learning_projection",
]
