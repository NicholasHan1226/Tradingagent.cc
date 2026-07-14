#!/usr/bin/env python3
"""Persist the sim-only A-share sample learning read model.

The append-only ``SampleJournal`` remains the only sample fact source.  This
operation materializes due forward labels, writes a reproducible KPI
projection, records the manual-only evolution assessment, and persists the
A-share maturity state.  It has no broker, order, email, capital creation, or
live transition path.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from time import perf_counter, process_time
from typing import Any, Mapping, Optional, Sequence
from uuid import uuid4

from Ashare.evolution_controller import (
    build_evolution_decision,
)
from shared.review.market_maturity import AshareEvidence, assess_ashare_maturity
from shared.review.forward_labels import validate_point_in_time_lineage
from shared.review.sample_journal import (
    JournalConflictError,
    JournalSafetyError,
    SampleJournal,
)
from shared.review.projection_generation import (
    CURRENT_MANIFEST,
    ProjectionGenerationError,
    publish_projection_generation,
)
from shared.runtime_test.ashare_forward_label_ops import (
    DEFAULT_BACKLOG_WINDOW_DAYS,
    ForwardLabelOpsSafetyError,
    _assert_sim_only,
    _find_live_marker,
    run_ashare_forward_label_backlog,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_JOURNAL_PATH = ROOT / "shared" / "review" / "ashare" / "sample_journal.jsonl"
DEFAULT_REVIEW_DIR = ROOT / "shared" / "review" / "ashare"
CN_TZ = timezone(timedelta(hours=8))

KPI_LATEST = "sample_kpi_latest.json"
KPI_LOG = "sample_kpi_log.jsonl"
MATURITY_LATEST = "market_maturity_latest.json"
MATURITY_LOG = "market_maturity_log.jsonl"


class AshareSampleOpsError(RuntimeError):
    """Base error for the unified A-share sample operation."""


class AshareSampleOpsSafetyError(AshareSampleOpsError):
    """Raised before output writes when a live or unsafe path is detected."""


def _compact_trade_date(value: Any) -> str:
    raw = str(value or "").strip()
    digits = "".join(character for character in raw[:10] if character.isdigit())
    compact = digits[:8]
    if len(compact) != 8:
        raise ValueError("trade_date must be YYYYMMDD or YYYY-MM-DD")
    try:
        datetime.strptime(compact, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("trade_date is not a valid calendar date") from exc
    return compact


def _parse_as_of(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("as_of is required")
        try:
            parsed = datetime.fromisoformat(
                raw.replace(" ", "T", 1).replace("Z", "+00:00")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("as_of must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _check_no_symlink(path: Path, *, label: str) -> None:
    """Reject an existing symlink at the path or any existing ancestor."""

    current = path.absolute()
    while True:
        if current.is_symlink():
            raise AshareSampleOpsSafetyError(
                "%s_symlink_not_allowed:%s" % (label, current)
            )
        if current == current.parent:
            break
        current = current.parent


def _review_output_paths(review_dir: Path) -> tuple[Path, ...]:
    return (
        review_dir / KPI_LATEST,
        review_dir / KPI_LOG,
        review_dir / "evolution_decision_latest.json",
        review_dir / "evolution_decision_log.jsonl",
        review_dir / MATURITY_LATEST,
        review_dir / MATURITY_LOG,
        review_dir / CURRENT_MANIFEST,
        review_dir / "projection_generations",
    )


def _assert_output_paths_safe(review_dir: Path) -> None:
    _check_no_symlink(review_dir, label="review_dir")
    for path in _review_output_paths(review_dir):
        _check_no_symlink(path, label=path.name)


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def _projection_input_sha256(
    events: Sequence[Mapping[str, Any]], *, data_as_of: str
) -> str:
    event_fingerprints: list[str] = []
    for sequence, event in enumerate(events, start=1):
        fingerprint = str(event.get("journal_payload_sha256") or "").strip().lower()
        if not _is_sha256(fingerprint):
            raise JournalSafetyError(
                "projection input event %d has no canonical fingerprint" % sequence
            )
        event_fingerprints.append(fingerprint)
    payload = {
        "data_as_of": data_as_of,
        "journal_event_fingerprints": event_fingerprints,
    }
    return sha256(_canonical_json_bytes(payload)).hexdigest()


@contextmanager
def _measure_stage(metrics: dict[str, Any], name: str):
    wall_started = perf_counter()
    cpu_started = process_time()
    try:
        yield
    finally:
        metrics[name] = {
            "wall_seconds": round(perf_counter() - wall_started, 6),
            "cpu_seconds": round(process_time() - cpu_started, 6),
        }


def _in_authority(record: Mapping[str, Any], scope: Mapping[str, Any]) -> bool:
    return (
        record.get("capital_authority_id") == scope.get("capital_authority_id")
        and record.get("authority_generation") == scope.get("authority_generation")
        and record.get("execution_lineage_id") == scope.get("execution_lineage_id")
    )


class _AuthorityScopedJournal:
    """Expose only current-authority facts while appending to the real journal."""

    def __init__(self, journal: SampleJournal, scope: Mapping[str, Any]) -> None:
        self._journal = journal
        self._scope = dict(scope)

    def read_events(self) -> list[dict[str, Any]]:
        return [
            event
            for event in self._journal.read_events()
            if _in_authority(event, self._scope)
        ]

    def materialize_labels(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._journal.materialize_labels(*args, **kwargs)


def _record_trade_date(record: Mapping[str, Any]) -> str:
    raw = str(record.get("trade_date") or "").strip()
    digits = "".join(character for character in raw[:10] if character.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    raw_timestamp = str(
        record.get("prediction_at")
        or record.get("event_at")
        or record.get("timestamp")
        or ""
    ).strip()
    try:
        parsed = datetime.fromisoformat(
            raw_timestamp.replace(" ", "T", 1).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ).strftime("%Y%m%d")


def _is_number(value: Any, *, nonnegative: bool = False) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    parsed = float(value)
    if not math.isfinite(parsed):
        return False
    return not nonnegative or parsed >= 0.0


def _is_sha256(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return len(raw) == 64 and all(character in "0123456789abcdef" for character in raw)


def _layers(record: Mapping[str, Any]) -> set[str]:
    values = record.get("sample_layers")
    layers = (
        {str(value or "").strip().lower() for value in values if value}
        if isinstance(values, (list, tuple, set))
        else set()
    )
    raw = str(record.get("sample_layer") or "").strip().lower()
    if raw:
        layers.add(raw)
    return layers


def _ready_forward_label_count(kpi: Mapping[str, Any]) -> int:
    total = 0
    styles = kpi.get("styles")
    for style in styles.values() if isinstance(styles, Mapping) else ():
        if not isinstance(style, Mapping):
            continue
        counts = style.get("forward_label_counts")
        for statuses in counts.values() if isinstance(counts, Mapping) else ():
            if not isinstance(statuses, Mapping):
                continue
            total += int(statuses.get("ready") or 0)
            total += int(statuses.get("labeled") or 0)
    return total


def _scientific_evidence(
    records: Sequence[Mapping[str, Any]], kpi: Mapping[str, Any]
) -> dict[str, Any]:
    all_predictions = [
        record
        for record in records
        if record.get("journal_event_type") == "prediction_snapshot"
    ]
    # Data-quality rejected observations remain immutable audit evidence, but
    # must not permanently poison the scientific gate for later valid samples
    # in the same authority generation.
    predictions = [
        record
        for record in all_predictions
        if record.get("forward_label_eligibility") == "eligible"
    ]
    fills = [
        record
        for record in records
        if "exploration_fill" in _layers(record)
        or "exploitation_fill" in _layers(record)
    ]
    round_trips = [
        record for record in records if "completed_round_trip" in _layers(record)
    ]
    prediction_pit = [validate_point_in_time_lineage(record) for record in predictions]
    primary_label_pit: list[dict[str, Any]] = []
    for record in predictions:
        primary_horizon = str(record.get("primary_label_horizon") or "").strip()
        labels = record.get("labels")
        label = labels.get(primary_horizon) if isinstance(labels, Mapping) else None
        if not isinstance(label, Mapping) or str(label.get("status") or "") not in {
            "ready",
            "labeled",
        }:
            continue
        raw_lineage = label.get("point_in_time_lineage")
        timestamps = (
            raw_lineage.get("timestamps")
            if isinstance(raw_lineage, Mapping)
            and isinstance(raw_lineage.get("timestamps"), Mapping)
            else {}
        )
        primary_label_pit.append(
            validate_point_in_time_lineage(
                {
                    **dict(timestamps),
                    "labels_as_of": record.get("labels_as_of"),
                }
            )
        )

    point_in_time_lineage_complete = (
        bool(predictions)
        and bool(primary_label_pit)
        and all(result.get("complete") is True for result in prediction_pit)
        and all(result.get("complete") is True for result in primary_label_pit)
        and all(
            str(record.get("point_in_time_as_of") or "").strip()
            and str(record.get("execution_lineage_id") or "").strip()
            and (
                _is_sha256(record.get("source_snapshot_sha256"))
                or _is_sha256(record.get("base_snapshot_sha256"))
            )
            for record in predictions
        )
    )
    costs_evidence_complete = bool(round_trips) and all(
        record.get("round_trip_complete") is True
        and record.get("execution_eligible") is True
        and str(record.get("costs_cover") or "") == "round_trip"
        and _is_number(record.get("gross_pnl_cny"))
        and (
            _is_number(record.get("net_pnl_cny"))
            or _is_number(record.get("post_cost_pnl_cny"))
        )
        and _is_number(record.get("fee_cny"), nonnegative=True)
        and _is_number(record.get("slippage_cny"), nonnegative=True)
        for record in round_trips
    )
    fill_evidence_revalidated = bool(fills) and all(
        record.get("execution_eligible") is True
        and (
            record.get("fill_evidence_revalidated") is True
            or str(record.get("fill_evidence_status") or "").strip().lower()
            == "revalidated"
        )
        for record in fills
    )
    duplicate_cluster_control_passed = (
        bool(records) and int(kpi.get("maturity_duplicate_count") or 0) == 0
    )
    calibration = kpi.get("calibration_evidence")
    calibration_metrics = calibration if isinstance(calibration, Mapping) else {}
    calibration_evidence_sufficient = calibration_metrics.get("sufficient") is True
    sample_size = kpi.get("sample_size_evidence")
    sample_size_metrics = sample_size if isinstance(sample_size, Mapping) else {}
    drawdown = kpi.get("account_drawdown_evidence")
    drawdown_metrics = drawdown if isinstance(drawdown, Mapping) else {}
    base = {
        "point_in_time_lineage_complete": point_in_time_lineage_complete,
        "costs_evidence_complete": costs_evidence_complete,
        "fill_evidence_revalidated": fill_evidence_revalidated,
        "duplicate_cluster_control_passed": duplicate_cluster_control_passed,
        "calibration_evidence_sufficient": calibration_evidence_sufficient,
        "prediction_pit_valid_count": sum(
            1 for result in prediction_pit if result.get("complete") is True
        ),
        "prediction_pit_total_count": len(prediction_pit),
        "primary_label_pit_valid_count": sum(
            1 for result in primary_label_pit if result.get("complete") is True
        ),
        "primary_label_pit_total_count": len(primary_label_pit),
        "calibration_metrics": deepcopy(dict(calibration_metrics)),
        "sample_size_evidence": deepcopy(dict(sample_size_metrics)),
        "account_drawdown_evidence": deepcopy(dict(drawdown_metrics)),
    }
    layer_totals = kpi.get("sample_layer_totals")
    completed_count = (
        int(layer_totals.get("completed_round_trip") or 0)
        if isinstance(layer_totals, Mapping)
        else 0
    )
    positive_style_count = _positive_expectancy_style_count(kpi)
    base["promotion_evidence_ready"] = (
        all(base.values())
        and completed_count >= 10
        and int(sample_size_metrics.get("unique_decision_cluster_count") or 0) >= 20
        and float(sample_size_metrics.get("N_eff") or 0.0) >= 10.0
        and drawdown_metrics.get("status") == "available"
        and positive_style_count >= 1
    )
    base["prediction_audit_total_count"] = len(all_predictions)
    base["prediction_data_quality_excluded_count"] = len(all_predictions) - len(
        predictions
    )
    return base


def _positive_expectancy_style_count(kpi: Mapping[str, Any]) -> int:
    count = 0
    styles = kpi.get("styles")
    for style in styles.values() if isinstance(styles, Mapping) else ():
        if not isinstance(style, Mapping):
            continue
        performance = style.get("performance_by_sample_intent")
        exploitation = (
            performance.get("exploitation")
            if isinstance(performance, Mapping)
            else None
        )
        if (
            isinstance(exploitation, Mapping)
            and _is_number(exploitation.get("expectancy_cny"))
            and float(exploitation["expectancy_cny"]) > 0.0
        ):
            count += 1
    return count


def _exploitation_performance(kpi: Mapping[str, Any]) -> dict[str, Optional[float]]:
    completed = 0
    weighted_win_rate = 0.0
    weighted_expectancy = 0.0
    post_cost_pnl = 0.0
    styles = kpi.get("styles")
    for style in styles.values() if isinstance(styles, Mapping) else ():
        if not isinstance(style, Mapping):
            continue
        by_intent = style.get("performance_by_sample_intent")
        metrics = (
            by_intent.get("exploitation") if isinstance(by_intent, Mapping) else None
        )
        if not isinstance(metrics, Mapping):
            continue
        count = int(metrics.get("completed_round_trip_count") or 0)
        if count <= 0:
            continue
        completed += count
        weighted_win_rate += float(metrics.get("win_rate") or 0.0) * count
        weighted_expectancy += float(metrics.get("expectancy_cny") or 0.0) * count
        post_cost_pnl += float(metrics.get("post_cost_pnl_cny") or 0.0)
    if completed <= 0:
        return {
            "win_rate": None,
            "expectancy_cny": None,
            "post_cost_pnl_cny": None,
            "max_drawdown_cny": None,
        }
    return {
        "win_rate": weighted_win_rate / completed,
        "expectancy_cny": weighted_expectancy / completed,
        "post_cost_pnl_cny": post_cost_pnl,
        # A per-style drawdown cannot be summed into an account drawdown.
        "max_drawdown_cny": None,
    }


def _explicit_ratio(
    records: Sequence[Mapping[str, Any]], field: str
) -> Optional[float]:
    values = [
        float(record[field])
        for record in records
        if _is_number(record.get(field)) and 0.0 <= float(record[field]) <= 1.0
    ]
    return min(values) if values else None


def _build_maturity(
    *,
    records: Sequence[Mapping[str, Any]],
    kpi: Mapping[str, Any],
    label_ops: Mapping[str, Any],
    trade_date: str,
    generated_at: str,
) -> dict[str, Any]:
    scope = kpi["authority_scope"]
    predictions = [
        record
        for record in records
        if record.get("journal_event_type") == "prediction_snapshot"
    ]
    trading_days = sorted(
        {
            date
            for date in (_record_trade_date(record) for record in predictions)
            if date
        }
    )
    if trade_date in trading_days:
        current_day_index = trading_days.index(trade_date)
    elif trading_days:
        current_day_index = len(trading_days) - 1
    else:
        current_day_index = 0

    layer_totals = kpi.get("sample_layer_totals")
    totals = layer_totals if isinstance(layer_totals, Mapping) else {}
    scientific = kpi.get("scientific_evidence")
    gates = scientific if isinstance(scientific, Mapping) else {}
    performance = _exploitation_performance(kpi)
    sample_size_raw = kpi.get("sample_size_evidence")
    sample_size = sample_size_raw if isinstance(sample_size_raw, Mapping) else {}
    drawdown_raw = kpi.get("account_drawdown_evidence")
    drawdown = drawdown_raw if isinstance(drawdown_raw, Mapping) else {}
    label_counts = label_ops.get("counts")
    counts = label_counts if isinstance(label_counts, Mapping) else {}
    degradation_events = sum(
        int(counts.get(field) or 0)
        for field in (
            "market_read_errors",
            "data_quality_rejected",
            "bar_quality_rejections",
            "missing_evidence",
            "cost_evidence_rejected",
        )
    )
    evidence = AshareEvidence(
        trading_days=trading_days,
        current_day_index=current_day_index,
        capital_authority_id=str(scope["capital_authority_id"]),
        authority_generation=int(scope["authority_generation"]),
        execution_lineage_id=str(scope["execution_lineage_id"]),
        observation_counterfactual_count=int(
            totals.get("observation_counterfactual") or 0
        ),
        execution_eligible_sample_count=sum(
            1 for record in records if record.get("execution_eligible") is True
        ),
        exploration_fill_count=int(totals.get("exploration_fill") or 0),
        exploitation_fill_count=int(totals.get("exploitation_fill") or 0),
        completed_round_trip_count=int(totals.get("completed_round_trip") or 0),
        exit_stop_count=int(totals.get("exit_stop") or 0),
        risk_reject_count=int(totals.get("risk_reject") or 0),
        forward_label_count=_ready_forward_label_count(kpi),
        primary_horizon_raw_n=int(sample_size.get("raw_N") or 0),
        unique_decision_cluster_count=int(
            sample_size.get("unique_decision_cluster_count") or 0
        ),
        independent_trading_day_count=int(
            sample_size.get("independent_trading_day_count") or 0
        ),
        n_eff=float(sample_size.get("N_eff") or 0.0),
        primary_horizon_policy_version=str(
            sample_size.get("primary_horizon_policy_version")
            or "ashare-primary-horizon-v1"
        ),
        win_rate=performance["win_rate"],
        expectancy_cny=performance["expectancy_cny"],
        post_cost_pnl_cny=performance["post_cost_pnl_cny"],
        max_drawdown_cny=(
            float(drawdown["max_drawdown_cny"])
            if _is_number(drawdown.get("max_drawdown_cny"), nonnegative=True)
            else None
        ),
        max_drawdown_source=(
            str(drawdown.get("source") or "")
            if drawdown.get("status") == "available"
            else ""
        ),
        chain_consistency_ratio=_explicit_ratio(records, "chain_consistency_ratio"),
        data_integrity_ratio=_explicit_ratio(records, "data_integrity_ratio"),
        degradation_events=degradation_events,
        calibration_evidence_sufficient=gates.get("calibration_evidence_sufficient")
        is True,
        point_in_time_lineage_complete=gates.get("point_in_time_lineage_complete")
        is True,
        costs_evidence_complete=gates.get("costs_evidence_complete") is True,
        fill_evidence_revalidated=gates.get("fill_evidence_revalidated") is True,
        duplicate_cluster_control_passed=gates.get("duplicate_cluster_control_passed")
        is True,
        strategy_count=len(kpi.get("styles") or {}),
        strategies_with_positive_expectancy=_positive_expectancy_style_count(kpi),
        human_confirmed=False,
    )
    payload = asdict(assess_ashare_maturity(evidence))
    payload.update(
        {
            "report_type": "ashare_market_maturity_v1",
            "evidence_source": "sample_journal_kpi",
            "generated_at": generated_at,
            "trade_date": trade_date,
            "observed_through_trade_date": trading_days[-1] if trading_days else None,
            "authority_scope": deepcopy(dict(scope)),
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
            "real_trading_enabled": False,
            "live_execution_enabled": False,
        }
    )
    return payload


def run_ashare_sample_ops(
    *,
    journal_path: Path | str,
    trade_date: Any,
    as_of: Any,
    review_dir: Path | str | None = None,
    reader: Any | None = None,
    environ: Optional[Mapping[str, Any]] = None,
    safety_flags: Optional[Mapping[str, Any]] = None,
    backlog_window_days: int = DEFAULT_BACKLOG_WINDOW_DAYS,
    label_batch_size: int = 200,
) -> dict[str, Any]:
    """Run one bounded, sim-only label/KPI/decision/maturity cycle."""

    active_environ = os.environ if environ is None else environ
    try:
        _assert_sim_only(active_environ, safety_flags)
    except ForwardLabelOpsSafetyError as exc:
        raise AshareSampleOpsSafetyError(str(exc)) from exc

    selected_trade_date = _compact_trade_date(trade_date)
    current_as_of = _parse_as_of(as_of)
    selected_review_dir = (
        Path(review_dir) if review_dir is not None else DEFAULT_REVIEW_DIR
    )
    _assert_output_paths_safe(selected_review_dir)

    run_id = "ashare-sample-ops:%s" % uuid4().hex
    generated_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    stage_metrics: dict[str, Any] = {}
    journal = SampleJournal(journal_path)
    try:
        with _measure_stage(stage_metrics, "journal_freeze"):
            frozen = journal.read_frozen(as_of=current_as_of)
            full_events_before = frozen.copy_events()
        live_journal_marker = _find_live_marker(full_events_before, "journal")
        if live_journal_marker:
            raise AshareSampleOpsSafetyError(
                "live trading journal marker rejected at %s" % live_journal_marker
            )
        initial_kpi = journal.build_kpi_from_events(full_events_before)
        authority_scope = initial_kpi["authority_scope"]
        with _measure_stage(stage_metrics, "forward_labels"):
            label_ops = run_ashare_forward_label_backlog(
                journal_path=journal_path,
                anchor_trade_date=selected_trade_date,
                as_of=current_as_of,
                window_days=backlog_window_days,
                reader=reader,
                environ=active_environ,
                safety_flags=safety_flags,
                journal=journal,
                frozen_view=frozen,
                authority_scope=authority_scope,
                batch_size=label_batch_size,
            )
        task_owned_delta_events = list(label_ops.pop("task_owned_delta_events", []))
        events = full_events_before + deepcopy(task_owned_delta_events)
        with _measure_stage(stage_metrics, "projection_build"):
            records = journal.project_sample_records(events)
            kpi = journal.build_kpi_from_events(events, authority_scope=authority_scope)
    except ForwardLabelOpsSafetyError as exc:
        raise AshareSampleOpsSafetyError(str(exc)) from exc

    scope = kpi["authority_scope"]
    current_events = [event for event in events if _in_authority(event, scope)]
    current_records = [record for record in records if _in_authority(record, scope)]
    current_predictions = [
        record
        for record in current_records
        if record.get("journal_event_type") == "prediction_snapshot"
        and _record_trade_date(record) == selected_trade_date
    ]
    data_as_of = current_as_of.isoformat(timespec="seconds")
    projection_input_sha = _projection_input_sha256(events, data_as_of=data_as_of)
    h0 = {
        "event_count": frozen.journal_head_event_count,
        "sha256": frozen.journal_head_sha256,
    }
    h1 = {
        **journal.canonical_head(events),
        "task_owned_delta_event_count": len(task_owned_delta_events),
    }
    common_projection_metadata = {
        "data_as_of": data_as_of,
        "generated_at": generated_at,
        "journal_head_event_count": frozen.journal_head_event_count,
        "journal_head_sha256": frozen.journal_head_sha256,
        "max_evidence_available_at": frozen.max_evidence_available_at,
        "excluded_after_as_of_count": frozen.excluded_after_as_of_count,
        "projection_input_sha256": projection_input_sha,
        "run_id": run_id,
        "H0": h0,
        "H1": h1,
        "task_owned_delta_event_count": len(task_owned_delta_events),
    }
    kpi.update(
        {
            "report_type": "sample_journal_kpi",
            "evidence_source": "sample_journal_kpi",
            **common_projection_metadata,
            "trade_date": selected_trade_date,
            "journal_event_count": len(current_events),
            "journal_total_event_count": len(events),
            "excluded_legacy_event_count": len(events) - len(current_events),
            "current_trade_date_prediction_count": len(current_predictions),
            "scientific_evidence": _scientific_evidence(current_records, kpi),
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
            "real_trading_enabled": False,
            "live_execution_enabled": False,
            "journal_metrics": journal.metrics_snapshot(),
            "stage_metrics": deepcopy(stage_metrics),
        }
    )

    decision = build_evolution_decision(
        kpi,
        authority_scope=scope,
        target_trade_date=selected_trade_date,
    )
    decision.update(common_projection_metadata)
    decision["live_execution_enabled"] = False
    maturity = _build_maturity(
        records=current_records,
        kpi=kpi,
        label_ops=label_ops,
        trade_date=selected_trade_date,
        generated_at=generated_at,
    )
    maturity.update(common_projection_metadata)

    with journal.guard_projection_head(
        frozen, task_owned_delta_events
    ) as projection_head_cas:
        if projection_head_cas["H1"] != {
            "event_count": h1["event_count"],
            "sha256": h1["sha256"],
        }:
            raise JournalConflictError(
                "projection H1 does not match guarded journal head"
            )
        with _measure_stage(stage_metrics, "projection_publish"):
            generation = publish_projection_generation(
                review_dir=selected_review_dir,
                projections={
                    KPI_LATEST: kpi,
                    "evolution_decision_latest.json": decision,
                    MATURITY_LATEST: maturity,
                },
                projection_input_sha256=projection_input_sha,
                run_id=run_id,
                generated_at=generated_at,
            )
    stage_metrics["total"] = {
        "wall_seconds": round(
            sum(float(value["wall_seconds"]) for value in stage_metrics.values()), 6
        ),
        "cpu_seconds": round(
            sum(float(value["cpu_seconds"]) for value in stage_metrics.values()), 6
        ),
    }
    label_ops["performance"] = {
        "http": deepcopy(label_ops.get("http_metrics") or {}),
        "journal_append": deepcopy(label_ops.get("journal_append") or {}),
        "pending_snapshot_count": int(
            (label_ops.get("backlog") or {}).get("pending_snapshot_count") or 0
        ),
        "selected_snapshot_count": int(
            (label_ops.get("counts") or {}).get("prediction_count") or 0
        ),
        "terminal_snapshot_count": int(
            (label_ops.get("backlog") or {}).get("terminal_snapshot_count") or 0
        ),
    }
    performance = {
        "stages": stage_metrics,
        "journal": journal.metrics_snapshot(),
        "http": deepcopy(label_ops.get("http_metrics") or {}),
        "as_of_drift_seconds": round(
            (
                datetime.fromisoformat(generated_at)
                - current_as_of.astimezone(timezone.utc)
            ).total_seconds(),
            6,
        ),
        "projection_generation": generation["generation_id"],
    }

    counts = label_ops.get("counts")
    label_counts = counts if isinstance(counts, Mapping) else {}
    warning_reasons: list[str] = []
    if not current_predictions:
        warning_reasons.append("no_current_trade_date_predictions")
    if int(label_counts.get("market_read_errors") or 0) > 0:
        warning_reasons.append("market_data_read_errors")
    if int(label_counts.get("data_quality_rejected") or 0) > 0:
        warning_reasons.append("forward_labels_rejected_data_quality")
    if int(label_counts.get("missing_evidence") or 0) > 0:
        warning_reasons.append("forward_label_evidence_missing")
    if int(label_counts.get("cost_evidence_rejected") or 0) > 0:
        warning_reasons.append("forward_labels_rejected_missing_cost_evidence")

    overall_status = "warn" if warning_reasons else "pass"
    return {
        "operation": "ashare_sample_ops",
        "overall_status": overall_status,
        "status": overall_status,
        "reason": warning_reasons[0] if warning_reasons else None,
        "warning_reasons": warning_reasons,
        "market": "Ashare",
        "trade_date": selected_trade_date,
        "as_of": data_as_of,
        "data_as_of": data_as_of,
        "generated_at": generated_at,
        "journal_head_event_count": frozen.journal_head_event_count,
        "journal_head_sha256": frozen.journal_head_sha256,
        "max_evidence_available_at": frozen.max_evidence_available_at,
        "excluded_after_as_of_count": frozen.excluded_after_as_of_count,
        "projection_input_sha256": projection_input_sha,
        "run_id": run_id,
        "H0": h0,
        "H1": h1,
        "journal_path": str(Path(journal_path).absolute()),
        "review_dir": str(selected_review_dir.absolute()),
        "current_trade_date_prediction_count": len(current_predictions),
        "label_ops": label_ops,
        "sample_kpi": kpi,
        "evolution_decision": decision,
        "market_maturity": maturity,
        "projection_generation": generation,
        "projection_head_cas": projection_head_cas,
        "performance": performance,
        "orders_created": 0,
        "emails_sent": 0,
        "accounts_created": 0,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "live_transition_authorized": False,
        "real_trading_enabled": False,
        "live_execution_enabled": False,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Persist sim-only A-share labels, KPIs, manual decision, and maturity."
    )
    parser.add_argument("--journal-path", type=Path, default=DEFAULT_JOURNAL_PATH)
    parser.add_argument("--review-dir", type=Path, default=DEFAULT_REVIEW_DIR)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument(
        "--backlog-window-days",
        type=int,
        default=DEFAULT_BACKLOG_WINDOW_DAYS,
    )
    parser.add_argument(
        "--label-batch-size",
        type=int,
        default=200,
        help="Forward-label append batch size (100-250).",
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_ashare_sample_ops(
            journal_path=args.journal_path,
            review_dir=args.review_dir,
            trade_date=args.trade_date,
            as_of=args.as_of,
            backlog_window_days=args.backlog_window_days,
            label_batch_size=args.label_batch_size,
        )
        exit_code = 0
    except (
        AshareSampleOpsSafetyError,
        JournalSafetyError,
        ProjectionGenerationError,
        ValueError,
    ) as exc:
        report = {
            "operation": "ashare_sample_ops",
            "overall_status": "blocked",
            "status": "blocked",
            "reason": str(exc),
            "orders_created": 0,
            "emails_sent": 0,
            "accounts_created": 0,
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
            "live_transition_authorized": False,
            "real_trading_enabled": False,
            "live_execution_enabled": False,
        }
        exit_code = 2
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
            default=str,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AshareSampleOpsError",
    "AshareSampleOpsSafetyError",
    "DEFAULT_JOURNAL_PATH",
    "DEFAULT_REVIEW_DIR",
    "main",
    "run_ashare_sample_ops",
]
