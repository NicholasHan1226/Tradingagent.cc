"""Daily capital plan generator for a 200 000 RMB A-share account.

Produces a structured capital allocation plan each trading day:

* Allocate to 2-3 positions (50 000 - 70 000 RMB each).
* Reserve 30 000 - 50 000 RMB as cash buffer.
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
from typing import Sequence

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
    suggested_buys: list[dict] = field(default_factory=list)
    reverse_repo: dict | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "available_cash": self.available_cash,
            "deployed_capital": self.deployed_capital,
            "cash_reserve": self.cash_reserve,
            "suggested_buys": self.suggested_buys,
            "reverse_repo": self.reverse_repo,
            "notes": self.notes,
        }


def plan_capital(
    holdings: Sequence[dict],
    available_cash: float,
    candidates: Sequence[dict] | None = None,
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
    n_holdings = len(holdings)
    notes: list[str] = []

    # --- decide how many new positions we can / should open -------------
    max_new = TARGET_POSITIONS[1] - n_holdings  # cap at 3 total
    if max_new < 0:
        max_new = 0
    min_new = max(0, TARGET_POSITIONS[0] - n_holdings)

    # cash needed for reserve
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
    if candidates and max_new > 0 and investable >= MIN_POSITION_VALUE:
        remaining = investable
        slots = min(max_new, len(candidates))

        # equal-weight by default, or use candidate weight if provided
        total_weight = sum(c.get("weight", 1.0) for c in candidates[:slots]) or 1.0

        for i, cand in enumerate(candidates[:slots]):
            if remaining < MIN_POSITION_VALUE:
                notes.append(
                    f"Stopped after {i} buys — remaining cash "
                    f"{remaining:.0f} below min {MIN_POSITION_VALUE}."
                )
                break
            weight = cand.get("weight", 1.0) / total_weight
            alloc = min(
                remaining * weight,
                MAX_POSITION_VALUE,
            )
            alloc = max(MIN_POSITION_VALUE, min(alloc, remaining))
            if alloc < MIN_POSITION_VALUE:
                break
            suggested_buys.append({
                "code": cand.get("code", cand.get("ts_code", f"slot_{i}")),
                "allocation": round(alloc, 2),
                "weight": round(weight, 4),
            })
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
        suggested_buys=suggested_buys,
        reverse_repo=reverse_repo,
        notes=notes,
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
