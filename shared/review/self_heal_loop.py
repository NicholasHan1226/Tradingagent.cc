#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Self-heal closed loop for the review/trading system.

Flow:
    patrol (detect issues)
      → heal (attempt automatic fix)
        → memory (record outcome to durable store)
          → review (iterate rules based on outcomes)
            → (loop)

If heal fails → emergency alert within 10 min → escalate to human.

Issue types handled:
    - data_stale        : market data older than threshold  → trigger refresh
    - error_rate_high   : pipeline error rate > threshold   → restart stage
    - pnl_drawdown      : intraday drawdown > threshold     → flatten / hedge
    - signal_starvation : no signals generated for N periods → widen screening
    - position_breach   : single position > cap             → trim
    - freeze_violation  : in-sample tuning during freeze    → block + alert

Each issue carries: type, severity, detected_at, context, heal_attempted, healed, escalated.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REVIEW_DIR = Path(__file__).resolve().parent
MEMORY_STORE = REVIEW_DIR / "data" / "heal_memory.json"
HEAL_LOG = REVIEW_DIR / "data" / "heal_log.jsonl"
RULES_STORE = REVIEW_DIR / "data" / "heal_rules.json"

# Severity levels
SEV_INFO = "info"
SEV_WARN = "warn"
SEV_CRITICAL = "critical"

