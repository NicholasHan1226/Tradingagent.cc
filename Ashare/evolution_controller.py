#!/usr/bin/env python3
"""A-share automated evolution controller.

This module turns portfolio/tier evidence into the next simulated-only action.
It does not place orders and never enables real trading.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from Ashare.epoch_review import validate_review_authority
from shared.execution.sim_account_epoch import (
    read_epoch_state,
    require_authoritative_epoch_metadata,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REVIEW_DIR = ROOT / "shared" / "review" / "ashare"
LATEST_DECISION = DEFAULT_REVIEW_DIR / "evolution_decision_latest.json"
DECISION_LOG = DEFAULT_REVIEW_DIR / "evolution_decision_log.jsonl"
CN_TZ = timezone(timedelta(hours=8))
MIN_EVOLUTION_EVIDENCE_SAMPLES = 20


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


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _compact_date(value: Any) -> str:
    raw = str(value or "").strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10].replace("-", "")
    return raw[:8] if raw else ""


def _today_cn_compact() -> str:
    return datetime.now(CN_TZ).strftime("%Y%m%d")


def build_evolution_decision(
    portfolio_evolution: dict[str, Any],
    *,
    epoch_authority: dict[str, Any],
    target_trade_date: str | None = None,
    min_strategy_samples: int = 5,
    current_epoch_id: int = 2,
) -> dict[str, Any]:
    """Build a simulated-only evolution decision from portfolio evidence."""

    min_samples = max(1, int(min_strategy_samples))
    min_evolution_evidence_samples = max(MIN_EVOLUTION_EVIDENCE_SAMPLES, min_samples)
    target_date = _compact_date(target_trade_date) or _today_cn_compact()
    evidence_date = _compact_date(portfolio_evolution.get("trade_date"))
    evidence_epoch = _safe_int(portfolio_evolution.get("capital_epoch"))
    authority_valid, authority_reason = validate_review_authority(
        portfolio_evolution,
        epoch_authority,
    )
    expected_epoch = epoch_authority.get(
        "capital_epoch", epoch_authority.get("current_epoch_id")
    )
    if isinstance(expected_epoch, int) and not isinstance(expected_epoch, bool):
        current_epoch_id = expected_epoch
    strategy_sample_count = _safe_int(portfolio_evolution.get("strategy_sample_count"))
    today_strategy_sample_count = _safe_int(portfolio_evolution.get("today_strategy_sample_count"))
    pnl = portfolio_evolution.get("pnl") if isinstance(portfolio_evolution.get("pnl"), dict) else {}
    total_pnl = _safe_float(pnl.get("total_pnl"), _safe_float(portfolio_evolution.get("total_pnl")))
    realized_pnl = _safe_float(pnl.get("realized_pnl"), _safe_float(portfolio_evolution.get("realized_pnl")))
    equity = _safe_float(pnl.get("equity"), 0.0)
    pnl_pct = round(total_pnl / equity, 6) if equity > 0 else 0.0
    rankings = portfolio_evolution.get("rankings") if isinstance(portfolio_evolution.get("rankings"), list) else []
    evidence = portfolio_evolution.get("evolution_evidence") if isinstance(portfolio_evolution.get("evolution_evidence"), dict) else {}
    eligible_sample_count = _safe_int(evidence.get("eligible_sample_count"))
    realized_round_trip_count = _safe_int(evidence.get("realized_round_trip_count"))
    forward_label_count = _safe_int(evidence.get("forward_label_count"))
    reasons: list[str] = []
    if evidence_date != target_date:
        today_strategy_sample_count = 0

    reasons.append("daily_trade_target_removed")
    if not authority_valid:
        state = "evidence_pending"
        action = "observe_and_label_candidates"
        reasons.append(authority_reason)
    elif evidence_epoch != current_epoch_id:
        state = "evidence_pending"
        action = "observe_and_label_candidates"
        reasons.append("capital_epoch_mismatch")
    elif evidence_date != target_date:
        state = "evidence_pending"
        action = "observe_and_label_candidates"
        reasons.append("portfolio_evolution_trade_date_stale")
    elif strategy_sample_count < min_samples:
        state = "evidence_pending"
        action = "observe_and_label_candidates"
        reasons.append("cumulative_strategy_samples_below_minimum")
    elif eligible_sample_count < min_evolution_evidence_samples:
        state = "evidence_pending"
        action = "observe_and_label_candidates"
        reasons.append("insufficient_verified_execution_evidence")
    elif realized_round_trip_count < max(1, min_evolution_evidence_samples // 2):
        state = "evidence_pending"
        action = "observe_and_label_candidates"
        reasons.append("insufficient_realized_round_trips")
    elif forward_label_count < min_evolution_evidence_samples:
        state = "evidence_pending"
        action = "observe_and_label_candidates"
        reasons.append("insufficient_forward_validation")
    elif realized_pnl <= 0:
        state = "evidence_pending"
        action = "observe_and_label_candidates"
        reasons.append("non_positive_realized_pnl")
    elif total_pnl < 0:
        state = "risk_tightening"
        action = "tighten_risk"
        reasons.append("negative_mark_to_market_pnl")
    else:
        state = "expansion_candidate"
        action = "expand_risk_candidate"
        reasons.append("positive_realized_pnl_after_all_gates")

    policy = {
        "today_strategy_sample_count": today_strategy_sample_count,
        "min_strategy_samples": min_samples,
        "min_evolution_evidence_samples": min_evolution_evidence_samples,
        "strategy_sample_count": strategy_sample_count,
        "sample_collection_min_score": 0.55,
        "max_probe_positions": 1,
        "probe_allocation_min": 20_000.0,
        "probe_allocation_max": 35_000.0,
        "real_trading_enabled": False,
    }
    if action == "tighten_risk":
        policy["sample_collection_min_score"] = 0.60
        policy["probe_allocation_max"] = 25_000.0

    return {
        "report_type": "ashare_evolution_decision",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "market": "ashare",
        "trade_date": target_date,
        "evidence_trade_date": evidence_date,
        "capital_epoch": evidence_epoch,
        "capital_cny": portfolio_evolution.get("capital_cny"),
        "epoch_cutover_timestamp": portfolio_evolution.get("epoch_cutover_timestamp"),
        "current_epoch_id": current_epoch_id,
        "evidence_authority_valid": authority_valid,
        "evidence_authority_rejection_reason": authority_reason,
        "state": state,
        "recommended_action": action,
        "reasons": reasons,
        "policy": policy,
        "metrics": {
            "strategy_sample_count": strategy_sample_count,
            "eligible_sample_count": eligible_sample_count,
            "realized_round_trip_count": realized_round_trip_count,
            "forward_label_count": forward_label_count,
            "today_strategy_sample_count": today_strategy_sample_count,
            "min_strategy_samples": min_samples,
            "min_evolution_evidence_samples": min_evolution_evidence_samples,
            "total_pnl": round(total_pnl, 6),
            "realized_pnl": round(realized_pnl, 6),
            "pnl_pct": pnl_pct,
            "ranking_count": len(rankings),
            "tier_account_count": _safe_int((portfolio_evolution.get("tier_experiments") or {}).get("account_count"))
            if isinstance(portfolio_evolution.get("tier_experiments"), dict)
            else 0,
        },
        "guardrails": [
            "simulated_only",
            "candidate_layer_required",
            "positive_fill_price_required",
            "risk_check_required",
            "cash_and_lot_size_required",
            "t_plus_1_required",
            "market_session_required",
        ],
        "real_trading_enabled": False,
        "read_only_decision": True,
    }


def write_evolution_decision(
    portfolio_evolution: dict[str, Any],
    *,
    review_dir: Path | str | None = None,
    target_trade_date: str | None = None,
    min_strategy_samples: int = 5,
) -> dict[str, Any]:
    review_path = Path(review_dir) if review_dir is not None else DEFAULT_REVIEW_DIR
    epoch_state = read_epoch_state()
    epoch_metadata = require_authoritative_epoch_metadata(epoch_state)
    current_epoch_id = int(epoch_metadata["capital_epoch"])
    epoch_authority = {
        "capital_epoch": current_epoch_id,
        "capital_cny": float(epoch_metadata["capital_cny"]),
        "epoch_cutover_timestamp": str(epoch_metadata["cutover_timestamp"]),
    }
    decision = build_evolution_decision(
        portfolio_evolution,
        target_trade_date=target_trade_date,
        min_strategy_samples=min_strategy_samples,
        current_epoch_id=current_epoch_id,
        epoch_authority=epoch_authority,
    )
    if not decision["evidence_authority_valid"]:
        raise ValueError(
            f"portfolio_evolution_authority_rejected:"
            f"{decision['evidence_authority_rejection_reason']}"
        )
    review_path.mkdir(parents=True, exist_ok=True)
    latest = review_path / LATEST_DECISION.name
    log = review_path / DECISION_LOG.name
    latest.write_text(json.dumps(decision, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(decision, ensure_ascii=False, sort_keys=True) + "\n")
    return decision


def load_latest_decision(path: Path | str | None = None, *, review_dir: Path | str | None = None) -> dict[str, Any]:
    if path is not None:
        return _read_json(Path(path))
    review_path = Path(review_dir) if review_dir is not None else DEFAULT_REVIEW_DIR
    return _read_json(review_path / LATEST_DECISION.name)


def decision_market_context(
    decision: dict[str, Any] | None,
    *,
    target_trade_date: str | None = None,
    current_epoch_id: int = 2,
) -> dict[str, Any]:
    if not isinstance(decision, dict):
        return {}
    policy = decision.get("policy") if isinstance(decision.get("policy"), dict) else {}
    if not policy:
        return {}
    target_date = _compact_date(target_trade_date) or _today_cn_compact()
    evidence_date = _compact_date(decision.get("evidence_trade_date"))
    evidence_epoch = _safe_int(decision.get("capital_epoch"))
    evidence_usable = True
    rejection_reason = ""
    if evidence_epoch != current_epoch_id:
        evidence_usable = False
        rejection_reason = "capital_epoch_mismatch"
    elif evidence_date != target_date:
        evidence_usable = False
        rejection_reason = "portfolio_evolution_trade_date_stale"
    return {
        "today_strategy_sample_count": _safe_float(policy.get("today_strategy_sample_count"), 0.0),
        "min_strategy_samples": _safe_float(policy.get("min_strategy_samples"), 5.0),
        "strategy_sample_valid_count": (
            _safe_float(policy.get("strategy_sample_count"), 0.0) if evidence_usable else 0.0
        ),
        "sample_collection_min_score": _safe_float(policy.get("sample_collection_min_score"), 0.55),
        "evolution_recommended_action": str(decision.get("recommended_action") or ""),
        "evidence_usable": evidence_usable,
        "evidence_rejection_reason": rejection_reason,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portfolio-evolution", type=Path, default=DEFAULT_REVIEW_DIR / "portfolio_evolution_latest.json")
    parser.add_argument("--review-dir", type=Path, default=DEFAULT_REVIEW_DIR)
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--min-strategy-samples", type=int, default=5)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    decision = write_evolution_decision(
        _read_json(args.portfolio_evolution),
        review_dir=args.review_dir,
        target_trade_date=args.trade_date or None,
        min_strategy_samples=args.min_strategy_samples,
    )
    print(json.dumps(decision, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
