"""Daily capital plan generator for a 200 000 RMB A-share account.

Produces a structured capital allocation plan each trading day:

* Allocate to 2-3 positions (50 000 - 70 000 RMB each).
* Dynamic cash buffer by risk mode (aggressive ~17.5%, balanced 25% capped at
  50 000, cautious 45%, defensive 100% for weak-candidate / high-risk).
* Suggest 204001 (GC-001) reverse repo for idle funds at close.

Functions
---------
plan_capital(holdings, available_cash)
    Build the day's buy / hold / cash plan given current holdings and cash.
suggest_reverse_repo(idle_cash)
    Return a reverse-repo suggestion dict for end-of-day idle cash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

# ---------------------------------------------------------------------------
# Account constants (200 000 RMB simulated account)
# ---------------------------------------------------------------------------
TOTAL_CAPITAL = 200_000         # total account capital in RMB
MIN_POSITION_VALUE = 50_000     # minimum allocation per position
MAX_POSITION_VALUE = 70_000     # maximum allocation per position
MIN_CASH_RESERVE = 30_000       # minimum cash buffer to keep
MAX_CASH_RESERVE = 50_000       # maximum cash buffer to keep
TARGET_POSITIONS = (2, 3)       # target 2-3 positions
REVERSE_REPO_CODE = "204001"    # GC-001 1-day reverse repo


@dataclass
class CapitalPlan:
    """Structured output of :func:`plan_capital`."""

    available_cash: float
    deployed_capital: float
    cash_reserve: float
    target_positions: int = TARGET_POSITIONS[1]
    max_new_positions: int = 0
    cash_reserve_pct: float = 0.0
    max_single_position_pct: float = 0.0
    risk_mode: str = "static"
    suggested_buys: list[dict] = field(default_factory=list)
    position_budget_by_symbol: dict[str, float] = field(default_factory=dict)
    reverse_repo: dict | None = None
    notes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "available_cash": self.available_cash,
            "deployed_capital": self.deployed_capital,
            "cash_reserve": self.cash_reserve,
            "target_positions": self.target_positions,
            "max_new_positions": self.max_new_positions,
            "cash_reserve_pct": self.cash_reserve_pct,
            "max_single_position_pct": self.max_single_position_pct,
            "risk_mode": self.risk_mode,
            "suggested_buys": self.suggested_buys,
            "position_budget_by_symbol": self.position_budget_by_symbol,
            "reverse_repo": self.reverse_repo,
            "notes": self.notes,
            "reasons": self.reasons,
        }


def _candidate_score(candidate: dict[str, Any]) -> float:
    for key in ("combined", "score", "total", "belief_score", "confidence", "weight"):
        try:
            value = float(candidate.get(key, 0.0))
        except (TypeError, ValueError):
            continue
        if value == value:
            return value
    return 0.0


def _context_float(context: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        value = float(context.get(key, default))
        return value if value == value else default
    except (TypeError, ValueError):
        return default


def _position_key(holding: dict[str, Any]) -> str:
    return str(holding.get("ts_code") or holding.get("code") or holding.get("symbol") or "").strip().upper()


def _count_unique_positions(holdings: Sequence[dict]) -> int:
    symbols = {
        _position_key(holding)
        for holding in holdings
        if isinstance(holding, dict) and _position_key(holding)
    }
    if symbols:
        return len(symbols)
    return len(holdings)


def _dynamic_profile(candidates: Sequence[dict], market_context: dict[str, Any] | None) -> dict[str, Any]:
    context = market_context or {}
    scores = sorted((_candidate_score(cand) for cand in candidates if isinstance(cand, dict)), reverse=True)
    top = scores[0] if scores else 0.0
    avg_top3 = sum(scores[:3]) / max(1, min(3, len(scores)))
    second = scores[1] if len(scores) > 1 else 0.0
    risk_rejection_rate = _context_float(context, "risk_rejection_rate")
    data_issue_rate = _context_float(context, "data_issue_rate")
    recent_win_rate = _context_float(context, "recent_win_rate", 0.5)
    trend = str(context.get("trend") or context.get("market_trend") or "").strip().lower()
    reasons: list[str] = []

    defensive = (
        not scores
        or top < 0.55
        or risk_rejection_rate >= 0.60
        or data_issue_rate >= 0.75
        or bool(context.get("force_defensive"))
    )
    if defensive:
        if not scores:
            reasons.append("no_candidates")
        if top < 0.55:
            reasons.append("weak_candidate_quality")
        if risk_rejection_rate >= 0.60:
            reasons.append("high_risk_rejection_rate")
        if data_issue_rate >= 0.75:
            reasons.append("high_data_issue_rate")
        if context.get("force_defensive"):
            reasons.append("forced_defensive")
        return {
            "risk_mode": "defensive",
            "target_positions": 0,
            "cash_reserve_pct": 1.0,
            "max_single_position_pct": 0.0,
            "max_cash_reserve": None,
            "reasons": reasons,
        }

    max_cash_reserve = None

    if top >= 0.75 and avg_top3 >= 0.65 and risk_rejection_rate <= 0.25 and trend not in {"bearish", "risk_off"}:
        risk_mode = "aggressive"
        target_positions = 3
        cash_reserve_pct = 0.175
        max_single_position_pct = 0.35
        reasons.append("strong_candidate_cluster")
    elif top >= 0.65:
        risk_mode = "balanced"
        target_positions = 2
        cash_reserve_pct = 0.25
        max_single_position_pct = 0.30
        max_cash_reserve = 50000
        reasons.append("qualified_candidate_quality")
    else:
        risk_mode = "cautious"
        target_positions = 1
        cash_reserve_pct = 0.45
        max_single_position_pct = 0.25
        reasons.append("thin_candidate_quality")

    if top - second >= 0.18 and target_positions > 2:
        target_positions = 2
        reasons.append("single_name_score_concentration")
    if recent_win_rate < 0.45:
        target_positions = min(target_positions, 1)
        cash_reserve_pct = max(cash_reserve_pct, 0.50)
        risk_mode = "cautious"
        reasons.append("recent_win_rate_below_threshold")

    return {
        "risk_mode": risk_mode,
        "target_positions": target_positions,
        "cash_reserve_pct": cash_reserve_pct,
        "max_single_position_pct": max_single_position_pct,
        "max_cash_reserve": max_cash_reserve,
        "reasons": reasons,
    }


def plan_capital(
    holdings: Sequence[dict],
    available_cash: float,
    candidates: Sequence[dict] | None = None,
    *,
    dynamic: bool = False,
    market_context: dict[str, Any] | None = None,
    total_capital: float = TOTAL_CAPITAL,
) -> CapitalPlan:
    """Generate a daily capital plan.

    Parameters
    ----------
    holdings
        Current open positions. Each dict should have at least
        ``"value"`` (current market value in RMB).
    available_cash
        Free cash available for new buys (RMB).
    candidates
        Optional ranked buy candidates. Each dict may carry a
        ``"weight"`` (0-1) used to size the allocation. If omitted
        the plan will simply note available capacity.

    Returns
    -------
    CapitalPlan
    """
    deployed = sum(h.get("value", 0.0) for h in holdings)
    n_holdings = _count_unique_positions(holdings)
    notes: list[str] = []
    reasons: list[str] = []

    # --- decide how many new positions we can / should open -------------
    target_positions = TARGET_POSITIONS[1]
    cash_reserve_pct = MIN_CASH_RESERVE / max(float(total_capital), 1.0)
    max_single_position_pct = MAX_POSITION_VALUE / max(float(total_capital), 1.0)
    risk_mode = "static"
    if dynamic:
        profile = _dynamic_profile(candidates or [], market_context)
        target_positions = int(profile["target_positions"])
        cash_reserve_pct = float(profile["cash_reserve_pct"])
        max_single_position_pct = float(profile["max_single_position_pct"])
        risk_mode = str(profile["risk_mode"])
        reasons = list(profile.get("reasons", []))
        max_cash_reserve = profile.get("max_cash_reserve")

    max_new = target_positions - n_holdings
    if max_new < 0:
        max_new = 0

    # cash needed for reserve
    if dynamic:
        cash_reserve = min(float(available_cash), max(0.0, float(total_capital) * cash_reserve_pct))
        if target_positions > 0:
            cash_reserve = max(min(float(available_cash), MIN_CASH_RESERVE), cash_reserve)
            if max_cash_reserve is not None:
                cash_reserve = min(cash_reserve, float(max_cash_reserve))
        else:
            cash_reserve = float(available_cash)
    else:
        cash_reserve = max(MIN_CASH_RESERVE, min(available_cash, MAX_CASH_RESERVE))
    investable = available_cash - cash_reserve

    if investable < MIN_POSITION_VALUE:
        notes.append(
            f"Insufficient investable cash ({investable:.0f} RMB) after "
            f"reserve ({cash_reserve:.0f} RMB); skip new buys."
        )
        max_new = 0

    # --- allocate to candidates -----------------------------------------
    suggested_buys: list[dict] = []
    position_budget_by_symbol: dict[str, float] = {}
    if candidates and max_new > 0 and investable >= MIN_POSITION_VALUE:
        remaining = investable
        slots = min(max_new, len(candidates))
        max_single_value = MAX_POSITION_VALUE
        if dynamic and max_single_position_pct > 0:
            max_single_value = min(MAX_POSITION_VALUE, float(total_capital) * max_single_position_pct)

        for i, cand in enumerate(candidates[:slots]):
            if remaining < MIN_POSITION_VALUE:
                notes.append(
                    f"Stopped after {i} buys — remaining cash "
                    f"{remaining:.0f} below min {MIN_POSITION_VALUE}."
                )
                break
            remaining_slots = max(1, slots - i)
            alloc = min(
                remaining / remaining_slots,
                max_single_value,
            )
            alloc = max(MIN_POSITION_VALUE, min(alloc, remaining))
            if alloc < MIN_POSITION_VALUE:
                break
            code = cand.get("code", cand.get("ts_code", f"slot_{i}"))
            suggested_buys.append({
                "code": code,
                "allocation": round(alloc, 2),
                "weight": round(alloc / max(float(total_capital), 1.0), 4),
                "score": round(_candidate_score(cand), 4),
            })
            position_budget_by_symbol[str(code)] = round(alloc, 2)
            remaining -= alloc

        investable -= sum(b["allocation"] for b in suggested_buys)

    elif not candidates and max_new > 0:
        notes.append(
            f"Capacity for {max_new} new position(s) but no candidates provided."
        )

    # --- reverse repo for truly idle cash -------------------------------
    idle = available_cash - sum(b["allocation"] for b in suggested_buys) - cash_reserve
    reverse_repo = None
    if idle > 1_000:  # only suggest repo for meaningful idle cash
        reverse_repo = suggest_reverse_repo(idle)

    return CapitalPlan(
        available_cash=round(available_cash, 2),
        deployed_capital=round(deployed, 2),
        cash_reserve=round(cash_reserve, 2),
        target_positions=target_positions,
        max_new_positions=max_new,
        cash_reserve_pct=round(cash_reserve_pct, 4),
        max_single_position_pct=round(max_single_position_pct, 4),
        risk_mode=risk_mode,
        suggested_buys=suggested_buys,
        position_budget_by_symbol=position_budget_by_symbol,
        reverse_repo=reverse_repo,
        notes=notes,
        reasons=reasons,
    )


def suggest_reverse_repo(idle_cash: float) -> dict:
    """Suggest 204001 (1-day reverse repo) for *idle_cash* at close.

    Reverse repo is a near-risk-free overnight lending rate available to
    A-share cash accounts. 204001 settles T+1 and is ideal for funds that
    would otherwise sit idle overnight.

    Parameters
    ----------
    idle_cash
        RMB amount available for reverse repo at close.

    Returns
    -------
    dict
        Suggestion with code, amount, and instruction.
    """
    if idle_cash <= 0:
        return {
            "code": REVERSE_REPO_CODE,
            "action": "skip",
            "amount": 0.0,
            "reason": "No idle cash available.",
        }

    # 204001 minimum lot is 1000 RMB (1 lot = 10 units * 100 RMB face)
    lots = int(idle_cash // 1000)
    amount = lots * 1000

    return {
        "code": REVERSE_REPO_CODE,
        "name": "GC-001 1-day reverse repo",
        "action": "lend" if lots > 0 else "skip",
        "amount": float(amount),
        "lots": lots,
        "instruction": (
            f"At 14:50 close, place reverse-repo order {REVERSE_REPO_CODE} "
            f"for {amount:.0f} RMB ({lots} lots) to earn overnight rate."
            if lots > 0
            else f"Idle cash {idle_cash:.0f} below 1000 RMB minimum lot; skip repo."
        ),
    }
