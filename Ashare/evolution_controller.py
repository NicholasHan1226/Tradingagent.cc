#!/usr/bin/env python3
"""A-share simulated evolution decisions backed only by SampleJournal KPIs.

This module never expands risk or enables real trading.  Inside the
simulation domain there is no human review gate: when the scientific evidence
gate reports ``promotion_evidence_ready=True`` the decision recommends
``execute_automatic_promotion`` and the promotion is executed through
:mod:`Ashare.promotion_executor` into the durable, simulation-only Champion
selection registry.

The top-level projection safety fields (``automatic_promotion_enabled`` etc.)
stay ``False`` by the canonical projection publisher contract; the standing
policy is carried by ``policy.automatic_promotion_enabled`` and the actual
promotion authority by the registry receipt chain.
"""

from __future__ import annotations

import argparse
import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REVIEW_DIR = ROOT / "shared" / "review" / "ashare"
LATEST_DECISION = DEFAULT_REVIEW_DIR / "evolution_decision_latest.json"
DECISION_LOG = DEFAULT_REVIEW_DIR / "evolution_decision_log.jsonl"
CN_TZ = timezone(timedelta(hours=8))

REQUIRED_AUTHORITY_FIELDS = (
    "capital_authority_id",
    "authority_generation",
    "execution_lineage_id",
)
MIN_COMPLETED_ROUND_TRIPS = 10
MIN_UNIQUE_DECISION_CLUSTERS = 20


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _compact_date(value: Any) -> str:
    raw = str(value or "").strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10].replace("-", "")
    return raw[:8] if raw else ""


