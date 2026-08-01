"""Fixture-only training baseline for the one CN futures strategy contract.

This is a deterministic pre-handoff harness, not a TradingDatas consumer,
paper runtime, broker adapter, scheduler, or training-quality claim.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
import hashlib
import json
import math
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from shared.capital.market_policy import MarketPolicy

from .adapter import CNFuturesAdapter
from .contract_rules import get_contract_rule, normalize_product
from .signal_engine import generate_style_signal


BASELINE_MODE = "fixture_mock_training_baseline"
STRATEGY_NAME = "commodity_intraday_trend"
_SYMBOL = re.compile(r"^m\d{3,4}\.dce$", re.IGNORECASE)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_FIXTURE_DAY_SESSION_WINDOWS = (
    (time(9), time(10, 15)),
    (time(10, 30), time(11, 30)),
    (time(13, 30), time(15)),
)


class TrainingBaselineError(ValueError):
    """Raised when a fixture tries to cross the training-baseline boundary."""


def run_fixture_training_baseline(fixture: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one M day-session fixture as a non-authoritative training sample."""

    _validate_fixture_marker(fixture)
    policy = _load_policy()
    style = _canonical_style()
    evidence = _project_evidence(_mapping(fixture, "data_evidence"))
    decision_time = _timestamp(fixture.get("decision_time"), "decision_time")
    trade_date = _trade_date(fixture.get("trade_date"))
    if decision_time.astimezone(_SHANGHAI).strftime("%Y%m%d") != trade_date:
        raise TrainingBaselineError("decision_trade_date_mismatch")
    generation = _whole_positive(fixture.get("generation"), "generation")
    symbol, rule = _contract(fixture)
    try:
        bars = _bars(_mapping(fixture, "bars"), trade_date)
    except TrainingBaselineError as exc:
        if str(exc) not in {
            "missing_5min_bars",
            "missing_5min_bar",
            "lunch_or_offsession_bar",
            "bar_not_on_5min_grid",
        }:
            raise
        lineage = _rejection_lineage(
            style,
            trade_date,
            symbol,
            rule,
            evidence,
            fixture.get("bars"),
            generation,
            decision_time,
        )
        return _hold(
            _base(
                style,
                policy,
                generation,
                trade_date,
                symbol,
                rule,
                evidence,
                lineage,
                decision_time,
            ),
            str(exc),
            lineage,
            policy,
        )
    lineage = _sha256(
        {
            "strategy": style,
            "trade_date": trade_date,
            "symbol": symbol,
            "contract": _contract_projection(rule),
            "data_evidence": evidence,
            "bars": bars,
            "generation": generation,
            "decision_time": decision_time.isoformat(),
        }
    )
    _validate_pit_order(bars, evidence, decision_time)
    base = _base(
        style,
        policy,
        generation,
        trade_date,
        symbol,
        rule,
        evidence,
        lineage,
        decision_time,
    )
    if _rollover_guard(symbol, trade_date, 5):
        return _hold(base, "rollover_guard", lineage, policy)
    reject = _pretrade_reject(bars, style, rule, policy, trade_date, symbol)
    if reject is not None:
        return _hold(base, reject, lineage, policy)

    signal = generate_style_signal(symbol, bars, style)
    if signal.get("action") not in {"buy", "sell"}:
        return _hold(
            base, str(signal.get("reason") or "strategy_hold"), lineage, policy, signal
        )
    return _one_lot_sample(base, signal, style, rule, policy, lineage)


def _base(
    style: Mapping[str, Any],
    policy: MarketPolicy,
    generation: int,
    trade_date: str,
    symbol: str,
    rule: Any,
    evidence: Mapping[str, Any],
    lineage: str,
    decision_time: datetime,
) -> dict[str, Any]:
    del style
    return {
        "mode": BASELINE_MODE,
        "real_trading_enabled": False,
        "training_data_authority": "fixture_mock_only",
        "not_real_market_data_training": True,
        "learning_evidence_eligible": False,
        "automatic_promotion": False,
        "strategy": {
            "name": STRATEGY_NAME,
            "style_family": STRATEGY_NAME,
            "product": "m",
            "cadence": "5min",
            "day_session_only": True,
            "one_lot_only": True,
            "no_overnight": True,
        },
        "account": {
            "account_id": policy.capital_authority_id,
            "generation": generation,
            "capital_layer": "simulated",
            "account_type": "simulated",
        },
        "trade_date": trade_date,
        "decision_time": decision_time.isoformat(),
        "contract": {
            "symbol": symbol,
            "product": "m",
            "exchange": "DCE",
            "static_fixture_spec": True,
            **_contract_projection(rule),
        },
        "data_evidence": evidence,
        "fixture_lineage_sha256": lineage,
    }


