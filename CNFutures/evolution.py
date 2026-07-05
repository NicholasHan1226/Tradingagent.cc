#!/usr/bin/env python3
"""Simulated-only style governor for CNFutures.

The governor reads review evidence, writes runtime overlays under
``shared/review/cn_futures``, and never mutates checked-in strategy files or
real-trading state.
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.markets.performance_tracker import compare_styles

from .adapter import STRATEGY_DIR
from .review import STYLE_REVIEW_MARKET


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REVIEW_ROOT = ROOT / "shared" / "review"
DEFAULT_STRATEGY_DIR = STRATEGY_DIR
MIN_WEIGHT = 0.05
MAX_WEIGHT = 0.60
DEFAULT_MIN_TRADES = 20
DEFAULT_MAX_VARIANTS_PER_CYCLE = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


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


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _review_dir(review_root: Path | str | None = None) -> Path:
    root = Path(review_root) if review_root is not None else DEFAULT_REVIEW_ROOT
    return root / STYLE_REVIEW_MARKET


def generated_styles_dir(review_root: Path | str | None = None) -> Path:
    return _review_dir(review_root) / "generated_styles"


def style_weights_path(review_root: Path | str | None = None) -> Path:
    return _review_dir(review_root) / "style_weights.json"


def evolution_plan_path(review_root: Path | str | None = None) -> Path:
    return _review_dir(review_root) / "evolution_plan.json"


def _load_strategy_files(strategy_dir: Path | str | None = None) -> dict[str, dict[str, Any]]:
    directory = Path(strategy_dir) if strategy_dir is not None else DEFAULT_STRATEGY_DIR
    styles: dict[str, dict[str, Any]] = {}
    if not directory.exists():
        return styles
    for path in sorted(directory.glob("*.json")):
        payload = _read_json(path)
        if not payload:
            continue
        name = str(payload.get("name") or path.stem).strip()
        if not name:
            continue
        payload.setdefault("name", name)
        payload["_path"] = str(path)
        payload["_source"] = "checked_in"
        styles[name] = payload
    return styles


def _load_generated_styles(review_root: Path | str | None = None) -> dict[str, dict[str, Any]]:
    styles: dict[str, dict[str, Any]] = {}
    directory = generated_styles_dir(review_root)
    if not directory.exists():
        return styles
    for path in sorted(directory.glob("*.json")):
        payload = _read_json(path)
        if not payload:
            continue
        name = str(payload.get("name") or path.stem).strip()
        if not name:
            continue
        payload.setdefault("name", name)
        payload["_path"] = str(path)
        payload["_source"] = "generated"
        styles[name] = payload
    return styles


def load_runtime_styles(
    *,
    strategy_dir: Path | str | None = None,
    review_root: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    styles = _load_strategy_files(strategy_dir)
    styles.update(_load_generated_styles(review_root))
    overlay = _read_json(style_weights_path(review_root)).get("styles")
    if isinstance(overlay, dict):
        for name, values in overlay.items():
            if name not in styles or not isinstance(values, dict):
                continue
            styles[name].update({
                key: values[key]
                for key in ("status", "enabled", "weight", "evolution_action", "evolution_reason", "last_modified")
                if key in values
            })
    return styles


def _latest_style_states(review_root: Path | str | None = None) -> dict[str, dict[str, Any]]:
    payload = _read_json(_review_dir(review_root) / "style_comparison.json")
    rows = payload.get("style_states") if isinstance(payload.get("style_states"), list) else []
    return {
        str(row.get("style_name")): dict(row)
        for row in rows
        if isinstance(row, dict) and row.get("style_name")
    }


def _style_status(style: dict[str, Any]) -> str:
    status = str(style.get("status") or "").strip().lower()
    if status in {"active", "paused", "deprecated"}:
        return status
    if not bool(style.get("enabled", True)) or bool(style.get("paused", False)):
        return "paused"
    return "active"


def _base_weight(style: dict[str, Any]) -> float:
    return min(MAX_WEIGHT, max(MIN_WEIGHT, _safe_float(style.get("weight"), 1.0)))


def _normalize_active_weights(weights: dict[str, dict[str, Any]]) -> None:
    active = [values for values in weights.values() if values.get("status") == "active"]
    total = sum(_safe_float(values.get("weight"), 0.0) for values in active)
    if not active or total <= 0:
        return
    for values in active:
        values["weight"] = round(_safe_float(values.get("weight"), 0.0) / total, 6)


def _tweak_style(base: dict[str, Any], name: str, *, experiment: str = "balanced") -> dict[str, Any]:
    variant = deepcopy({key: value for key, value in base.items() if not str(key).startswith("_")})
    signal_threshold = _safe_float(base.get("signal_threshold"), 0.01)
    risk_per_trade = _safe_float(base.get("risk_per_trade"), 0.02)
    max_margin_usage = _safe_float(base.get("max_margin_usage"), 0.20)
    family = str(base.get("style_family") or "").strip().lower()
    threshold_multiplier = {
        "precision": 1.10,
        "fast": 0.85,
        "smooth": 1.00,
    }.get(experiment, 0.95)
    risk_multiplier = {
        "precision": 0.75,
        "fast": 0.90,
        "smooth": 0.80,
    }.get(experiment, 0.85)
    variant.update({
        "name": name,
        "description": f"Auto-generated simulated CNFutures variant from {base.get('name')}.",
        "parent_style": base.get("name"),
        "evolution_experiment": experiment,
        "selection_objective": "win_rate_first_risk_adjusted",
        "generation": _safe_int(base.get("generation"), 1) + 1,
        "status": "active",
        "enabled": True,
        "weight": MIN_WEIGHT,
        "created_at": _now_iso(),
        "last_modified": _now_iso(),
        "real_trading_enabled": False,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "signal_threshold": round(min(0.05, max(0.003, signal_threshold * threshold_multiplier)), 6),
        "risk_per_trade": round(min(0.05, max(0.003, risk_per_trade * risk_multiplier)), 6),
        "max_margin_usage": round(min(0.50, max(0.05, max_margin_usage * 0.90)), 6),
    })
    if family == "index_intraday_directional":
        variant["signal_threshold"] = round(min(0.02, max(0.001, signal_threshold * threshold_multiplier)), 6)
        variant["risk_per_trade"] = round(min(0.03, max(0.002, risk_per_trade * risk_multiplier)), 6)
        variant["max_margin_usage"] = round(min(0.20, max(0.03, max_margin_usage * 0.90)), 6)
        lookback_delta = {"precision": 1, "fast": -1, "smooth": 2}.get(experiment, 1)
        ma_delta = {"precision": 1, "fast": -1, "smooth": 3}.get(experiment, 1)
        variant["momentum_lookback_bars"] = max(2, _safe_int(base.get("momentum_lookback_bars"), 3) + lookback_delta)
        variant["moving_average_bars"] = max(
            variant["momentum_lookback_bars"] + 1,
            _safe_int(base.get("moving_average_bars"), 6) + ma_delta,
        )
        variant["prediction_horizon_bars"] = max(1, _safe_int(base.get("prediction_horizon_bars"), 3))
        variant["no_overnight"] = True
        variant["day_session_only"] = True
        variant["trend_alignment_required"] = True
        base_volume = max(1.0, _safe_float(base.get("min_volume_ratio"), 1.05))
        volume_delta = {"precision": 0.05, "fast": -0.02, "smooth": 0.02}.get(experiment, 0.0)
        variant["min_volume_ratio"] = round(min(1.30, max(1.00, base_volume + volume_delta)), 4)
        base_consistency = max(0.0, min(1.0, _safe_float(base.get("min_directional_consistency"), 0.60)))
        consistency_delta = {"precision": 0.08, "fast": -0.05, "smooth": 0.03}.get(experiment, 0.0)
        variant["min_directional_consistency"] = round(min(0.90, max(0.50, base_consistency + consistency_delta)), 4)
        base_reversal = max(0.0, _safe_float(base.get("max_intrabar_reversal_pct"), 0.002))
        reversal_multiplier = {"precision": 0.75, "fast": 1.20, "smooth": 0.90}.get(experiment, 1.0)
        variant["max_intrabar_reversal_pct"] = round(min(0.005, max(0.0005, base_reversal * reversal_multiplier)), 6)
        base_signal_to_range = max(0.0, _safe_float(base.get("min_signal_to_range_ratio"), 0.35))
        signal_to_range_delta = {"precision": 0.10, "fast": -0.08, "smooth": 0.05}.get(experiment, 0.0)
        variant["min_signal_to_range_ratio"] = round(min(0.80, max(0.20, base_signal_to_range + signal_to_range_delta)), 4)
        variant["max_bar_gap_minutes"] = max(5, _safe_int(base.get("max_bar_gap_minutes"), 7))
        base_body = max(0.0, min(1.0, _safe_float(base.get("min_body_to_range_ratio"), 0.30)))
        body_delta = {"precision": 0.08, "fast": -0.05, "smooth": 0.03}.get(experiment, 0.0)
        variant["min_body_to_range_ratio"] = round(min(0.70, max(0.15, base_body + body_delta)), 4)
        base_consecutive = max(0, _safe_int(base.get("min_consecutive_aligned_bars"), 2))
        consecutive_delta = {"precision": 1, "fast": -1, "smooth": 0}.get(experiment, 0)
        variant["min_consecutive_aligned_bars"] = max(1, min(4, base_consecutive + consecutive_delta))
        base_chase = max(0.0, _safe_float(base.get("max_late_chase_pct"), 0.012))
        chase_multiplier = {"precision": 0.80, "fast": 1.25, "smooth": 0.95}.get(experiment, 1.0)
        variant["max_late_chase_pct"] = round(min(0.025, max(0.004, base_chase * chase_multiplier)), 6)
        variant["slippage_bps"] = max(0.0, _safe_float(base.get("slippage_bps"), 2.0))
        variant["volume_participation"] = round(min(0.20, max(0.01, _safe_float(base.get("volume_participation"), 0.05))), 4)
        variant["flatten_before_session_close_minutes"] = max(5, _safe_int(base.get("flatten_before_session_close_minutes"), 10))
        variant["rollover_min_days_to_contract_month_start"] = max(0, _safe_int(base.get("rollover_min_days_to_contract_month_start"), 5))
    return variant


def _maybe_generate_variants(
    top_style: dict[str, Any],
    *,
    review_root: Path | str | None = None,
    dry_run: bool = False,
    max_variants: int = DEFAULT_MAX_VARIANTS_PER_CYCLE,
) -> list[dict[str, Any]]:
    base_name = str(top_style.get("name") or "style")
    generation = _safe_int(top_style.get("generation"), 1) + 1
    experiments = ["precision", "fast", "smooth"][: max(1, int(max_variants))]
    results: list[dict[str, Any]] = []
    for experiment in experiments:
        name = f"{base_name}_g{generation}_{experiment}_{_today_compact()}"
        path = generated_styles_dir(review_root) / f"{name}.json"
        if path.exists():
            results.append({"action": "variant_exists", "style_name": name, "base_style": base_name, "experiment": experiment, "path": str(path)})
            continue
        payload = _tweak_style(top_style, name, experiment=experiment)
        if not dry_run:
            _write_json(path, payload)
        results.append({
            "action": "variant_generated" if not dry_run else "variant_planned",
            "style_name": name,
            "base_style": base_name,
            "experiment": experiment,
            "path": str(path),
        })
    return results


def evaluate_styles(
    *,
    strategy_dir: Path | str | None = None,
    review_root: Path | str | None = None,
    min_trades: int = DEFAULT_MIN_TRADES,
    max_variants_per_cycle: int = DEFAULT_MAX_VARIANTS_PER_CYCLE,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Evaluate CNFutures styles and write simulated runtime overlays."""

    styles = load_runtime_styles(strategy_dir=strategy_dir, review_root=review_root)
    rankings = compare_styles(STYLE_REVIEW_MARKET, review_root=review_root)
    style_states = _latest_style_states(review_root)
    ranking_by_style = {str(row.get("style_name")): row for row in rankings}
    weights: dict[str, dict[str, Any]] = {}
    actions: list[dict[str, Any]] = []

    if not styles:
        result = {
            "market": STYLE_REVIEW_MARKET,
            "generated_at": _now_iso(),
            "state": "no_styles",
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
            "dry_run": dry_run,
            "actions": [],
            "rankings": rankings,
        }
        if not dry_run:
            _write_json(evolution_plan_path(review_root), result)
            _append_jsonl(_review_dir(review_root) / "evolution_log.jsonl", result)
        return result

    top_name = str(rankings[0].get("style_name")) if rankings else ""
    for name, style in sorted(styles.items()):
        current_status = _style_status(style)
        current_weight = _base_weight(style)
        state = style_states.get(name, {})
        metric = ranking_by_style.get(name, {})
        trades = _safe_int(metric.get("trades") or state.get("filled_count"), 0)
        pnl = _safe_float(metric.get("pnl"), 0.0)
        trend = str(metric.get("trend") or "stable")
        latest_status = str(state.get("status") or metric.get("status") or "observe")
        action = "observe"
        reason = "collect_more_samples"
        status = current_status if current_status in {"active", "paused"} else "active"
        weight = current_weight

        if latest_status == "blocked":
            action = "pause"
            reason = "latest_style_health_blocked"
            status = "paused"
            weight = MIN_WEIGHT
        elif latest_status == "degraded":
            action = "reduce_weight"
            reason = "latest_style_health_degraded"
            weight = max(MIN_WEIGHT, current_weight * 0.70)
        elif trades < min_trades:
            action = "observe"
            reason = f"sample_insufficient: trades={trades}, min_trades={min_trades}"
        elif name == top_name and pnl > 0 and trend != "declining":
            action = "promote"
            reason = "top_rank_positive_sample"
            status = "active"
            weight = min(MAX_WEIGHT, current_weight * 1.25)
        elif pnl < 0 or trend == "declining":
            action = "reduce_weight"
            reason = "negative_or_declining_performance"
            weight = max(MIN_WEIGHT, current_weight * 0.75)

        weights[name] = {
            "status": status,
            "enabled": status == "active",
            "weight": round(weight, 6),
            "evolution_action": action,
            "evolution_reason": reason,
            "last_modified": _now_iso(),
        }
        actions.append({
            "style_name": name,
            "action": action,
            "reason": reason,
            "before": {"status": current_status, "weight": current_weight},
            "after": {"status": status, "weight": round(weight, 6)},
            "metric": metric,
            "latest_state": state,
        })

    _normalize_active_weights(weights)
    variant_actions: list[dict[str, Any]] = []
    top_metric = ranking_by_style.get(top_name, {})
    if top_name and top_name in styles and _safe_int(top_metric.get("trades"), 0) >= min_trades and top_metric.get("trend") == "improving" and _safe_float(top_metric.get("pnl"), 0.0) > 0:
        variant_actions = _maybe_generate_variants(
            styles[top_name],
            review_root=review_root,
            dry_run=dry_run,
            max_variants=max_variants_per_cycle,
        )
        for variant_action in variant_actions:
            actions.append({"style_name": top_name, **variant_action, "reason": "top_style_improving_parameter_search"})
            weights[str(variant_action["style_name"])] = {
                "status": "active",
                "enabled": True,
                "weight": MIN_WEIGHT,
                "evolution_action": variant_action["action"],
                "evolution_reason": "top_style_improving_parameter_search",
                "last_modified": _now_iso(),
            }
        _normalize_active_weights(weights)

    state = "no_performance_history" if not rankings else ("adjusted" if any(action["action"] not in {"observe"} for action in actions) else "observed")
    result = {
        "market": STYLE_REVIEW_MARKET,
        "generated_at": _now_iso(),
        "state": state,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_execution": False,
        "real_trading_enabled": False,
        "dry_run": dry_run,
        "min_trades": min_trades,
        "max_variants_per_cycle": max_variants_per_cycle,
        "selection_objective": "win_rate_first_risk_adjusted",
        "actions": actions,
        "rankings": rankings,
        "weights": weights,
        "generated_variant": variant_actions[0] if variant_actions else {},
        "generated_variants": variant_actions,
        "written_paths": {} if dry_run else {
            "style_weights": str(style_weights_path(review_root)),
            "evolution_plan": str(evolution_plan_path(review_root)),
            "evolution_log": str(_review_dir(review_root) / "evolution_log.jsonl"),
        },
    }
    if not dry_run:
        payload = {
            "market": STYLE_REVIEW_MARKET,
            "generated_at": result["generated_at"],
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
            "real_trading_enabled": False,
            "styles": weights,
            "rankings": rankings,
        }
        _write_json(style_weights_path(review_root), payload)
        _write_json(evolution_plan_path(review_root), result)
        _append_jsonl(_review_dir(review_root) / "evolution_log.jsonl", result)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate simulated-only CNFutures style evolution.")
    parser.add_argument("--strategy-dir", type=Path, default=DEFAULT_STRATEGY_DIR)
    parser.add_argument("--review-root", type=Path, default=DEFAULT_REVIEW_ROOT)
    parser.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES)
    parser.add_argument("--max-variants-per-cycle", type=int, default=DEFAULT_MAX_VARIANTS_PER_CYCLE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = evaluate_styles(
        strategy_dir=args.strategy_dir,
        review_root=args.review_root,
        min_trades=args.min_trades,
        max_variants_per_cycle=args.max_variants_per_cycle,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


__all__ = [
    "evaluate_styles",
    "generated_styles_dir",
    "load_runtime_styles",
    "style_weights_path",
]


if __name__ == "__main__":
    raise SystemExit(main())
