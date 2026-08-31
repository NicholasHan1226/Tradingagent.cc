"""Historical-only daily sampled risk overlay; never a production risk engine.

The original frozen signal and forward plan are not modified. Close-triggered
actions wait for the next observed 00:05 open; unknown intraday paths are never
interpolated. A 7% trigger cannot guarantee a 7% maximum loss. All fills remain
fractional, sampled-price counterfactuals without order-book or PIT authority.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

import Crypto.slow_trend_research as original
from Crypto.research_accounting import number
from shared.capital.market_policy import (
    CANONICAL_DAILY_LOSS_PAUSE_PCT,
    CANONICAL_DRAWDOWN_HALT_PCT,
    CANONICAL_DRAWDOWN_TIGHTEN_PCT,
    CANONICAL_DRAWDOWN_TIGHTEN_RISK_MULTIPLIER,
    CANONICAL_MAX_CONSECUTIVE_LOSSES,
)

ZERO, ONE = Decimal(0), Decimal(1)
CONTRACT = "tradingagent.crypto.slow_trend_risk_replay.v1"
HISTORY_END = datetime(2026, 8, 30, tzinfo=timezone.utc)


def frozen_plan() -> dict[str, Any]:
    """Only risk constants are reused; no domestic account/policy is loaded."""
    return {
        "candidate_id": "slow_trend_sma20_60_daily_sampled_risk_v1",
        "signal_plan_sha256": original.history._sha256(original.frozen_plan()),
        "risk_constant_source": "shared.capital.market_policy.CANONICAL_*; research adaptation only",
        "daily_loss_pause_pct": str(CANONICAL_DAILY_LOSS_PAUSE_PCT),
        "drawdown_tighten_pct": str(CANONICAL_DRAWDOWN_TIGHTEN_PCT),
        "drawdown_halt_pct": str(CANONICAL_DRAWDOWN_HALT_PCT),
        "drawdown_tighten_risk_multiplier": str(CANONICAL_DRAWDOWN_TIGHTEN_RISK_MULTIPLIER),
        "max_consecutive_losses": CANONICAL_MAX_CONSECUTIVE_LOSSES,
        "historical_end_exclusive": original.history._iso(HISTORY_END),
        "day_loss_basis": "previous_UTC_close_equity; first_day_opening_cash",
        "day_reset": "daily_PnL_baseline_only; pause_latch_never_auto_clears",
        "pause_action": "reduce_only; flatten_at_next_observed_execution_open; no_automatic_resume",
        "tightening": "sticky_multiplier; target_exposure_cap_not_guaranteed_stop_loss",
        "losing_streak": "consecutive_timestamp_batches_of_fully_closed_position_episodes; net_all_fees",
        "partial_exit_rule": "accrue_episode_PnL; count_only_when_position_zero",
        "simultaneous_exits": "aggregate_completed_episodes_before_one_streak_update; zero_resets",
        "buy_batch_risk": "observe_after_each_leg; block_remaining_on_pause; cap_remaining_after_tighten",
        "benchmark": "BTC_cash_daily_target_equals_original_causal_trend_gross_target; same_costs_band",
        "parameter_search": False,
        "new_forward_window": None,
    }


class _Risk:
    def __init__(self, initial: Decimal):
        self.plan = frozen_plan()
        self.peak = initial
        self.maximum_drawdown = ZERO
        self.multiplier = ONE
        self.streak = 0
        self.pauses: set[str] = set()
        self.events: list[dict] = []

    def observe(self, equity: Decimal, day_base: Decimal, *, at: datetime, phase: str) -> None:
        self.peak = max(self.peak, equity)
        drawdown = (self.peak - equity) / self.peak
        self.maximum_drawdown = max(self.maximum_drawdown, drawdown)
        checks = {
            "daily_loss_pause": (day_base - equity) / day_base >= number(self.plan["daily_loss_pause_pct"]),
            "drawdown_halt": drawdown >= number(self.plan["drawdown_halt_pct"]),
            "consecutive_loss_pause": self.streak >= self.plan["max_consecutive_losses"],
        }
        for reason, triggered in checks.items():
            if triggered and reason not in self.pauses:
                self.pauses.add(reason)
                self.events.append({"at": original.history._iso(at), "phase": phase, "reason": reason,
                                    "equity": str(equity), "drawdown": str(drawdown),
                                    "streak": self.streak, "action": "reduce_only_no_auto_resume"})
        if drawdown >= number(self.plan["drawdown_tighten_pct"]) and self.multiplier == ONE:
            self.multiplier = number(self.plan["drawdown_tighten_risk_multiplier"])
            self.events.append({"at": original.history._iso(at), "phase": phase,
                                "reason": "drawdown_tighten", "multiplier": str(self.multiplier),
                                "equity": str(equity), "drawdown": str(drawdown)})

    def record_closed_batch(self, pnl: Decimal) -> None:
        self.streak = self.streak + 1 if pnl < ZERO else 0


def _simulate(days: Mapping[str, Mapping], dates: list[datetime], *, mode: str) -> dict:
    """Three modes share accounting; only risk_trend applies the risk latch."""
    if mode not in {"trend", "risk_trend", "btc_cash"}:
        raise ValueError("risk_replay_mode_invalid")
    plan = original.frozen_plan()
    initial = number(plan["starting_research_cash"])
    fee, slip = number(plan["fee_each_side"]), number(plan["slippage_each_side"])
    cash = initial
    quantities = {s: ZERO for s in plan["symbols"]}
    basis = dict(quantities)
    episode_pnl = dict(quantities)
    risk = _Risk(initial)
    ledger, equity, daily, targets, closed = [], [], [], [], []
    fees = turnover = slippage = ZERO
    previous_close = initial

    def value(prices):
        return cash + sum(quantities[s] * prices[s] for s in quantities)

    def mark(prices, day_base, at, phase):
        nav = value(prices)
        risk.observe(nav, day_base, at=at, phase=phase)
        equity.append({"at": original.history._iso(at), "phase": phase, "equity": str(nav),
                       "cash": str(cash), "gross_exposure": str((nav - cash) / nav)})
        return nav

    def trade(symbol, quantity, side, price, at, reason):
        nonlocal cash, fees, turnover, slippage
        if quantity <= ZERO:
            return None
        fill = price * (ONE + slip if side == "buy" else ONE - slip)
        notional = quantity * fill
        cost = notional * fee
        fees += cost
        turnover += notional
        slippage += quantity * abs(fill - price)
        realized = None
        completed = None
        if side == "buy":
            cash -= notional + cost
            quantities[symbol] += quantity
            basis[symbol] += notional + cost
        else:
            allocation = basis[symbol] if quantity == quantities[symbol] else basis[symbol] * quantity / quantities[symbol]
            realized = notional - cost - allocation
            cash += notional - cost
            quantities[symbol] -= quantity
            basis[symbol] -= allocation
            episode_pnl[symbol] += realized
            if quantities[symbol] == ZERO:
                completed = episode_pnl[symbol]
                closed.append({"at": original.history._iso(at), "symbol": symbol, "net_pnl": str(completed)})
                episode_pnl[symbol] = ZERO
        if cash < Decimal("-1e-18") or quantities[symbol] < ZERO:
            raise ValueError("risk_replay_cash_or_quantity_conservation_failed")
        ledger.append({"at": original.history._iso(at), "symbol": symbol, "side": side,
                       "quantity": str(quantity), "mark_price": str(price), "fill_price": str(fill),
                       "fee": str(cost), "slippage_cost": str(quantity * abs(fill - price)),
                       "cash_after": str(cash), "quantity_after": str(quantities[symbol]),
                       "realized_net_pnl": None if realized is None else str(realized), "reason": reason})
        return completed

    def sell_batch(orders, prices, at, reason):
        completed = []
        for symbol, change in sorted(orders.items()):
            if change < ZERO:
                pnl = trade(symbol, -change, "sell", prices[symbol], at, reason)
                if pnl is not None:
                    completed.append(pnl)
        if completed:
            risk.record_closed_batch(sum(completed))

    for index, day in enumerate(dates):
        prices = {s: days[s][day]["execution_open"] for s in quantities}
        at = day + original.BAR
        day_base = previous_close
        before = mark(prices, day_base, at, "execution_open_before")
        signal = original._weights(days, day)
        weights = ({s: sum(signal.values()) if s == "BTCUSDT" else ZERO for s in quantities}
                   if mode == "btc_cash" else signal)
        factor = risk.multiplier if mode == "risk_trend" else ONE
        paused = bool(risk.pauses) if mode == "risk_trend" else False
        weights = {s: ZERO if paused else w * factor for s, w in weights.items()}
        targets.append({"at": original.history._iso(at), "weights": {s: str(w) for s, w in weights.items()},
                        "signal_cutoff": original.history._iso(day), "risk_multiplier": str(factor),
                        "buy_blocked": paused})
        orders = {}
        for symbol, weight in weights.items():
            current, target = quantities[symbol] * prices[symbol], weight * before
            # Tightened caps must not be swallowed by the original no-trade band.
            cap_sell = mode == "risk_trend" and factor < ONE and current > target
            if target and current and not cap_sell and abs(target - current) <= target * number(plan["rebalance_relative_band"]):
                continue
            orders[symbol] = target / prices[symbol] - quantities[symbol]
        sell_batch(orders, prices, at, "risk_flatten" if paused else "rebalance")
        mark(prices, day_base, at, "after_sells")
        # A sell fee can cross the tightening threshold. Do not use a larger
        # pre-sell risk budget for new purchases after that fact is observed.
        if mode == "risk_trend" and risk.multiplier < factor:
            reduced_nav = value(prices)
            orders = {s: min(change, max(ZERO, signal[s] * risk.multiplier * reduced_nav / prices[s] - quantities[s]))
                      if change > ZERO else change for s, change in orders.items()}
        if mode != "risk_trend" or not risk.pauses:
            required = sum(change * prices[s] * (ONE + slip) * (ONE + fee) for s, change in orders.items() if change > ZERO)
            scale = min(ONE, cash / required) if required else ZERO
            for symbol, change in sorted(orders.items()):
                if mode == "risk_trend" and risk.pauses:
                    break
                if change > ZERO and scale > ZERO:
                    quantity = change * scale
                    if mode == "risk_trend" and risk.multiplier < ONE:
                        # Earlier fills are facts, not reversible proposals.
                        # Only the remaining legs receive the newly known cap.
                        remaining_target = signal[symbol] * risk.multiplier * value(prices) / prices[symbol] - quantities[symbol]
                        quantity = min(quantity, max(ZERO, remaining_target))
                    if quantity > ZERO:
                        trade(symbol, quantity, "buy", prices[symbol], at, "rebalance")
                        mark(prices, day_base, at, "after_buy:" + symbol)
        mark(prices, day_base, at, "execution_open_after")
        closes = {s: days[s][day]["close"] for s in quantities}
        close_at = day + original.DAY
        mark(closes, day_base, close_at, "day_close_before_terminal")
        if index == len(dates) - 1:
            sell_batch({s: -q for s, q in quantities.items()}, closes, close_at, "predeclared_terminal_liquidation")
            mark(closes, day_base, close_at, "terminal_after_costs")
        previous_close = value(closes)
        daily.append([original.history._iso(close_at), str(previous_close)])

    final = cash
    daily_peak, daily_drawdown = initial, ZERO
    for _, day_equity in daily:
        nav = number(day_equity)
        daily_peak = max(daily_peak, nav)
        daily_drawdown = max(daily_drawdown, (daily_peak - nav) / daily_peak)
    return {
        "return": str(final / initial - ONE), "final_equity": str(final), "fees": str(fees),
        "slippage_cost": str(slippage), "turnover_over_initial_cash": str(turnover / initial),
        "trade_leg_count": len(ledger), "ledger": ledger, "ledger_sha256": original.history._sha256(ledger),
        "sampled_equity": equity, "daily_equity": daily,
        "max_drawdown_sampled": str(risk.maximum_drawdown),
        "max_drawdown_daily_close": str(daily_drawdown),
        "drawdown_basis": "daily_close_matches_original; sampled_also_includes_open_and_each_fill",
        "targets": targets, "completed_position_episodes": closed,
        "final_consecutive_losing_exit_batches": risk.streak,
        "risk_events": risk.events if mode == "risk_trend" else [],
        "final_pause_reasons": sorted(risk.pauses) if mode == "risk_trend" else [],
        "final_positions": {s: str(q) for s, q in quantities.items()},
        "risk_overlay_applied": mode == "risk_trend",
    }


def analyze(rows: Mapping[str, list[dict[str, Any]]], *, as_of: datetime) -> dict:
    original.history._assert_simulation_only()
    if not isinstance(as_of, datetime) or as_of.utcoffset() != original.DAY * 0:
        raise ValueError("risk_replay_as_of_invalid")
    cutoff = min(as_of, HISTORY_END)
    if set(rows) != set(original.frozen_plan()["symbols"]):
        raise ValueError("risk_replay_frozen_universe_mismatch")
    days, excluded = {}, {}
    for symbol, bars in rows.items():
        days[symbol], excluded[symbol] = original._daily(bars, as_of=cutoff)
    common = sorted(set.intersection(*(set(day) for day in days.values())))
    eligible = [d for d in common if any(all(d - i * original.DAY in days[s] for i in range(1, 61)) for s in days)]
    segments: list[list[datetime]] = []
    for day in eligible:
        if not segments or day != segments[-1][-1] + original.DAY:
            segments.append([])
        segments[-1].append(day)
    selected = max(segments, key=len) if segments else []
    plan = frozen_plan()
    result = {
        "contract": CONTRACT, **original.history._non_evidence_fields(),
        "research_only": True, "clean_holdout": False,
        "as_of": original.history._iso(as_of), "effective_historical_cutoff": original.history._iso(cutoff),
        "plan": plan, "plan_sha256": original.history._sha256(plan),
        "sampling": "daily_sampled_not_intraday",
        "risk_warning": "sampled_thresholds_not_5m_stops; gaps_and_execution_costs_can_exceed_7pct",
        "execution_warning": "hypothetical_sampled_fills; no_depth_exchange_filters_or_receipt_PIT",
        "forward": {"status": "not_evaluated_original_window_untouched", "net_returns": None},
        "data_quality": {"incomplete_days": excluded, "common_complete_days": len(common), "segments": len(segments)},
        "historical": {"status": "insufficient_complete_history", "required_lookback_days": 60},
    }
    if selected:
        result["historical"] = {
            "status": "historical_diagnostic_not_holdout", "start": original.history._iso(selected[0]),
            "end": original.history._iso(selected[-1] + original.DAY), "days": len(selected),
            "original_trend": original._simulate(days, selected, benchmark=False),
            "risk_trend": _simulate(days, selected, mode="risk_trend"),
            "btc_causal_exposure_cash": _simulate(days, selected, mode="btc_cash"),
            "btc_buy_hold": original._simulate(days, selected, benchmark=True), "cash_return": "0",
            "comparison_warning": "BTC_cash_uses_same_ex_ante_gross_signal_budget_not_ex_post_matched_risk; no_alpha_claim",
        }
    result["report_sha256"] = original.history._sha256(result)
    return result
