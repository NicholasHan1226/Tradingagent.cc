#!/usr/bin/env python3
"""Self-evolution engine for simulated multi-style trading."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.markets.evolution_guard import evaluate_guard
from shared.markets.performance_tracker import compare_styles, load_history
from shared.markets.style_config import (
    generated_styles_dir_for_market,
    styles_dir_for_market,
)


TRADINGAGENT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REVIEW_ROOT = TRADINGAGENT_ROOT / "shared" / "review"
MARKETS = ("crypto", "pm", "us")
RETIRED_SINGLE_ACCOUNT_MARKETS = frozenset({"ashare", "cn_futures"})
MIN_ACTIVE_WEIGHT = 0.02
MAX_ACTIVE_WEIGHT = 0.50
LOSER_DAYS = 3
LOSER_SCORE_THRESHOLD = 0.0
DEFAULT_AUTO_GENERATE = {
    "position_pct": {"min": 0.02, "max": 0.15, "step": 0.01},
    "stop_loss_pct": {"min": -0.15, "max": -0.05, "step": 0.01},
    "take_profit_pct": {"min": 0.05, "max": 0.30, "step": 0.02},
    "conviction_min": {"min": 0.30, "max": 0.80, "step": 0.03},
    "max_hold_days": {"min": 1, "max": 30, "step": 2},
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _normalize_market(market: Any) -> str:
    return str(market or "").strip().lower()


def _require_evolution_market(market: Any) -> str:
    market_key = _normalize_market(market)
    if market_key in RETIRED_SINGLE_ACCOUNT_MARKETS:
        raise RuntimeError(
            f"{market_key} generic auto evolution authority is retired; "
            "SampleJournal/KPI is the only evolution authority"
        )
    return market_key


def _review_dir(market: str, review_root: Path | str | None = None) -> Path:
    root = Path(review_root) if review_root is not None else DEFAULT_REVIEW_ROOT
    return root / _normalize_market(market)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return default if number != number else number
    except (TypeError, ValueError):
        return default


def _style_files(market: str, styles_dir: Path | str | None = None) -> list[Path]:
    directory = (
        Path(styles_dir) if styles_dir is not None else styles_dir_for_market(market)
    )
    return sorted(directory.glob("*.json")) if directory.exists() else []


def _read_style_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_style_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _load_styles(
    market: str,
    styles_dir: Path | str | None = None,
    *,
    review_root: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    styles: dict[str, dict[str, Any]] = {}
    paths = _style_files(market, styles_dir)
    paths.extend(
        sorted(
            generated_styles_dir_for_market(market, review_root=review_root).glob(
                "*.json"
            )
        )
    )
    for path in paths:
        payload = _read_style_file(path)
        name = str(payload.get("name") or path.stem).strip()
        if not name:
            continue
        payload.setdefault("name", name)
        payload.setdefault("_path", str(path))
        styles[name] = payload
    return styles


def _style_status(style: dict[str, Any]) -> str:
    status = str(style.get("status") or "").strip().lower()
    if status in {"active", "paused", "deprecated"}:
        return status
    if not bool(style.get("enabled", True)) or bool(style.get("paused", False)):
        return "paused"
    return "active"


def _set_status(style: dict[str, Any], status: str) -> None:
    style["status"] = status
    style["enabled"] = status == "active"
    if status == "paused":
        style["paused"] = True
    else:
        style.pop("paused", None)
    style["last_modified"] = _now_iso()


def _style_weight(style: dict[str, Any]) -> float:
    return max(0.0, _safe_float(style.get("weight"), 1.0))


def _normalize_weights(styles: dict[str, dict[str, Any]]) -> None:
    active = [style for style in styles.values() if _style_status(style) == "active"]
    total = sum(_style_weight(style) for style in active)
    if not active:
        return
    if total <= 0:
        equal = round(1.0 / len(active), 6)
        for style in active:
            style["weight"] = equal
        return
    for style in active:
        style["weight"] = round(_style_weight(style) / total, 6)
    inactive = [style for style in styles.values() if _style_status(style) != "active"]
    for style in inactive:
        style["weight"] = round(min(_style_weight(style), MIN_ACTIVE_WEIGHT), 6)


def _append_evolution_log(
    market: str,
    record: dict[str, Any],
    *,
    review_root: Path | str | None = None,
) -> None:
    output_dir = _review_dir(market, review_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "evolution_log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _write_weights_snapshot(
    market: str,
    styles: dict[str, dict[str, Any]],
    rankings: list[dict[str, Any]],
    *,
    review_root: Path | str | None = None,
) -> dict[str, Any]:
    payload = {
        "market": _normalize_market(market),
        "generated_at": _now_iso(),
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_execution": False,
        "styles": {
            name: {
                "status": _style_status(style),
                "weight": round(_style_weight(style), 6),
                "generation": int(style.get("generation", 1) or 1),
                "last_modified": style.get("last_modified", ""),
            }
            for name, style in sorted(styles.items())
        },
        "rankings": rankings,
    }
    output_dir = _review_dir(market, review_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "style_weights.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def _consecutive_loser_days(
    style_name: str, market: str, review_root: Path | str | None = None
) -> int:
    rows = [
        row
        for row in load_history(market, days=30, review_root=review_root)
        if row.style_name == style_name
    ]
    daily: dict[str, float] = {}
    for row in rows:
        daily[row.date] = daily.get(row.date, 0.0) + row.pnl
    streak = 0
    for date in sorted(daily, reverse=True):
        if daily[date] < 0:
            streak += 1
            continue
        break
    return streak


def promote_style(style: dict[str, Any]) -> dict[str, Any]:
    before = {"status": _style_status(style), "weight": _style_weight(style)}
    _set_status(style, "active")
    style["weight"] = min(
        MAX_ACTIVE_WEIGHT, max(MIN_ACTIVE_WEIGHT, _style_weight(style) * 1.20)
    )
    return {
        "action": "promoted",
        "before": before,
        "after": {"status": _style_status(style), "weight": style["weight"]},
    }


def demote_style(style: dict[str, Any]) -> dict[str, Any]:
    before = {"status": _style_status(style), "weight": _style_weight(style)}
    style["weight"] = max(MIN_ACTIVE_WEIGHT, _style_weight(style) * 0.75)
    if style["weight"] <= MIN_ACTIVE_WEIGHT:
        _set_status(style, "paused")
    else:
        style["last_modified"] = _now_iso()
    return {
        "action": "demoted",
        "before": before,
        "after": {"status": _style_status(style), "weight": style["weight"]},
    }


def invalidate_style(
    style: dict[str, Any], reason: str = "performance below threshold"
) -> dict[str, Any]:
    before = {"status": _style_status(style), "weight": _style_weight(style)}
    _set_status(style, "deprecated")
    style["deprecated_reason"] = reason
    style["deprecated_at"] = _now_iso()
    style["weight"] = 0.0
    return {
        "action": "deprecated",
        "before": before,
        "after": {"status": _style_status(style), "weight": 0.0},
    }


def _range_for(style: dict[str, Any], field: str) -> dict[str, float]:
    ranges = (
        style.get("auto_generate")
        if isinstance(style.get("auto_generate"), dict)
        else {}
    )
    value = ranges.get(field) if isinstance(ranges, dict) else None
    if isinstance(value, dict):
        return value
    return DEFAULT_AUTO_GENERATE[field]


def _tweak_numeric(style: dict[str, Any], field: str, direction: int) -> float | int:
    spec = _range_for(style, field)
    current = _safe_float(style.get(field))
    step = _safe_float(spec.get("step"), 0.01) * direction
    low = _safe_float(spec.get("min"), current)
    high = _safe_float(spec.get("max"), current)
    value = min(high, max(low, current + step))
    if field == "max_hold_days":
        return int(round(value))
    return round(value, 4)


def generate_variant(
    base_style: dict[str, Any],
    *,
    market: str | None = None,
    review_root: Path | str | None = None,
    styles_dir: Path | str | None = None,
) -> dict[str, Any]:
    base_name = str(base_style.get("name") or "style").strip()
    base_generation = int(base_style.get("generation", 1) or 1)
    today = _today_compact()
    name = f"{base_name}_g{base_generation + 1}_{today}"
    if market:
        directory = generated_styles_dir_for_market(market, review_root=review_root)
    else:
        directory = (
            Path(styles_dir)
            if styles_dir is not None
            else Path(str(base_style.get("_path", "."))).parent
        )
    path = directory / f"{name}.json"
    if path.exists():
        return {"action": "variant_exists", "style_name": name, "path": str(path)}

    variant = deepcopy(
        {key: value for key, value in base_style.items() if key != "_path"}
    )
    variant.update(
        {
            "name": name,
            "status": "active",
            "enabled": True,
            "weight": MIN_ACTIVE_WEIGHT,
            "created_at": _now_iso(),
            "last_modified": _now_iso(),
            "generation": base_generation + 1,
            "parent_style": base_name,
            "description": f"Auto-generated simulated variant from {base_name}.",
        }
    )
    variant["position_pct"] = _tweak_numeric(base_style, "position_pct", -1)
    variant["stop_loss_pct"] = _tweak_numeric(base_style, "stop_loss_pct", 1)
    variant["take_profit_pct"] = _tweak_numeric(base_style, "take_profit_pct", 1)
    variant["conviction_min"] = _tweak_numeric(base_style, "conviction_min", 1)
    variant["max_hold_days"] = _tweak_numeric(base_style, "max_hold_days", -1)
    _write_style_file(path, variant)
    return {
        "action": "variant_generated",
        "style_name": name,
        "base_style": base_name,
        "path": str(path),
    }


def evaluate_and_adjust(
    market: str,
    *,
    review_root: Path | str | None = None,
    styles_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Compare style performance, adjust weights, and log all changes."""

    market_key = _require_evolution_market(market)
    styles = _load_styles(market_key, styles_dir, review_root=review_root)
    rankings = compare_styles(market_key, review_root=review_root)
    by_rank = {row["style_name"]: row for row in rankings}
    actions: list[dict[str, Any]] = []

    if not styles:
        result = {
            "market": market_key,
            "generated_at": _now_iso(),
            "state": "no_styles",
            "actions": actions,
            "rankings": rankings,
        }
        _append_evolution_log(market_key, result, review_root=review_root)
        return result

    if not rankings:
        weights = _write_weights_snapshot(
            market_key, styles, rankings, review_root=review_root
        )
        result = {
            "market": market_key,
            "generated_at": _now_iso(),
            "state": "no_performance_history",
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
            "actions": actions,
            "rankings": rankings,
            "weights": weights.get("styles", {}),
        }
        _append_evolution_log(market_key, result, review_root=review_root)
        return result

    top_name = rankings[0]["style_name"] if rankings else ""
    for name, style in styles.items():
        metric = by_rank.get(name)
        if not metric:
            continue
        action: dict[str, Any] | None = None
        loser_days = _consecutive_loser_days(name, market_key, review_root)
        if (
            loser_days >= LOSER_DAYS
            and metric.get("composite_score", 0.0) <= LOSER_SCORE_THRESHOLD
        ):
            action = invalidate_style(
                style, reason=f"{loser_days} consecutive losing days"
            )
        elif (
            name == top_name
            and metric.get("composite_score", 0.0) > 0
            and metric.get("trend") != "declining"
        ):
            action = promote_style(style)
        elif metric.get("pnl", 0.0) < 0 or metric.get("trend") == "declining":
            action = demote_style(style)
        if action:
            action.update(
                {
                    "market": market_key,
                    "style_name": name,
                    "reason": {
                        "rank": metric.get("rank"),
                        "composite_score": metric.get("composite_score"),
                        "pnl": metric.get("pnl"),
                        "trend": metric.get("trend"),
                        "loser_days": loser_days,
                    },
                }
            )
            actions.append(action)

    if rankings:
        top = rankings[0]
        top_style = styles.get(str(top.get("style_name")))
        if (
            top_style
            and top.get("composite_score", 0.0) > 0
            and top.get("trend") == "improving"
        ):
            variant_action = generate_variant(
                top_style,
                market=market_key,
                review_root=review_root,
                styles_dir=styles_dir,
            )
            if variant_action["action"] == "variant_generated":
                actions.append(
                    {
                        "market": market_key,
                        **variant_action,
                        "reason": "top style improving",
                    }
                )
                variant_path = Path(str(variant_action.get("path", "")))
                variant_payload = _read_style_file(variant_path)
                if variant_payload:
                    variant_payload["_path"] = str(variant_path)
                    styles[str(variant_payload.get("name"))] = variant_payload

    _normalize_weights(styles)
    weights = _write_weights_snapshot(
        market_key, styles, rankings, review_root=review_root
    )
    result = {
        "market": market_key,
        "generated_at": _now_iso(),
        "state": "adjusted" if actions else "observed",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_execution": False,
        "actions": actions,
        "rankings": rankings,
        "weights": weights.get("styles", {}),
    }
    _append_evolution_log(market_key, result, review_root=review_root)
    return result