def _today_cn_compact() -> str:
    return datetime.now(CN_TZ).strftime("%Y%m%d")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _authority(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {field: value.get(field) for field in REQUIRED_AUTHORITY_FIELDS}


def _authority_matches(actual: Any, expected: Any) -> bool:
    actual_scope = _authority(actual)
    expected_scope = _authority(expected)
    if any(
        actual_scope.get(field) in (None, "") for field in REQUIRED_AUTHORITY_FIELDS
    ):
        return False
    if any(
        expected_scope.get(field) in (None, "") for field in REQUIRED_AUTHORITY_FIELDS
    ):
        return False
    return actual_scope == expected_scope


def _sample_metrics(sample_kpi: Mapping[str, Any]) -> dict[str, Any]:
    styles = sample_kpi.get("styles")
    style_rows = styles.values() if isinstance(styles, Mapping) else ()
    predictions = 0
    exploration_fills = 0
    exploitation_fills = 0
    completed_round_trips = 0
    ready_label_cells = 0
    post_cost_pnl = 0.0
    max_drawdown = 0.0
    for row in style_rows:
        if not isinstance(row, Mapping):
            continue
        predictions += _safe_int(row.get("prediction_count"))
        exploration_fills += _safe_int(row.get("exploration_fill_count"))
        exploitation_fills += _safe_int(row.get("exploitation_fill_count"))
        completed_round_trips += _safe_int(row.get("completed_round_trip_count"))
        post_cost_pnl += _safe_float(row.get("post_cost_pnl_cny"))
        max_drawdown = max(
            max_drawdown,
            _safe_float(row.get("trade_pnl_sequence_max_drawdown_cny")),
        )
        label_counts = row.get("forward_label_counts")
        if not isinstance(label_counts, Mapping):
            continue
        for statuses in label_counts.values():
            if not isinstance(statuses, Mapping):
                continue
            ready_label_cells += _safe_int(statuses.get("ready"))
            ready_label_cells += _safe_int(statuses.get("labeled"))
    sample_size = sample_kpi.get("sample_size_evidence")
    sample_size_map = sample_size if isinstance(sample_size, Mapping) else {}
    account_drawdown = sample_kpi.get("account_drawdown_evidence")
    drawdown_map = account_drawdown if isinstance(account_drawdown, Mapping) else {}
    return {
        "prediction_count": predictions,
        "exploration_fill_count": exploration_fills,
        "exploitation_fill_count": exploitation_fills,
        "completed_round_trip_count": completed_round_trips,
        "ready_label_cell_count": ready_label_cells,
        "primary_horizon_raw_N": _safe_int(sample_size_map.get("raw_N")),
        "unique_decision_cluster_count": _safe_int(
            sample_size_map.get("unique_decision_cluster_count")
        ),
        "independent_trading_day_count": _safe_int(
            sample_size_map.get("independent_trading_day_count")
        ),
        "N_eff": _safe_float(sample_size_map.get("N_eff")),
        "post_cost_pnl_cny": round(post_cost_pnl, 6),
        "trade_pnl_sequence_max_drawdown_cny": round(max_drawdown, 6),
        "account_mtm_max_drawdown_cny": (
            _safe_float(drawdown_map.get("max_drawdown_cny"))
            if drawdown_map.get("status") == "available"
            else None
        ),
        "journal_event_count": _safe_int(sample_kpi.get("journal_event_count")),
    }


def build_evolution_decision(
    sample_kpi: Mapping[str, Any],
    *,
    authority_scope: Mapping[str, Any],
    target_trade_date: str | None = None,
) -> dict[str, Any]:
    """Build an evolution assessment; the decision itself never mutates state."""

    payload = sample_kpi if isinstance(sample_kpi, Mapping) else {}
    target_date = _compact_date(target_trade_date) or _today_cn_compact()
    evidence_date = _compact_date(payload.get("trade_date"))
    metrics = _sample_metrics(payload)
    reasons: list[str] = []

    source_valid = (
        str(payload.get("report_type") or "") == "sample_journal_kpi"
        and str(payload.get("evidence_source") or "") == "sample_journal_kpi"
    )
    if not source_valid:
        reasons.append("source_not_sample_journal_kpi")
    authority_valid = _authority_matches(
        payload.get("authority_scope"), authority_scope
    )
    if not authority_valid:
        reasons.append("authority_scope_mismatch")
    date_valid = evidence_date == target_date
    if not date_valid:
        reasons.append("sample_journal_kpi_trade_date_stale")

    scientific = payload.get("scientific_evidence")
    scientific_map = scientific if isinstance(scientific, Mapping) else {}
    required_scientific = (
        "point_in_time_lineage_complete",
        "costs_evidence_complete",
        "fill_evidence_revalidated",
        "duplicate_cluster_control_passed",
        "calibration_evidence_sufficient",
    )
    scientific_blockers = [
        field for field in required_scientific if scientific_map.get(field) is not True
    ]
    if scientific_blockers:
        reasons.extend("missing_%s" % field for field in scientific_blockers)
    if metrics["completed_round_trip_count"] < MIN_COMPLETED_ROUND_TRIPS:
        reasons.append("insufficient_completed_round_trips")
    if metrics["unique_decision_cluster_count"] < MIN_UNIQUE_DECISION_CLUSTERS:
        reasons.append("insufficient_unique_decision_clusters")

    evidence_usable = source_valid and authority_valid and date_valid
    promotion_evidence_ready = (
        evidence_usable
        and not scientific_blockers
        and scientific_map.get("promotion_evidence_ready") is True
        and metrics["completed_round_trip_count"] >= MIN_COMPLETED_ROUND_TRIPS
        and metrics["unique_decision_cluster_count"] >= MIN_UNIQUE_DECISION_CLUSTERS
    )
    if not evidence_usable:
        state = "evidence_rejected"
        action = "observe_and_label_candidates"
    elif promotion_evidence_ready:
        state = "automatic_promotion_ready"
        action = "execute_automatic_promotion"
        reasons.append("scientific_evidence_ready_for_automatic_promotion")
    else:
        state = "evidence_pending"
        action = "observe_and_label_candidates"

    policy = {
        "observation_enabled": True,
        "safe_exploration_enabled": True,
        "exploration_selection": "top_k_epsilon_greedy",
        "propensity_recording_required": True,
        "max_exploration_new_positions_per_day": 1,
        "exploration_total_exposure_limit_cny": 7_500.0,
        "automatic_promotion_enabled": True,
        "automatic_risk_expansion_enabled": False,
        "real_trading_enabled": False,
    }
    return {
        "report_type": "ashare_evolution_decision_v2",
        "evidence_source": "sample_journal_kpi",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "market": "ashare",
        "trade_date": target_date,
        "evidence_trade_date": evidence_date,
        "authority_scope": deepcopy(_authority(authority_scope)),
        "evidence_authority_scope": deepcopy(
            _authority(payload.get("authority_scope"))
        ),
        "evidence_authority_valid": authority_valid,
        "evidence_usable": evidence_usable,
        "state": state,
        "recommended_action": action,
        "reasons": list(dict.fromkeys(reasons)),
        "policy": policy,
        "metrics": metrics,
        "scientific_evidence": deepcopy(dict(scientific_map)),
        "promotion_evidence_ready": promotion_evidence_ready,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "live_transition_authorized": False,
        "real_trading_enabled": False,
        "read_only_decision": True,
    }


def _write_latest(path: Path, payload: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError("evolution_decision_latest_symlink_not_allowed")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.%s.tmp" % (path.name, os.getpid()))
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(tmp), str(path))


def _append_log(path: Path, payload: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError("evolution_decision_log_symlink_not_allowed")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    try:
        data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_evolution_decision(
    sample_kpi: Mapping[str, Any],
    *,
    authority_scope: Mapping[str, Any],
    review_dir: Path | str | None = None,
    target_trade_date: str | None = None,
) -> dict[str, Any]:
    decision = build_evolution_decision(
        sample_kpi,
        authority_scope=authority_scope,
        target_trade_date=target_trade_date,
    )
    if decision["state"] == "evidence_rejected":
        reason = decision["reasons"][0] if decision["reasons"] else "evidence_rejected"
        raise ValueError(reason)
    review_path = Path(review_dir) if review_dir is not None else DEFAULT_REVIEW_DIR
    if review_path.is_symlink():
        raise ValueError("evolution_review_dir_symlink_not_allowed")
    review_path.mkdir(parents=True, exist_ok=True)
    _write_latest(review_path / LATEST_DECISION.name, decision)
    _append_log(review_path / DECISION_LOG.name, decision)
    return decision


def run_automatic_promotion(
    decision: Mapping[str, Any],
    *,
    registry_root: Path | str,
    challenger_candidates: list[Mapping[str, Any]] | None = None,
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    """Execute the evidence-gated automatic promotion for a ready decision.

    Returns a copy of *decision* with the ``promotion_execution`` outcome
    embedded.  Decisions without ``promotion_evidence_ready=True`` are returned
    unchanged with an explicit ``no_op`` outcome; no Champion change is ever
    fabricated.
    """

    from Ashare.promotion_executor import execute_automatic_promotion

    result = execute_automatic_promotion(
        decision,
        registry_root=registry_root,
        challenger_candidates=challenger_candidates,
        recorded_at=recorded_at,
    )
    updated = dict(decision)
    updated["promotion_execution"] = result
    return updated


def load_latest_decision(
    path: Path | str | None = None, *, review_dir: Path | str | None = None
) -> dict[str, Any]:
    if path is not None:
        return _read_json(Path(path))
    review_path = Path(review_dir) if review_dir is not None else DEFAULT_REVIEW_DIR
    return _read_json(review_path / LATEST_DECISION.name)


def decision_market_context(
    decision: Mapping[str, Any] | None,
    *,
    target_trade_date: str | None = None,
    authority_scope: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(decision, Mapping):
        return {
            "evidence_usable": False,
            "observation_enabled": True,
            "safe_exploration_enabled": True,
            "automatic_risk_expansion_enabled": False,
            "strategy_sample_valid_count": 0,
        }
    target_date = _compact_date(target_trade_date) or _today_cn_compact()
    evidence_usable = (
        decision.get("evidence_usable") is True
        and _compact_date(decision.get("evidence_trade_date")) == target_date
        and _authority_matches(decision.get("authority_scope"), authority_scope)
    )
    policy = (
        decision.get("policy") if isinstance(decision.get("policy"), Mapping) else {}
    )
    metrics = (
        decision.get("metrics") if isinstance(decision.get("metrics"), Mapping) else {}
    )
    return {
        "evidence_usable": evidence_usable,
        "evidence_rejection_reason": ""
        if evidence_usable
        else "evolution_evidence_unusable",
        "strategy_sample_valid_count": (
            _safe_int(metrics.get("completed_round_trip_count"))
            if evidence_usable
            else 0
        ),
        "sample_journal_event_count": (
            _safe_int(metrics.get("journal_event_count")) if evidence_usable else 0
        ),
        "observation_enabled": policy.get("observation_enabled") is not False,
        "safe_exploration_enabled": policy.get("safe_exploration_enabled") is not False,
        "exploration_selection": str(
            policy.get("exploration_selection") or "top_k_epsilon_greedy"
        ),
        "propensity_recording_required": True,
        "automatic_promotion_enabled": (
            evidence_usable and policy.get("automatic_promotion_enabled") is True
        ),
        "automatic_risk_expansion_enabled": False,
        "evolution_recommended_action": str(
            decision.get("recommended_action") or "observe_and_label_candidates"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-kpi", type=Path, required=True)
    parser.add_argument("--authority-scope", type=Path, required=True)
    parser.add_argument("--review-dir", type=Path, default=DEFAULT_REVIEW_DIR)
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    decision = write_evolution_decision(
        _read_json(args.sample_kpi),
        authority_scope=_read_json(args.authority_scope),
        review_dir=args.review_dir,
        target_trade_date=args.trade_date or None,
    )
    print(json.dumps(decision, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
