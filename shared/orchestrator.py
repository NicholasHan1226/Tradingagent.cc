#!/usr/bin/env python3
"""Market-agnostic shadow trading orchestrator."""

from __future__ import annotations

import json
import inspect
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from shared.markets.base import MarketAdapter
from shared.notify import email_sender

ROOT = Path(__file__).resolve().parent.parent
SIGNALS_DIR = ROOT / "signals"

StageFn = Callable[..., Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _date_iso(date_value: str) -> str:
    raw = str(date_value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_quantity(value: Any, default: int | float = 0) -> int | float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:
        return default
    if abs(result - round(result)) < 1e-12:
        return int(round(result))
    return result


def _strategy_config(adapter: MarketAdapter) -> dict[str, Any]:
    try:
        config = adapter.get_strategy_config()
    except Exception:
        return {}
    return config if isinstance(config, dict) else {}


@dataclass
class OrchestratorDeps:
    score_stock: StageFn
    build_pool: StageFn
    debate: StageFn
    risk_check: StageFn
    construct: StageFn
    size_position: StageFn
    record_shadow: StageFn
    run_review: StageFn
    record_audit_event: StageFn
    execute_sim_order: StageFn | None = None
    send_email: StageFn | None = None


def _default_deps() -> OrchestratorDeps:
    from shared.accounting.trade_audit_trail import record_event
    from shared.adversarial.bull_bear_debate import debate
    from shared.execution import sim_broker
    from shared.execution.shadow_broker import record_shadow
    from shared.portfolio.constructor import construct
    from shared.portfolio.position_sizer import size_position
    from shared.review.daily_review import run_daily_review as review
    from shared.risk.pre_trade_check import check
    from shared.screening.candidate_pool import build_pool
    from shared.screening.six_dimension_scorer import score_stock

    return OrchestratorDeps(
        score_stock=score_stock,
        build_pool=build_pool,
        debate=debate,
        risk_check=check,
        construct=construct,
        size_position=size_position,
        record_shadow=record_shadow,
        run_review=review,
        record_audit_event=record_event,
        execute_sim_order=getattr(sim_broker, "execute_sim_order", sim_broker.simulate_order),
        send_email=email_sender.send_email,
    )


def _record_audit(
    deps: OrchestratorDeps,
    stage: str,
    symbol: str,
    *,
    parent_audit_id: str = "",
    payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    capital_layer: str = "shadow",
) -> dict[str, Any]:
    payload = dict(payload or {})
    metadata = {"capital_layer": capital_layer, **dict(metadata or {})}
    kwargs: dict[str, Any] = {
        "stage": stage,
        "ts_code": symbol,
        "parent_audit_id": parent_audit_id,
        "metadata": metadata,
    }
    if stage == "signal":
        kwargs["signal_data"] = payload
    elif stage == "decision":
        kwargs["decision_data"] = payload
    elif stage == "risk":
        kwargs["risk_data"] = payload
    elif stage == "execution":
        kwargs["execution_data"] = payload
    elif stage == "result":
        kwargs["result_data"] = payload
    return deps.record_audit_event(**kwargs)


def _stage_error(stage: str, exc: Exception, *, capital_layer: str = "shadow") -> dict[str, Any]:
    return {
        "stage": stage,
        "status": "degraded",
        "error_type": exc.__class__.__name__,
        "error": str(exc),
        "capital_layer": capital_layer,
    }


def _safe_stage(
    stage: str,
    errors: list[dict[str, Any]],
    func: Callable[[], Any],
    *,
    default: Any,
    capital_layer: str = "shadow",
) -> Any:
    try:
        return func()
    except Exception as exc:
        errors.append(_stage_error(stage, exc, capital_layer=capital_layer))
        return default


def _latest_price(reader: Any, market: str, symbol: str, date: str, default: float) -> float:
    try:
        rows = reader.get_bars_daily(market, symbol, None, date)
    except Exception:
        return default
    if not rows:
        return default
    return max(_safe_float(rows[-1].get("close"), default), 0.0) or default


def _latest_volatility(reader: Any, market: str, symbol: str, date: str, default: float) -> float:
    try:
        rows = reader.get_bars_daily(market, symbol, None, date)
    except Exception:
        return default
    closes = [_safe_float(row.get("close"), 0.0) for row in rows[-21:]]
    closes = [close for close in closes if close > 0]
    if len(closes) < 2:
        return default
    returns = [(closes[idx] / closes[idx - 1]) - 1.0 for idx in range(1, len(closes))]
    mean = sum(returns) / len(returns)
    variance = sum((ret - mean) ** 2 for ret in returns) / len(returns)
    return max((variance ** 0.5) * (252 ** 0.5), 0.01)


def _write_pending_signal(card: dict[str, Any], signals_dir: Path = SIGNALS_DIR) -> dict[str, Any]:
    from shared.execution.signal_state_machine import SignalStateConflict, SignalStateMachine

    # Shadow records are research/paper-tracking signals. Keep their pending lifecycle
    # for review and email de-duplication, but isolate them from executable queues.
    layer = str(card.get("capital_layer") or "").strip().lower()
    direct_execution = bool(card.get("direct_execution"))
    state_root = signals_dir / "shadow" if layer == "shadow" and not direct_execution else signals_dir
    machine = SignalStateMachine(state_root)
    try:
        result = machine.write_pending(card)
        if state_root != signals_dir:
            result["queue_scope"] = "shadow"
        return result
    except SignalStateConflict as exc:
        return {
            "order_id": card.get("order_id", ""),
            "status": "duplicate",
            "recorded": False,
            "message": str(exc),
            "signal_card": card,
            "queue_scope": "shadow" if state_root != signals_dir else "execution",
        }


def _write_execution_signal(
    card: dict[str, Any],
    receipt: dict[str, Any],
    signals_dir: Path = SIGNALS_DIR,
) -> dict[str, Any]:
    from shared.execution.signal_state_machine import SignalStateConflict, SignalStateMachine

    machine = SignalStateMachine(signals_dir)
    status = str(receipt.get("status", "")).strip().lower()
    retryable = bool(receipt.get("retryable")) or status in {"pending", "queued", "retryable", "unfilled"}
    rejected = status in {"rejected", "reject", "failed", "failure", "error", "cancelled", "canceled"}
    raw_response = receipt.get("raw_response") if isinstance(receipt.get("raw_response"), dict) else {}
    existing_signal_path = str(receipt.get("signal_path") or raw_response.get("signal_path") or "")

    if retryable and existing_signal_path:
        return {
            "order_id": card.get("order_id", ""),
            "status": "pending",
            "pending_signal": {
                "status": "pending",
                "signal_path": existing_signal_path,
                "signal_card": raw_response.get("signal_card", card),
                "source": "sim_executor",
            },
        }

    webhook_payload = raw_response.get("webhook") if isinstance(raw_response.get("webhook"), dict) else {}
    mini_webhook_sent = raw_response.get("mode") == "mini_webhook_sent" or bool(webhook_payload.get("success"))
    if retryable and mini_webhook_sent:
        return {
            "order_id": card.get("order_id", ""),
            "status": "pending",
            "pending_signal": {
                "status": "pending",
                "signal_card": raw_response.get("signal_card", card),
                "source": "mini_webhook",
                "webhook": webhook_payload,
            },
        }

    try:
        pending = machine.write_pending(card)
    except SignalStateConflict as exc:
        if retryable:
            return {
                "order_id": card.get("order_id", ""),
                "status": "pending",
                "pending_signal": {
                    "status": "pending",
                    "signal_card": card,
                    "source": "existing_state_machine_card",
                },
                "message": str(exc),
            }
        return {
            "order_id": card.get("order_id", ""),
            "status": "duplicate",
            "recorded": False,
            "message": str(exc),
            "signal_card": card,
        }

    if retryable:
        return {"order_id": card.get("order_id", ""), "status": "pending", "pending_signal": pending}
    if rejected:
        reason = str(receipt.get("message") or receipt.get("reason") or status or "sim order rejected")
        failed = machine.fail(str(card.get("order_id", "")), reason=reason)
        return {"order_id": card.get("order_id", ""), "status": "failed", "pending_signal": pending, "failed_signal": failed}

    fill_info = dict(receipt)
    if "filled_quantity" in fill_info and "filled_qty" not in fill_info:
        fill_info["filled_qty"] = fill_info["filled_quantity"]
    if "filled_qty" in fill_info and "filled_quantity" not in fill_info:
        fill_info["filled_quantity"] = fill_info["filled_qty"]
    fill_info.setdefault("filled_price", card.get("price", 0.0))
    fill_info.setdefault("filled_quantity", card.get("quantity", 0))
    fill_info.setdefault("filled_qty", fill_info.get("filled_quantity", card.get("quantity", 0)))
    claimed = machine.claim(str(card.get("order_id", "")), worker_id="sim_loop")
    running = machine.mark_running(str(card.get("order_id", "")), worker_id="sim_loop")
    filled = machine.fill(str(card.get("order_id", "")), fill_info)
    return {
        "order_id": card.get("order_id", ""),
        "status": "filled",
        "pending_signal": pending,
        "claimed_signal": claimed,
        "running_signal": running,
        "filled_signal": filled,
    }


def _make_order_id(prefix: str, market: str, symbol: str, date: str) -> str:
    return f"{prefix}{market}-{symbol}-{date}-{uuid.uuid4().hex[:8]}".replace("/", "-")


def _compact_date_key(value: Any) -> str:
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    return raw.replace("-", "")[:8]


def _sim_idempotency_key(market: str, account: str, symbol: str, date: str, side: str) -> str:
    date_key = _compact_date_key(date)
    parts = ("SIM", market.lower(), account, date_key, symbol.upper(), side.lower())
    return ":".join(str(part).replace("/", "-").replace(" ", "_") for part in parts)


def _signal_card_date_key(card: dict[str, Any], fallback_name: str = "") -> str:
    for key in ("trade_date", "date", "valid_until", "timestamp", "filled_at", "received_at", "created_at"):
        value = card.get(key)
        date_key = _compact_date_key(value)
        if len(date_key) == 8 and date_key.isdigit():
            return date_key
    return _compact_date_key(fallback_name)


def _find_existing_sim_signal(
    signals_dir: Path,
    *,
    market: str,
    account: str,
    symbol: str,
    date: str,
    side: str,
    capital_layer: str,
    account_type: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    target_market = market.lower()
    target_symbol = symbol.upper()
    target_date = _compact_date_key(date)
    target_side = side.lower()
    states = ("pending", "claimed", "running", "filled", "failed", "partial", "expired", "cancelled")
    for state in states:
        state_dir = signals_dir / state
        if not state_dir.exists():
            continue
        for path in sorted(state_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                card = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(card.get("idempotency_key") or "") == idempotency_key:
                return {"state": state, "path": str(path), "order_id": card.get("order_id"), "idempotency_key": idempotency_key}
            card_symbol = str(card.get("ts_code") or card.get("symbol") or card.get("code") or "").upper()
            if card_symbol != target_symbol:
                continue
            card_market = str(card.get("market") or "").lower()
            if card_market and card_market != target_market:
                continue
            if not card_market and f"{target_market}-" not in path.name.lower():
                continue
            card_layer = str(card.get("capital_layer") or "").lower()
            if card_layer and card_layer != capital_layer.lower():
                continue
            card_account_type = str(card.get("account_type") or "").lower()
            if card_account_type and card_account_type != account_type.lower():
                continue
            card_side = str(card.get("direction") or card.get("side") or "buy").lower()
            if card_side != target_side:
                continue
            if _signal_card_date_key(card, path.name) != target_date:
                continue
            return {
                "state": state,
                "path": str(path),
                "order_id": card.get("order_id"),
                "idempotency_key": card.get("idempotency_key"),
                "matched_by": "same_day_symbol_side",
                "account": account,
            }
    return None


def _build_signal_card(
    *,
    market: str,
    symbol: str,
    account: str,
    date: str,
    order: dict[str, Any],
    risk: dict[str, Any],
    trade: dict[str, Any],
    audit_id: str,
    order_id: str | None = None,
    order_id_prefix: str = "SHADOW-",
    capital_layer: str = "shadow",
    account_type: str = "shadow",
    direct_execution: bool = False,
) -> dict[str, Any]:
    order_id = order_id or _make_order_id(order_id_prefix, market, symbol, date)
    return {
        "order_id": order_id,
        "ts_code": symbol,
        "market": market,
        "direction": order.get("side", "buy"),
        "quantity": _safe_quantity(order.get("quantity"), 0),
        "price": _safe_float(order.get("price"), 0.0),
        "strategy_name": account,
        "timestamp": _now_iso(),
        "status": "pending",
        "capital_layer": capital_layer,
        "account_type": account_type,
        "manual_confirm_required": False,
        "direct_execution": direct_execution,
        "risk_check": {
            "passed": bool(risk.get("approved", False)),
            "adjusted_weight": risk.get("adjusted_weight"),
            "adjustments": risk.get("adjustments", []),
            "reasons": risk.get("reasons", []),
        },
        "shadow_trade_id": trade.get("trade_id", ""),
        "source_audit_id": audit_id,
        "valid_until": _date_iso(date),
        "idempotency_key": order.get("idempotency_key") or order_id,
        "evidence_refs": [audit_id],
    }


def _send_template_email_now(
    sender: StageFn | None,
    template_name: str,
    data: dict[str, Any],
    *,
    subject: str,
) -> dict[str, Any]:
    if sender is None:
        return {"status": "skipped", "reason": "email sender unavailable", "template": template_name}
    html_body = email_sender.render_template_html(template_name, data)
    channel = email_sender._channel_key_for_template(template_name)
    recipient = email_sender.get_channel(template_name)["to"]
    plain_body = str(data.get("summary") or f"{subject}\n请查看 HTML 邮件内容。")
    result = sender(
        recipient,
        subject,
        plain_body,
        html_body,
        channel=channel,
        rate_limit_type=template_name,
    )
    if isinstance(result, dict):
        return result
    return {"status": "unknown", "template": template_name, "raw_result": result}


def _trading_signal_email_data(
    *,
    market: str,
    symbol: str,
    date: str,
    account: str,
    order: dict[str, Any],
    position: dict[str, Any],
    score: dict[str, Any],
    risk: dict[str, Any],
    card: dict[str, Any],
) -> dict[str, Any]:
    quantity = _safe_quantity(order.get("quantity"), 0)
    price = _safe_float(order.get("price"), 0.0)
    weight = _safe_float(position.get("weight"), _safe_float(risk.get("adjusted_weight"), 0.0))
    total_score = score.get("total", score.get("combined", score.get("belief_score", "--")))
    reasons = risk.get("reasons") if isinstance(risk.get("reasons"), list) else []
    condition = "; ".join(str(item) for item in reasons if item) or f"{market} shadow signal generated"
    return {
        "date": _date_iso(date),
        "ts_code": symbol,
        "name": str(score.get("name", "")),
        "current_price": price,
        "trigger_condition": condition,
        "scores": {
            "macro": score.get("macro", "--"),
            "event": score.get("event", "--"),
            "fundamental": score.get("fundamental", "--"),
            "moneyflow": score.get("moneyflow", "--"),
            "technical": score.get("technical", "--"),
            "sentiment": score.get("sentiment", "--"),
            "total": total_score,
        },
        "action": order.get("side", "buy"),
        "position_size": {
            "shares": quantity,
            "amount": quantity * price,
            "pct_of_capital": weight * 100 if 0 <= weight <= 1 else weight,
        },
        "capital_layer": "shadow",
        "account_type": "shadow",
        "account": account,
        "order_id": card.get("order_id", ""),
        "summary": f"影子盘新信号: {symbol} {order.get('side', 'buy')} {quantity} @ {price}",
    }


def _trade_receipt_email_data(
    *,
    market: str,
    symbol: str,
    date: str,
    account: str,
    order: dict[str, Any],
    receipt: dict[str, Any],
    card: dict[str, Any],
) -> dict[str, Any]:
    filled_price = _safe_float(receipt.get("filled_price"), _safe_float(order.get("price"), 0.0))
    requested_price = _safe_float(order.get("price"), 0.0)
    quantity = _safe_int(receipt.get("filled_quantity", receipt.get("filled_qty")), _safe_int(order.get("quantity"), 0))
    slippage_pct = 0.0
    if requested_price > 0:
        slippage_pct = ((filled_price / requested_price) - 1.0) * 100
    fill_time = str(receipt.get("fill_time") or receipt.get("filled_at") or _now_iso())
    order_id = str(receipt.get("order_id") or card.get("order_id") or order.get("order_id") or "")
    return {
        "date": _date_iso(date),
        "ts_code": symbol,
        "name": "",
        "direction": order.get("side", "buy"),
        "quantity": quantity,
        "filled_price": filled_price,
        "requested_price": requested_price,
        "slippage_pct": slippage_pct,
        "fill_time": fill_time,
        "order_id": order_id,
        "commission": _safe_float(receipt.get("commission"), 0.0),
        "channel": "sim",
        "market": market,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "account": account,
        "summary": f"模拟盘成交回执: {symbol} {order.get('side', 'buy')} {quantity} @ {filled_price}",
    }


def _load_shadow_trades_for_date(date: str) -> list[dict[str, Any]]:
    from shared.execution import shadow_broker

    target = _date_iso(date)
    rows: list[dict[str, Any]] = []
    path = shadow_broker.SHADOW_TRADES
    try:
        if not path.exists():
            return rows
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("trade_date") == target:
                    normalized = dict(row)
                    normalized["capital_layer"] = "shadow"
                    rows.append(normalized)
    except Exception:
        return []
    return rows


def _candidate_symbols(pool: dict[str, Any], fallback_universe: list[str]) -> list[str]:
    symbols: list[str] = []
    for layer in ("holdings", "watch", "candidate"):
        values = pool.get(layer, []) if isinstance(pool, dict) else []
        if isinstance(values, list):
            symbols.extend(str(item) for item in values if item)
    if not symbols:
        symbols = [str(item) for item in fallback_universe if item]
    seen: set[str] = set()
    ordered: list[str] = []
    for symbol in symbols:
        if symbol not in seen:
            seen.add(symbol)
            ordered.append(symbol)
    return ordered


def _account_name(account: Any, default: str) -> str:
    if isinstance(account, dict):
        for key in ("account", "account_id", "account_name", "name", "strategy_name"):
            value = str(account.get(key, "")).strip()
            if value:
                return value
        return default
    value = str(account or "").strip()
    return value or default


def _account_positions(account: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[Any] = []
    if isinstance(account, dict):
        sources.extend([account.get("positions"), account.get("holdings")])
    sources.extend([config.get("sim_positions"), config.get("positions")])
    positions: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, list):
            continue
        for row in source:
            if isinstance(row, dict):
                normalized = dict(row)
                normalized["capital_layer"] = "simulated"
                normalized.setdefault("account_type", "simulated")
                positions.append(normalized)
    return positions


def _account_capital(account: Any, config: dict[str, Any]) -> float:
    for source in (account if isinstance(account, dict) else {}, config):
        if not isinstance(source, dict):
            continue
        for key in ("sim_capital", "cash", "available_cash", "equity", "net_liquidation", "shadow_capital"):
            value = _safe_float(source.get(key), -1.0)
            if value >= 0:
                return value
    return 100000.0


def _run_review_for_layer(
    deps: OrchestratorDeps,
    date: str,
    *,
    session: str,
    capital_layer: str,
) -> dict[str, Any]:
    try:
        signature = inspect.signature(deps.run_review)
    except (TypeError, ValueError):
        result = deps.run_review(date, session=session)
    else:
        if "capital_layer" in signature.parameters:
            result = deps.run_review(date, session=session, capital_layer=capital_layer)
        else:
            result = deps.run_review(date, session=session)
    if isinstance(result, dict):
        result = dict(result)
        result["capital_layer"] = capital_layer
        return result
    return {"session": session, "trade_date": date, "capital_layer": capital_layer, "raw_result": result}


def _coerce_sim_receipt(receipt: Any, order: dict[str, Any]) -> dict[str, Any]:
    if isinstance(receipt, dict):
        payload = dict(receipt)
    elif is_dataclass(receipt):
        payload = asdict(receipt)
    elif hasattr(receipt, "__dict__"):
        payload = {
            key: getattr(receipt, key)
            for key in (
                "status",
                "filled_qty",
                "filled_quantity",
                "avg_price",
                "filled_price",
                "fee",
                "message",
                "capital_layer",
                "account_type",
                "order_id",
                "market",
                "raw_response",
            )
            if hasattr(receipt, key)
        }
    else:
        return {"status": "failed", "message": "invalid sim broker receipt"}

    payload.setdefault("order_id", order.get("order_id", ""))
    if "filled_qty" in payload and "filled_quantity" not in payload:
        payload["filled_quantity"] = payload["filled_qty"]
    if "filled_quantity" in payload and "filled_qty" not in payload:
        payload["filled_qty"] = payload["filled_quantity"]
    if "avg_price" in payload and "filled_price" not in payload:
        payload["filled_price"] = payload["avg_price"]
    payload.setdefault("capital_layer", "simulated")
    payload.setdefault("account_type", "simulated")
    return payload


def _execute_sim_order(deps: OrchestratorDeps, order: dict[str, Any], account: Any) -> dict[str, Any]:
    if deps.execute_sim_order is None:
        raise RuntimeError("sim_broker.execute_sim_order is unavailable")
    try:
        signature = inspect.signature(deps.execute_sim_order)
    except (TypeError, ValueError):
        receipt = deps.execute_sim_order(order)
    else:
        params = signature.parameters
        kwargs: dict[str, Any] = {}
        if "market" in params:
            kwargs["market"] = str(order.get("market") or "").lower().strip()
        if "account" in params:
            kwargs["account"] = account
        elif "sim_account" in params:
            kwargs["sim_account"] = account
        if kwargs:
            receipt = deps.execute_sim_order(order, **kwargs)
        elif len(params) >= 2 and not any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in params.values()):
            receipt = deps.execute_sim_order(order, account)
        else:
            receipt = deps.execute_sim_order(order)
    return _coerce_sim_receipt(receipt, order)


def _supports_market_aware_score(score_stock: StageFn) -> bool:
    try:
        signature = inspect.signature(score_stock)
    except (TypeError, ValueError):
        return True
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    first_name = positional[0].name.lower() if positional else ""
    second_name = positional[1].name.lower() if len(positional) > 1 else ""
    if first_name in {"symbol", "ts_code", "ticker", "market_id"} and second_name in {"date", "trade_date"}:
        return False
    return (
        any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in signature.parameters.values())
        or len(positional) >= 4
        or first_name.startswith("market")
        or "market" in signature.parameters
    )


def _score_stock_for_market(
    deps: OrchestratorDeps,
    market: str,
    symbol: str,
    date: str,
    reader: Any,
) -> Any:
    if _supports_market_aware_score(deps.score_stock):
        return deps.score_stock(market, symbol, reader, date)
    return deps.score_stock(symbol, date, data_reader=reader)


def _build_pool_for_market(
    deps: OrchestratorDeps,
    market: str,
    date: str,
    universe: list[str],
    reader: Any,
) -> Any:
    try:
        signature = inspect.signature(deps.build_pool)
    except (TypeError, ValueError):
        return deps.build_pool(date=date, universe=universe, market=market, reader=reader)
    if "market" in signature.parameters or "reader" in signature.parameters or "market_adapter" in signature.parameters:
        return deps.build_pool(date=date, universe=universe, market=market, reader=reader)
    return deps.build_pool(date=date, universe=universe)


def _run_condition_lifecycle(
    market: str,
    pool: dict[str, Any],
    scores_by_symbol: dict[str, dict[str, Any]],
    date: str,
    reader: Any,
) -> dict[str, Any]:
    from shared.screening.condition_generator import generate_conditions
    from shared.screening.condition_monitor import trigger_replay

    conditions = generate_conditions(pool=pool, scores_map=scores_by_symbol, date=date, reader=reader, market=market)
    replay = trigger_replay(conditions, date=date)
    return {
        "condition_count": len(conditions),
        "trigger_replay_count": len(replay),
        "filled_replay_count": sum(1 for row in replay if row.get("replay_fillable")),
        "conditions": conditions,
        "trigger_replay": replay,
    }


def run_shadow_loop(
    market_adapter: MarketAdapter,
    date: str,
    reader: Any,
    *,
    deps: OrchestratorDeps | None = None,
    signals_dir: Path = SIGNALS_DIR,
) -> dict[str, Any]:
    """Run screening -> debate -> risk -> portfolio -> shadow execution -> review."""

    deps = deps or _default_deps()
    errors: list[dict[str, Any]] = []
    stage_calls: list[str] = []
    audits: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    market = _safe_stage("adapter.get_market", errors, market_adapter.get_market, default="unknown")
    account = _safe_stage("adapter.get_shadow_account", errors, market_adapter.get_shadow_account, default=f"{market}_shadow")
    config = _strategy_config(market_adapter)
    capital = _safe_float(config.get("shadow_capital"), 100000.0)
    method = str(config.get("portfolio_method", "conviction_weighted"))
    regime = str(config.get("regime", "unknown"))
    max_candidates = max(1, int(config.get("max_candidates", 20)))
    default_price = _safe_float(config.get("default_price"), 1.0)
    default_volatility = _safe_float(config.get("default_volatility"), 0.20)
    market_rules = config.get("market_rules") if isinstance(config.get("market_rules"), dict) else {}
    lot_size = _safe_float(market_rules.get("lot_size"), 1.0) if isinstance(market_rules, dict) else 1.0

    universe = _safe_stage("screening.universe", errors, lambda: market_adapter.get_universe(date), default=[])
    stage_calls.append("screening.universe")
    if not isinstance(universe, list):
        errors.append({"stage": "screening.universe", "status": "degraded", "error": "adapter returned non-list", "capital_layer": "shadow"})
        universe = []

    scores_by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in universe[:max_candidates]:
        mapped_market, mapped_symbol = _safe_stage(
            "adapter.map_symbol_to_reader",
            errors,
            lambda symbol=symbol: market_adapter.map_symbol_to_reader(symbol),
            default=(market, symbol),
        )
        score = _safe_stage(
            "screening.six_dim",
            errors,
            lambda symbol=mapped_symbol: _score_stock_for_market(deps, market, symbol, date, reader),
            default={"combined": 0.5},
        )
        stage_calls.append("screening.six_dim")
        if not isinstance(score, dict):
            score = {"combined": 0.5}
        score["capital_layer"] = "shadow"
        score["market"] = market
        scores_by_symbol[symbol] = score
        audit = _record_audit(
            deps,
            "signal",
            symbol,
            payload={"scores": score, "market": market},
            metadata={"date": date, "account": account},
        )
        audits.append(audit)

    pool = _safe_stage(
        "screening.candidate_pool",
        errors,
        lambda: _build_pool_for_market(deps, market, date, list(scores_by_symbol), reader),
        default={"candidate": list(scores_by_symbol), "watch": [], "holdings": [], "universe": list(scores_by_symbol)},
    )
    stage_calls.append("screening.candidate_pool")
    if not isinstance(pool, dict):
        pool = {"candidate": list(scores_by_symbol), "watch": [], "holdings": [], "universe": list(scores_by_symbol)}
    condition_lifecycle = _safe_stage(
        "screening.condition_lifecycle",
        errors,
        lambda: _run_condition_lifecycle(market, pool, scores_by_symbol, date, reader),
        default={"condition_count": 0, "trigger_replay_count": 0, "filled_replay_count": 0, "conditions": [], "trigger_replay": []},
    )
    stage_calls.append("screening.condition_lifecycle")

    candidates = _candidate_symbols(pool, list(scores_by_symbol))[:max_candidates]
    orders_for_portfolio: list[dict[str, Any]] = []
    skipped_candidates: list[dict[str, Any]] = []
    signal_audit_by_symbol = {audit["ts_code"]: audit for audit in audits if audit.get("stage") == "signal"}

    for symbol in candidates:
        mapped_market, mapped_symbol = _safe_stage(
            "adapter.map_symbol_to_reader",
            errors,
            lambda symbol=symbol: market_adapter.map_symbol_to_reader(symbol),
            default=(market, symbol),
        )
        score = scores_by_symbol.get(symbol, {"combined": 0.5, "market": mapped_market, "capital_layer": "shadow"})
        parent = signal_audit_by_symbol.get(symbol, {}).get("audit_id", "")
        debate = _safe_stage(
            "adversarial.bull_bear_debate",
            errors,
            lambda symbol=mapped_symbol, score=score: deps.debate(symbol, score),
            default={"ts_code": mapped_symbol, "belief_score": 0.5, "bull_case": "degraded", "bear_case": "degraded"},
        )
        stage_calls.append("adversarial.bull_bear_debate")
        if not isinstance(debate, dict):
            debate = {"belief_score": 0.5}
        decision_audit = _record_audit(
            deps,
            "decision",
            symbol,
            parent_audit_id=parent,
            payload={"debate": debate, "capital_layer": "shadow"},
            metadata={"date": date, "account": account},
        )
        audits.append(decision_audit)

        price = _latest_price(reader, mapped_market, mapped_symbol, date, default_price)
        volatility = _latest_volatility(reader, mapped_market, mapped_symbol, date, default_volatility)
        if price <= 0:
            skipped_candidates.append({"symbol": symbol, "reason": "missing_or_non_positive_price", "price": price, "capital_layer": "shadow"})
            continue
        proposed_weight = _safe_stage(
            "portfolio.position_sizer",
            errors,
            lambda debate=debate, volatility=volatility: deps.size_position(
                _safe_float(debate.get("belief_score"), 0.5), volatility, regime
            ),
            default=0.0,
        )
        stage_calls.append("portfolio.position_sizer")
        risk_order = {
            "ts_code": symbol,
            "weight": proposed_weight,
            "sector": str(score.get("sector", "unknown")),
            "turnover_wan": _safe_float(score.get("turnover_wan"), 0.0),
            "capital_layer": "shadow",
        }
        risk = _safe_stage("risk.pre_trade_check", errors, lambda: deps.risk_check(risk_order, {"positions": []}), default={"approved": False, "adjusted_weight": 0.0, "reasons": ["degraded"]})
        stage_calls.append("risk.pre_trade_check")
        if not isinstance(risk, dict):
            risk = {"approved": False, "adjusted_weight": 0.0, "reasons": ["invalid risk result"]}
        risk_audit = _record_audit(
            deps,
            "risk",
            symbol,
            parent_audit_id=decision_audit["audit_id"],
            payload=risk,
            metadata={"date": date, "account": account},
        )
        audits.append(risk_audit)
        if not risk.get("approved") or _safe_float(risk.get("adjusted_weight"), 0.0) <= 0:
            continue
        orders_for_portfolio.append({
            "ts_code": symbol,
            "belief_score": _safe_float(debate.get("belief_score"), 0.5),
            "volatility": volatility,
            "sector": str(score.get("sector", "unknown")),
            "price": price,
            "weight": _safe_float(risk.get("adjusted_weight"), proposed_weight),
            "lot_size": lot_size,
            "risk_audit_id": risk_audit["audit_id"],
            "mapped_market": mapped_market,
            "mapped_symbol": mapped_symbol,
        })

    portfolio = _safe_stage(
        "portfolio.constructor",
        errors,
        lambda: deps.construct(orders_for_portfolio, capital, method=method, regime=regime),
        default={"positions": [], "total_weight": 0.0, "cash_weight": 1.0},
    )
    stage_calls.append("portfolio.constructor")
    if not isinstance(portfolio, dict):
        portfolio = {"positions": [], "total_weight": 0.0, "cash_weight": 1.0}

    order_meta = {order["ts_code"]: order for order in orders_for_portfolio}
    for position in portfolio.get("positions", []) or []:
        if not isinstance(position, dict) or not position.get("ts_code"):
            continue
        symbol = str(position["ts_code"])
        meta = order_meta.get(symbol, {})
        order = {
            "ts_code": symbol,
            "side": "buy",
            "quantity": _safe_quantity(position.get("shares"), 0),
            "price": _safe_float(position.get("price"), 0.0),
            "trade_date": date,
            "market": market,
            "capital_layer": "shadow",
            "note": f"orchestrator shadow loop {market} {date}",
        }
        if order["quantity"] <= 0 or order["price"] <= 0:
            errors.append({"stage": "execution.shadow_broker", "status": "skipped", "symbol": symbol, "reason": "non-positive quantity or price", "capital_layer": "shadow"})
            continue
        trade = _safe_stage("execution.shadow_broker", errors, lambda order=order: deps.record_shadow(order, account), default={"recorded": False, "status": "degraded"})
        stage_calls.append("execution.shadow_broker")
        if not isinstance(trade, dict):
            trade = {"recorded": False, "status": "invalid"}
        execution_audit = _record_audit(
            deps,
            "execution",
            symbol,
            parent_audit_id=str(meta.get("risk_audit_id", "")),
            payload={"order": order, "shadow_broker": trade, "capital_layer": "shadow"},
            metadata={"date": date, "account": account},
        )
        audits.append(execution_audit)
        risk = {"approved": True, "adjusted_weight": position.get("weight"), "adjustments": [], "reasons": []}
        card = _build_signal_card(
            market=market,
            symbol=symbol,
            account=account,
            date=date,
            order=order,
            risk=risk,
            trade=trade,
            audit_id=execution_audit["audit_id"],
        )
        pending = _safe_stage("signals.pending", errors, lambda card=card: _write_pending_signal(card, signals_dir), default={"status": "degraded", "recorded": False})
        stage_calls.append("signals.pending")
        email_notification = {"status": "skipped", "reason": "signal not newly pending", "template": "trading_signal"}
        if pending.get("status") == "pending":
            email_data = _trading_signal_email_data(
                market=market,
                symbol=symbol,
                date=date,
                account=account,
                order=order,
                position=position,
                score=scores_by_symbol.get(symbol, {}),
                risk=risk,
                card=card,
            )
            email_notification = _safe_stage(
                "notify.trading_signal",
                errors,
                lambda data=email_data: _send_template_email_now(
                    deps.send_email,
                    "trading_signal",
                    data,
                    subject=f"Tradings 影子盘新信号 {symbol} {_date_iso(date)}",
                ),
                default={"status": "degraded", "template": "trading_signal"},
            )
            stage_calls.append("notify.trading_signal")
        result_audit = _record_audit(
            deps,
            "result",
            symbol,
            parent_audit_id=execution_audit["audit_id"],
            payload={"pending_signal": pending, "email_notification": email_notification, "capital_layer": "shadow"},
            metadata={"date": date, "account": account},
        )
        audits.append(result_audit)
        records.append({"symbol": symbol, "order": order, "trade": trade, "pending_signal": pending, "email_notification": email_notification})

    review = _safe_stage("review.daily_review", errors, lambda: deps.run_review(date, session="close"), default={"session": "close", "error": "degraded", "trade_date": date})
    stage_calls.append("review.daily_review")

    return {
        "market": market,
        "date": date,
        "capital_layer": "shadow",
        "account": account,
        "state": "degraded" if errors else "ok",
        "stage_calls": stage_calls,
        "universe_count": len(universe),
        "candidate_count": len(candidates),
        "order_count": len(orders_for_portfolio),
        "skipped_candidate_count": len(skipped_candidates),
        "skipped_candidates": skipped_candidates[:20],
        "recorded_count": sum(1 for record in records if record["trade"].get("recorded")),
        "portfolio": portfolio,
        "records": records,
        "audit_events": audits,
        "errors": errors,
        "review": review,
        "condition_lifecycle": condition_lifecycle,
        "generated_at": _now_iso(),
    }


def run_sim_loop(
    market_adapter: MarketAdapter,
    date: str,
    reader: Any,
    *,
    deps: OrchestratorDeps | None = None,
    signals_dir: Path = SIGNALS_DIR,
) -> dict[str, Any]:
    """Run screening -> debate -> simulated risk/portfolio/execution -> review."""

    deps = deps or _default_deps()
    errors: list[dict[str, Any]] = []
    stage_calls: list[str] = []
    audits: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    capital_layer = "simulated"
    account_type = "simulated"
    market = _safe_stage("adapter.get_market", errors, market_adapter.get_market, default="unknown", capital_layer=capital_layer)
    sim_account_getter = getattr(market_adapter, "get_sim_account", None)
    if callable(sim_account_getter):
        account_obj = _safe_stage(
            "adapter.get_sim_account",
            errors,
            sim_account_getter,
            default={"account": f"{market}_simulated"},
            capital_layer=capital_layer,
        )
    else:
        account_obj = _safe_stage(
            "adapter.get_shadow_account",
            errors,
            market_adapter.get_shadow_account,
            default=f"{market}_simulated",
            capital_layer=capital_layer,
        )
    config = _strategy_config(market_adapter)
    account = _account_name(account_obj, f"{market}_simulated")
    existing_positions = _account_positions(account_obj, config)
    capital = _account_capital(account_obj, config)
    method = str(config.get("portfolio_method", "conviction_weighted"))
    regime = str(config.get("regime", "unknown"))
    max_candidates = max(1, int(config.get("max_candidates", 20)))
    default_price = _safe_float(config.get("default_price"), 1.0)
    default_volatility = _safe_float(config.get("default_volatility"), 0.20)

    universe = _safe_stage("screening.universe", errors, lambda: market_adapter.get_universe(date), default=[], capital_layer=capital_layer)
    stage_calls.append("screening.universe")
    if not isinstance(universe, list):
        errors.append({"stage": "screening.universe", "status": "degraded", "error": "adapter returned non-list", "capital_layer": capital_layer})
        universe = []

    scores_by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in universe[:max_candidates]:
        mapped_market, mapped_symbol = _safe_stage(
            "adapter.map_symbol_to_reader",
            errors,
            lambda symbol=symbol: market_adapter.map_symbol_to_reader(symbol),
            default=(market, symbol),
            capital_layer=capital_layer,
        )
        score = _safe_stage(
            "screening.six_dim",
            errors,
            lambda symbol=mapped_symbol: _score_stock_for_market(deps, market, symbol, date, reader),
            default={"combined": 0.5},
            capital_layer=capital_layer,
        )
        stage_calls.append("screening.six_dim")
        if not isinstance(score, dict):
            score = {"combined": 0.5}
        score["capital_layer"] = capital_layer
        score["account_type"] = account_type
        score["market"] = market
        scores_by_symbol[symbol] = score
        audit = _record_audit(
            deps,
            "signal",
            symbol,
            payload={"scores": score, "market": market, "capital_layer": capital_layer},
            metadata={"date": date, "account": account, "account_type": account_type},
            capital_layer=capital_layer,
        )
        audits.append(audit)

    pool = _safe_stage(
        "screening.candidate_pool",
        errors,
        lambda: _build_pool_for_market(deps, market, date, list(scores_by_symbol), reader),
        default={"candidate": list(scores_by_symbol), "watch": [], "holdings": [], "universe": list(scores_by_symbol)},
        capital_layer=capital_layer,
    )
    stage_calls.append("screening.candidate_pool")
    if not isinstance(pool, dict):
        pool = {"candidate": list(scores_by_symbol), "watch": [], "holdings": [], "universe": list(scores_by_symbol)}
    condition_lifecycle = _safe_stage(
        "screening.condition_lifecycle",
        errors,
        lambda: _run_condition_lifecycle(market, pool, scores_by_symbol, date, reader),
        default={"condition_count": 0, "trigger_replay_count": 0, "filled_replay_count": 0, "conditions": [], "trigger_replay": []},
        capital_layer=capital_layer,
    )
    stage_calls.append("screening.condition_lifecycle")

    candidates = _candidate_symbols(pool, list(scores_by_symbol))[:max_candidates]
    orders_for_portfolio: list[dict[str, Any]] = []
    skipped_candidates: list[dict[str, Any]] = []
    signal_audit_by_symbol = {audit["ts_code"]: audit for audit in audits if audit.get("stage") == "signal"}
    risk_portfolio = {
        "positions": existing_positions,
        "total_exposure": sum(_safe_float(position.get("weight"), 0.0) for position in existing_positions),
        "capital_layer": capital_layer,
        "account_type": account_type,
    }

    for symbol in candidates:
        mapped_market, mapped_symbol = _safe_stage(
            "adapter.map_symbol_to_reader",
            errors,
            lambda symbol=symbol: market_adapter.map_symbol_to_reader(symbol),
            default=(market, symbol),
            capital_layer=capital_layer,
        )
        score = scores_by_symbol.get(symbol, {"combined": 0.5, "market": mapped_market, "capital_layer": capital_layer, "account_type": account_type})
        parent = signal_audit_by_symbol.get(symbol, {}).get("audit_id", "")
        debate = _safe_stage(
            "adversarial.bull_bear_debate",
            errors,
            lambda symbol=mapped_symbol, score=score: deps.debate(symbol, score),
            default={"ts_code": mapped_symbol, "belief_score": 0.5, "bull_case": "degraded", "bear_case": "degraded"},
            capital_layer=capital_layer,
        )
        stage_calls.append("adversarial.bull_bear_debate")
        if not isinstance(debate, dict):
            debate = {"belief_score": 0.5}
        debate["capital_layer"] = capital_layer
        debate["account_type"] = account_type
        decision_audit = _record_audit(
            deps,
            "decision",
            symbol,
            parent_audit_id=parent,
            payload={"debate": debate, "capital_layer": capital_layer, "account_type": account_type},
            metadata={"date": date, "account": account, "account_type": account_type},
            capital_layer=capital_layer,
        )
        audits.append(decision_audit)

        price = _latest_price(reader, mapped_market, mapped_symbol, date, default_price)
        volatility = _latest_volatility(reader, mapped_market, mapped_symbol, date, default_volatility)
        if price <= 0:
            skipped_candidates.append({"symbol": symbol, "reason": "missing_or_non_positive_price", "price": price, "capital_layer": capital_layer})
            continue
        proposed_weight = _safe_stage(
            "portfolio.position_sizer",
            errors,
            lambda debate=debate, volatility=volatility: deps.size_position(
                _safe_float(debate.get("belief_score"), 0.5), volatility, regime
            ),
            default=0.0,
            capital_layer=capital_layer,
        )
        stage_calls.append("portfolio.position_sizer")
        risk_order = {
            "ts_code": symbol,
            "weight": proposed_weight,
            "sector": str(score.get("sector", "unknown")),
            "turnover_wan": _safe_float(score.get("turnover_wan"), 0.0),
            "capital_layer": capital_layer,
            "account_type": account_type,
        }
        risk = _safe_stage(
            "risk.pre_trade_check",
            errors,
            lambda: deps.risk_check(risk_order, risk_portfolio),
            default={"approved": False, "adjusted_weight": 0.0, "reasons": ["degraded"]},
            capital_layer=capital_layer,
        )
        stage_calls.append("risk.pre_trade_check")
        if not isinstance(risk, dict):
            risk = {"approved": False, "adjusted_weight": 0.0, "reasons": ["invalid risk result"]}
        risk["capital_layer"] = capital_layer
        risk["account_type"] = account_type
        risk_audit = _record_audit(
            deps,
            "risk",
            symbol,
            parent_audit_id=decision_audit["audit_id"],
            payload=risk,
            metadata={"date": date, "account": account, "account_type": account_type},
            capital_layer=capital_layer,
        )
        audits.append(risk_audit)
        if not risk.get("approved") or _safe_float(risk.get("adjusted_weight"), 0.0) <= 0:
            continue
        orders_for_portfolio.append({
            "ts_code": symbol,
            "belief_score": _safe_float(debate.get("belief_score"), 0.5),
            "volatility": volatility,
            "sector": str(score.get("sector", "unknown")),
            "price": price,
            "weight": _safe_float(risk.get("adjusted_weight"), proposed_weight),
            "risk_audit_id": risk_audit["audit_id"],
            "mapped_market": mapped_market,
            "mapped_symbol": mapped_symbol,
        })

    portfolio = _safe_stage(
        "portfolio.constructor",
        errors,
        lambda: deps.construct(orders_for_portfolio, capital, method=method, regime=regime),
        default={"positions": [], "total_weight": 0.0, "cash_weight": 1.0},
        capital_layer=capital_layer,
    )
    stage_calls.append("portfolio.constructor")
    if not isinstance(portfolio, dict):
        portfolio = {"positions": [], "total_weight": 0.0, "cash_weight": 1.0}
    portfolio["capital_layer"] = capital_layer
    portfolio["account_type"] = account_type
    portfolio["existing_positions"] = existing_positions

    order_meta = {order["ts_code"]: order for order in orders_for_portfolio}
    for position in portfolio.get("positions", []) or []:
        if not isinstance(position, dict) or not position.get("ts_code"):
            continue
        symbol = str(position["ts_code"])
        meta = order_meta.get(symbol, {})
        side = "buy"
        order_id = _make_order_id("SIM-", market, symbol, date)
        idempotency_key = _sim_idempotency_key(market, account, symbol, date, side)
        order = {
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "ts_code": symbol,
            "side": side,
            "quantity": _safe_int(position.get("shares"), 0),
            "price": _safe_float(position.get("price"), 0.0),
            "mid_price": _safe_float(position.get("price"), 0.0),
            "limit_price": _safe_float(position.get("price"), 0.0),
            "order_type": "market",
            "trade_date": date,
            "strategy_name": account,
            "market": market,
            "capital_layer": capital_layer,
            "account_type": account_type,
            "note": f"orchestrator sim loop {market} {date}",
        }
        if order["quantity"] <= 0 or order["price"] <= 0:
            errors.append({"stage": "execution.sim_broker", "status": "skipped", "symbol": symbol, "reason": "non-positive quantity or price", "capital_layer": capital_layer})
            continue
        existing_signal = _find_existing_sim_signal(
            signals_dir,
            market=market,
            account=account,
            symbol=symbol,
            date=date,
            side=side,
            capital_layer=capital_layer,
            account_type=account_type,
            idempotency_key=idempotency_key,
        )
        if existing_signal:
            stage_calls.append("signals.sim_dedup")
            signal_result = {
                "order_id": order_id,
                "status": "duplicate",
                "recorded": False,
                "existing_signal": existing_signal,
            }
            receipt = {
                "order_id": order_id,
                "status": "duplicate",
                "message": "same-day simulated signal already exists",
                "existing_signal": existing_signal,
                "capital_layer": capital_layer,
                "account_type": account_type,
            }
            email_notification = {"status": "skipped", "reason": "duplicate same-day sim signal", "template": "trade_receipt"}
            records.append({"symbol": symbol, "order": order, "receipt": receipt, "signal_result": signal_result, "email_notification": email_notification})
            continue
        receipt = _safe_stage(
            "execution.sim_broker",
            errors,
            lambda order=order: _execute_sim_order(deps, order, account_obj),
            default={"order_id": order_id, "status": "failed", "message": "degraded"},
            capital_layer=capital_layer,
        )
        stage_calls.append("execution.sim_broker")
        if not isinstance(receipt, dict):
            receipt = {"order_id": order_id, "status": "failed", "message": "invalid sim broker receipt"}
        receipt.setdefault("order_id", order_id)
        receipt["capital_layer"] = capital_layer
        receipt["account_type"] = account_type
        execution_audit = _record_audit(
            deps,
            "execution",
            symbol,
            parent_audit_id=str(meta.get("risk_audit_id", "")),
            payload={"order": order, "sim_broker": receipt, "capital_layer": capital_layer, "account_type": account_type},
            metadata={"date": date, "account": account, "account_type": account_type},
            capital_layer=capital_layer,
        )
        audits.append(execution_audit)
        risk = {"approved": True, "adjusted_weight": position.get("weight"), "adjustments": [], "reasons": [], "capital_layer": capital_layer, "account_type": account_type}
        card = _build_signal_card(
            market=market,
            symbol=symbol,
            account=account,
            date=date,
            order=order,
            risk=risk,
            trade=receipt,
            audit_id=execution_audit["audit_id"],
            order_id=order_id,
            order_id_prefix="SIM-",
            capital_layer=capital_layer,
            account_type=account_type,
            direct_execution=True,
        )
        signal_result = _safe_stage(
            "signals.sim_execution",
            errors,
            lambda card=card, receipt=receipt: _write_execution_signal(card, receipt, signals_dir),
            default={"status": "degraded", "recorded": False},
            capital_layer=capital_layer,
        )
        stage_calls.append("signals.sim_execution")
        email_notification = {"status": "skipped", "reason": "sim order not filled", "template": "trade_receipt"}
        if signal_result.get("status") == "filled":
            email_data = _trade_receipt_email_data(
                market=market,
                symbol=symbol,
                date=date,
                account=account,
                order=order,
                receipt=receipt,
                card=card,
            )
            email_notification = _safe_stage(
                "notify.trade_receipt",
                errors,
                lambda data=email_data: _send_template_email_now(
                    deps.send_email,
                    "trade_receipt",
                    data,
                    subject=f"Tradings 模拟盘成交回执 {symbol} {_date_iso(date)}",
                ),
                default={"status": "degraded", "template": "trade_receipt"},
                capital_layer=capital_layer,
            )
            stage_calls.append("notify.trade_receipt")
        result_audit = _record_audit(
            deps,
            "result",
            symbol,
            parent_audit_id=execution_audit["audit_id"],
            payload={"signal_result": signal_result, "email_notification": email_notification, "capital_layer": capital_layer, "account_type": account_type},
            metadata={"date": date, "account": account, "account_type": account_type},
            capital_layer=capital_layer,
        )
        audits.append(result_audit)
        records.append({"symbol": symbol, "order": order, "receipt": receipt, "signal_result": signal_result, "email_notification": email_notification})

    review = _safe_stage(
        "review.daily_review",
        errors,
        lambda: _run_review_for_layer(deps, date, session="close", capital_layer=capital_layer),
        default={"session": "close", "error": "degraded", "trade_date": date, "capital_layer": capital_layer},
        capital_layer=capital_layer,
    )
    stage_calls.append("review.daily_review")

    return {
        "market": market,
        "date": date,
        "capital_layer": capital_layer,
        "account_type": account_type,
        "account": account,
        "state": "degraded" if errors else "ok",
        "stage_calls": stage_calls,
        "universe_count": len(universe),
        "candidate_count": len(candidates),
        "order_count": len(orders_for_portfolio),
        "skipped_candidate_count": len(skipped_candidates),
        "skipped_candidates": skipped_candidates[:20],
        "filled_count": sum(1 for record in records if record["signal_result"].get("status") == "filled"),
        "failed_count": sum(1 for record in records if record["signal_result"].get("status") == "failed"),
        "pending_count": sum(1 for record in records if record["signal_result"].get("status") == "pending"),
        "portfolio": portfolio,
        "records": records,
        "audit_events": audits,
        "errors": errors,
        "review": review,
        "condition_lifecycle": condition_lifecycle,
        "generated_at": _now_iso(),
    }


def run_daily_review(
    market: str,
    date: str,
    lunch_or_close: str,
    *,
    deps: OrchestratorDeps | None = None,
) -> dict[str, Any]:
    """Run daily review for shadow trades and persist through review/data."""

    deps = deps or _default_deps()
    session = "lunch" if str(lunch_or_close).lower() in {"lunch", "midday", "day"} else "close"
    shadow_trades = _load_shadow_trades_for_date(date)
    try:
        result = deps.run_review(date, session=session)
    except Exception as exc:
        result = {"session": session, "trade_date": date, "error": str(exc)}
    if isinstance(result, dict):
        result.update({
            "market": market,
            "capital_layer": "shadow",
            "job": "orchestrator_daily_review",
            "generated_at": _now_iso(),
            "shared_shadow_trade_count": len(shadow_trades),
        })
    return result


__all__ = [
    "MarketAdapter",
    "OrchestratorDeps",
    "run_daily_review",
    "run_sim_loop",
    "run_shadow_loop",
]
