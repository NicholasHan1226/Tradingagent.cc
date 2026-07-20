"""Offline-only CN futures simulation slice for fixture and mock validation.

This module deliberately has no HTTP client, data-provider client, SQLite access,
or shared broker dependency.  It is a deterministic contract harness: callers
provide fixture evidence and contract metadata, then receive an auditable
candidate-to-reconcile result.  It is not a broker, exchange simulator, or a
TradingDatas runtime integration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import math
from typing import Any, Mapping

from .session import CN_TZ, parse_cn_datetime


ACCOUNT_ID = "cn-futures-capital-v1"
INITIAL_EQUITY_CNY = 50_000.0
MAX_MARGIN_CNY = 25_000.0
SIMULATION_MARKER = "fixture_mock_only"


class FixtureContractError(ValueError):
    """Raised when a fixture tries to bypass the simulation-only contract."""


@dataclass(frozen=True)
class FixtureContract:
    symbol: str
    product: str
    multiplier: float
    tick_size: float
    initial_margin_rate: float
    maintenance_margin_rate: float
    open_fee_rate: float
    close_fee_rate: float
    night_session: bool
    active_symbol: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "FixtureContract":
        symbol = _text(raw, "symbol")
        active_symbol = _text(raw, "active_symbol")
        product = _text(raw, "product")
        values = {
            name: _positive(raw, name)
            for name in (
                "multiplier",
                "tick_size",
                "initial_margin_rate",
                "maintenance_margin_rate",
            )
        }
        for name in ("open_fee_rate", "close_fee_rate"):
            value = raw.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FixtureContractError(f"{name} must be a non-negative number")
            if not math.isfinite(float(value)) or float(value) < 0:
                raise FixtureContractError(f"{name} must be a non-negative number")
            values[name] = float(value)
        if values["maintenance_margin_rate"] > values["initial_margin_rate"]:
            raise FixtureContractError(
                "maintenance margin cannot exceed initial margin"
            )
        if (
            raw.get("night_session") is not True
            and raw.get("night_session") is not False
        ):
            raise FixtureContractError("night_session must be a boolean")
        return cls(
            symbol=symbol,
            active_symbol=active_symbol,
            product=product,
            night_session=bool(raw["night_session"]),
            **values,
        )


def run_fixture_closed_loop(fixture: Mapping[str, Any]) -> dict[str, Any]:
    """Run one deterministic, in-memory CN futures open/MTM/close/reconcile loop.

    The output contains no persistent side effect.  All input must carry the
    explicit mock marker, and any real/live marker is rejected before a candidate
    or order is formed.
    """

    _validate_fixture_evidence(fixture)
    contract = FixtureContract.from_mapping(_mapping(fixture, "contract"))
    bar = _mapping(fixture, "bar")
    timestamp = parse_cn_datetime(bar.get("timestamp"))
    if timestamp is None:
        raise FixtureContractError("bar timestamp must be an ISO-8601 datetime")
    price = _positive(bar, "price")
    requested_side = _text(fixture, "side").lower()
    if requested_side not in {"long", "short"}:
        raise FixtureContractError("side must be long or short")
    requested_quantity = _whole_positive(fixture, "quantity")
    generation = _whole_positive(fixture, "generation")

    trade_date, session = _trade_date_and_session(timestamp, contract.night_session)
    base = _base_result(
        contract,
        timestamp,
        trade_date,
        session,
        generation,
        _mapping(fixture, "data_evidence"),
    )
    candidate = {
        "symbol": contract.symbol,
        "product": contract.product,
        "side": requested_side,
        "raw_heuristic_score": _finite(fixture.get("raw_heuristic_score", 0.0)),
        "uncalibrated_prior": _finite(fixture.get("uncalibrated_prior", 0.0)),
        "trade_date": trade_date,
        "session": session,
        "counterfactual_only": False,
        "execution_eligible": True,
    }
    if contract.symbol != contract.active_symbol:
        return _hold(base, candidate, "rollover_guard_active_contract_mismatch")
    if session == "closed":
        return _hold(base, candidate, "outside_contract_session")

    stop_distance = _positive(fixture, "stop_distance")
    maximum_loss = _positive(fixture, "maximum_loss_cny")
    entry = _round_tick(price, contract.tick_size, requested_side in {"long"})
    one_lot_margin = entry * contract.multiplier * contract.initial_margin_rate
    one_lot_loss = stop_distance * contract.multiplier
    max_lots_by_margin = int(MAX_MARGIN_CNY // one_lot_margin)
    max_lots_by_loss = int(maximum_loss // one_lot_loss)
    quantity = min(requested_quantity, max_lots_by_margin, max_lots_by_loss)
    if quantity < 1:
        candidate["counterfactual_only"] = True
        return _hold(base, candidate, "one_lot_margin_or_stop_budget_ineligible")

    open_fee = entry * contract.multiplier * quantity * contract.open_fee_rate
    margin = entry * contract.multiplier * quantity * contract.initial_margin_rate
    cash_after_open = INITIAL_EQUITY_CNY - open_fee
    mark_price = _positive(_mapping(fixture, "mark"), "price")
    unrealized = _pnl(requested_side, entry, mark_price, contract.multiplier, quantity)
    equity_before_close = cash_after_open + unrealized
    maintenance = (
        mark_price * contract.multiplier * quantity * contract.maintenance_margin_rate
    )
    liquidation = equity_before_close < maintenance
    close_price_raw = (
        mark_price if liquidation else _positive(_mapping(fixture, "close"), "price")
    )
    close_side_is_buy = requested_side == "short"
    close_price = _round_tick(close_price_raw, contract.tick_size, close_side_is_buy)
    close_fee = close_price * contract.multiplier * quantity * contract.close_fee_rate
    realized = _pnl(requested_side, entry, close_price, contract.multiplier, quantity)
    final_cash = cash_after_open + realized - close_fee
    open_order = _order(
        "open", requested_side, contract, quantity, entry, open_fee, margin
    )
    close_order = _order(
        "forced_liquidation" if liquidation else "close",
        requested_side,
        contract,
        quantity,
        close_price,
        close_fee,
        0.0,
    )
    execution = {
        "simulation_only": True,
        "broker": None,
        "orders": [open_order, close_order],
        "realized_pnl_cny": _money(realized),
        "fees_cny": _money(open_fee + close_fee),
        "liquidation_risk_triggered": liquidation,
        "daily_mtm": {
            "mark_price": _money(mark_price),
            "unrealized_pnl_cny": _money(unrealized),
            "equity_before_close_cny": _money(equity_before_close),
            "maintenance_margin_cny": _money(maintenance),
        },
    }
    sample = {
        "sample_class": "completed_round_trip",
        "trade_date": trade_date,
        "session": session,
        "symbol": contract.symbol,
        "side": requested_side,
        "execution_eligible": True,
        "counterfactual_only": False,
        "reason": "forced_liquidation" if liquidation else "fixture_round_trip",
    }
    reconcile = {
        "account_id": ACCOUNT_ID,
        "generation": generation,
        "initial_equity_cny": INITIAL_EQUITY_CNY,
        "cash_cny": _money(final_cash),
        "equity_cny": _money(final_cash),
        "margin_cny": 0.0,
        "open_position_quantity": 0,
        "reconciled": True,
    }
    return _with_lineage(base, candidate, execution, sample, reconcile)


def _validate_fixture_evidence(fixture: Mapping[str, Any]) -> None:
    if fixture.get("fixture_only") is not True:
        raise FixtureContractError("fixture_only must be true")
    for key in (
        "real_trading_enabled",
        "live_broker",
        "real_account",
        "network_enabled",
    ):
        if fixture.get(key) not in (None, False):
            raise FixtureContractError(f"{key} is forbidden in fixture simulation")
    evidence = _mapping(fixture, "data_evidence")
    if evidence.get("source_kind") != "fixture_mock":
        raise FixtureContractError("data evidence must be fixture_mock")
    if (
        evidence.get("catalog_state") != "ready"
        or evidence.get("query_state") != "ready"
    ):
        raise FixtureContractError("catalog and query fixture evidence must be ready")
    if evidence.get("freshness") != "fresh" or evidence.get("quality") != "valid":
        raise FixtureContractError("fixture data evidence must be fresh and valid")
    if (
        not isinstance(evidence.get("lineage_ref"), str)
        or not evidence["lineage_ref"].strip()
    ):
        raise FixtureContractError("fixture lineage_ref is required")


def _trade_date_and_session(
    timestamp: datetime, night_session: bool
) -> tuple[str, str]:
    local = timestamp.astimezone(CN_TZ)
    minute = local.hour * 60 + local.minute
    weekday = local.weekday()
    if weekday >= 5 and not (weekday == 6 and night_session and minute >= 21 * 60):
        return local.strftime("%Y%m%d"), "closed"
    if night_session and (minute >= 21 * 60 or minute < 2 * 60 + 30):
        anchor = (
            local.date() if minute >= 21 * 60 else (local - timedelta(days=1)).date()
        )
        trade_day = anchor + timedelta(days=1)
        while trade_day.weekday() >= 5:
            trade_day += timedelta(days=1)
        return trade_day.strftime("%Y%m%d"), "night"
    if weekday < 5 and 9 * 60 <= minute <= 11 * 60 + 30:
        return local.strftime("%Y%m%d"), "day_morning"
    if weekday < 5 and 13 * 60 <= minute <= 15 * 60:
        return local.strftime("%Y%m%d"), "day_afternoon"
    return local.strftime("%Y%m%d"), "closed"


def _base_result(
    contract: FixtureContract,
    timestamp: datetime,
    trade_date: str,
    session: str,
    generation: int,
    data_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "mode": SIMULATION_MARKER,
        "real_trading_enabled": False,
        "data_source": "fixture_mock",
        "account": {
            "account_id": ACCOUNT_ID,
            "generation": generation,
            "capital_layer": "simulated",
            "account_type": "simulated",
        },
        "contract": {"symbol": contract.symbol, "product": contract.product},
        "data_evidence": dict(data_evidence),
        "bar_timestamp": timestamp.astimezone(CN_TZ).isoformat(),
        "trade_date": trade_date,
        "session": session,
    }


def _hold(
    base: dict[str, Any], candidate: dict[str, Any], reason: str
) -> dict[str, Any]:
    candidate.update(
        {"execution_eligible": False, "counterfactual_only": True, "reason": reason}
    )
    sample = {
        **candidate,
        "sample_class": "counterfactual_or_hold",
        "counterfactual_only": True,
    }
    reconcile = {
        "account_id": ACCOUNT_ID,
        "generation": base["account"]["generation"],
        "cash_cny": INITIAL_EQUITY_CNY,
        "equity_cny": INITIAL_EQUITY_CNY,
        "margin_cny": 0.0,
        "reconciled": True,
    }
    return _with_lineage(
        base, candidate, {"simulation_only": True, "orders": []}, sample, reconcile
    )


def _with_lineage(
    base: dict[str, Any],
    candidate: dict[str, Any],
    execution: dict[str, Any],
    sample: dict[str, Any],
    reconcile: dict[str, Any],
) -> dict[str, Any]:
    result = {
        **base,
        "candidate": candidate,
        "execution": execution,
        "sample_review": sample,
        "daily_reconcile": reconcile,
    }
    canonical = json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    result["lineage_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return result


def _order(
    effect: str,
    side: str,
    contract: FixtureContract,
    quantity: int,
    price: float,
    fee: float,
    margin: float,
) -> dict[str, Any]:
    return {
        "position_effect": effect,
        "side": side,
        "symbol": contract.symbol,
        "quantity": quantity,
        "price": _money(price),
        "fee_cny": _money(fee),
        "margin_cny": _money(margin),
        "simulated": True,
    }


def _pnl(
    side: str, entry: float, exit_price: float, multiplier: float, quantity: int
) -> float:
    direction = 1.0 if side == "long" else -1.0
    return direction * (exit_price - entry) * multiplier * quantity


def _round_tick(price: float, tick_size: float, upward: bool) -> float:
    units = price / tick_size
    rounded = math.ceil(units - 1e-12) if upward else math.floor(units + 1e-12)
    return _money(rounded * tick_size)


def _mapping(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    if not isinstance(value, Mapping):
        raise FixtureContractError(f"{key} must be a mapping")
    return value


def _text(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FixtureContractError(f"{key} must be a non-empty string")
    return value.strip()


def _positive(raw: Mapping[str, Any], key: str) -> float:
    value = raw.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise FixtureContractError(f"{key} must be a positive finite number")
    return float(value)


def _whole_positive(raw: Mapping[str, Any], key: str) -> int:
    value = _positive(raw, key)
    if not value.is_integer():
        raise FixtureContractError(f"{key} must be a whole positive number")
    return int(value)


def _finite(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise FixtureContractError("scores must be finite numeric values")
    return float(value)


def _money(value: float) -> float:
    return round(float(value), 8)


__all__ = ["ACCOUNT_ID", "FixtureContractError", "run_fixture_closed_loop"]