def _validate_fixture_marker(fixture: Mapping[str, Any]) -> None:
    if fixture.get("fixture_only") is not True:
        raise TrainingBaselineError("fixture_only_required")
    for key in (
        "real_trading_enabled",
        "live_broker",
        "real_account",
        "network_enabled",
        "scheduler_enabled",
        "delayed_paper_enabled",
    ):
        if fixture.get(key) not in (None, False):
            raise TrainingBaselineError(f"forbidden_fixture_marker:{key}")


def _canonical_style() -> dict[str, Any]:
    styles = CNFuturesAdapter().get_strategy_config().get("styles", {})
    if set(styles) != {STRATEGY_NAME}:
        raise TrainingBaselineError("single_strategy_contract_required")
    style = dict(styles[STRATEGY_NAME])
    if (
        tuple(str(item).lower() for item in style.get("products", ())) != ("m",)
        or style.get("style_family") != STRATEGY_NAME
        or style.get("day_session_only") is not True
        or style.get("no_overnight") is not True
    ):
        raise TrainingBaselineError("commodity_intraday_strategy_contract_mismatch")
    return style


def _load_policy() -> MarketPolicy:
    try:
        policy = MarketPolicy.load("cn_futures")
    except Exception as exc:
        raise TrainingBaselineError("cn_futures_policy_unavailable") from exc
    if (
        policy.market != "cn_futures"
        or policy.currency != "CNY"
        or policy.capital_layer != "simulated"
        or policy.real_trading_enabled is not False
        or policy.margin_utilization_limit_cny <= 0
        or policy.initial_equity_cny <= 0
    ):
        raise TrainingBaselineError("cn_futures_policy_mismatch")
    return policy


def _project_evidence(raw: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "source_kind": "fixture_mock",
        "catalog_route": "GET /v1/catalog",
        "query_route": "POST /v1/query",
        "catalog_state": "ready",
        "query_state": "ready",
        "degraded": False,
        "freshness": "fresh",
        "quality": "valid",
    }
    for key, value in expected.items():
        if raw.get(key) != value:
            raise TrainingBaselineError(f"invalid_fixture_evidence:{key}")
    return {
        **expected,
        "lineage_ref": _text(raw.get("lineage_ref"), "lineage_ref"),
        "receipt_id": _text(raw.get("receipt_id"), "receipt_id"),
        "available_at": _timestamp(raw.get("available_at"), "available_at").isoformat(),
    }


def _contract(fixture: Mapping[str, Any]) -> tuple[str, Any]:
    raw = _mapping(fixture, "contract")
    symbol = _text(raw.get("symbol"), "contract.symbol").upper()
    if not _SYMBOL.fullmatch(symbol):
        raise TrainingBaselineError("only_concrete_m_dce_contracts_are_supported")
    if normalize_product(symbol) != "m":
        raise TrainingBaselineError("only_m_product_is_supported")
    rule = get_contract_rule(symbol)
    if rule.product != "m" or rule.exchange != "DCE":
        raise TrainingBaselineError("m_contract_rule_mismatch")
    return symbol, rule


def _bars(raw: Mapping[str, Any], trade_date: str) -> list[dict[str, Any]]:
    rows = raw.get("rows")
    if not isinstance(rows, list) or not rows:
        raise TrainingBaselineError("missing_5min_bars")
    normalized: list[dict[str, Any]] = []
    previous: datetime | None = None
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise TrainingBaselineError("bar_must_be_mapping")
        timestamp = _timestamp(
            row.get("bar_time"), f"bars[{index}].bar_time"
        ).astimezone(_SHANGHAI)
        if timestamp.strftime("%Y%m%d") != trade_date:
            raise TrainingBaselineError("bar_trade_date_mismatch")
        if timestamp.second != 0 or timestamp.microsecond != 0 or timestamp.minute % 5:
            raise TrainingBaselineError("bar_not_on_5min_grid")
        if not _is_day_session(timestamp):
            raise TrainingBaselineError("lunch_or_offsession_bar")
        if previous is not None:
            if not _is_expected_5min_gap(previous, timestamp):
                raise TrainingBaselineError("missing_5min_bar")
        previous = timestamp
        open_price = _positive(row.get("open"), "open")
        high = _positive(row.get("high"), "high")
        low = _positive(row.get("low"), "low")
        close = _positive(row.get("close"), "close")
        if high < max(open_price, low, close) or low > min(open_price, high, close):
            raise TrainingBaselineError("invalid_ohlc_relationship")
        normalized.append(
            {
                "bar_time": timestamp.isoformat(),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": _nonnegative(row.get("volume"), "volume"),
                "previous_close": _positive_or_none(row.get("previous_close")),
            }
        )
    return normalized


