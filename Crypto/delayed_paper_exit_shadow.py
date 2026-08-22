"""Read-only exit-policy shadow for the Crypto delayed-paper account.

This projection deliberately does not change the current buy-only capital
generation.  It binds verified delayed-paper completion evidence to the latest
validated capital snapshot and records what a conservative full-position exit
would have done.  The result is counterfactual research evidence only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any, Mapping

from Crypto.delayed_paper_learning import (
    CryptoDelayedPaperLearningError,
    _verified_sources,
)
from Crypto.delayed_paper_ledger import (
    CryptoDelayedPaperLedgerError,
    CryptoDelayedPaperObservationStore,
    _canonical_value,
    _ensure_directory,
    _sha256,
    _write_immutable_json,
)
from Crypto.fixture_sim.ledger import CryptoCapitalLedger, CryptoLedgerError


EXIT_SHADOW_CONTRACT = "tradingagent.crypto.delayed_paper_exit_shadow.v1"
EXIT_SHADOW_POLICY_ID = "crypto-delayed-paper-exit-shadow-v1"
TAKE_PROFIT_RETURN = Decimal("0.03")
STOP_LOSS_RETURN = Decimal("-0.02")
# Mirrors Crypto.round_trip_capital._exit_reason: with a negative 1h regime,
# any strictly negative 15m decision return ends the round trip.
MOMENTUM_EXIT_RETURN = Decimal("0")
MAX_HOLD_SECONDS = 24 * 60 * 60
EXIT_SLIPPAGE_BPS = Decimal("2")
EXIT_FEE_RATE = Decimal("0.001")
MONEY_QUANTUM = Decimal("0.00000001")


class CryptoDelayedPaperExitShadowError(RuntimeError):
    """Stable fail-closed error for exit-shadow source or projection faults."""


def _non_authority_fields() -> dict[str, Any]:
    return {
        "authority": "none",
        "counterfactual_only": True,
        "execution_eligible": False,
        "execution_authority": False,
        "durable_execution_receipt": False,
        "production_eligible": False,
        "real_trading_enabled": False,
        "network_used": False,
        "model_network_used": False,
        "testnet_used": False,
        "live_broker_used": False,
        "promotion_authorized": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "manual_review_required": True,
        "outbox_id": None,
        "capital_commit_id": None,
    }


def _decimal(value: Any, *, field: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise CryptoDelayedPaperExitShadowError(f"exit_shadow_{field}_invalid")
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise CryptoDelayedPaperExitShadowError(f"exit_shadow_{field}_invalid") from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise CryptoDelayedPaperExitShadowError(f"exit_shadow_{field}_invalid")
    return parsed


def _utc(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CryptoDelayedPaperExitShadowError(f"exit_shadow_{field}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CryptoDelayedPaperExitShadowError(f"exit_shadow_{field}_invalid") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timezone.utc.utcoffset(parsed)
    ):
        raise CryptoDelayedPaperExitShadowError(f"exit_shadow_{field}_invalid")
    return parsed.astimezone(timezone.utc)


def _money(value: Decimal, *, rounding: str = ROUND_DOWN) -> str:
    return format(value.quantize(MONEY_QUANTUM, rounding=rounding), "f")


def _latest_capital_snapshot(
    *,
    output_root: Path,
    trusted: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], int, str]:
    candidates: list[dict[str, Any]] = []
    for item in trusted.values():
        bundle = item.get("bundle")
        capital = bundle.get("capital") if isinstance(bundle, Mapping) else None
        final = capital.get("final") if isinstance(capital, Mapping) else None
        if isinstance(final, Mapping):
            candidates.append(dict(final))
    if not candidates:
        raise CryptoDelayedPaperExitShadowError("exit_shadow_capital_snapshot_missing")
    try:
        snapshot = max(candidates, key=lambda row: int(row.get("head_sequence", -1)))
        sequence = int(snapshot.get("head_sequence"))
    except (TypeError, ValueError) as exc:
        raise CryptoDelayedPaperExitShadowError(
            "exit_shadow_capital_snapshot_invalid"
        ) from exc
    checksum = snapshot.get("head_checksum")
    positions = snapshot.get("positions")
    orders = snapshot.get("orders")
    if (
        snapshot.get("account_type") != "simulated"
        or snapshot.get("real_trading_enabled") is not False
        or snapshot.get("execution_authority") is not False
        or snapshot.get("balanced") is not True
        or not isinstance(checksum, str)
        or len(checksum) != 64
        or not isinstance(positions, Mapping)
        or not isinstance(orders, Mapping)
    ):
        raise CryptoDelayedPaperExitShadowError("exit_shadow_capital_snapshot_invalid")
    try:
        actual_sequence, actual_checksum = CryptoCapitalLedger(
            output_root / "capital"
        ).head()
    except (OSError, CryptoLedgerError) as exc:
        raise CryptoDelayedPaperExitShadowError(
            "exit_shadow_capital_ledger_invalid"
        ) from exc
    if (sequence, checksum) != (actual_sequence, actual_checksum):
        raise CryptoDelayedPaperExitShadowError("exit_shadow_capital_head_mismatch")
    return snapshot, actual_sequence, actual_checksum


def _entry_order(
    *,
    symbol: str,
    quantity: Decimal,
    orders: Mapping[str, Any],
) -> dict[str, Any]:
    matches = [
        dict(order)
        for order in orders.values()
        if isinstance(order, Mapping)
        and order.get("symbol") == symbol
        and order.get("status") == "fixture_simulated"
    ]
    if len(matches) != 1:
        raise CryptoDelayedPaperExitShadowError("exit_shadow_entry_order_invalid")
    order = matches[0]
    if (
        _decimal(order.get("quantity"), field="entry_quantity", positive=True)
        != quantity
    ):
        raise CryptoDelayedPaperExitShadowError("exit_shadow_entry_quantity_mismatch")
    receipt_id = order.get("receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id.startswith(
        "crypto-paper-receipt-"
    ):
        raise CryptoDelayedPaperExitShadowError("exit_shadow_entry_receipt_invalid")
    return order


def _trigger(
    *,
    raw_return: Decimal,
    holding_seconds: int,
    decision: Mapping[str, Any],
) -> tuple[str, str]:
    if raw_return <= STOP_LOSS_RETURN:
        return "shadow_exit", "stop_loss_threshold_reached"
    if raw_return >= TAKE_PROFIT_RETURN:
        return "shadow_exit", "take_profit_threshold_reached"
    if holding_seconds >= MAX_HOLD_SECONDS:
        return "shadow_exit", "max_holding_period_reached"
    regime_return = _decimal(decision.get("regime_return"), field="regime_return")
    decision_return = _decimal(
        decision.get("decision_return"),
        field="decision_return",
    )
    if (
        decision.get("action") == "observe"
        and regime_return < 0
        and decision_return < MOMENTUM_EXIT_RETURN
    ):
        return "shadow_exit", "momentum_reversal_observed"
    return "hold", "exit_threshold_not_met"


def _symbol_projection(
    *,
    symbol: str,
    item: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    positions = snapshot["positions"]
    quantity_raw = positions.get(symbol)
    if quantity_raw is None:
        return {
            "symbol": symbol,
            "action": "no_position",
            "reason_code": "no_open_position",
            "holding_seconds": None,
            "quantity": "0",
            "entry_price": None,
            "entry_notional": None,
            "entry_fee": None,
            "shadow_exit_price": None,
            "shadow_exit_notional": None,
            "shadow_exit_fee": None,
            "shadow_net_pnl": None,
            "shadow_return_on_entry_cost": None,
            "source_entry_receipt_id": None,
            **_non_authority_fields(),
        }
    quantity = _decimal(quantity_raw, field="position_quantity", positive=True)
    order = _entry_order(
        symbol=symbol,
        quantity=quantity,
        orders=snapshot["orders"],
    )
    bundle = item.get("bundle")
    qualification = (
        bundle.get("evidence_qualification") if isinstance(bundle, Mapping) else None
    )
    quote = (
        qualification.get("next_executable_quote")
        if isinstance(qualification, Mapping)
        else None
    )
    decision = bundle.get("decision") if isinstance(bundle, Mapping) else None
    if (
        not isinstance(quote, Mapping)
        or quote.get("symbol") != symbol
        or not isinstance(decision, Mapping)
        or decision.get("symbol") != symbol
    ):
        raise CryptoDelayedPaperExitShadowError("exit_shadow_quote_invalid")
    bid = _decimal(quote.get("bid"), field="quote_bid", positive=True)
    entry_price = _decimal(order.get("price"), field="entry_price", positive=True)
    entry_notional = _decimal(
        order.get("notional"),
        field="entry_notional",
        positive=True,
    )
    entry_fee = _decimal(order.get("fee"), field="entry_fee")
    entry_time = _utc(order.get("filled_at"), field="entry_time")
    quote_time = _utc(quote.get("observed_at"), field="quote_time")
    holding_seconds = int((quote_time - entry_time).total_seconds())
    if holding_seconds < 0:
        raise CryptoDelayedPaperExitShadowError("exit_shadow_time_order_invalid")
    raw_return = bid / entry_price - Decimal("1")
    action, reason = _trigger(
        raw_return=raw_return,
        holding_seconds=holding_seconds,
        decision=decision,
    )
    shadow_price = (
        bid * (Decimal("1") - EXIT_SLIPPAGE_BPS / Decimal("10000"))
    ).quantize(MONEY_QUANTUM, rounding=ROUND_DOWN)
    exit_notional = (quantity * shadow_price).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_DOWN,
    )
    exit_fee = (exit_notional * EXIT_FEE_RATE).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_UP,
    )
    net_pnl = exit_notional - exit_fee - entry_notional - entry_fee
    entry_cost = entry_notional + entry_fee
    return {
        "symbol": symbol,
        "action": action,
        "reason_code": reason,
        "holding_seconds": holding_seconds,
        "quantity": _money(quantity),
        "entry_time": entry_time.isoformat().replace("+00:00", "Z"),
        "entry_price": _money(entry_price),
        "entry_notional": _money(entry_notional),
        "entry_fee": _money(entry_fee),
        "quote_time": quote_time.isoformat().replace("+00:00", "Z"),
        "quote_bid": _money(bid),
        "raw_mark_return": _money(raw_return),
        "shadow_exit_price": _money(shadow_price),
        "shadow_exit_notional": _money(exit_notional),
        "shadow_exit_fee": _money(exit_fee),
        "shadow_net_pnl": _money(net_pnl),
        "shadow_return_on_entry_cost": _money(net_pnl / entry_cost),
        "source_entry_receipt_id": order["receipt_id"],
        "source_market_evidence_sha256": qualification.get("market_evidence_sha256"),
        "source_business_bundle_sha256": bundle.get("business_bundle_sha256"),
        **_non_authority_fields(),
    }


def project_crypto_delayed_paper_exit_shadow(
    *,
    output_root: Path | str,
) -> dict[str, Any]:
    """Project the latest completed observation into immutable exit evidence."""

    root = Path(output_root)
    try:
        checkpoint = CryptoDelayedPaperObservationStore(root).runtime_checkpoint()
        observation_id = checkpoint.get("latest_observation_id")
        if observation_id is None:
            state_path = root / "delayed_paper" / "observation_state.json"
            from Crypto.delayed_paper_ledger import _read_json

            observation_id = _read_json(state_path).get("latest_observation_id")
        if not isinstance(observation_id, str) or checkpoint.get("pending") is not None:
            raise CryptoDelayedPaperExitShadowError(
                "exit_shadow_completed_observation_missing"
            )
        observation, completion, trusted = _verified_sources(
            root=root,
            observation_id=observation_id,
            supplied_symbols=None,
        )
        snapshot, head_sequence, head_checksum = _latest_capital_snapshot(
            output_root=root,
            trusted=trusted,
        )
        symbols = {
            symbol: _symbol_projection(
                symbol=symbol,
                item=item,
                snapshot=snapshot,
            )
            for symbol, item in sorted(trusted.items())
        }
    except CryptoDelayedPaperExitShadowError:
        raise
    except (
        OSError,
        CryptoDelayedPaperLearningError,
        CryptoDelayedPaperLedgerError,
        CryptoLedgerError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise CryptoDelayedPaperExitShadowError("exit_shadow_source_invalid") from exc
    policy = {
        "policy_id": EXIT_SHADOW_POLICY_ID,
        "take_profit_return": format(TAKE_PROFIT_RETURN, "f"),
        "stop_loss_return": format(STOP_LOSS_RETURN, "f"),
        "momentum_exit_return": format(MOMENTUM_EXIT_RETURN, "f"),
        "max_hold_seconds": MAX_HOLD_SECONDS,
        "exit_slippage_bps": format(EXIT_SLIPPAGE_BPS, "f"),
        "exit_fee_rate": format(EXIT_FEE_RATE, "f"),
        "full_position_only": True,
    }
    artifact: dict[str, Any] = {
        "contract": EXIT_SHADOW_CONTRACT,
        "status": "projected",
        "market": "crypto",
        "mode": "detached_exit_shadow",
        "observation_id": observation_id,
        "market_slot": observation.get("market_slot"),
        "source_observation_content_sha256": observation.get(
            "observation_content_sha256"
        ),
        "source_completion_sha256": completion.get("completion_sha256"),
        "source_capital_head_sequence": head_sequence,
        "source_capital_head_checksum": head_checksum,
        "policy": policy,
        "policy_sha256": _sha256(policy),
        "position_count": len(snapshot["positions"]),
        "shadow_exit_count": sum(
            item["action"] == "shadow_exit" for item in symbols.values()
        ),
        "symbols": symbols,
        **_non_authority_fields(),
    }
    artifact["projection_sha256"] = _sha256(artifact)
    try:
        evolution = root / "evolution"
        exit_shadow = evolution / "exit_shadow"
        _ensure_directory(evolution)
        _ensure_directory(exit_shadow)
        stored = _write_immutable_json(
            exit_shadow / f"{observation_id}.json",
            _canonical_value(artifact),
        )
    except (OSError, CryptoDelayedPaperLedgerError) as exc:
        raise CryptoDelayedPaperExitShadowError(
            "exit_shadow_projection_write_failed"
        ) from exc
    return stored
