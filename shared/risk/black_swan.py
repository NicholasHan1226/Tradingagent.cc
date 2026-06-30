#!/usr/bin/env python3
"""黑天鹅应急 — 大盘 -3% / 重大政策 / 流动性危机 → 强制减仓。

check_black_swan(market_data) → {triggered, action, trigger_reason, force_reduce_to}
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

_DEFAULT_BLACK_SWAN = {
    "market_drop_pct": -0.03,
    "force_reduce_to": 0.50,
    "reduce_window_min": 30,
}

_LIMITS_PATH = Path(__file__).resolve().parent / "risk_limits.yaml"


def _load_black_swan_config() -> dict[str, Any]:
    """加载黑天鹅配置。"""
    defaults = dict(_DEFAULT_BLACK_SWAN)
    if yaml is None:
        return defaults
    try:
        with open(_LIMITS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            bs = data.get("black_swan")
            if isinstance(bs, dict):
                defaults.update(bs)
    except (OSError, yaml.YAMLError):
        pass
    return defaults


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def check_black_swan(market_data: dict[str, Any]) -> dict[str, Any]:
    """黑天鹅检测主函数。

    Args:
        market_data: {
            "market_change_pct": float,     # 大盘涨跌幅
            "policy_shock": str | None,     # 重大政策事件描述 (None=无)
            "liquidity_stress": bool,       # 流动性危机标志
            "vix_spike": bool,              # VIX 飙升 (可选)
            "bond_yield_spike": bool,       # 国债收益率飙升 (可选)
            "fx_shock": bool,               # 汇率冲击 (可选)
        }

    Returns:
        {
            "triggered": bool,
            "action": str,           # "force_reduce" | "monitor" | "normal"
            "trigger_reason": str,
            "force_reduce_to": float,  # 强制减仓目标
            "reduce_window_min": int,
            "timestamp": str,
        }
    """
    if not isinstance(market_data, dict):
        market_data = {}

    config = _load_black_swan_config()
    market_drop_threshold = _safe_float(config.get("market_drop_pct", -0.03))
    force_reduce_to = _safe_float(config.get("force_reduce_to", 0.50))
    reduce_window = int(config.get("reduce_window_min", 30))

    triggers: list[str] = []
    action = "normal"

    # 1. 大盘跌幅触发
    market_change = _safe_float(market_data.get("market_change_pct", 0.0))
    if market_change <= market_drop_threshold:
        triggers.append(f"大盘跌幅 {market_change:.4f} ≤ {market_drop_threshold:.4f}")

    # 2. 重大政策触发
    policy_shock = market_data.get("policy_shock")
    if policy_shock and str(policy_shock).strip():
        triggers.append(f"重大政策冲击: {policy_shock}")

    # 3. 流动性危机触发
    if market_data.get("liquidity_stress"):
        triggers.append("流动性危机标志触发")

    # 4. VIX 飙升 (可选)
    if market_data.get("vix_spike"):
        triggers.append("VIX 飙升")

    # 5. 国债收益率飙升 (可选)
    if market_data.get("bond_yield_spike"):
        triggers.append("国债收益率飙升")

    # 6. 汇率冲击 (可选)
    if market_data.get("fx_shock"):
        triggers.append("汇率冲击")

    triggered = len(triggers) > 0
    if triggered:
        action = "force_reduce"
        trigger_reason = "; ".join(triggers)
    else:
        # 检查是否需要监控 (接近但未触发)
        if market_change <= market_drop_threshold * 0.7:  # 接近阈值 70%
            action = "monitor"
            trigger_reason = f"接近黑天鹅阈值: 大盘跌幅 {market_change:.4f} (阈值 {market_drop_threshold:.4f})"
        else:
            trigger_reason = "正常"

    return {
        "triggered": triggered,
        "action": action,
        "trigger_reason": trigger_reason,
        "force_reduce_to": force_reduce_to if triggered else 1.0,
        "reduce_window_min": reduce_window if triggered else 0,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "triggers": triggers,
    }


def compute_force_reduce(
    current_exposure: float,
    target_exposure: float,
) -> dict[str, Any]:
    """计算强制减仓的具体操作。

    Args:
        current_exposure: 当前总敞口 (0-1)
        target_exposure: 目标敞口 (如 0.50)

    Returns:
        {
            "need_reduce": bool,
            "reduce_amount": float,   # 需要减少的敞口
            "reduce_pct": float,      # 减少比例
            "target_exposure": float,
        }
    """
    current = _safe_float(current_exposure, 0.0)
    target = _safe_float(target_exposure, 0.5)
    if current <= target:
        return {
            "need_reduce": False,
            "reduce_amount": 0.0,
            "reduce_pct": 0.0,
            "target_exposure": target,
        }
    reduce_amount = current - target
    reduce_pct = reduce_amount / current if current > 0 else 0.0
    return {
        "need_reduce": True,
        "reduce_amount": round(reduce_amount, 6),
        "reduce_pct": round(reduce_pct, 6),
        "target_exposure": target,
    }


if __name__ == "__main__":
    import json
    # 测试: 大盘 -3.5%
    test_data = {"market_change_pct": -0.035, "policy_shock": None, "liquidity_stress": False}
    r = check_black_swan(test_data)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("--- force reduce ---")
    print(json.dumps(compute_force_reduce(0.75, r["force_reduce_to"]), ensure_ascii=False, indent=2))

def auto_detect(market_data=None, vix=None, recent_events=None):
    alerts = []
    if market_data and market_data.get("pct_change", 0) < -3.0: alerts.append({"type": "market_drop"})
    if vix and vix > 30: alerts.append({"type": "vix_spike"})
    if recent_events:
        for e in recent_events:
            if "policy" in str(e.get("event_type", "")).lower(): alerts.append({"type": "policy_shock"})
    return {"triggered": len(alerts) > 0, "alerts": alerts}