def evaluate_all_markets(
    markets: tuple[str, ...] = MARKETS,
    *,
    review_root: Path | str | None = None,
) -> dict[str, Any]:
    market_keys = tuple(_require_evolution_market(market) for market in markets)
    guard_state = evaluate_guard(market_keys, review_root=review_root)
    guard_blocked = bool(
        guard_state.get("blocked")
        or guard_state.get("sim_halted")
        or guard_state.get("evolution_paused")
        or guard_state.get("weights_frozen")
        or str(guard_state.get("state") or "").strip().lower()
        in {"blocked", "guard_blocked"}
    )
    if guard_blocked:
        return {
            "generated_at": _now_iso(),
            "state": "guard_blocked",
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
            "markets": [],
            "changed": False,
            "guard": guard_state,
        }

    results = [
        evaluate_and_adjust(market, review_root=review_root) for market in market_keys
    ]
    return {
        "generated_at": _now_iso(),
        "state": "evaluated",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_execution": False,
        "markets": results,
        "changed": any(result.get("actions") for result in results),
        "guard": guard_state,
    }


__all__ = [
    "MARKETS",
    "demote_style",
    "evaluate_all_markets",
    "evaluate_and_adjust",
    "generate_variant",
    "invalidate_style",
    "promote_style",
]