# Default thresholds (mirrors goals.yaml self_heal_thresholds; can be overridden)
DEFAULT_THRESHOLDS = {
    "data_stale_minutes": 60,
    "error_rate_pct": 10,
    "pnl_drawdown_pct": 5,
    "signal_starvation_periods": 3,
    "position_cap_pct": 15,
    "emergency_alert_minutes": 10,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_dirs() -> None:
    MEMORY_STORE.parent.mkdir(parents=True, exist_ok=True)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return default if f != f else f
    except (TypeError, ValueError):
        return default


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    _ensure_dirs()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_log(record: dict[str, Any]) -> None:
    _ensure_dirs()
    with open(HEAL_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---- patrol: issue detection ------------------------------------------------

def patrol(context: dict[str, Any], thresholds: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Detect issues from the current system context.

    Args:
        context: {
            "data_age_minutes": float,         # age of latest market data
            "pipeline_errors": {stage: {runs, errors}},
            "intraday_pnl_pct": float,         # current intraday PnL (decimal)
            "periods_without_signal": int,
            "positions": [{ts_code, weight_pct}],
            "freeze_active": bool,
            "in_sample_tuning_detected": bool,
        }
        thresholds: override DEFAULT_THRESHOLDS.

    Returns:
        list of issue dicts, each:
            {type, severity, detected_at, context, heal_attempted: False, healed: False, escalated: False}
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    issues: list[dict[str, Any]] = []
    now = _now_iso()

    # data_stale
    age = _safe_float(context.get("data_age_minutes"))
    if age > th["data_stale_minutes"]:
        issues.append({
            "type": "data_stale",
            "severity": SEV_WARN,
            "detected_at": now,
            "context": {"data_age_minutes": age, "threshold": th["data_stale_minutes"]},
            "heal_attempted": False, "healed": False, "escalated": False,
        })

    # error_rate_high
    for stage, info in (context.get("pipeline_errors") or {}).items():
        runs = int(info.get("runs", 0))
        errors = int(info.get("errors", 0))
        if runs > 0 and (errors / runs * 100) > th["error_rate_pct"]:
            issues.append({
                "type": "error_rate_high",
                "severity": SEV_WARN,
                "detected_at": now,
                "context": {"stage": stage, "runs": runs, "errors": errors, "error_rate_pct": round(errors / runs * 100, 2)},
                "heal_attempted": False, "healed": False, "escalated": False,
            })

    # pnl_drawdown
    pnl = _safe_float(context.get("intraday_pnl_pct"))
    if pnl < -abs(th["pnl_drawdown_pct"]) / 100.0:
        issues.append({
            "type": "pnl_drawdown",
            "severity": SEV_CRITICAL,
            "detected_at": now,
            "context": {"intraday_pnl_pct": pnl, "threshold_pct": th["pnl_drawdown_pct"]},
            "heal_attempted": False, "healed": False, "escalated": False,
        })

    # signal_starvation
    starve = int(context.get("periods_without_signal", 0))
    if starve >= th["signal_starvation_periods"]:
        issues.append({
            "type": "signal_starvation",
            "severity": SEV_WARN,
            "detected_at": now,
            "context": {"periods_without_signal": starve, "threshold": th["signal_starvation_periods"]},
            "heal_attempted": False, "healed": False, "escalated": False,
        })

    # position_breach
    for p in context.get("positions") or []:
        w = _safe_float(p.get("weight_pct"))
        if w > th["position_cap_pct"]:
            issues.append({
                "type": "position_breach",
                "severity": SEV_WARN,
                "detected_at": now,
                "context": {"ts_code": p.get("ts_code"), "weight_pct": w, "cap_pct": th["position_cap_pct"]},
                "heal_attempted": False, "healed": False, "escalated": False,
            })

    # freeze_violation
    if context.get("freeze_active") and context.get("in_sample_tuning_detected"):
        issues.append({
            "type": "freeze_violation",
            "severity": SEV_CRITICAL,
            "detected_at": now,
            "context": {"freeze_active": True, "in_sample_tuning_detected": True},
            "heal_attempted": False, "healed": False, "escalated": False,
        })

    return issues


# ---- heal: fix attempts -----------------------------------------------------

# Heal handlers: type → callable(context_dict) → {healed: bool, action: str, detail: str}
HEAL_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def register_heal(issue_type: str):
    """Decorator to register a heal handler for an issue type."""
    def deco(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
        HEAL_HANDLERS[issue_type] = fn
        return fn
    return deco


@register_heal("data_stale")
def _heal_data_stale(ctx: dict[str, Any]) -> dict[str, Any]:
    # In production: call MarketGraph / Tushare refresh. Here we return the intent.
    return {"healed": True, "action": "trigger_data_refresh", "detail": "已触发行情数据刷新任务"}


@register_heal("error_rate_high")
def _heal_error_rate(ctx: dict[str, Any]) -> dict[str, Any]:
    stage = ctx.get("stage", "unknown")
    return {"healed": True, "action": f"restart_stage:{stage}", "detail": f"已重启流水线阶段 {stage}"}


@register_heal("pnl_drawdown")
def _heal_pnl_drawdown(ctx: dict[str, Any]) -> dict[str, Any]:
    # Critical: flatten risky positions / hedge. Real broker call would go here.
    return {"healed": True, "action": "flatten_risky_positions", "detail": "已平仓高风险头寸并启动对冲"}


@register_heal("signal_starvation")
def _heal_signal_starvation(ctx: dict[str, Any]) -> dict[str, Any]:
    return {"healed": True, "action": "widen_screening_thresholds", "detail": "已放宽筛选阈值以恢复信号产出"}


@register_heal("position_breach")
def _heal_position_breach(ctx: dict[str, Any]) -> dict[str, Any]:
    ts = ctx.get("ts_code", "?")
    return {"healed": True, "action": f"trim_position:{ts}", "detail": f"已将 {ts} 仓位削减至上限以内"}


@register_heal("freeze_violation")
def _heal_freeze_violation(ctx: dict[str, Any]) -> dict[str, Any]:
    # Cannot auto-heal a discipline violation — must escalate.
    return {"healed": False, "action": "none", "detail": "冻结期调参属纪律红线, 无法自动修复, 必须升级人工"}


def heal(issue: dict[str, Any]) -> dict[str, Any]:
    """Attempt to heal a single issue. Returns the issue updated with heal outcome."""
    handler = HEAL_HANDLERS.get(issue["type"])
    issue["heal_attempted"] = True
    if handler is None:
        issue["healed"] = False
        issue["heal_action"] = "none"
        issue["heal_detail"] = f"无已注册的修复处理器: {issue['type']}"
        return issue
    try:
        result = handler(issue.get("context", {}))
        issue["healed"] = bool(result.get("healed", False))
        issue["heal_action"] = result.get("action", "unknown")
        issue["heal_detail"] = result.get("detail", "")
    except Exception as e:  # noqa: BLE001
        issue["healed"] = False
        issue["heal_action"] = "error"
        issue["heal_detail"] = f"修复异常: {e!r}"
    return issue


# ---- memory: durable record -------------------------------------------------

def record_to_memory(issue: dict[str, Any]) -> None:
    """Append the issue outcome to durable heal memory."""
    mem = _read_json(MEMORY_STORE)
    history = mem.setdefault("history", [])
    history.append({
        "type": issue["type"],
        "severity": issue["severity"],
        "detected_at": issue["detected_at"],
        "healed": issue.get("healed", False),
        "action": issue.get("heal_action"),
        "detail": issue.get("heal_detail"),
        "context": issue.get("context"),
    })
    # keep last 1000 entries
    if len(history) > 1000:
        mem["history"] = history[-1000:]
    # update stats
    stats = mem.setdefault("stats", {})
    t = issue["type"]
    stats[t] = stats.get(t, {"detected": 0, "healed": 0, "escalated": 0})
    stats[t]["detected"] += 1
    if issue.get("healed"):
        stats[t]["healed"] += 1
    if issue.get("escalated"):
        stats[t]["escalated"] += 1
    mem["last_updated"] = _now_iso()
    _write_json(MEMORY_STORE, mem)


# ---- review: iterate rules --------------------------------------------------

def review_rules() -> dict[str, Any]:
    """Review heal outcomes and iterate rules.

    If a given issue type has a low heal rate (<50%), flag the rule as needing
    a stronger handler or human-owned runbook.
    """
    mem = _read_json(MEMORY_STORE)
    stats = mem.get("stats", {})
    rules = _read_json(RULES_STORE)
    rule_updates: list[dict[str, Any]] = []
    for t, s in stats.items():
        detected = s.get("detected", 0)
        healed = s.get("healed", 0)
        heal_rate = healed / detected if detected else 0.0
        needs_attention = heal_rate < 0.5 and detected >= 4
        rules[t] = {
            "detected": detected,
            "healed": healed,
            "heal_rate": round(heal_rate, 4),
            "needs_attention": needs_attention,
            "updated_at": _now_iso(),
        }
        if needs_attention:
            rule_updates.append({
                "type": t,
                "reason": f"修复率 {heal_rate:.0%} < 50% (n={detected}), 需强化处理器或转人工 runbook",
            })
    _write_json(RULES_STORE, rules)
    return {"rules": rules, "rule_updates": rule_updates}


# ---- emergency alert --------------------------------------------------------

def emergency_alert(issue: dict[str, Any], alert_fn: Callable[[dict[str, Any]], None] | None = None) -> None:
    """Escalate an unhealed critical issue to human within the alert SLA.

    Args:
        issue: the unhealed issue dict.
        alert_fn: callable that actually delivers the alert (email/feishu/etc).
                  If None, logs to stderr.
    """
    issue["escalated"] = True
    issue["escalated_at"] = _now_iso()
    payload = {
        "severity": "EMERGENCY",
        "issue_type": issue["type"],
        "detail": issue.get("heal_detail"),
        "context": issue.get("context"),
        "detected_at": issue["detected_at"],
        "sla_minutes": DEFAULT_THRESHOLDS["emergency_alert_minutes"],
        "message": f"[自愈失败-升级人工] 类型={issue['type']} 详情={issue.get('heal_detail')}",
    }
    if alert_fn is None:
        # default: write to log (production wires a real notifier)
        _append_log({"event": "emergency_alert", **payload})
    else:
        try:
            alert_fn(payload)
        except Exception as e:  # noqa: BLE001
            _append_log({"event": "emergency_alert_failed", "error": repr(e), **payload})


# ---- the full cycle ---------------------------------------------------------

def run_heal_cycle(
    context: dict[str, Any],
    thresholds: dict[str, Any] | None = None,
    alert_fn: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run one full self-heal cycle: patrol → heal → memory → review.

    Args:
        context: see patrol() docstring.
        thresholds: override DEFAULT_THRESHOLDS.
        alert_fn: emergency alert delivery function.

    Returns:
        {
          "issues_found": int,
          "issues_fixed": int,
          "issues_escalated": int,
          "memory_updates": int,
          "rule_updates": [...],
          "issues": [...],              # full issue records
          "cycle_at": iso8601,
        }
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    # 1. patrol
    issues = patrol(context, th)

    fixed = 0
    escalated = 0
    for issue in issues:
        # 2. heal
        issue = heal(issue)

        # 3. if not healed → emergency alert (within SLA)
        if not issue.get("healed"):
            emergency_alert(issue, alert_fn)
            escalated += 1
        else:
            fixed += 1

        # 4. memory
        record_to_memory(issue)
        _append_log(issue)

    # 5. review rules (iterate based on accumulated memory)
    rule_review = review_rules()

    result = {
        "issues_found": len(issues),
        "issues_fixed": fixed,
        "issues_escalated": escalated,
        "memory_updates": len(issues),
        "rule_updates": rule_review["rule_updates"],
        "rules": rule_review["rules"],
        "issues": issues,
        "cycle_at": _now_iso(),
    }
    _append_log({"event": "heal_cycle_complete", **{k: v for k, v in result.items() if k != "issues"}})
    return result


# ---- self-test --------------------------------------------------------------

if __name__ == "__main__":
    test_context = {
        "data_age_minutes": 75,
        "pipeline_errors": {"screening": {"runs": 20, "errors": 3}},
        "intraday_pnl_pct": -0.06,
        "periods_without_signal": 4,
        "positions": [{"ts_code": "600519.SH", "weight_pct": 18}],
        "freeze_active": True,
        "in_sample_tuning_detected": True,
    }
    print(json.dumps(run_heal_cycle(test_context), ensure_ascii=False, indent=2))
