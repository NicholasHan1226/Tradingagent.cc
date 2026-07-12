"""A-share capital planning for the independent 50,000 CNY simulation account.

This module is a planning read model.  It never creates an order, reserves
capital, or promotes a strategy.  Hard execution checks (100-share lots,
price/fee/slippage evidence, T+1, liquidity, and ledger reservation) remain in
the downstream execution boundary.

The canonical policy is :class:`shared.capital.market_policy.MarketPolicy` for
``ashare``.  Historical tier capital and the retired shared-master allocation
are intentionally not accepted as sizing authorities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from shared.capital.market_policy import MarketPolicy


CAPITAL_POLICY = MarketPolicy.load("ashare")
TOTAL_CAPITAL = int(CAPITAL_POLICY.initial_equity_cny)
MAX_SINGLE_POSITION_PCT = float(CAPITAL_POLICY.single_name_max_pct or 0.0)
MAX_POSITION_VALUE = int(CAPITAL_POLICY.single_name_cap_cny)
STOCK_EXPOSURE_LIMIT_CNY = float(CAPITAL_POLICY.stock_gross_exposure_limit_cny)
POSITION_CAPACITY = 8

# Compatibility constants consumed by the replacement-budget adapter.  They
# are not fixed target allocations and the final lot/cost gate remains
# downstream.
MIN_POSITION_VALUE = 5_000
MIN_CASH_RESERVE = 0
MAX_CASH_RESERVE = 0
TARGET_POSITIONS = (0, POSITION_CAPACITY)

REVERSE_REPO_CODE = "204001"
MAX_EXPLORATION_NEW_POSITIONS_PER_DAY = 1
EXPLORATION_TOTAL_EXPOSURE_LIMIT_CNY = float(MAX_POSITION_VALUE)
EXPLORATION_DAILY_LOSS_LIMIT_CNY = round(
    EXPLORATION_TOTAL_EXPOSURE_LIMIT_CNY * CAPITAL_POLICY.daily_loss_pause_pct,
    2,
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return (
        parsed
        if parsed == parsed and parsed not in (float("inf"), float("-inf"))
        else default
    )


def _context_float(context: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    return _safe_float(context.get(key), default)


def _symbol(row: Mapping[str, Any]) -> str:
    return (
        str(row.get("ts_code") or row.get("symbol") or row.get("code") or "")
        .strip()
        .upper()
    )


def _market_value(row: Mapping[str, Any]) -> float:
    for key in ("market_value_cny", "market_value", "value", "amount"):
        value = _safe_float(row.get(key), -1.0)
        if value >= 0.0:
            return value
    return 0.0


def _candidate_score(candidate: Mapping[str, Any]) -> float:
    # This is a ranking score, not a calibrated probability.
    for key in (
        "raw_style_score",
        "combined",
        "score",
        "total",
        "belief_score",
        "confidence",
        "weight",
    ):
        value = _safe_float(candidate.get(key), float("nan"))
        if value == value:
            return value
    return 0.0


def _explicit_false(row: Mapping[str, Any], *keys: str) -> bool:
    return any(key in row and row.get(key) is False for key in keys)


def _requested_budget(candidate: Mapping[str, Any]) -> float | None:
    for key in (
        "worst_case_budget_cny",
        "requested_budget_cny",
        "requested_budget",
        "position_budget_cny",
        "allocation",
    ):
        if key not in candidate:
            continue
        value = _safe_float(candidate.get(key), -1.0)
        if value > 0.0:
            return value
    return None


def _aggregate_positions(
    holdings: Sequence[dict[str, Any]],
) -> tuple[dict[str, float], float, int]:
    by_symbol: dict[str, float] = {}
    anonymous_total = 0.0
    anonymous_count = 0
    for raw in holdings:
        if not isinstance(raw, Mapping):
            continue
        value = max(0.0, _market_value(raw))
        symbol = _symbol(raw)
        if symbol:
            by_symbol[symbol] = by_symbol.get(symbol, 0.0) + value
        else:
            anonymous_total += value
            anonymous_count += 1
    total = sum(by_symbol.values()) + anonymous_total
    return by_symbol, round(total, 2), len(by_symbol) + anonymous_count


def _aggregate_pending_reservations(
    context: Mapping[str, Any],
    candidates: Sequence[dict[str, Any]],
) -> tuple[dict[str, float], float]:
    by_symbol: dict[str, float] = {}
    raw_rows = context.get("pending_buy_reservations")
    if isinstance(raw_rows, Mapping):
        for raw_symbol, raw_value in raw_rows.items():
            symbol = str(raw_symbol or "").strip().upper()
            if symbol:
                by_symbol[symbol] = by_symbol.get(symbol, 0.0) + max(
                    0.0, _safe_float(raw_value)
                )
    elif isinstance(raw_rows, Sequence) and not isinstance(raw_rows, (str, bytes)):
        for row in raw_rows:
            if not isinstance(row, Mapping):
                continue
            symbol = _symbol(row)
            if not symbol:
                continue
            value = 0.0
            for key in ("reserved_cny", "pending_buy_reserved_cny", "amount", "value"):
                if key in row:
                    value = max(0.0, _safe_float(row.get(key)))
                    break
            by_symbol[symbol] = by_symbol.get(symbol, 0.0) + value

    # Some callers attach a same-symbol pending snapshot directly to the
    # candidate.  Use the maximum declared value per symbol to avoid counting
    # duplicate style rows as separate reservations.
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        symbol = _symbol(candidate)
        if not symbol or "pending_buy_reserved_cny" not in candidate:
            continue
        declared = max(0.0, _safe_float(candidate.get("pending_buy_reserved_cny")))
        by_symbol[symbol] = max(by_symbol.get(symbol, 0.0), declared)

    attributed_total = sum(by_symbol.values())
    declared_total = max(0.0, _context_float(context, "pending_buy_reserved_cny"))
    total = max(attributed_total, declared_total)
    return by_symbol, round(total, 2)


def _existing_exploration_exposure(holdings: Sequence[dict[str, Any]]) -> float:
    intents = {"exploration", "sample_collection", "cumulative_sample_collection"}
    total = 0.0
    for raw in holdings:
        if not isinstance(raw, Mapping):
            continue
        if "exploration_exposure_cny" in raw:
            total += max(0.0, _safe_float(raw.get("exploration_exposure_cny")))
        elif str(raw.get("sample_intent") or "").strip().lower() in intents:
            total += max(0.0, _market_value(raw))
    return round(total, 2)


def _dynamic_operating_cash(
    context: Mapping[str, Any],
) -> tuple[float, dict[str, float]]:
    components = {
        "frozen_cash_cny": max(0.0, _context_float(context, "frozen_cash_cny")),
        "expected_execution_cost_buffer_cny": max(
            0.0, _context_float(context, "expected_execution_cost_buffer_cny")
        ),
        "lot_rounding_cash_cny": max(
            0.0, _context_float(context, "lot_rounding_cash_cny")
        ),
        "other_operating_cash_cny": max(
            0.0, _context_float(context, "other_operating_cash_cny")
        ),
    }
    component_total = sum(components.values())
    explicit_total = max(0.0, _context_float(context, "dynamic_operating_cash_cny"))
    return round(max(component_total, explicit_total), 2), {
        key: round(value, 2) for key, value in components.items() if value > 0.0
    }


def _is_sample_debt(context: Mapping[str, Any]) -> bool:
    minimum = max(0.0, _context_float(context, "min_strategy_samples"))
    valid = max(0.0, _context_float(context, "strategy_sample_valid_count"))
    return minimum > 0.0 and valid < minimum


def _hard_new_risk_blockers(context: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if context.get("force_defensive") is True:
        blockers.append("forced_defensive")
    if context.get("new_risk_allowed") is False:
        blockers.append("new_risk_not_allowed")
    if context.get("risk_gate_passed") is False:
        blockers.append("risk_gate_failed")
    explicit = context.get("hard_gate_blockers")
    if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
        blockers.extend(str(value).strip() for value in explicit if str(value).strip())
    return list(dict.fromkeys(blockers))


def _exploration_limits(
    *,
    existing_exposure_cny: float,
    pending_exposure_cny: float,
    daily_loss_used_cny: float,
    existing_new_positions_today: int,
) -> dict[str, Any]:
    committed = max(0.0, existing_exposure_cny) + max(0.0, pending_exposure_cny)
    return {
        "max_new_positions_per_day": MAX_EXPLORATION_NEW_POSITIONS_PER_DAY,
        "single_name_limit_cny": float(MAX_POSITION_VALUE),
        "total_exposure_limit_cny": EXPLORATION_TOTAL_EXPOSURE_LIMIT_CNY,
        "daily_loss_limit_cny": EXPLORATION_DAILY_LOSS_LIMIT_CNY,
        "existing_exposure_cny": round(existing_exposure_cny, 2),
        "pending_exposure_cny": round(pending_exposure_cny, 2),
        "remaining_exposure_cny": round(
            max(0.0, EXPLORATION_TOTAL_EXPOSURE_LIMIT_CNY - committed), 2
        ),
        "existing_new_positions_today": max(0, existing_new_positions_today),
        "remaining_new_positions_today": max(
            0,
            MAX_EXPLORATION_NEW_POSITIONS_PER_DAY
            - max(0, existing_new_positions_today),
        ),
        "daily_loss_used_cny": round(max(0.0, daily_loss_used_cny), 2),
        "daily_loss_remaining_cny": round(
            max(0.0, EXPLORATION_DAILY_LOSS_LIMIT_CNY - daily_loss_used_cny), 2
        ),
        "policy_initial_equity_cny": CAPITAL_POLICY.initial_equity_cny,
        "single_name_max_pct": CAPITAL_POLICY.single_name_max_pct,
        "daily_loss_pause_pct": CAPITAL_POLICY.daily_loss_pause_pct,
        "lot_sizing_owner": "downstream_execution_gate",
    }


def _reason(code: str, amount: float, details: str) -> dict[str, Any]:
    return {"code": code, "amount_cny": round(max(0.0, amount), 2), "details": details}


@dataclass
class CapitalPlan:
    """Auditable output of :func:`plan_capital`."""

    available_cash: float
    deployed_capital: float
    cash_reserve: float
    target_positions: int = POSITION_CAPACITY
    max_new_positions: int = 0
    existing_position_count: int = 0
    capacity_reason: str = ""
    cash_reserve_pct: float = 0.0
    max_single_position_pct: float = MAX_SINGLE_POSITION_PCT
    risk_mode: str = "normal"
    suggested_buys: list[dict[str, Any]] = field(default_factory=list)
    position_budget_by_symbol: dict[str, float] = field(default_factory=dict)
    reverse_repo: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    dynamic_probe_budget: dict[str, Any] | None = None
    sample_intent: str = "observation"
    exploration_limits: dict[str, Any] = field(default_factory=dict)
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "available_cash": self.available_cash,
            "deployed_capital": self.deployed_capital,
            "cash_reserve": self.cash_reserve,
            "target_positions": self.target_positions,
            "max_new_positions": self.max_new_positions,
            "existing_position_count": self.existing_position_count,
            "capacity_reason": self.capacity_reason,
            "cash_reserve_pct": self.cash_reserve_pct,
            "max_single_position_pct": self.max_single_position_pct,
            "risk_mode": self.risk_mode,
            "suggested_buys": self.suggested_buys,
            "position_budget_by_symbol": self.position_budget_by_symbol,
            "reverse_repo": self.reverse_repo,
            "notes": self.notes,
            "reasons": self.reasons,
            "sample_intent": self.sample_intent,
            "exploration_limits": self.exploration_limits,
        }
        payload.update(self.audit)
        if self.dynamic_probe_budget is not None:
            payload["dynamic_probe_budget"] = self.dynamic_probe_budget
        return payload


def plan_capital(
    holdings: Sequence[dict[str, Any]],
    available_cash: float,
    candidates: Sequence[dict[str, Any]] | None = None,
    *,
    dynamic: bool = False,
    market_context: dict[str, Any] | None = None,
    total_capital: float | None = None,
) -> CapitalPlan:
    """Build a suggestion-only plan under the independent A-share policy.

    ``total_capital`` is retained for caller compatibility but cannot mint
    capacity.  Policy limits always come from ``MarketPolicy.load("ashare")``.
    """

    context: dict[str, Any] = dict(market_context or {})
    candidate_rows = [row for row in (candidates or []) if isinstance(row, dict)]
    free_cash = max(0.0, _safe_float(available_cash))
    positions_by_symbol, deployed, existing_position_count = _aggregate_positions(
        [row for row in holdings if isinstance(row, dict)]
    )
    pending_by_symbol, pending_total = _aggregate_pending_reservations(
        context, candidate_rows
    )
    committed_stock_exposure = round(deployed + pending_total, 2)
    remaining_stock_budget = max(
        0.0, STOCK_EXPOSURE_LIMIT_CNY - committed_stock_exposure
    )
    dynamic_cash, dynamic_cash_components = _dynamic_operating_cash(context)
    deployable_cash = round(
        min(max(0.0, free_cash - dynamic_cash), remaining_stock_budget), 2
    )

    existing_symbols = set(positions_by_symbol)
    remaining_position_slots = max(0, POSITION_CAPACITY - existing_position_count)
    hard_blockers = _hard_new_risk_blockers(context)

    direct_loss = max(0.0, _context_float(context, "exploration_daily_loss_cny"))
    realized_loss = max(
        0.0, -_context_float(context, "exploration_daily_realized_pnl_cny")
    )
    exploration_daily_loss_used = max(direct_loss, realized_loss)
    exploration_existing = _existing_exploration_exposure(holdings)
    exploration_pending = max(
        0.0, _context_float(context, "exploration_pending_reserved_cny")
    )
    exploration_new_today = max(
        0, int(_context_float(context, "existing_exploration_new_positions"))
    )
    exploration_limits = _exploration_limits(
        existing_exposure_cny=exploration_existing,
        pending_exposure_cny=exploration_pending,
        daily_loss_used_cny=exploration_daily_loss_used,
        existing_new_positions_today=exploration_new_today,
    )

    explicit_exploration = any(
        str(row.get("sample_intent") or "").strip().lower() == "exploration"
        for row in candidate_rows
    )
    exploration_mode = (
        bool(candidate_rows)
        and not hard_blockers
        and context.get("exploration_enabled") is not False
        and (explicit_exploration or _is_sample_debt(context))
    )

    qualified_count = 0
    execution_eligible_count = 0
    candidate_rejections: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    planned_new_symbols: set[str] = set()

    indexed_candidates = list(enumerate(candidate_rows))
    if explicit_exploration:
        # Reserve planning capacity for the explicitly selected exploration
        # candidate before ordinary ranked rows can consume all eight slots.
        indexed_candidates.sort(
            key=lambda item: (
                str(item[1].get("sample_intent") or "").strip().lower()
                != "exploration",
                item[0],
            )
        )

    for index, candidate in indexed_candidates:
        symbol = _symbol(candidate)
        if not symbol:
            candidate_rejections.append(
                {"candidate_index": index, "symbol": "", "code": "missing_symbol"}
            )
            continue
        if _explicit_false(
            candidate, "data_qualified", "source_qualified", "qualified"
        ):
            candidate_rejections.append(
                {
                    "candidate_index": index,
                    "symbol": symbol,
                    "code": "data_not_qualified",
                }
            )
            continue
        qualified_count += 1
        if _explicit_false(
            candidate,
            "execution_eligible",
            "risk_gate_passed",
            "liquidity_qualified",
            "price_evidence_valid",
        ):
            candidate_rejections.append(
                {
                    "candidate_index": index,
                    "symbol": symbol,
                    "code": "execution_not_eligible",
                }
            )
            continue
        execution_eligible_count += 1
        if symbol in seen_symbols:
            candidate_rejections.append(
                {
                    "candidate_index": index,
                    "symbol": symbol,
                    "code": "duplicate_candidate_symbol",
                }
            )
            continue
        seen_symbols.add(symbol)

        current_symbol_commitment = positions_by_symbol.get(
            symbol, 0.0
        ) + pending_by_symbol.get(symbol, 0.0)
        symbol_headroom = max(0.0, MAX_POSITION_VALUE - current_symbol_commitment)
        if symbol_headroom <= 1e-9:
            candidate_rejections.append(
                {
                    "candidate_index": index,
                    "symbol": symbol,
                    "code": "single_name_aggregate_limit_reached",
                    "committed_symbol_exposure_cny": round(
                        current_symbol_commitment, 2
                    ),
                }
            )
            continue

        is_new_symbol = symbol not in existing_symbols
        if is_new_symbol and len(planned_new_symbols) >= remaining_position_slots:
            candidate_rejections.append(
                {
                    "candidate_index": index,
                    "symbol": symbol,
                    "code": "position_capacity_reached",
                }
            )
            continue

        eligible.append(
            {
                "candidate": candidate,
                "symbol": symbol,
                "symbol_headroom": symbol_headroom,
                "is_new_symbol": is_new_symbol,
            }
        )
        if is_new_symbol:
            planned_new_symbols.add(symbol)

    reasons: list[str] = []
    notes: list[str] = []
    risk_mode = "normal"
    sample_intent = "exploitation"
    max_new_positions = remaining_position_slots
    capacity_reason = (
        "new_position_capacity_available"
        if remaining_position_slots > 0
        else "position_capacity_reached"
    )

    if hard_blockers:
        eligible = []
        risk_mode = "risk_halt"
        sample_intent = "observation"
        max_new_positions = 0
        capacity_reason = hard_blockers[0]
        reasons.extend(hard_blockers)
    elif remaining_stock_budget <= 1e-9:
        eligible = []
        risk_mode = "risk_limit"
        sample_intent = "observation"
        max_new_positions = 0
        capacity_reason = "portfolio_stock_exposure_limit_reached"
        reasons.append(capacity_reason)
    elif deployable_cash <= 1e-9:
        eligible = []
        risk_mode = "cash_constrained"
        sample_intent = "observation"
        max_new_positions = 0
        capacity_reason = "insufficient_deployable_cash"
        reasons.append(capacity_reason)
    elif exploration_mode:
        remaining_exploration = exploration_limits["remaining_exposure_cny"]
        if explicit_exploration:
            normal_rows = [
                row
                for row in eligible
                if str(row["candidate"].get("sample_intent") or "").strip().lower()
                != "exploration"
            ]
            exploration_rows = sorted(
                (
                    row
                    for row in eligible
                    if str(row["candidate"].get("sample_intent") or "").strip().lower()
                    == "exploration"
                ),
                key=lambda row: _candidate_score(row["candidate"]),
                reverse=True,
            )
            exploration_block = ""
            if exploration_daily_loss_used >= EXPLORATION_DAILY_LOSS_LIMIT_CNY:
                exploration_block = "exploration_daily_loss_limit_reached"
            elif exploration_new_today >= MAX_EXPLORATION_NEW_POSITIONS_PER_DAY:
                exploration_block = "exploration_daily_new_position_limit_reached"
            elif remaining_exploration <= 0.0:
                exploration_block = "exploration_total_exposure_limit_reached"

            if exploration_block:
                for row in exploration_rows:
                    candidate_rejections.append(
                        {"symbol": row["symbol"], "code": exploration_block}
                    )
                exploration_rows = []
                reasons.append(exploration_block)
            else:
                for row in exploration_rows[1:]:
                    candidate_rejections.append(
                        {
                            "symbol": row["symbol"],
                            "code": "exploration_daily_selection_limit",
                        }
                    )
                exploration_rows = exploration_rows[:1]

            eligible = exploration_rows + normal_rows
            if exploration_rows and normal_rows:
                risk_mode = "mixed_sampling"
                sample_intent = "mixed"
                capacity_reason = "mixed_exploration_exploitation_capacity_available"
            elif exploration_rows:
                risk_mode = "sample_collection"
                sample_intent = "exploration"
                max_new_positions = min(
                    MAX_EXPLORATION_NEW_POSITIONS_PER_DAY, remaining_position_slots
                )
                capacity_reason = "exploration_capacity_available"
            elif normal_rows:
                risk_mode = "normal"
                sample_intent = "exploitation"
                capacity_reason = (
                    "normal_capacity_available_exploration_blocked"
                    if exploration_block
                    else "new_position_capacity_available"
                )
            else:
                risk_mode = "observation_only"
                sample_intent = "observation"
                max_new_positions = 0
                capacity_reason = (
                    exploration_block or "no_execution_eligible_candidates"
                )
            reasons.append("explicit_exploration_selection")
        else:
            risk_mode = "sample_collection"
            sample_intent = "exploration"
            if exploration_daily_loss_used >= EXPLORATION_DAILY_LOSS_LIMIT_CNY:
                eligible = []
                max_new_positions = 0
                capacity_reason = "exploration_daily_loss_limit_reached"
                reasons.append(capacity_reason)
            elif exploration_new_today >= MAX_EXPLORATION_NEW_POSITIONS_PER_DAY:
                eligible = []
                max_new_positions = 0
                capacity_reason = "exploration_daily_new_position_limit_reached"
                reasons.append(capacity_reason)
            elif remaining_exploration <= 0.0:
                eligible = []
                max_new_positions = 0
                capacity_reason = "exploration_total_exposure_limit_reached"
                reasons.append(capacity_reason)
            else:
                eligible = sorted(
                    eligible,
                    key=lambda row: _candidate_score(row["candidate"]),
                    reverse=True,
                )
                for row in eligible[1:]:
                    candidate_rejections.append(
                        {
                            "symbol": row["symbol"],
                            "code": "exploration_daily_selection_limit",
                        }
                    )
                eligible = eligible[:1]
                max_new_positions = min(
                    MAX_EXPLORATION_NEW_POSITIONS_PER_DAY, remaining_position_slots
                )
                capacity_reason = (
                    "exploration_capacity_available"
                    if eligible and max_new_positions > 0
                    else "no_execution_eligible_exploration_candidate"
                )
                remaining_stock_budget = min(
                    remaining_stock_budget, remaining_exploration
                )
                deployable_cash = min(deployable_cash, remaining_exploration)
                reasons.append("sample_collection_before_min_samples")
    elif not eligible:
        risk_mode = "observation_only"
        sample_intent = "observation"
        capacity_reason = "no_execution_eligible_candidates"
        reasons.append(capacity_reason)

    suggested_buys: list[dict[str, Any]] = []
    position_budget_by_symbol: dict[str, float] = {}
    remaining_budget = min(deployable_cash, remaining_stock_budget)
    remaining_exploration_budget = float(exploration_limits["remaining_exposure_cny"])
    selected_count = len(eligible)
    for index, row in enumerate(eligible):
        if remaining_budget <= 1e-9:
            candidate_rejections.append(
                {
                    "symbol": row["symbol"],
                    "code": "portfolio_stock_exposure_limit_reached",
                }
            )
            continue
        remaining_candidates = max(1, selected_count - index)
        fair_share = remaining_budget / remaining_candidates
        request = _requested_budget(row["candidate"])
        requested = fair_share if request is None else request
        candidate_intent = (
            str(row["candidate"].get("sample_intent") or sample_intent).strip().lower()
        )
        if candidate_intent not in {"exploration", "exploitation"}:
            candidate_intent = (
                "exploration" if sample_intent == "exploration" else "exploitation"
            )
        intent_budget_limit = (
            remaining_exploration_budget
            if candidate_intent == "exploration"
            else MAX_POSITION_VALUE
        )
        allocation = min(
            requested,
            row["symbol_headroom"],
            fair_share,
            remaining_budget,
            MAX_POSITION_VALUE,
            intent_budget_limit,
        )
        if allocation <= 1e-9:
            candidate_rejections.append(
                {"symbol": row["symbol"], "code": "no_positive_worst_case_budget"}
            )
            continue
        allocation = round(allocation, 2)
        candidate = row["candidate"]
        suggestion = {
            "code": row["symbol"],
            "ts_code": row["symbol"],
            "allocation": allocation,
            "requested_budget": allocation,
            "risk_limit_budget": round(row["symbol_headroom"], 2),
            "executable_budget": allocation,
            "weight": round(allocation / TOTAL_CAPITAL, 6),
            "raw_ranking_score": round(_candidate_score(candidate), 6),
            "sample_intent": candidate_intent,
            "selection_method": str(
                candidate.get("selection_method")
                or context.get("relative_exploration_selection_method")
                or (
                    "epsilon_greedy_upstream"
                    if candidate_intent == "exploration"
                    else "ranked_candidate_order"
                )
            ),
            "selection_propensity": (
                _safe_float(
                    candidate.get("selection_propensity", candidate.get("propensity"))
                )
                if candidate.get("selection_propensity", candidate.get("propensity"))
                is not None
                else None
            ),
            "lot_sizing_status": "pending_downstream_100_share_and_cost_gate",
        }
        suggested_buys.append(suggestion)
        position_budget_by_symbol[row["symbol"]] = allocation
        remaining_budget = max(0.0, remaining_budget - allocation)
        if candidate_intent == "exploration":
            remaining_exploration_budget = max(
                0.0, remaining_exploration_budget - allocation
            )

    planned_allocation = round(sum(row["allocation"] for row in suggested_buys), 2)
    planned_stock_exposure = round(committed_stock_exposure + planned_allocation, 2)
    undeployed_capital = round(
        max(0.0, CAPITAL_POLICY.initial_equity_cny - committed_stock_exposure), 2
    )
    planned_undeployed_capital = round(
        max(0.0, CAPITAL_POLICY.initial_equity_cny - planned_stock_exposure), 2
    )

    undeployed_reasons: list[dict[str, Any]] = []
    policy_cash = max(
        0.0,
        CAPITAL_POLICY.initial_equity_cny - STOCK_EXPOSURE_LIMIT_CNY,
    )
    if policy_cash > 0.0:
        undeployed_reasons.append(
            _reason(
                "stock_gross_exposure_limit",
                min(undeployed_capital, policy_cash),
                "Not a protected-cash reserve; stock gross exposure is capped at 90% and the cash remains eligible for separate cash management.",
            )
        )
    if dynamic_cash > 0.0:
        undeployed_reasons.append(
            _reason(
                "dynamic_operating_cash",
                min(undeployed_capital, dynamic_cash),
                f"Explicit frozen/cost/rounding evidence: {dynamic_cash_components}",
            )
        )
    if hard_blockers:
        undeployed_reasons.append(
            _reason(
                "hard_new_risk_gate",
                min(undeployed_capital, deployable_cash),
                ",".join(hard_blockers),
            )
        )
    elif execution_eligible_count == 0:
        undeployed_reasons.append(
            _reason(
                "no_execution_eligible_candidates",
                min(undeployed_capital, deployable_cash),
                "Observation remains allowed; no stock order is forced.",
            )
        )
    elif planned_allocation > 0.0:
        undeployed_reasons.append(
            _reason(
                "planned_not_reserved",
                min(undeployed_capital, planned_allocation),
                "Suggestion only; becomes committed only after the capital ledger accepts a reservation.",
            )
        )
    if remaining_position_slots == 0 and deployable_cash > 0.0:
        undeployed_reasons.append(
            _reason(
                "position_capacity_reached",
                min(undeployed_capital, deployable_cash),
                "Eight distinct stock positions already exist; do not force a ninth.",
            )
        )
    if planned_allocation < deployable_cash and execution_eligible_count > 0:
        undeployed_reasons.append(
            _reason(
                "quality_or_candidate_budget_not_forced",
                min(undeployed_capital, deployable_cash - planned_allocation),
                "Capital remains eligible but the plan does not pad weak or undersized candidates.",
            )
        )

    projected_cash_after_plan = max(0.0, free_cash - dynamic_cash - planned_allocation)
    reverse_repo = (
        suggest_reverse_repo(projected_cash_after_plan)
        if projected_cash_after_plan >= 1_000.0
        else None
    )
    cash_management = {
        "auto_order": False,
        "status": "suggestion_only",
        "attribution_bucket": "cash_management_yield",
        "excluded_from_stock_alpha": True,
        "eligible_cash_cny": round(projected_cash_after_plan, 2),
        "suggestion": reverse_repo,
    }

    if (
        total_capital is not None
        and abs(_safe_float(total_capital) - TOTAL_CAPITAL) > 1e-6
    ):
        notes.append(
            "noncanonical_total_capital_ignored; MarketPolicy ashare remains authority"
        )

    exploration_planned_allocation = round(
        sum(
            row["allocation"]
            for row in suggested_buys
            if row.get("sample_intent") == "exploration"
        ),
        2,
    )
    suggestion_intents = {row.get("sample_intent") for row in suggested_buys}
    reported_sample_intent = (
        "mixed"
        if suggestion_intents == {"exploration", "exploitation"}
        else next(iter(suggestion_intents))
        if len(suggestion_intents) == 1
        else "observation"
    )

    dynamic_probe_budget = None
    if exploration_mode:
        dynamic_probe_budget = {
            "min": 0.0,
            "max": exploration_limits["remaining_exposure_cny"],
            "recommended": exploration_planned_allocation,
            "sample_intent": "exploration",
            "selection_method": str(
                context.get("relative_exploration_selection_method")
                or "epsilon_greedy_upstream"
            ),
            "legacy_probe_overrides_applied": False,
        }

    return CapitalPlan(
        available_cash=round(free_cash, 2),
        deployed_capital=round(deployed, 2),
        cash_reserve=round(dynamic_cash, 2),
        target_positions=POSITION_CAPACITY,
        max_new_positions=max_new_positions,
        existing_position_count=existing_position_count,
        capacity_reason=capacity_reason,
        cash_reserve_pct=round(dynamic_cash / TOTAL_CAPITAL, 6),
        max_single_position_pct=MAX_SINGLE_POSITION_PCT,
        risk_mode=risk_mode,
        suggested_buys=suggested_buys,
        position_budget_by_symbol=position_budget_by_symbol,
        reverse_repo=reverse_repo,
        notes=notes,
        reasons=reasons,
        dynamic_probe_budget=dynamic_probe_budget,
        sample_intent=reported_sample_intent,
        exploration_limits=exploration_limits,
        audit={
            "schema_version": "ashare-capital-plan.v2",
            "capital_authority_id": CAPITAL_POLICY.capital_authority_id,
            "authority_generation": CAPITAL_POLICY.authority_generation,
            "cutover_state": CAPITAL_POLICY.cutover_state,
            "initial_equity_cny": CAPITAL_POLICY.initial_equity_cny,
            "stock_exposure_limit_cny": STOCK_EXPOSURE_LIMIT_CNY,
            "deployed_market_value_cny": round(deployed, 2),
            "pending_buy_reserved_cny": round(pending_total, 2),
            "committed_stock_exposure_cny": committed_stock_exposure,
            "planned_stock_exposure_cny": planned_stock_exposure,
            "dynamic_operating_cash_cny": round(dynamic_cash, 2),
            "dynamic_operating_cash_components": dynamic_cash_components,
            "deployable_cash_cny": deployable_cash,
            "deployed_utilization_rate": round(deployed / TOTAL_CAPITAL, 6),
            "committed_utilization_rate": round(
                committed_stock_exposure / TOTAL_CAPITAL, 6
            ),
            "planned_stock_utilization_rate": round(
                planned_stock_exposure / TOTAL_CAPITAL, 6
            ),
            "undeployed_capital_cny": undeployed_capital,
            "planned_undeployed_capital_cny": planned_undeployed_capital,
            "undeployed_reasons": undeployed_reasons,
            "position_capacity": POSITION_CAPACITY,
            "remaining_position_slots": remaining_position_slots,
            "qualified_candidate_count": qualified_count,
            "execution_eligible_candidate_count": execution_eligible_count,
            "candidate_rejections": candidate_rejections,
            "cash_management": cash_management,
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
            "real_trading_enabled": False,
            "capital_layer": "simulated",
        },
    )


def suggest_reverse_repo(idle_cash: float) -> dict[str, Any]:
    """Return a non-executing cash-management suggestion.

    The amount is excluded from stock deployment and stock-alpha attribution.
    """

    cash = max(0.0, _safe_float(idle_cash))
    lots = int(cash // 1_000)
    amount = float(lots * 1_000)
    return {
        "code": REVERSE_REPO_CODE,
        "name": "GC-001 1-day reverse repo",
        "action": "suggest_lend" if lots > 0 else "skip",
        "amount": amount,
        "lots": lots,
        "auto_order": False,
        "status": "suggestion_only",
        "attribution_bucket": "cash_management_yield",
        "excluded_from_stock_alpha": True,
        "instruction": (
            f"Manual review only: consider {REVERSE_REPO_CODE} for {amount:.0f} CNY."
            if lots > 0
            else "Below the 1,000 CNY suggestion threshold."
        ),
    }


__all__ = [
    "CAPITAL_POLICY",
    "CapitalPlan",
    "EXPLORATION_DAILY_LOSS_LIMIT_CNY",
    "EXPLORATION_TOTAL_EXPOSURE_LIMIT_CNY",
    "MAX_POSITION_VALUE",
    "MIN_POSITION_VALUE",
    "POSITION_CAPACITY",
    "STOCK_EXPOSURE_LIMIT_CNY",
    "TOTAL_CAPITAL",
    "plan_capital",
    "suggest_reverse_repo",
]