def _validate_pit_order(
    bars: list[dict[str, Any]],
    evidence: Mapping[str, Any],
    decision_time: datetime,
) -> None:
    latest_event = _timestamp(bars[-1]["bar_time"], "latest_bar_time")
    available_at = _timestamp(evidence["available_at"], "available_at")
    if latest_event > available_at:
        raise TrainingBaselineError("bar_not_available_at_evidence_time")
    if available_at > decision_time:
        raise TrainingBaselineError("evidence_available_after_decision")


def _pretrade_reject(
    bars: list[dict[str, Any]],
    style: Mapping[str, Any],
    rule: Any,
    policy: MarketPolicy,
    trade_date: str,
    symbol: str,
) -> str | None:
    del trade_date, symbol
    latest_time = _timestamp(bars[-1]["bar_time"], "latest bar_time")
    minutes_to_close = int(
        (
            datetime.combine(latest_time.date(), time(15), latest_time.tzinfo)
            - latest_time
        ).total_seconds()
        // 60
    )
    if minutes_to_close <= int(style["flatten_before_session_close_minutes"]):
        return "session_close_guard"
    price = float(bars[-1]["close"])
    margin = price * rule.contract_multiplier * rule.margin_rate
    stop_loss = price * rule.contract_multiplier * float(style["stop_loss_pct"])
    fees = _fee_per_lot(rule, price, closing=False) + _fee_per_lot(
        rule, price, closing=True
    )
    allowed_margin = min(
        policy.margin_utilization_limit_cny,
        policy.initial_equity_cny * float(style["max_margin_usage"]),
    )
    allowed_loss = policy.initial_equity_cny * float(style["risk_per_trade"])
    if not all(
        math.isfinite(value) and value >= 0 for value in (margin, stop_loss, fees)
    ):
        raise TrainingBaselineError("nonfinite_pretrade_math")
    if margin > allowed_margin:
        return "one_lot_margin_reject"
    if stop_loss + fees > allowed_loss:
        return "one_lot_stop_budget_reject"
    return None


def _one_lot_sample(
    base: dict[str, Any],
    signal: Mapping[str, Any],
    style: Mapping[str, Any],
    rule: Any,
    policy: MarketPolicy,
    lineage: str,
) -> dict[str, Any]:
    price = float(signal["price"])
    entry_fee = _fee_per_lot(rule, price, closing=False)
    close_fee = _fee_per_lot(rule, price, closing=True)
    margin = price * rule.contract_multiplier * rule.margin_rate
    intent_id = f"cnf-training-intent-{lineage[:24]}"
    record = {
        "sample_id": f"cnf-training-sample-{lineage[:24]}",
        "sample_class": "fixture_training_one_lot_round_trip",
        "symbol": signal["symbol"],
        "strategy": STRATEGY_NAME,
        "side": signal["action"],
        "quantity": 1,
        "entry_price": _money(price),
        "exit_price": _money(price),
        "entry_fee_cny": _money(entry_fee),
        "close_fee_cny": _money(close_fee),
        "margin_cny": _money(margin),
        "realized_pnl_cny": 0.0,
        "net_pnl_cny": _money(-(entry_fee + close_fee)),
        "exit_reason": "fixture_same_session_flatten_no_overnight",
        "not_real_market_data_training": True,
        "learning_evidence_eligible": False,
        "execution_eligible": False,
        "fixture_simulation_eligible": True,
        "execution_authority": False,
        "durable": False,
        "capital_commit_id": None,
        "outbox_id": None,
    }
    return _finalize(
        base,
        {
            "action": signal["action"],
            "reason": signal["reason"],
            "execution_eligible": False,
            "fixture_simulation_eligible": True,
            "learning_evidence_eligible": False,
            "intent_id": intent_id,
        },
        [record],
        policy,
    )


