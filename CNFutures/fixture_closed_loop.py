"""Offline-only CN futures simulation slice for fixture and mock validation.

This module deliberately has no HTTP client, data-provider client, SQLite access,
or shared broker dependency.  It is a deterministic contract harness: callers
provide fixture evidence and contract metadata, then receive an auditable
candidate-to-reconcile result.  It is not a broker, exchange simulator, or a
TradingDatas runtime integration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import re
from typing import Any, Mapping

from shared.capital.market_policy import MarketPolicy

from .session import CN_TZ, parse_cn_datetime


SIMULATION_MARKER = "fixture_mock_only"
_CONTRACT_SYMBOL = re.compile(
    r"^(?P<product>[A-Za-z]+)(?P<month>\d{3,4})\.(?P<exchange>[A-Za-z]+)$"
)
_FIXTURE_PRODUCT_EXCHANGES = {
    "rb": "SHF",
    "cu": "SHF",
    "i": "DCE",
    "m": "DCE",
    "if": "CFFEX",
    "ih": "CFFEX",
    "ic": "CFFEX",
    "im": "CFFEX",
}


class FixtureContractError(ValueError):
    """Raised when a fixture tries to bypass the simulation-only contract."""


@dataclass(frozen=True)
class EventTiming:
    event_time: datetime
    available_at: datetime
    decision_time: datetime


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
    open_fee_type: str
    close_fee_type: str
    night_session: bool
    night_session_end_minute: int | None
    session_windows: Mapping[str, tuple[tuple[int, int], ...]]
    active_symbol: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "FixtureContract":
        product = _text(raw, "product").lower()
        symbol = _canonical_contract_symbol(_text(raw, "symbol"), product, "symbol")
        active_symbol = _canonical_contract_symbol(
            _text(raw, "active_symbol"), product, "active_symbol"
        )
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
        night_session = bool(raw["night_session"])
        night_session_end_minute = _minute_or_none(raw.get("night_session_end_minute"))
        if night_session != (night_session_end_minute is not None):
            raise FixtureContractError(
                "night_session and night_session_end_minute must agree"
            )
        session_windows = _session_windows(raw, night_session, night_session_end_minute)
        open_fee_type = _fee_type(raw, "open_fee_type")
        close_fee_type = _fee_type(raw, "close_fee_type")
        return cls(
            symbol=symbol,
            active_symbol=active_symbol,
            product=product,
            night_session=night_session,
            night_session_end_minute=night_session_end_minute,
            session_windows=session_windows,
            open_fee_type=open_fee_type,
            close_fee_type=close_fee_type,
            **values,
        )


def run_fixture_closed_loop(fixture: Mapping[str, Any]) -> dict[str, Any]:
    """Run one deterministic, in-memory CN futures open/MTM/close/reconcile loop.

    The output contains no persistent side effect.  All input must carry the
    explicit mock marker, and any real/live marker is rejected before a candidate
    or order is formed.
    """

    _validate_fixture_evidence(fixture)
    policy = _load_cn_futures_policy()
    contract = FixtureContract.from_mapping(_mapping(fixture, "contract"))
    bar = _mapping(fixture, "bar")
    mark = _mapping(fixture, "mark")
    close = _mapping(fixture, "close")
    entry_timing = _event_timing(bar, "bar")
    mark_timing = _event_timing(mark, "mark")
    close_timing = _event_timing(close, "close")
    if not (
        entry_timing.decision_time < mark_timing.event_time
        and mark_timing.decision_time <= close_timing.event_time
    ):
        raise FixtureContractError("event evidence and decisions are out of PIT order")
    evidence = _mapping(fixture, "data_evidence")
    _assert_available_by(evidence, "data_evidence", entry_timing.decision_time)
    _assert_available_by(
        _mapping(fixture, "contract"), "contract", entry_timing.decision_time
    )
    price = _positive(bar, "price")
    requested_side = _text(fixture, "side").lower()
    if requested_side not in {"long", "short"}:
        raise FixtureContractError("side must be long or short")
    requested_quantity = _whole_positive(fixture, "quantity")
    generation = _whole_positive(fixture, "generation")

    trade_date, session = _event_trade_date_and_session(
        bar, entry_timing, contract, "bar"
    )
    if session != "closed":
        _validate_followup_event_calendar(
            mark, mark_timing, contract, trade_date, "mark"
        )
        _validate_followup_event_calendar(
            close, close_timing, contract, trade_date, "close"
        )
    fixture_lineage_sha256 = _canonical_sha256(fixture)
    intent_id = f"cnf-intent-{fixture_lineage_sha256[:24]}"
    base = _base_result(
        contract,
        entry_timing.event_time,
        trade_date,
        session,
        generation,
        evidence,
        fixture_lineage_sha256,
        policy,
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
        "execution_eligible": False,
        "fixture_simulation_eligible": True,
        "intent_id": intent_id,
    }
    if contract.symbol != contract.active_symbol:
        return _hold(base, candidate, policy, "rollover_guard_active_contract_mismatch")
    if session == "closed":
        return _hold(base, candidate, policy, "outside_contract_session")

    stop_distance = _positive(fixture, "stop_distance")
    maximum_loss = _positive(fixture, "maximum_loss_cny")
    daily_risk_budget = _finite_derived(
        policy.initial_equity_cny * policy.daily_loss_pause_pct, "daily risk budget"
    )
    if maximum_loss > daily_risk_budget:
        return _hold(
            base, candidate, policy, "maximum_loss_exceeds_canonical_daily_budget"
        )
    entry = _round_tick(price, contract.tick_size, requested_side in {"long"})
    one_lot_margin = _finite_derived(
        entry * contract.multiplier * contract.initial_margin_rate, "one-lot margin"
    )
    one_lot_loss = _finite_derived(
        stop_distance * contract.multiplier, "one-lot stop loss"
    )
    max_lots_by_margin = int(policy.margin_utilization_limit_cny // one_lot_margin)
    max_lots_by_loss = int(maximum_loss // one_lot_loss)
    quantity = min(requested_quantity, max_lots_by_margin, max_lots_by_loss)
    if quantity < 1:
        candidate["counterfactual_only"] = True
        return _hold(
            base, candidate, policy, "one_lot_margin_or_stop_budget_ineligible"
        )

    open_fee = _fee(
        entry,
        contract.multiplier,
        quantity,
        contract.open_fee_rate,
        contract.open_fee_type,
    )
    margin = _finite_derived(
        entry * contract.multiplier * quantity * contract.initial_margin_rate, "margin"
    )
    expected_close_fee = _fee(
        entry,
        contract.multiplier,
        quantity,
        contract.close_fee_rate,
        contract.close_fee_type,
    )
    reserved_cash_after_order = _finite_derived(
        policy.initial_equity_cny - margin - open_fee - expected_close_fee,
        "reserved cash after order",
    )
    stop_exposure = _finite_derived(
        one_lot_loss * quantity + open_fee + expected_close_fee, "stop exposure"
    )
    if reserved_cash_after_order < 0 or stop_exposure > daily_risk_budget:
        return _hold(base, candidate, policy, "margin_stop_or_fee_pretrade_ineligible")
    cash_after_open = _finite_derived(
        policy.initial_equity_cny - open_fee, "cash after open"
    )
    mark_price = _positive(mark, "price")
    unrealized = _pnl(requested_side, entry, mark_price, contract.multiplier, quantity)
    equity_before_close = _finite_derived(
        cash_after_open + unrealized, "equity before close"
    )
    maintenance = _finite_derived(
        mark_price * contract.multiplier * quantity * contract.maintenance_margin_rate,
        "maintenance margin",
    )
    liquidation = equity_before_close < maintenance
    close_price_raw = mark_price if liquidation else _positive(close, "price")
    close_side_is_buy = requested_side == "short"
    close_price = _round_tick(close_price_raw, contract.tick_size, close_side_is_buy)
    close_fee = _fee(
        close_price,
        contract.multiplier,
        quantity,
        contract.close_fee_rate,
        contract.close_fee_type,
    )
    realized = _pnl(requested_side, entry, close_price, contract.multiplier, quantity)
    final_cash = _finite_derived(cash_after_open + realized - close_fee, "final cash")
    capital_deficit = _finite_derived(max(0.0, -final_cash), "capital deficit")
    open_order = _order(
        intent_id,
        "open",
        requested_side,
        contract,
        quantity,
        entry,
        open_fee,
        margin,
    )
    close_order = _order(
        intent_id,
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
        "execution_authority": False,
        "durable": False,
        "capital_commit_id": None,
        "outbox_id": None,
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
        "execution_eligible": False,
        "fixture_simulation_eligible": True,
        "counterfactual_only": False,
        "reason": (
            "forced_liquidation_capital_deficit"
            if liquidation and capital_deficit > 0
            else "forced_liquidation"
            if liquidation
            else "fixture_round_trip"
        ),
        "capital_deficit_cny": _money(capital_deficit),
    }
    reconcile = {
        "account_id": policy.capital_authority_id,
        "generation": generation,
        "initial_equity_cny": policy.initial_equity_cny,
        "cash_cny": _money(final_cash),
        "equity_cny": _money(final_cash),
        "margin_cny": 0.0,
        "open_position_quantity": 0,
        "fixture_reconciled": capital_deficit == 0,
        "non_authoritative": True,
        "risk_state": "capital_deficit" if capital_deficit > 0 else "fixture_balanced",
        "capital_deficit_cny": _money(capital_deficit),
        "durable": False,
        "capital_commit_id": None,
        "outbox_id": None,
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
    if evidence.get("catalog_route") != "GET /v1/catalog":
        raise FixtureContractError("fixture catalog route must be GET /v1/catalog")
    if evidence.get("query_route") != "POST /v1/query":
        raise FixtureContractError("fixture query route must be POST /v1/query")
    if (
        evidence.get("catalog_state") != "ready"
        or evidence.get("query_state") != "ready"
    ):
        raise FixtureContractError("catalog and query fixture evidence must be ready")
    if evidence.get("degraded") is not False:
        raise FixtureContractError(
            "fixture data evidence must explicitly be non-degraded"
        )
    if evidence.get("freshness") != "fresh" or evidence.get("quality") != "valid":
        raise FixtureContractError("fixture data evidence must be fresh and valid")
    if (
        not isinstance(evidence.get("lineage_ref"), str)
        or not evidence["lineage_ref"].strip()
    ):
        raise FixtureContractError("fixture lineage_ref is required")
    _nonempty_string_field(evidence, "receipt_id", "fixture data receipt_id")


def _load_cn_futures_policy() -> MarketPolicy:
    try:
        policy = MarketPolicy.load("cn_futures")
    except Exception as exc:
        raise FixtureContractError("canonical_cn_futures_policy_unavailable") from exc
    if (
        policy.market != "cn_futures"
        or policy.currency != "CNY"
        or policy.capital_layer != "simulated"
        or policy.real_trading_enabled is not False
        or policy.margin_utilization_limit_pct is None
        or policy.margin_utilization_limit_cny <= 0
        or policy.daily_loss_pause_pct <= 0
    ):
        raise FixtureContractError("canonical_cn_futures_policy_mismatch")
    return policy


def _canonical_contract_symbol(symbol: str, product: str, field_name: str) -> str:
    match = _CONTRACT_SYMBOL.fullmatch(symbol)
    if match is None:
        raise FixtureContractError(
            f"{field_name} must be a concrete contract for the declared product"
        )
    normalized_product = match.group("product").lower()
    normalized_exchange = match.group("exchange").upper()
    month = match.group("month")
    if (
        normalized_product != product
        or _FIXTURE_PRODUCT_EXCHANGES.get(product) != normalized_exchange
    ):
        raise FixtureContractError(
            f"{field_name} must match the declared product and fixture exchange"
        )
    if not 1 <= int(month[-2:]) <= 12:
        raise FixtureContractError(f"{field_name} must have month 01 through 12")
    return f"{normalized_product}{month}.{normalized_exchange}"


def _minute_or_none(value: Any) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value < 24 * 60
    ):
        raise FixtureContractError("night_session_end_minute must be a valid minute")
    return value


def _session_windows(
    raw: Mapping[str, Any],
    night_session: bool,
    night_session_end_minute: int | None,
) -> Mapping[str, tuple[tuple[int, int], ...]]:
    value = raw.get("session_windows")
    if not isinstance(value, Mapping):
        raise FixtureContractError("session_windows must be a mapping")
    required = {"day_morning", "day_afternoon"}
    if night_session:
        required.add("night")
    if set(value) != required:
        raise FixtureContractError(
            "session_windows must contain exactly the enabled sessions"
        )
    parsed: dict[str, tuple[tuple[int, int], ...]] = {}
    for name in sorted(required):
        windows = value.get(name)
        if not isinstance(windows, (list, tuple)) or not windows:
            raise FixtureContractError(f"session_windows.{name} must be non-empty")
        rows: list[tuple[int, int]] = []
        for window in windows:
            if (
                not isinstance(window, (list, tuple))
                or len(window) != 2
                or any(
                    isinstance(item, bool) or not isinstance(item, int)
                    for item in window
                )
            ):
                raise FixtureContractError(
                    f"session_windows.{name} entries must be minute pairs"
                )
            start, end = window
            if not 0 <= start < end <= 24 * 60:
                raise FixtureContractError(
                    f"session_windows.{name} minute range is invalid"
                )
            rows.append((start, end))
        if name == "night" and len(rows) > 1:
            if not (len(rows) == 2 and rows[0][1] == 24 * 60 and rows[1][0] == 0):
                raise FixtureContractError(
                    "cross-midnight night windows must be [start, 1440], [0, end]"
                )
        elif any(end == 24 * 60 for _, end in rows):
            raise FixtureContractError("1440 is only valid for a cross-midnight seam")
        elif any(
            previous[1] >= current[0] for previous, current in zip(rows, rows[1:])
        ):
            raise FixtureContractError(f"session_windows.{name} must be ordered")
        parsed[name] = tuple(rows)
    all_windows = sorted(
        (start, end, name) for name, windows in parsed.items() for start, end in windows
    )
    if any(
        previous[1] >= current[0]
        for previous, current in zip(all_windows, all_windows[1:])
    ):
        raise FixtureContractError("session windows must not overlap")
    if night_session and parsed["night"][-1][1] != night_session_end_minute:
        raise FixtureContractError("night window must end at night_session_end_minute")
    return parsed


def _fee_type(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if value not in {"rate", "fixed_per_lot"}:
        raise FixtureContractError(f"{key} must be rate or fixed_per_lot")
    return str(value)


def _event_timing(raw: Mapping[str, Any], name: str) -> EventTiming:
    event_time = _aware_timestamp(raw.get("timestamp"), f"{name} timestamp")
    available_at = _aware_timestamp(raw.get("available_at"), f"{name} available_at")
    decision_time = _aware_timestamp(raw.get("decision_time"), f"{name} decision_time")
    if event_time is None or available_at is None or decision_time is None:
        raise FixtureContractError(
            f"{name} timestamp, available_at, and decision_time must be ISO-8601"
        )
    if not event_time <= available_at <= decision_time:
        raise FixtureContractError(
            f"{name} must satisfy event_time <= available_at <= decision_time"
        )
    return EventTiming(event_time, available_at, decision_time)


def _assert_available_by(
    raw: Mapping[str, Any], name: str, decision_time: datetime
) -> None:
    available_at = _aware_timestamp(raw.get("available_at"), f"{name} available_at")
    if available_at > decision_time:
        raise FixtureContractError(f"{name} became available after entry decision")


def _aware_timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise FixtureContractError(f"{name} must be an ISO-8601 string with timezone")
    text = value.strip()
    if not (text.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", text)):
        raise FixtureContractError(f"{name} must include an explicit timezone")
    parsed = parse_cn_datetime(text)
    if parsed is None:
        raise FixtureContractError(f"{name} must be an ISO-8601 datetime")
    return parsed


def _session_for_contract(timestamp: datetime, contract: FixtureContract) -> str:
    local = timestamp.astimezone(CN_TZ)
    minute = local.hour * 60 + local.minute
    for name, windows in contract.session_windows.items():
        if any(
            start <= minute < end
            or (
                minute == end
                and end < 24 * 60
                and local.second == 0
                and local.microsecond == 0
            )
            for start, end in windows
        ):
            return name
    return "closed"


def _event_trade_date_and_session(
    raw: Mapping[str, Any],
    timing: EventTiming,
    contract: FixtureContract,
    name: str,
) -> tuple[str, str]:
    calendar = _mapping(raw, "exchange_calendar")
    trade_date = calendar.get("trade_date")
    if not isinstance(trade_date, str) or not re.fullmatch(r"\d{8}", trade_date):
        raise FixtureContractError(f"{name} calendar trade_date must be YYYYMMDD")
    try:
        datetime.strptime(trade_date, "%Y%m%d")
    except ValueError as exc:
        raise FixtureContractError(f"{name} calendar trade_date is invalid") from exc
    _nonempty_string_field(calendar, "calendar_lineage_ref", f"{name} calendar lineage")
    _nonempty_string_field(calendar, "receipt_id", f"{name} calendar receipt")
    _assert_available_by(calendar, f"{name} exchange_calendar", timing.decision_time)
    if calendar.get("calendar_eligible") is not True:
        if (
            calendar.get("calendar_eligible") is False
            and calendar.get("session") == "closed"
        ):
            return trade_date, "closed"
        raise FixtureContractError(f"{name} calendar eligibility is required")
    actual_session = _session_for_contract(timing.event_time, contract)
    if actual_session == "closed" or calendar.get("session") != actual_session:
        raise FixtureContractError(
            f"{name} calendar session is not permitted by contract"
        )
    return trade_date, actual_session


def _validate_followup_event_calendar(
    raw: Mapping[str, Any],
    timing: EventTiming,
    contract: FixtureContract,
    entry_trade_date: str,
    name: str,
) -> None:
    trade_date, session = _event_trade_date_and_session(raw, timing, contract, name)
    if session == "closed" or trade_date != entry_trade_date:
        raise FixtureContractError(f"{name} calendar does not cover entry trade date")


def _fee(
    price: float,
    multiplier: float,
    quantity: int,
    value: float,
    fee_type: str,
) -> float:
    if fee_type == "fixed_per_lot":
        return _finite_derived(value * quantity, "fee")
    return _finite_derived(price * multiplier * quantity * value, "fee")


def _base_result(
    contract: FixtureContract,
    timestamp: datetime,
    trade_date: str,
    session: str,
    generation: int,
    data_evidence: Mapping[str, Any],
    fixture_lineage_sha256: str,
    policy: MarketPolicy,
) -> dict[str, Any]:
    return {
        "mode": SIMULATION_MARKER,
        "real_trading_enabled": False,
        "data_source": "fixture_mock",
        "account": {
            "account_id": policy.capital_authority_id,
            "generation": generation,
            "capital_layer": "simulated",
            "account_type": "simulated",
        },
        "contract": {"symbol": contract.symbol, "product": contract.product},
        "data_evidence": dict(data_evidence),
        "fixture_lineage_sha256": fixture_lineage_sha256,
        "bar_timestamp": timestamp.astimezone(CN_TZ).isoformat(),
        "trade_date": trade_date,
        "session": session,
    }


def _hold(
    base: dict[str, Any],
    candidate: dict[str, Any],
    policy: MarketPolicy,
    reason: str,
) -> dict[str, Any]:
    candidate.update(
        {
            "execution_eligible": False,
            "fixture_simulation_eligible": False,
            "counterfactual_only": True,
            "reason": reason,
        }
    )
    sample = {
        **candidate,
        "sample_class": "counterfactual_or_hold",
        "counterfactual_only": True,
    }
    reconcile = {
        "account_id": policy.capital_authority_id,
        "generation": base["account"]["generation"],
        "cash_cny": policy.initial_equity_cny,
        "equity_cny": policy.initial_equity_cny,
        "margin_cny": 0.0,
        "fixture_reconciled": True,
        "non_authoritative": True,
        "durable": False,
        "capital_commit_id": None,
        "outbox_id": None,
    }
    return _with_lineage(
        base,
        candidate,
        {
            "simulation_only": True,
            "execution_authority": False,
            "durable": False,
            "capital_commit_id": None,
            "outbox_id": None,
            "orders": [],
        },
        sample,
        reconcile,
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
    try:
        canonical = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise FixtureContractError("fixture output must be canonical JSON") from exc
    result["lineage_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return result


def _order(
    intent_id: str,
    effect: str,
    side: str,
    contract: FixtureContract,
    quantity: int,
    price: float,
    fee: float,
    margin: float,
) -> dict[str, Any]:
    return {
        "intent_id": intent_id,
        "order_id": f"cnf-order-{_canonical_sha256({'intent_id': intent_id, 'effect': effect})[:24]}",
        "position_effect": effect,
        "side": side,
        "symbol": contract.symbol,
        "quantity": quantity,
        "price": _money(price),
        "fee_cny": _money(fee),
        "margin_cny": _money(margin),
        "simulated": True,
        "status": "simulated_filled",
        "execution_authority": False,
        "durable": False,
        "capital_commit_id": None,
        "outbox_id": None,
    }


def _pnl(
    side: str, entry: float, exit_price: float, multiplier: float, quantity: int
) -> float:
    direction = 1.0 if side == "long" else -1.0
    return _finite_derived(
        direction * (exit_price - entry) * multiplier * quantity, "PnL"
    )


def _round_tick(price: float, tick_size: float, upward: bool) -> float:
    units = _finite_derived(price / tick_size, "tick units")
    rounded = math.ceil(units - 1e-12) if upward else math.floor(units + 1e-12)
    result = _money(rounded * tick_size)
    if not math.isfinite(result) or result <= 0:
        raise FixtureContractError("tick-rounded price must be positive and finite")
    return result


def _nonempty_string_field(raw: Mapping[str, Any], key: str, name: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FixtureContractError(f"{name} is required")
    return value.strip()


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


def _finite_derived(value: float, name: str) -> float:
    if not math.isfinite(float(value)):
        raise FixtureContractError(f"derived {name} must be finite")
    return float(value)


def _money(value: float) -> float:
    return round(_finite_derived(value, "money value"), 8)


def _canonical_sha256(value: Any) -> str:
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise FixtureContractError("fixture identity must be canonical JSON") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["FixtureContractError", "run_fixture_closed_loop"]
