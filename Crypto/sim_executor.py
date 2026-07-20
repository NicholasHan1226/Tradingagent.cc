#!/usr/bin/env python3
"""Provider-neutral, simulation-only Crypto executor.

The executor consumes a validated market-evidence fixture/port.  It never
constructs a Binance client and never reads provider-specific payloads.  A
future TradingDatas adapter may translate ``GET /v1/catalog`` and
``POST /v1/query`` envelopes into this evidence object after a fresh handoff.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import math
from typing import Any

from shared.execution.sim_broker import SimResult
from shared.execution.sim_executor_registry import register_sim_executor
from shared.markets.safety import reject_real_execution_payload


PAPER_BROKER_CONTRACT = "tradingagent.crypto.paper_broker.v1"
SIM_AUTHORITY_ID = "crypto-shadow-sim-v1"
LOCAL_EVIDENCE_DATASET_PREFIXES = {
    "fixture": "fixture.",
    "mock": "mock.",
}
FROZEN_TRADINGDATAS_DATASET_IDS: frozenset[str] = frozenset()
READY_STATES = {"ready", "active", "success"}
PASS_STATES = {"fresh", "current", "ok", "pass", "passed", "good"}


def _decimal(value: Any, *, name: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(f"{name} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        qualifier = "positive " if positive else ""
        raise ValueError(f"{name} must be a {qualifier}finite decimal")
    return parsed


def _positive_schema_major(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("market_evidence.schema_major must be a positive integer")
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(
            "market_evidence.schema_major must be a positive integer"
        ) from exc
    if not math.isfinite(parsed) or not parsed.is_integer() or parsed <= 0:
        raise ValueError("market_evidence.schema_major must be a positive integer")
    return int(parsed)


def _extract_symbol(order: dict[str, Any]) -> str:
    symbol = str(
        order.get("symbol") or order.get("ts_code") or order.get("pair") or ""
    ).strip().upper()
    if not symbol:
        raise ValueError("order symbol is required")
    return symbol


def _metadata_state(value: Any) -> str:
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, dict):
        return str(value.get("state") or value.get("status") or "").strip().lower()
    return ""


def _aware_timestamp(value: Any, *, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError(f"{name} must be a timezone-aware ISO timestamp")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"{name} must be a timezone-aware ISO timestamp"
            ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware ISO timestamp")
    return parsed


def _lineage_has_content(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value) and all(
            str(key or "").strip() and _lineage_has_content(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return bool(value) and all(_lineage_has_content(item) for item in value)
    if isinstance(value, bool) or value is None:
        return False
    return bool(str(value or "").strip())


def _validated_market_evidence(
    config: dict[str, Any], *, symbol: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence = config.get("market_evidence")
    if not isinstance(evidence, dict):
        raise ValueError("provider-neutral market_evidence is required")
    transport = str(evidence.get("transport") or "").strip().lower()
    dataset_id = str(evidence.get("dataset_id") or "").strip()
    if not dataset_id:
        raise ValueError("market_evidence.dataset_id is required")
    if transport in LOCAL_EVIDENCE_DATASET_PREFIXES:
        expected_prefix = LOCAL_EVIDENCE_DATASET_PREFIXES[transport]
        if not dataset_id.startswith(expected_prefix):
            raise ValueError(
                f"{transport} market_evidence must use an explicit "
                f"{transport} dataset_id"
            )
    elif transport == "tradingdatas_v1":
        if dataset_id not in FROZEN_TRADINGDATAS_DATASET_IDS:
            raise ValueError(
                "TradingDatas dataset ID is not frozen for Crypto fills"
            )
    else:
        raise ValueError("market_evidence transport is not approved")
    _positive_schema_major(evidence.get("schema_major"))
    evidence_symbol = str(evidence.get("symbol") or "").strip().upper()
    if evidence_symbol != symbol:
        raise ValueError("market_evidence symbol does not match order")
    metadata = evidence.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("market_evidence.metadata is required")
    state = _metadata_state(metadata.get("state"))
    if state not in READY_STATES or metadata.get("degraded") is not False:
        raise ValueError("market_evidence is not ready or is degraded")
    if _metadata_state(metadata.get("freshness")) not in PASS_STATES:
        raise ValueError("market_evidence freshness is not acceptable")
    if _metadata_state(metadata.get("quality")) not in PASS_STATES:
        raise ValueError("market_evidence quality is not acceptable")
    if not str(metadata.get("receipt_id") or "").strip():
        raise ValueError("market_evidence.metadata.receipt_id is required")
    observed_at = _aware_timestamp(
        metadata.get("observed_at"), name="market_evidence.metadata.observed_at"
    )
    data_through = _aware_timestamp(
        metadata.get("data_through"), name="market_evidence.metadata.data_through"
    )
    if data_through > observed_at:
        raise ValueError(
            "market_evidence.metadata.data_through cannot exceed observed_at"
        )
    lineage = metadata.get("lineage")
    if not isinstance(lineage, (dict, list)) or not _lineage_has_content(lineage):
        raise ValueError("market_evidence.metadata.lineage is required")
    normalized_metadata = dict(metadata)
    normalized_metadata["observed_at"] = observed_at.isoformat(timespec="seconds")
    normalized_metadata["data_through"] = data_through.isoformat(timespec="seconds")
    return dict(evidence), normalized_metadata


def _filled_quantity_value(quantity: Decimal) -> int | float:
    integral = quantity.to_integral_value()
    return int(integral) if quantity == integral else float(quantity)


def crypto_sim_execute(
    order: dict[str, Any],
    account: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> SimResult:
    """Simulate one spot fill from authority-bound provider-neutral evidence."""

    reject_real_execution_payload(order, context="crypto_sim_execute.order")
    reject_real_execution_payload(account or {}, context="crypto_sim_execute.account")
    reject_real_execution_payload(config or {}, context="crypto_sim_execute.config")
    order = dict(order or {})
    account = dict(account or {})
    config = dict(config or {})
    symbol = _extract_symbol(order)
    side = str(order.get("side") or order.get("direction") or "").strip().lower()
    if side not in {"buy", "sell"}:
        raise ValueError("Crypto order side must be buy or sell")
    quantity = _decimal(
        order.get("quantity", order.get("qty", order.get("filled_qty"))),
        name="quantity",
        positive=True,
    )
    evidence, metadata = _validated_market_evidence(config, symbol=symbol)
    price = _decimal(evidence.get("price"), name="market_evidence.price", positive=True)
    rules = evidence.get("rules")
    if not isinstance(rules, dict):
        raise ValueError("market_evidence.rules is required")
    quantity_step = _decimal(
        rules.get("quantity_step"), name="rules.quantity_step", positive=True
    )
    min_quantity = _decimal(
        rules.get("min_quantity"), name="rules.min_quantity", positive=True
    )
    min_notional = _decimal(
        rules.get("min_notional"), name="rules.min_notional", positive=True
    )
    if quantity < min_quantity or quantity % quantity_step != 0:
        raise ValueError("Crypto quantity violates fixture/port market rules")
    notional = quantity * price
    if notional < min_notional:
        raise ValueError("Crypto order is below minimum notional")
    base_asset = str(rules.get("base_asset") or "").strip().upper()
    quote_asset = str(rules.get("quote_asset") or "").strip().upper()
    account_id = str(account.get("account_id") or account.get("account") or "").strip()
    balances = account.get("balances")
    if not account_id or not base_asset or not quote_asset or not isinstance(balances, dict):
        raise ValueError("Crypto simulated account identity and asset balances are required")
    fee_rate = _decimal(
        config.get("fee_rate", "0.001"), name="fee_rate", positive=False
    )
    if fee_rate < 0 or fee_rate > Decimal("0.1"):
        raise ValueError("fee_rate must be between 0 and 0.1")
    fee = notional * fee_rate
    if side == "buy":
        available = _decimal(
            balances.get(quote_asset, 0), name=f"balances.{quote_asset}"
        )
        required = notional + fee
    else:
        available = _decimal(
            balances.get(base_asset, 0), name=f"balances.{base_asset}"
        )
        required = quantity
    if available < required:
        raise ValueError("Crypto simulated account balance is insufficient")

    order_id = str(order.get("order_id") or f"SIM-CRYPTO-{symbol}")
    fee_value = round(float(fee), 8)
    return SimResult(
        status="filled",
        filled_qty=_filled_quantity_value(quantity),
        avg_price=float(price),
        fee=fee_value,
        message="Simulated Crypto spot fill from provider-neutral market evidence",
        capital_layer="simulated",
        account_type="simulated",
        order_id=order_id,
        market="crypto",
        raw_response={
            "symbol": symbol,
            "side": side,
            "price": float(price),
            "quantity": _filled_quantity_value(quantity),
            "notional": float(notional),
            "fee_rate": float(fee_rate),
            "account_id": account_id,
            "transport": evidence["transport"],
            "dataset_id": evidence["dataset_id"],
            "schema_major": _positive_schema_major(evidence.get("schema_major")),
            "receipt_id": metadata["receipt_id"],
            "observed_at": metadata["observed_at"],
            "data_through": metadata["data_through"],
            "source": "provider_neutral_market_evidence",
            "broker_contract": PAPER_BROKER_CONTRACT,
            "authority_id": SIM_AUTHORITY_ID,
        },
        broker_contract=PAPER_BROKER_CONTRACT,
        authority_id=SIM_AUTHORITY_ID,
    )


register_sim_executor(
    "crypto",
    crypto_sim_execute,
    simulation_contract=PAPER_BROKER_CONTRACT,
    authority_id=SIM_AUTHORITY_ID,
)


__all__ = ["crypto_sim_execute"]
