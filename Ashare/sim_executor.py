#!/usr/bin/env python3
"""A-share server-local simulated executor.

Mini/Hermes bridge inputs are retired and fail closed. This module owns only the
A-share paper-broker semantics; future broker adapters remain market-specific
and are not implemented or enabled here.
"""

from __future__ import annotations

import os
import math
from datetime import datetime, time
from typing import Any

from shared.markets.sim_capital import default_sim_capital
from zoneinfo import ZoneInfo

from shared.execution.execution_reality import ashare_execution_reality
from shared.execution.sim_engine import SimExecutionEngine, SimOrder
from shared.execution.sim_broker import SimResult
from shared.execution.sim_executor_registry import register_sim_executor
from shared.markets.safety import reject_real_execution_payload
from shared.universe.policy import classify_instrument


MARKET = "ashare"
SIM_ACCOUNT = "ashare_sim"
PAPER_BROKER_CONTRACT = "tradingagent.ashare.paper_broker.v1"
SIM_AUTHORITY_ID = "ashare-capital-v1"
CN_TZ = ZoneInfo("Asia/Shanghai")
MAX_EXECUTION_BAR_AGE_SECONDS = 15 * 60
MAX_EXECUTION_BAR_FUTURE_SECONDS = 5 * 60
_DISABLED_VALUES = frozenset({"", "0", "false", "no", "off", "disabled"})
_RETIRED_BRIDGE_ENV = (
    "ASHARE_SIM_HERMES_ENABLED",
    "ASHARE_SIM_WEBHOOK_ENABLED",
)
_RETIRED_BRIDGE_CONFIG = (
    "hermes_enabled",
    "webhook",
    "webhook_url",
    "webhook_secret",
    "webhook_timeout",
    "webhook_retries",
    "signals_dir",
    "mock_mini_filled",
)
_TEST_ONLY_MOCK_CONFIG_KEY = "_test_only_ashare_mock_token"
_TEST_ONLY_MOCK_TOKEN = object()


def _now_cn() -> datetime:
    return datetime.now(CN_TZ)


def _account_name(account: dict[str, Any] | str | None) -> str:
    if isinstance(account, dict):
        for key in ("account", "account_id", "account_name", "name"):
            value = str(account.get(key, "")).strip()
            if value:
                return value
    value = str(account or "").strip()
    return value or SIM_ACCOUNT


def _coerce_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or not parsed.is_integer():
        return default
    return int(parsed)


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def build_test_only_ashare_mock_config(**overrides: Any) -> dict[str, Any]:
    """Build an in-process mock config that cannot cross a serialized boundary."""

    return {**overrides, _TEST_ONLY_MOCK_CONFIG_KEY: _TEST_ONLY_MOCK_TOKEN}


def _retired_bridge_requests(config: dict[str, Any]) -> tuple[str, ...]:
    requested: list[str] = []
    for name in _RETIRED_BRIDGE_ENV:
        value = str(os.environ.get(name, "")).strip().lower()
        if value not in _DISABLED_VALUES:
            requested.append(f"env:{name}")
    for name in _RETIRED_BRIDGE_CONFIG:
        value = config.get(name)
        if value not in (None, "", False, 0):
            requested.append(f"config:{name}")
    return tuple(requested)


def _parse_bar_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _fresh_5min_bar(
    value: Any, *, now: datetime | None = None
) -> tuple[bool, float | None]:
    parsed = _parse_bar_time(value)
    if parsed is None:
        return False, None
    reference = (now or _now_cn()).astimezone(CN_TZ)
    age_seconds = (reference - parsed).total_seconds()
    return (
        -MAX_EXECUTION_BAR_FUTURE_SECONDS
        <= age_seconds
        <= MAX_EXECUTION_BAR_AGE_SECONDS,
        age_seconds,
    )


def _is_supported_ashare_code(code: Any) -> bool:
    """Compatibility wrapper backed by the canonical instrument policy.

    Runtime validators still import this private helper.  Keep the call shape
    during migration, but do not retain a second prefix table here.
    """

    return classify_instrument(code).order_identity_allowed


