"""Network-closed, fixture-only 20-trading-day A-share simulation slice.

This module is deliberately an engineering fixture, not a scheduler, data
client, broker, or capital-ledger replacement.  Its inputs are supplied by the
caller and retain the two permitted TradingDatas wire-route names solely as
evidence metadata.  It never opens a socket, guesses a dataset ID, or invokes
an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


ACCOUNT_ID = "ashare-capital-v1"
INITIAL_CASH_CNY = 50_000.0
MAX_GROSS_CNY = 45_000.0
MAX_SINGLE_NAME_CNY = 7_500.0
LOT_SIZE = 100
MAX_POSITIONS = 8
MIN_ECONOMIC_ORDER_CNY = 2_000.0
NO_TRADE_SCORE = 0.60
_CATALOG_ROUTE = "GET /v1/catalog"
_QUERY_ROUTE = "POST /v1/query"
_BLOCKED_BOARDS = frozenset({"chinext", "star", "beijing"})


@dataclass(frozen=True)
class FixtureDay:
    """One explicitly injected, non-production day of market evidence."""

    trade_date: str
    evidence_eligible: bool
    evidence_reason: str
    instruments: Sequence[Mapping[str, Any]]
    catalog_route: str = _CATALOG_ROUTE
    query_route: str = _QUERY_ROUTE


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed and abs(parsed) != float("inf") else default


def _symbol(row: Mapping[str, Any]) -> str:
    return str(row.get("symbol") or row.get("ts_code") or "").strip().upper()


def _board(row: Mapping[str, Any]) -> str:
    return str(row.get("board") or "").strip().lower()


def _mainboard_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    tradable: list[dict[str, Any]] = []
    context: list[str] = []
    for source in rows:
        row = dict(source)
        symbol = _symbol(row)
        board = _board(row)
        if not symbol:
            continue
        if bool(row.get("context_only")) or board in _BLOCKED_BOARDS:
            context.append(symbol)
            continue
        if (
            board != "mainboard"
            or str(row.get("instrument_type") or "common_stock") != "common_stock"
        ):
            continue
        tradable.append(row)
    return tradable, context


def _fee(symbol: str, side: str, notional: float) -> float:
    commission = max(5.0, notional * 0.0003)
    transfer = notional * 0.00001 if symbol.endswith(".SH") else 0.0
    stamp = notional * 0.0005 if side == "sell" else 0.0
    return round(commission + transfer + stamp, 2)


def _day_report(
    *,
    day: FixtureDay,
    reason: str,
    universe: Sequence[str],
    context: Sequence[str],
    receipt: Mapping[str, Any] | None,
    cash: float,
    positions: Mapping[str, Mapping[str, Any]],
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    market_value = round(
        sum(
            _number(position["quantity"]) * _number(position["price"])
            for position in positions.values()
        ),
        2,
    )
    return {
        "trade_date": day.trade_date,
        "status": "completed" if receipt else "completed_with_blocks",
        "reason_code": reason,
        "evidence": {
            "fixture_only": True,
            "eligible": day.evidence_eligible,
            "reason_code": day.evidence_reason,
            "catalog_route": day.catalog_route,
            "query_route": day.query_route,
        },
        "universe": {
            "tradable_mainboard": list(universe),
            "context_only": list(context),
        },
        "intent_receipt": dict(receipt) if receipt else None,
        "reconcile": {
            "account_id": ACCOUNT_ID,
            "capital_layer": "simulated",
            "real_trading_enabled": False,
            "cash_cny": round(cash, 2),
            "market_value_cny": market_value,
            "equity_cny": round(cash + market_value, 2),
            "position_count": len(positions),
            "status": "reconciled",
        },
        "sample_review": list(samples),
    }


def run_fixture_twenty_day_loop(
    days: Sequence[FixtureDay], *, real_trading_enabled: bool = False
) -> dict[str, Any]:
    """Run exactly 20 supplied fixture days without network or execution ports.

    A row may carry ``signal=buy`` (default) or ``signal=sell``.  The result is
    an immutable-shaped report suitable for tests and review only; it does not
    write a ledger, create an outbox entry, or establish runtime authority.
    """

    if real_trading_enabled:
        raise RuntimeError("REAL_TRADING_ENABLED=false")
    if len(days) != 20:
        raise ValueError("fixture_twenty_trading_days_required")
    dates = [day.trade_date for day in days]
    if len(set(dates)) != 20 or dates != sorted(dates):
        raise ValueError("fixture_trade_dates_must_be_20_unique_sorted_values")

    cash = INITIAL_CASH_CNY
    positions: dict[str, dict[str, Any]] = {}
    reports: list[dict[str, Any]] = []
    sample_journal: list[dict[str, Any]] = []

    for day_index, day in enumerate(days):
        if day.catalog_route != _CATALOG_ROUTE or day.query_route != _QUERY_ROUTE:
            raise ValueError("tradingdatas_wire_contract_mismatch")
        universe_rows, context = _mainboard_rows(day.instruments)
        universe = [_symbol(row) for row in universe_rows]
        samples = [
            {
                "sample_type": "observation",
                "trade_date": day.trade_date,
                "symbol": symbol,
                "execution_eligible": False,
                "reason_code": "fixture_mainboard_observation",
            }
            for symbol in universe
        ]
        reason = "no_eligible_mainboard_candidate"
        receipt: dict[str, Any] | None = None

        if not day.evidence_eligible:
            reason = f"evidence_ineligible:{day.evidence_reason or 'unspecified'}"
        elif universe_rows:
            candidate = max(
                universe_rows, key=lambda row: _number(row.get("rank_score"))
            )
            symbol = _symbol(candidate)
            score = _number(candidate.get("rank_score"))
            price = _number(candidate.get("price"))
            side = str(candidate.get("signal") or "buy").strip().lower()
            if score < NO_TRADE_SCORE:
                reason = "no_trade_band"
            elif price <= 0.0:
                reason = "invalid_fixture_price"
            elif side not in {"buy", "sell"}:
                reason = "unsupported_fixture_signal"
            elif side == "sell":
                holding = positions.get(symbol)
                if holding is None:
                    reason = "no_position_to_sell"
                elif holding["acquired_day_index"] >= day_index:
                    reason = "t_plus_1_not_sellable"
                else:
                    quantity = int(holding["quantity"])
                    notional = quantity * price
                    fee = _fee(symbol, side, notional)
                    cash = round(cash + notional - fee, 2)
                    del positions[symbol]
                    receipt = {
                        "status": "filled",
                        "side": side,
                        "symbol": symbol,
                        "quantity": quantity,
                        "price": price,
                        "fee_cny": fee,
                        "capital_layer": "simulated",
                        "real_trading_enabled": False,
                    }
                    reason = "simulated_sell_filled"
            elif symbol in positions:
                reason = "same_symbol_position_exists"
            elif len(positions) >= MAX_POSITIONS:
                reason = "position_capacity_reached"
            else:
                gross = sum(
                    _number(item["quantity"]) * _number(item["price"])
                    for item in positions.values()
                )
                budget = min(MAX_SINGLE_NAME_CNY, MAX_GROSS_CNY - gross, cash)
                quantity = int(budget // (price * LOT_SIZE)) * LOT_SIZE
                notional = quantity * price
                fee = _fee(symbol, side, notional) if quantity else 0.0
                if quantity == 0:
                    reason = "lot_or_single_name_cap_not_feasible"
                elif notional < MIN_ECONOMIC_ORDER_CNY:
                    reason = "minimum_economic_order_not_met"
                elif notional + fee > cash:
                    reason = "insufficient_cash_after_fee"
                else:
                    cash = round(cash - notional - fee, 2)
                    positions[symbol] = {
                        "quantity": quantity,
                        "price": price,
                        "acquired_day_index": day_index,
                    }
                    receipt = {
                        "status": "filled",
                        "side": side,
                        "symbol": symbol,
                        "quantity": quantity,
                        "price": price,
                        "fee_cny": fee,
                        "capital_layer": "simulated",
                        "real_trading_enabled": False,
                    }
                    reason = "simulated_buy_filled"

        samples.append(
            {
                "sample_type": "execution" if receipt else "risk_reject",
                "trade_date": day.trade_date,
                "symbol": receipt["symbol"] if receipt else None,
                "execution_eligible": bool(receipt),
                "reason_code": reason,
            }
        )
        sample_journal.extend(samples)
        reports.append(
            _day_report(
                day=day,
                reason=reason,
                universe=universe,
                context=context,
                receipt=receipt,
                cash=cash,
                positions=positions,
                samples=samples,
            )
        )

    return {
        "contract_id": "tradingagent.ashare.fixture_twenty_day_loop.v1",
        "fixture_only": True,
        "account_id": ACCOUNT_ID,
        "capital_layer": "simulated",
        "real_trading_enabled": False,
        "day_count": len(reports),
        "days": reports,
        "sample_journal": sample_journal,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
    }
