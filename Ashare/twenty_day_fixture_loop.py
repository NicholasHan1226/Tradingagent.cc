"""Network-closed 20-session A-share fixture simulation.

This is a non-authoritative, test/review composition only: it has no external
market-data client, network access, persistent write, broker, scheduler, LLM,
outbox, or durable ledger side effect. It reads the canonical A-share market
policy only, as its single capital/risk authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from datetime import datetime, time
import re
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from shared.capital.market_policy import MarketPolicy
from shared.execution.execution_reality import ashare_execution_reality
from shared.universe.policy import InstrumentRole, classify_instrument


_CATALOG_ROUTE = "GET /v1/catalog"
_QUERY_ROUTE = "POST /v1/query"
_NO_TRADE_SCORE = 0.60
_AGGREGATE_NAMESPACE = re.compile(r"^(?:SECTOR|INDUSTRY):[A-Z0-9._:-]+$")
_FIXTURE_SESSION = "ashare_regular"
_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class FixtureEvidence:
    """Explicit, non-authoritative TradingDatas-shaped fixture evidence."""

    catalog_route: str
    query_route: str
    state: str
    degraded: bool
    freshness: str
    quality: str
    lineage_id: str
    receipt_id: str
    calendar_eligible: bool
    calendar_lineage_id: str
    available_at: str
    decision_time: str
    session: str = _FIXTURE_SESSION


@dataclass(frozen=True)
class FixtureDay:
    trade_date: str
    instruments: Sequence[Mapping[str, Any]]
    evidence: FixtureEvidence
    mark_prices: Mapping[str, Any] = field(default_factory=dict)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed and abs(parsed) != float("inf") else default


def _symbol(row: Mapping[str, Any]) -> str:
    return str(row.get("symbol") or row.get("ts_code") or "").strip().upper()


def _instrument_type(row: Mapping[str, Any]) -> str:
    return str(row.get("instrument_type") or "common_stock")


def _tick_aligned(price: float, tick: float) -> bool:
    if price <= 0.0 or tick <= 0.0:
        return False
    try:
        units = Decimal(str(price)) / Decimal(str(tick))
        return units == units.quantize(Decimal("1"))
    except (ArithmeticError, ValueError):
        return False


def _price_limit_reason(
    *, symbol: str, previous_close: float, reference: float, fill: float, mark: float
) -> str | None:
    if previous_close <= 0.0:
        return "price_limit_evidence_missing"
    reality = ashare_execution_reality()
    tick = _number(reality.price_tick_cny, -1.0)
    if not all(_tick_aligned(price, tick) for price in (reference, fill, mark)):
        return "price_tick_invalid"
    try:
        lower, upper = reality.price_limit_bounds(previous_close, symbol=symbol)
    except (TypeError, ValueError):
        return "price_limit_evidence_missing"
    if not all(lower <= price <= upper for price in (reference, fill, mark)):
        return "price_limit_violation"
    return None


def _bar_capacity_shares(row: Mapping[str, Any], *, lot_size: int) -> tuple[int, str | None]:
    raw_volume = row.get("bar_volume_shares")
    if type(raw_volume) is not int or raw_volume <= 0:
        return 0, "bar_volume_shares_invalid"
    participation_cap = _number(
        ashare_execution_reality().as_engine_config().get("bar_participation_cap"),
        -1.0,
    )
    if not 0.0 < participation_cap <= 1.0 or lot_size <= 0:
        return 0, "bar_participation_capacity_unavailable"
    capacity = int(raw_volume * participation_cap)
    return (capacity // lot_size) * lot_size, None


def _execution_supported_session(local_time: time) -> bool:
    """Read the canonical execution-reality contract; never infer auction support."""
    sessions = ashare_execution_reality().as_contract().get("sessions", {})
    if not isinstance(sessions, Mapping):
        return False
    for definition in sessions.values():
        if not isinstance(definition, Mapping) or not definition.get(
            "execution_supported"
        ):
            continue
        try:
            start = time.fromisoformat(str(definition["start"]))
            end = time.fromisoformat(str(definition["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if start <= local_time <= end:
            return True
    return False


def _evidence_reason(day: FixtureDay) -> str | None:
    evidence = day.evidence
    if evidence.catalog_route != _CATALOG_ROUTE or evidence.query_route != _QUERY_ROUTE:
        return "evidence_route_invalid"
    if type(evidence.degraded) is not bool or evidence.degraded:
        return "evidence_degraded"
    if evidence.state != "ready":
        return "evidence_state_invalid"
    if evidence.freshness != "fresh":
        return "evidence_stale"
    if evidence.quality != "valid":
        return "evidence_quality_invalid"
    if not isinstance(evidence.lineage_id, str) or not evidence.lineage_id.strip():
        return "evidence_lineage_missing"
    if not isinstance(evidence.receipt_id, str) or not evidence.receipt_id.strip():
        return "evidence_receipt_missing"
    if type(evidence.calendar_eligible) is not bool or not evidence.calendar_eligible:
        return "calendar_ineligible"
    if (
        not isinstance(evidence.calendar_lineage_id, str)
        or not evidence.calendar_lineage_id.strip()
    ):
        return "calendar_lineage_missing"
    if evidence.session != _FIXTURE_SESSION:
        return "fixture_session_invalid"
    try:
        available_at = datetime.fromisoformat(
            evidence.available_at.replace("Z", "+00:00")
        )
    except (AttributeError, ValueError):
        return "evidence_available_at_invalid"
    if available_at.tzinfo is None:
        return "evidence_available_at_invalid"
    try:
        decision_time = datetime.fromisoformat(
            evidence.decision_time.replace("Z", "+00:00")
        )
    except (AttributeError, ValueError):
        return "evidence_decision_time_invalid"
    if decision_time.tzinfo is None:
        return "evidence_decision_time_invalid"
    try:
        session = datetime.strptime(day.trade_date, "%Y%m%d")
    except (TypeError, ValueError):
        return "trade_date_invalid"
    if session.weekday() >= 5:
        return "trade_date_not_weekday"
    if (
        decision_time.astimezone(_SHANGHAI).strftime("%Y%m%d")
        != day.trade_date
    ):
        return "decision_time_trade_date_mismatch"
    if available_at > decision_time:
        return "evidence_available_after_decision"
    local_decision = decision_time.astimezone(_SHANGHAI)
    local_time = local_decision.time()
    if not _execution_supported_session(local_time):
        return "decision_time_outside_fixture_session"
    return None


def _restricted_individual(symbol: str) -> bool:
    digits = symbol.split(".", 1)[0]
    return digits.isdigit() and (
        digits.startswith(("300", "301", "688", "689", "4", "8", "92"))
    )


def _mainboard_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], str | None]:
    tradable: list[dict[str, Any]] = []
    context: list[str] = []
    seen_symbols: set[str] = set()
    for source in rows:
        row = dict(source)
        raw_symbol = row.get("symbol") or row.get("ts_code") or ""
        normalized = str(raw_symbol).strip().upper()
        if normalized in seen_symbols:
            return [], [], "duplicate_instrument_symbol"
        seen_symbols.add(normalized)
        if _restricted_individual(normalized):
            continue
        eligibility = classify_instrument(
            raw_symbol, instrument_type=_instrument_type(row)
        )
        is_known_index = eligibility.role in {
            InstrumentRole.CHINEXT_INDEX,
            InstrumentRole.STAR_INDEX,
        }
        is_namespaced_aggregate = (
            eligibility.role is InstrumentRole.SECTOR_AGGREGATE
            and bool(_AGGREGATE_NAMESPACE.fullmatch(eligibility.normalized_symbol))
        )
        if eligibility.context_only and (is_known_index or is_namespaced_aggregate):
            context.append(eligibility.normalized_symbol)
            continue
        if eligibility.role is not InstrumentRole.MAINBOARD_COMMON_STOCK:
            continue
        row["symbol"] = eligibility.normalized_symbol
        tradable.append(row)
    tradable.sort(key=_symbol)
    context.sort()
    return tradable, context, None


def _mark_portfolio(
    positions: Mapping[str, Mapping[str, Any]], mark_prices: Mapping[str, Any]
) -> tuple[dict[str, float] | None, str | None]:
    value = unrealized = 0.0
    for symbol, position in positions.items():
        mark = _number(mark_prices.get(symbol), -1.0)
        if mark <= 0.0:
            return None, f"mark_unavailable:{symbol}"
        market_value = int(position["quantity"]) * mark
        value += market_value
        unrealized += market_value - _number(position["entry_cost_cny"])
    return {
        "market_value_cny": round(value, 2),
        "gross_exposure_cny": round(value, 2),
        "unrealized_pnl_cny": round(unrealized, 2),
    }, None


def _fill_price(reference_price: float, side: str) -> tuple[float, float, str]:
    reality = ashare_execution_reality()
    slippage_bps = reality.conservative_label_slippage_bps_per_side
    tick = Decimal(str(reality.price_tick_cny))
    if not tick.is_finite() or tick <= 0:
        return 0.0, slippage_bps, reality.model_version
    raw = Decimal(str(reference_price)) * (
        Decimal("1")
        + (Decimal(str(slippage_bps)) / Decimal("10000"))
        * (Decimal("1") if side == "buy" else Decimal("-1"))
    )
    rounding = ROUND_CEILING if side == "buy" else ROUND_FLOOR
    try:
        fill = float((raw / tick).quantize(Decimal("1"), rounding=rounding) * tick)
    except (ArithmeticError, ValueError):
        fill = 0.0
    return (
        fill,
        slippage_bps,
        reality.model_version,
    )


def _receipt(
    *,
    side: str,
    symbol: str,
    quantity: int,
    reference_price: float,
    fill_price: float,
    slippage_bps: float,
    fees: Mapping[str, Any],
    policy: MarketPolicy,
) -> dict[str, Any]:
    notional = round(quantity * fill_price, 2)
    fee = _number(fees["total"])
    return {
        "status": "simulated_filled",
        "side": side,
        "symbol": symbol,
        "quantity": quantity,
        "reference_price": reference_price,
        "fill_price": fill_price,
        "slippage_bps_per_side": slippage_bps,
        "fee_cny": fee,
        "total_cost_cny": round(notional + fee if side == "buy" else fee, 2),
        "cost_model_version": fees["execution_reality_model_version"],
        "commission_schedule_status": fees["commission_schedule_status"],
        "commission_schedule_version": fees["commission_schedule_version"],
        "capital_authority_id": policy.capital_authority_id,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
        "execution_authority": False,
        "durable": False,
        "capital_commit_id": None,
        "outbox_id": None,
    }


def _report(
    *,
    day: FixtureDay,
    reason: str,
    universe: Sequence[str],
    context: Sequence[str],
    receipt: Mapping[str, Any] | None,
    cash: float,
    positions: Mapping[str, Mapping[str, Any]],
    mark: Mapping[str, float] | None,
    mark_reason: str | None,
    realized_pnl: float,
    policy: MarketPolicy,
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "trade_date": day.trade_date,
        "status": "fixture_completed" if receipt else "fixture_completed_with_blocks",
        "non_authoritative": True,
        "execution_authority": False,
        "durable": False,
        "capital_commit_id": None,
        "outbox_id": None,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
        "reason_code": reason,
        "evidence": {
            "fixture_only": True,
            "catalog_route": day.evidence.catalog_route,
            "query_route": day.evidence.query_route,
            "state": day.evidence.state,
            "degraded": day.evidence.degraded,
            "freshness": day.evidence.freshness,
            "quality": day.evidence.quality,
            "lineage_id": day.evidence.lineage_id,
            "receipt_id": day.evidence.receipt_id,
            "calendar_eligible": day.evidence.calendar_eligible,
            "calendar_lineage_id": day.evidence.calendar_lineage_id,
            "available_at": day.evidence.available_at,
            "decision_time": day.evidence.decision_time,
            "session": day.evidence.session,
            "calendar_authoritative": False,
            "real_session_verified": False,
        },
        "universe": {
            "tradable_mainboard": list(universe),
            "context_only": list(context),
        },
        "simulated_receipt": dict(receipt) if receipt else None,
        "reconcile": {
            "account_id": policy.capital_authority_id,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_trading_enabled": False,
            "non_authoritative": True,
            "durable": False,
            "capital_commit_id": None,
            "outbox_id": None,
            "cash_cny": round(cash, 2),
            "market_value_cny": mark["market_value_cny"] if mark else None,
            "gross_exposure_cny": mark["gross_exposure_cny"] if mark else None,
            "realized_pnl_cny": round(realized_pnl, 2),
            "unrealized_pnl_cny": mark["unrealized_pnl_cny"] if mark else None,
            "equity_cny": round(cash + _number(mark["market_value_cny"]), 2)
            if mark
            else None,
            "position_count": len(positions),
            "status": "fixture_reconciled" if mark else "fixture_blocked",
            "reason_code": mark_reason,
        },
        "sample_review": list(samples),
    }


def run_fixture_twenty_day_loop(
    days: Sequence[FixtureDay], *, real_trading_enabled: bool = False
) -> dict[str, Any]:
    """Run 20 validated weekday fixture sessions with no external side effect."""
    if real_trading_enabled:
        raise RuntimeError("REAL_TRADING_ENABLED=false")
    try:
        policy = MarketPolicy.load("ashare")
    except Exception as exc:  # policy integrity is a hard boundary
        raise RuntimeError("ashare_policy_unavailable") from exc
    if len(days) != 20:
        raise ValueError("fixture_twenty_trading_days_required")
    if [day.trade_date for day in days] != sorted(
        day.trade_date for day in days
    ) or len({day.trade_date for day in days}) != 20:
        raise ValueError("fixture_trade_dates_must_be_20_unique_sorted_values")

    cash = policy.initial_equity_cny
    positions: dict[str, dict[str, Any]] = {}
    realized_pnl = 0.0
    reports: list[dict[str, Any]] = []
    journal: list[dict[str, Any]] = []
    for session_index, day in enumerate(days):
        evidence_reason = _evidence_reason(day)
        reason = "no_eligible_mainboard_candidate"
        receipt: dict[str, Any] | None = None
        if evidence_reason:
            mark: dict[str, float] | None = None
            mark_reason: str | None = evidence_reason
            universe_rows: list[dict[str, Any]] = []
            context: list[str] = []
            universe: list[str] = []
            samples: list[dict[str, Any]] = [
                {
                    "sample_type": "data_reject",
                    "trade_date": day.trade_date,
                    "symbol": None,
                    "execution_eligible": False,
                    "training_eligible": False,
                    "fixture_simulation_eligible": False,
                    "simulated_fill_observed": False,
                    "reason_code": evidence_reason,
                }
            ]
            reason = evidence_reason
        else:
            mark, mark_reason = _mark_portfolio(positions, day.mark_prices)
            universe_rows, context, universe_reason = _mainboard_rows(day.instruments)
            universe = [_symbol(row) for row in universe_rows]
            samples = [
                {
                    "sample_type": "observation",
                    "trade_date": day.trade_date,
                    "symbol": symbol,
                    "execution_eligible": False,
                    "training_eligible": False,
                    "fixture_simulation_eligible": False,
                    "simulated_fill_observed": False,
                    "reason_code": "fixture_mainboard_observation",
                }
                for symbol in universe
            ]
        if evidence_reason:
            pass
        elif universe_reason:
            reason = universe_reason
        elif mark is None:
            reason = str(mark_reason)
        elif universe_rows:
            candidate = min(
                universe_rows,
                key=lambda row: (-_number(row.get("rank_score")), _symbol(row)),
            )
            symbol = _symbol(candidate)
            side = str(candidate.get("signal") or "buy").strip().lower()
            reference = _number(candidate.get("price"), -1.0)
            previous_close = _number(candidate.get("previous_close_cny"), -1.0)
            mark_price = _number(day.mark_prices.get(symbol), -1.0)
            lot = int(policy.buy_lot_size_shares or 0)
            bar_capacity, capacity_reason = _bar_capacity_shares(
                candidate, lot_size=lot
            )
            if _number(candidate.get("rank_score")) < _NO_TRADE_SCORE:
                reason = "no_trade_band"
            elif candidate.get("suspended") is not False:
                reason = "instrument_suspended"
            elif capacity_reason:
                reason = capacity_reason
            elif bar_capacity < lot:
                reason = "bar_participation_capacity_not_feasible"
            elif reference <= 0.0:
                reason = "invalid_reference_price"
            elif side not in {"buy", "sell"}:
                reason = "unsupported_fixture_signal"
            elif previous_close <= 0.0:
                reason = "price_limit_evidence_missing"
            elif side == "sell":
                holding = positions.get(symbol)
                if holding is None:
                    reason = "no_position_to_sell"
                elif holding["acquired_session_index"] >= session_index:
                    reason = "t_plus_1_not_sellable"
                elif mark_price <= 0.0:
                    reason = f"mark_unavailable:{symbol}"
                else:
                    fill, slippage, _ = _fill_price(reference, side)
                    if fill <= 0.0:
                        reason = "invalid_fill_price"
                    elif price_reason := _price_limit_reason(
                        symbol=symbol,
                        previous_close=previous_close,
                        reference=reference,
                        fill=fill,
                        mark=mark_price,
                    ):
                        reason = price_reason
                    else:
                        holding_quantity = int(holding["quantity"])
                        quantity = min(holding_quantity, bar_capacity)
                        notional = quantity * fill
                        fees = ashare_execution_reality().calculate_fees(side, notional)
                        cash = round(cash + notional - _number(fees["total"]), 2)
                        sold_cost = round(
                            _number(holding["entry_cost_cny"])
                            * quantity
                            / holding_quantity,
                            2,
                        )
                        realized_pnl = round(
                            realized_pnl
                            + notional
                            - _number(fees["total"])
                            - sold_cost,
                            2,
                        )
                        if quantity == holding_quantity:
                            del positions[symbol]
                        else:
                            positions[symbol]["quantity"] = holding_quantity - quantity
                            positions[symbol]["entry_cost_cny"] = round(
                                _number(holding["entry_cost_cny"]) - sold_cost, 2
                            )
                        receipt = _receipt(
                            side=side,
                            symbol=symbol,
                            quantity=quantity,
                            reference_price=reference,
                            fill_price=fill,
                            slippage_bps=slippage,
                            fees=fees,
                            policy=policy,
                        )
                        reason = "simulated_sell_filled"
            elif symbol in positions:
                reason = "same_symbol_position_exists"
            elif len(positions) >= int(policy.max_positions or 0):
                reason = "position_capacity_reached"
            elif any(
                int(row["quantity"]) * _number(day.mark_prices.get(held))
                > policy.single_name_cap_cny
                for held, row in positions.items()
            ):
                reason = "single_name_mark_limit_breached"
            elif (
                _number(mark["gross_exposure_cny"])
                >= policy.stock_gross_exposure_limit_cny
            ):
                reason = "gross_exposure_limit_reached"
            elif mark_price <= 0.0:
                reason = f"mark_unavailable:{symbol}"
            else:
                fill, slippage, _ = _fill_price(reference, side)
                risk_price = max(fill, mark_price)
                budget = min(
                    policy.single_name_cap_cny,
                    policy.stock_gross_exposure_limit_cny
                    - _number(mark["gross_exposure_cny"]),
                    cash,
                )
                quantity = int(budget // (risk_price * lot)) * lot if lot else 0
                quantity = min(quantity, bar_capacity)
                notional = quantity * fill
                post_single_name = quantity * mark_price
                post_gross = _number(mark["gross_exposure_cny"]) + post_single_name
                if fill <= 0.0:
                    reason = "invalid_fill_price"
                elif price_reason := _price_limit_reason(
                    symbol=symbol,
                    previous_close=previous_close,
                    reference=reference,
                    fill=fill,
                    mark=mark_price,
                ):
                    reason = price_reason
                elif quantity == 0:
                    reason = "lot_or_single_name_cap_not_feasible"
                elif post_single_name > policy.single_name_cap_cny:
                    reason = "post_trade_single_name_mark_limit_breached"
                elif post_gross > policy.stock_gross_exposure_limit_cny:
                    reason = "post_trade_gross_exposure_limit_breached"
                elif notional < max(
                    _number(policy.minimum_economic_order_cny),
                    _number(policy.no_trade_band_cny),
                ):
                    reason = "minimum_economic_order_not_met"
                else:
                    fees = ashare_execution_reality().calculate_fees(side, notional)
                    total = notional + _number(fees["total"])
                    if total > cash:
                        reason = "insufficient_cash_after_fee"
                    else:
                        cash = round(cash - total, 2)
                        positions[symbol] = {
                            "quantity": quantity,
                            "entry_cost_cny": round(total, 2),
                            "acquired_session_index": session_index,
                        }
                        receipt = _receipt(
                            side=side,
                            symbol=symbol,
                            quantity=quantity,
                            reference_price=reference,
                            fill_price=fill,
                            slippage_bps=slippage,
                            fees=fees,
                            policy=policy,
                        )
                        reason = "simulated_buy_filled"
        if not evidence_reason:
            mark, mark_reason = _mark_portfolio(positions, day.mark_prices)
            if receipt and mark is None:
                raise RuntimeError("fixture_mark_required_before_simulated_receipt")
        if not evidence_reason:
            samples.append(
                {
                    "sample_type": "fixture_simulated_fill"
                    if receipt
                    else "risk_reject",
                    "trade_date": day.trade_date,
                    "symbol": receipt["symbol"] if receipt else None,
                    "execution_eligible": False,
                    "training_eligible": False,
                    "fixture_simulation_eligible": bool(receipt),
                    "simulated_fill_observed": bool(receipt),
                    "reason_code": reason,
                }
            )
        journal.extend(samples)
        reports.append(
            _report(
                day=day,
                reason=reason,
                universe=universe,
                context=context,
                receipt=receipt,
                cash=cash,
                positions=positions,
                mark=mark,
                mark_reason=mark_reason,
                realized_pnl=realized_pnl,
                policy=policy,
                samples=samples,
            )
        )
    return {
        "contract_id": "tradingagent.ashare.fixture_twenty_day_loop.v1",
        "fixture_only": True,
        "non_authoritative": True,
        "execution_authority": False,
        "durable": False,
        "capital_commit_id": None,
        "outbox_id": None,
        "calendar_authoritative": False,
        "real_session_verified": False,
        "training_eligible": False,
        "promotion_eligible": False,
        "account_id": policy.capital_authority_id,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
        "day_count": len(reports),
        "days": reports,
        "sample_journal": journal,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
    }