def _reject(
    order_id: str,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> SimResult:
    model = ashare_execution_reality()
    return SimResult(
        status="rejected",
        filled_qty=0,
        avg_price=0.0,
        fee=0.0,
        message=message,
        order_id=order_id,
        market=MARKET,
        raw_response={
            "mode": "pre_bridge_validation",
            "code": code,
            "reason": message,
            "broker_contract": PAPER_BROKER_CONTRACT,
            "authority_id": SIM_AUTHORITY_ID,
            "execution_reality_model_version": model.model_version,
            **dict(details or {}),
        },
        broker_contract=PAPER_BROKER_CONTRACT,
        authority_id=SIM_AUTHORITY_ID,
    )


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_session_now(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    else:
        parsed = _now_cn()
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _classify_market_session(now: datetime) -> str:
    from Ashare.t_plus_1 import is_trading_day

    if not is_trading_day(now.date()):
        return "closed"
    current = now.time()
    if time(9, 15) <= current < time(9, 25):
        return "opening_auction"
    if time(9, 30) <= current <= time(11, 30):
        return "continuous_auction_am"
    if time(13, 0) <= current < time(14, 57):
        return "continuous_auction_pm"
    if time(14, 57) <= current <= time(15, 0):
        return "closing_auction"
    if time(15, 5) <= current <= time(15, 30):
        return "after_hours_fixed_price"
    return "closed"


def _is_regular_trading_session(now: datetime) -> bool:
    return _classify_market_session(now).startswith("continuous_auction")


def _market_session_validation(
    order: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    model = ashare_execution_reality()
    order_type = (
        str(order.get("order_type") or config.get("order_type") or "market")
        .strip()
        .lower()
    )
    base = {
        "execution_reality_model_version": model.model_version,
        "required_order_type": None,
        "market_session": "bypass",
        "allowed": True,
        "reason": "",
    }
    if _coerce_bool(config.get("bypass_market_hours"), False):
        return base
    enforce = _coerce_bool(os.environ.get("ASHARE_SIM_ENFORCE_MARKET_HOURS"), True)
    enforce = _coerce_bool(config.get("enforce_market_hours"), enforce)
    if not enforce:
        return base
    now = _parse_session_now(config.get("market_session_now") or config.get("now"))
    session = _classify_market_session(now)
    result = {
        **base,
        "market_session": session,
        "now": now.isoformat(timespec="seconds"),
    }
    if session.startswith("continuous_auction"):
        if order_type == "after_hours_fixed_price":
            return {
                **result,
                "allowed": False,
                "required_order_type": "market_or_limit",
                "reason": "after_hours_fixed_price_order_type_outside_after_hours_session",
            }
        return result
    session_rule = model.as_contract()["sessions"].get(session)
    if isinstance(session_rule, dict):
        return {
            **result,
            "allowed": False,
            "required_order_type": session_rule.get("order_type"),
            "reason": str(
                session_rule.get("unsupported_reason")
                or f"{session}_unsupported_by_sim_engine"
            ),
        }
    return {
        **result,
        "allowed": False,
        "reason": (
            "market_closed: A-share continuous auction is 09:30-11:30 or "
            "13:00-14:57 Asia/Shanghai; closing auction and after-hours fixed "
            f"price are separate sessions; now={now.isoformat(timespec='seconds')}"
        ),
    }


def _market_session_rejection(config: dict[str, Any]) -> str:
    validation = _market_session_validation({}, config)
    return "" if validation["allowed"] else str(validation["reason"])


def _first_value(*values: Any, default: Any = None) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return default


def _snapshot_field_source(
    field: str,
    order: dict[str, Any],
    config: dict[str, Any],
    card: dict[str, Any],
    snapshot: dict[str, Any],
) -> str:
    for owner_name, owner in (("order", order), ("config", config)):
        source_snapshot = owner.get("market_snapshot")
        if isinstance(source_snapshot, dict) and source_snapshot.get(field) not in (
            None,
            "",
        ):
            return f"{owner_name}.market_snapshot.{field}"
    if order.get(field) not in (None, ""):
        return f"order.{field}"
    if config.get(field) not in (None, ""):
        return f"config.{field}"
    if field in {"ask_price", "bid_price", "last_price"} and card.get("price") not in (
        None,
        "",
    ):
        if snapshot.get(field) == card.get("price"):
            return "signal_card.price"
    if snapshot.get(field) not in (None, ""):
        return f"snapshot.{field}"
    return ""


def _fill_evidence_from_snapshot(
    order: dict[str, Any],
    config: dict[str, Any],
    card: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    side = str(card.get("side") or order.get("side") or "buy").lower()
    quote_field = "ask_price" if side == "buy" else "bid_price"
    quote_source = _snapshot_field_source(quote_field, order, config, card, snapshot)
    last_source = _snapshot_field_source("last_price", order, config, card, snapshot)
    volume_source = (
        _snapshot_field_source("bar_volume", order, config, card, snapshot)
        or _snapshot_field_source("volume", order, config, card, snapshot)
        or _snapshot_field_source("vol", order, config, card, snapshot)
    )
    bar_time = _first_value(
        order.get("bar_time"),
        order.get("trade_time"),
        config.get("bar_time"),
        config.get("trade_time"),
        snapshot.get("bar_time"),
        snapshot.get("trade_time"),
    )
    bar_volume = _first_value(
        snapshot.get("bar_volume"), snapshot.get("volume"), snapshot.get("vol")
    )
    evidence_now = _parse_session_now(
        config.get("_resolved_market_session_now")
        or config.get("market_session_now")
        or config.get("now")
    )
    fresh_bar, bar_age_seconds = _fresh_5min_bar(bar_time, now=evidence_now)
    verified_5min = (
        quote_source.startswith(("order.market_snapshot.", "config.market_snapshot."))
        and bool(str(bar_time or "").strip())
        and _coerce_float(bar_volume, 0.0) > 0
        and fresh_bar
    )
    if verified_5min:
        evidence_reason = "verified_fresh_5min_bar"
    elif not quote_source.startswith(
        ("order.market_snapshot.", "config.market_snapshot.")
    ):
        evidence_reason = "unverified_snapshot_source"
    elif not str(bar_time or "").strip():
        evidence_reason = "missing_bar_time"
    elif _coerce_float(bar_volume, 0.0) <= 0:
        evidence_reason = "missing_bar_volume"
    else:
        evidence_reason = "stale_or_future_5min_bar"
    source_class = "market_data" if verified_5min else "signal_card_price"
    return {
        "fill_price_field": quote_field,
        "fill_price_source": quote_source or last_source or "unknown",
        "fill_price_source_class": source_class,
        "quote_price": snapshot.get(quote_field),
        "last_price": snapshot.get("last_price"),
        "last_price_source": last_source,
        "bar_volume": bar_volume,
        "bar_volume_source": volume_source,
        "bar_time": bar_time,
        "bar_age_seconds": round(bar_age_seconds, 3)
        if bar_age_seconds is not None
        else None,
        "evidence_reason": evidence_reason,
        "execution_evidence_class": "verified_5min_market_data"
        if verified_5min
        else "weak_price_only",
    }


def _date_iso(value: Any, fallback: str) -> str:
    raw = str(value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
        return raw[:10]
    return fallback


def _next_sellable_date_iso(trade_date: str) -> str:
    try:
        from Ashare.t_plus_1 import next_sellable_date

        return next_sellable_date(trade_date).isoformat()
    except Exception:
        return trade_date


def _snapshot_from_payload(
    order: dict[str, Any],
    account: dict[str, Any] | str | None,
    config: dict[str, Any],
    card: dict[str, Any],
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for source in (config.get("market_snapshot"), order.get("market_snapshot")):
        if isinstance(source, dict):
            snapshot.update(source)

    price = float(card.get("price") or 0.0)
    side = str(card.get("side") or "buy").lower()
    bar_liquidity = _first_value(
        order.get("bar_volume"),
        order.get("volume"),
        order.get("vol"),
        config.get("bar_volume"),
        config.get("volume"),
        config.get("vol"),
    )
    default_size = None if bar_liquidity is not None else card.get("quantity")
    if side == "buy":
        snapshot.setdefault(
            "ask_price",
            _first_value(order.get("ask_price"), config.get("ask_price"), price),
        )
        ask_size = _first_value(
            order.get("ask_size"), config.get("ask_size"), default_size
        )
        if ask_size is not None:
            snapshot.setdefault("ask_size", ask_size)
    else:
        snapshot.setdefault(
            "bid_price",
            _first_value(order.get("bid_price"), config.get("bid_price"), price),
        )
        bid_size = _first_value(
            order.get("bid_size"), config.get("bid_size"), default_size
        )
        if bid_size is not None:
            snapshot.setdefault("bid_size", bid_size)
    snapshot.setdefault(
        "last_price",
        _first_value(order.get("last_price"), config.get("last_price"), price),
    )
    available_qty = _first_value(
        order.get("available_qty"), config.get("available_qty"), default_size
    )
    if available_qty is not None:
        snapshot.setdefault("available_qty", available_qty)

    for key in (
        "previous_close",
        "pre_close",
        "reference_price",
        "official_closing_price",
        "upper_limit",
        "lower_limit",
        "price_limit_pct",
        "price_limit_exempt",
        "price_cage_reference",
        "buy_price_cage_reference",
        "sell_price_cage_reference",
        "price_cage_reference_required",
        "board",
        "board_type",
        "risk_warning",
        "is_st",
        "bar_volume",
        "volume",
        "vol",
        "volatility",
        "volatility_bps",
        "queue_position",
        "participation_cap",
        "liquidity_multiplier",
        "market_impact_multiplier",
        "counterparty_profile",
        "market_environment",
    ):
        value = _first_value(order.get(key), config.get(key))
        if value is not None:
            snapshot.setdefault(key, value)

    model = ashare_execution_reality()
    snapshot.setdefault(
        "market_session",
        str(config.get("_resolved_market_session") or ""),
    )
    snapshot.setdefault(
        "execution_time",
        str(
            config.get("_resolved_market_session_now")
            or config.get("market_session_now")
            or config.get("now")
            or ""
        ),
    )
    snapshot.setdefault("execution_reality_model_version", model.model_version)

    if isinstance(account, dict):
        cash_available = _first_value(
            account.get("cash_available"),
            account.get("cash"),
            account.get("available_cash"),
        )
        sellable_qty = _first_value(
            account.get("sellable_qty"), account.get("available_position")
        )
        position_qty = _first_value(
            account.get("position_qty"), account.get("current_position")
        )
    else:
        cash_available = None
        sellable_qty = None
        position_qty = None
    cash_available = _first_value(
        order.get("cash_available"), config.get("cash_available"), cash_available
    )
    sellable_qty = _first_value(
        order.get("sellable_qty"), config.get("sellable_qty"), sellable_qty
    )
    position_qty = _first_value(
        order.get("position_qty"), config.get("position_qty"), position_qty
    )
    if cash_available is None or sellable_qty is None or position_qty is None:
        try:
            from shared.execution.local_sim_ledger import get_local_sim_account_snapshot

            account_snapshot = get_local_sim_account_snapshot(
                account or _account_name(account),
                symbol=str(card.get("ts_code") or ""),
                trade_date=str(
                    card.get("valid_until")
                    or order.get("trade_date")
                    or order.get("date")
                    or ""
                ),
                starting_cash=_first_value(
                    config.get("starting_cash"),
                    config.get("initial_capital"),
                    account.get("initial_capital")
                    if isinstance(account, dict)
                    else None,
                    account.get("cash") if isinstance(account, dict) else None,
                    default=default_sim_capital(MARKET),
                ),
            )
        except Exception:
            account_snapshot = {}
        if cash_available is None:
            cash_available = account_snapshot.get("cash_available")
        if sellable_qty is None:
            sellable_qty = account_snapshot.get("sellable_qty")
        if position_qty is None:
            position_qty = account_snapshot.get("position_qty")
    if cash_available is not None:
        snapshot.setdefault("cash_available", cash_available)
    if sellable_qty is not None:
        snapshot.setdefault("sellable_qty", sellable_qty)
    if position_qty is not None:
        snapshot.setdefault("position_qty", position_qty)
    return snapshot


def _execute_server_local(
    order: dict[str, Any],
    account: dict[str, Any] | str | None,
    config: dict[str, Any],
    card: dict[str, Any],
) -> SimResult:
    safe_metadata = dict(order)
    safe_card = dict(card)
    safe_card["direct_execution"] = False
    safe_metadata["signal_card"] = safe_card
    market_snapshot = _snapshot_from_payload(order, account, config, card)
    fill_evidence = _fill_evidence_from_snapshot(order, config, card, market_snapshot)
    if fill_evidence.get("execution_evidence_class") != "verified_5min_market_data":
        return _reject(
            str(card["order_id"]),
            str(card["ts_code"]),
            "unverified_execution_evidence",
            details={
                "reason_code": "verified_fresh_5min_market_data_required",
                "fill_evidence": fill_evidence,
            },
        )
    safe_metadata["fill_evidence"] = fill_evidence
    safe_metadata["fill_price_source"] = fill_evidence["fill_price_source"]
    safe_metadata["fill_price_source_class"] = fill_evidence["fill_price_source_class"]
    safe_metadata["market_session"] = market_snapshot.get("market_session")
    safe_metadata["execution_reality_model_version"] = market_snapshot.get(
        "execution_reality_model_version"
    )
    sim_order = SimOrder(
        symbol=str(card["ts_code"]),
        side=str(card.get("side") or "buy"),
        quantity=int(card["quantity"]),
        limit_price=float(card["price"]),
        order_type=str(order.get("order_type") or config.get("order_type") or "market"),
        time_in_force=str(
            order.get("time_in_force") or config.get("time_in_force") or "day"
        ),
        market=MARKET,
        order_id=str(card["order_id"]),
        submitted_at=str(card.get("timestamp") or ""),
        metadata=safe_metadata,
    )
    engine = SimExecutionEngine(MARKET, profile=config.get("sim_engine_profile"))
    current_position = _coerce_int(market_snapshot.get("position_qty"), 0)
    if current_position > 0:
        engine.position(sim_order.symbol).current_holdings = current_position
    record = engine.submit_order(sim_order, market_snapshot)
    status = "pending" if record.state == "open" else record.state
    fee = float((record.fees or {}).get("total", 0.0) or 0.0)
    reason_suffix = f": {record.reason}" if getattr(record, "reason", "") else ""
    return SimResult(
        status=status,
        filled_qty=int(record.filled_qty or 0),
        avg_price=float(record.avg_fill_price or 0.0),
        fee=fee,
        message=f"Server-local A-share simulated fill via matching engine: {record.state}{reason_suffix}",
        order_id=sim_order.order_id,
        market=MARKET,
        raw_response={
            "mode": "server_local_sim_engine",
            "broker_contract": PAPER_BROKER_CONTRACT,
            "signal_card": card,
            "market_snapshot": market_snapshot,
            "fill_evidence": fill_evidence,
            "engine_record": record.as_dict(),
        },
        broker_contract=PAPER_BROKER_CONTRACT,
        authority_id=SIM_AUTHORITY_ID,
    )


def _signal_card(
    order: dict[str, Any],
    account: dict[str, Any] | str | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    model = ashare_execution_reality()
    now = _parse_session_now(config.get("market_session_now") or config.get("now"))
    today = now.date().isoformat()
    trade_date = _date_iso(order.get("trade_date") or order.get("date"), today)
    order_id = str(
        order.get("order_id") or f"SIM-ASHARE-{now.strftime('%Y%m%d%H%M%S')}"
    )
    price = _coerce_float(
        order.get("price", order.get("limit_price", order.get("mid_price"))), 0.0
    )
    quantity = _coerce_int(
        order.get("quantity", order.get("qty", order.get("filled_qty"))), 0
    )
    side = (
        str(order.get("side", order.get("direction", "buy"))).lower().strip() or "buy"
    )
    sellable_date = _date_iso(
        config.get("sellable_from") or config.get("sellable_date"), ""
    )
    if not sellable_date:
        sellable_date = (
            _next_sellable_date_iso(trade_date) if side == "buy" else trade_date
        )
    card = {
        "order_id": order_id,
        "market": MARKET,
        "ts_code": str(order.get("ts_code") or order.get("symbol") or "").strip(),
        "direction": side,
        "side": side,
        "order_type": str(
            order.get("order_type") or config.get("order_type") or "market"
        )
        .strip()
        .lower(),
        "quantity": quantity,
        "price": price,
        "trigger_price": price,
        "status": "pending",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
        "execution_reality_model_version": model.model_version,
        "commission_schedule_status": model.commission_schedule_status,
        "commission_schedule_version": model.commission_schedule_version,
        "account": _account_name(account),
        "manual_confirm_required": False,
        "direct_execution": False,
        "dry_run": bool(config.get("dry_run", False)),
        "strategy_name": str(order.get("strategy_name") or "ashare_sim_executor"),
        "timestamp": now.isoformat(timespec="seconds"),
        "valid_until": str(config.get("valid_until") or trade_date),
        "idempotency_key": str(order.get("idempotency_key") or order_id),
        "source": "ashare_server_local_paper_broker",
        "broker_contract": PAPER_BROKER_CONTRACT,
        "authority_id": SIM_AUTHORITY_ID,
        "t_plus_1": {
            "sellable_from": sellable_date,
            "sellable_date": sellable_date,
        },
        "notes": "A-share simulated execution is server-local and isolated from every future live broker adapter.",
    }
    for key in (
        "capital_scope",
        "market_capital_required",
        "market_capital_reference_id",
        "market_capital_reservation_id",
        "market_capital_event_id",
        "market_reserved_gross_cny",
    ):
        if key in order:
            card[key] = order.get(key)
    return card


def ashare_sim_execute(
    order: dict[str, Any],
    account: dict[str, Any] | str | None = None,
    config: dict[str, Any] | None = None,
) -> SimResult:
    """Execute one A-share order through the server-local paper broker."""

    reject_real_execution_payload(order, context="ashare_sim_execute.order")
    reject_real_execution_payload(
        account if isinstance(account, dict) else {},
        context="ashare_sim_execute.account",
    )
    reject_real_execution_payload(config or {}, context="ashare_sim_execute.config")
    config = dict(config or {})
    code = str(order.get("ts_code") or order.get("symbol") or "").strip().upper()
    eligibility = classify_instrument(
        code,
        exchange=order.get("exchange"),
        instrument_type=order.get("instrument_type") or "common_stock",
    )
    if not eligibility.order_identity_allowed:
        now = _parse_session_now(config.get("market_session_now") or config.get("now"))
        order_id = str(
            order.get("order_id") or f"REJECTED-ASHARE-{now.strftime('%Y%m%d%H%M%S')}"
        )
        return _reject(
            order_id,
            code,
            f"unsupported or non-mainboard A-share code: {code}",
            details={
                "reason_code": "instrument_not_mainboard_tradable",
                "instrument_policy_id": eligibility.policy_id,
                "instrument_role": eligibility.role.value,
                "instrument_reason_code": eligibility.reason_code,
                "context_only": eligibility.context_only,
                "order_identity_allowed": eligibility.order_identity_allowed,
            },
        )
    card = _signal_card(order, account, config)
    order_id = str(card["order_id"])
    if int(card.get("quantity") or 0) <= 0 or float(card.get("price") or 0.0) <= 0:
        return _reject(order_id, code, "non-positive quantity or price")

    session_validation = _market_session_validation(order, config)
    card["market_session"] = session_validation["market_session"]
    card["execution_reality_model_version"] = session_validation[
        "execution_reality_model_version"
    ]
    config["_resolved_market_session"] = session_validation["market_session"]
    config["_resolved_market_session_now"] = (
        session_validation.get("now") or card["timestamp"]
    )
    if not session_validation["allowed"]:
        return _reject(
            order_id,
            code,
            str(session_validation["reason"]),
            details={
                key: value
                for key, value in session_validation.items()
                if key not in {"allowed", "reason"}
            },
        )
    mock_token = config.pop(_TEST_ONLY_MOCK_CONFIG_KEY, None)
    serialized_mock_request = bool(config.get("mock") or config.get("mock_filled"))
    mock_mode = mock_token is _TEST_ONLY_MOCK_TOKEN

    retired_bridge_requests = _retired_bridge_requests(config)
    if retired_bridge_requests:
        return _reject(
            order_id,
            code,
            "mini_hermes_bridge_retired",
            details={
                "broker_contract": PAPER_BROKER_CONTRACT,
                "retired_inputs": list(retired_bridge_requests),
            },
        )

    if serialized_mock_request and not mock_mode:
        return _reject(
            order_id,
            code,
            "serialized_mock_fill_not_authorized",
            details={"reason_code": "test_only_mock_token_required"},
        )

    if mock_mode:
        return SimResult(
            status="filled",
            filled_qty=int(card["quantity"]),
            avg_price=float(card["price"]),
            fee=float(config.get("mock_fee", 0.0) or 0.0),
            message="Local mock fill for A-share simulated execution tests",
            order_id=order_id,
            market=MARKET,
            raw_response={
                "mode": "mock_filled",
                "broker_contract": PAPER_BROKER_CONTRACT,
                "signal_card": card,
            },
            broker_contract=PAPER_BROKER_CONTRACT,
            authority_id=SIM_AUTHORITY_ID,
        )
    return _execute_server_local(order, account, config, card)


register_sim_executor(
    MARKET,
    ashare_sim_execute,
    simulation_contract=PAPER_BROKER_CONTRACT,
    authority_id=SIM_AUTHORITY_ID,
)


__all__ = ["ashare_sim_execute", "build_test_only_ashare_mock_config"]