def _hold(
    base: dict[str, Any],
    reason: str,
    lineage: str,
    policy: MarketPolicy,
    signal: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    action = str(signal.get("action") if signal else "hold")
    return _finalize(
        base,
        {
            "action": action,
            "reason": reason,
            "execution_eligible": False,
            "fixture_simulation_eligible": False,
            "learning_evidence_eligible": False,
            "intent_id": f"cnf-training-intent-{lineage[:24]}",
        },
        [
            {
                "sample_id": f"cnf-training-sample-{lineage[:24]}",
                "sample_class": "fixture_training_hold_or_risk_reject",
                "reason": reason,
                "not_real_market_data_training": True,
                "learning_evidence_eligible": False,
                "execution_eligible": False,
                "fixture_simulation_eligible": False,
                "execution_authority": False,
                "durable": False,
                "capital_commit_id": None,
                "outbox_id": None,
            }
        ],
        policy,
    )


def _rejection_lineage(
    style: Mapping[str, Any],
    trade_date: str,
    symbol: str,
    rule: Any,
    evidence: Mapping[str, Any],
    bars: Any,
    generation: int,
    decision_time: datetime,
) -> str:
    return _sha256(
        {
            "strategy": style,
            "trade_date": trade_date,
            "symbol": symbol,
            "contract": _contract_projection(rule),
            "data_evidence": evidence,
            "bars": bars,
            "generation": generation,
            "decision_time": decision_time.isoformat(),
            "baseline_rejection_projection": True,
        }
    )


def _finalize(
    base: dict[str, Any],
    candidate: dict[str, Any],
    samples: list[dict[str, Any]],
    policy: MarketPolicy,
) -> dict[str, Any]:
    return {
        **base,
        "candidate": candidate,
        "sample_records": samples,
        "daily_reconcile": {
            "account_id": policy.capital_authority_id,
            "generation": base["account"]["generation"],
            "fixture_reconciled": True,
            "non_authoritative": True,
            "learning_evidence_eligible": False,
            "durable": False,
            "capital_commit_id": None,
            "outbox_id": None,
        },
    }


def _fee_per_lot(rule: Any, price: float, *, closing: bool) -> float:
    fee_type = rule.close_fee_type if closing else rule.open_fee_type
    fee_rate = rule.close_fee_rate if closing else rule.open_fee_rate
    if fee_type == "fixed_per_lot":
        return float(fee_rate)
    return price * rule.contract_multiplier * float(fee_rate)


def _contract_projection(rule: Any) -> dict[str, Any]:
    return {
        "multiplier": rule.contract_multiplier,
        "tick_size": rule.tick_size,
        "margin_rate": rule.margin_rate,
        "open_fee_type": rule.open_fee_type,
        "open_fee_rate": rule.open_fee_rate,
        "close_fee_type": rule.close_fee_type,
        "close_fee_rate": rule.close_fee_rate,
    }


def _rollover_guard(symbol: str, trade_date: str, minimum_days: int) -> bool:
    month = int(symbol.split(".", 1)[0][-2:])
    year = 2000 + int(symbol.split(".", 1)[0][-4:-2])
    contract_month = datetime(year, month, 1)
    trade_day = datetime.strptime(trade_date, "%Y%m%d")
    return 0 <= (contract_month - trade_day).days < minimum_days


def _is_day_session(value: datetime) -> bool:
    if value.weekday() >= 5:
        return False
    current = value.time()
    return any(start <= current <= end for start, end in _FIXTURE_DAY_SESSION_WINDOWS)


def _is_expected_5min_gap(previous: datetime, current: datetime) -> bool:
    if current - previous == timedelta(minutes=5):
        return True
    return (previous.time(), current.time(), current - previous) in {
        (time(10, 15), time(10, 30), timedelta(minutes=15)),
        (time(11, 30), time(13, 30), timedelta(minutes=120)),
    }


def _trade_date(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        raise TrainingBaselineError("trade_date_required")
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise TrainingBaselineError("invalid_trade_date") from exc
    return value


def _timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TrainingBaselineError(f"timestamp_required:{name}")
    raw = value.strip()
    if not (raw.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", raw)):
        raise TrainingBaselineError(f"timezone_required:{name}")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TrainingBaselineError(f"invalid_timestamp:{name}") from exc
    if parsed.tzinfo is None:
        raise TrainingBaselineError(f"timezone_required:{name}")
    return parsed


def _mapping(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    if not isinstance(value, Mapping):
        raise TrainingBaselineError(f"mapping_required:{key}")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TrainingBaselineError(f"text_required:{name}")
    return value.strip()


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrainingBaselineError(f"positive_number_required:{name}")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise TrainingBaselineError(f"positive_number_required:{name}")
    return result


def _positive_or_none(value: Any) -> float | None:
    return None if value is None else _positive(value, "previous_close")


def _nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrainingBaselineError(f"nonnegative_number_required:{name}")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise TrainingBaselineError(f"nonnegative_number_required:{name}")
    return result


def _whole_positive(value: Any, name: str) -> int:
    result = _positive(value, name)
    if not result.is_integer():
        raise TrainingBaselineError(f"whole_number_required:{name}")
    return int(result)


def _money(value: float) -> float:
    if not math.isfinite(value):
        raise TrainingBaselineError("nonfinite_money")
    return round(value, 8)


def _sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TrainingBaselineError("canonical_fixture_required") from exc
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["TrainingBaselineError", "run_fixture_training_baseline"]
