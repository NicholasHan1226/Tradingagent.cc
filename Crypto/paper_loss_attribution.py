"""Read-only, reconciled P&L attribution for one round-trip Paper epoch.

Quote-midpoint movement, execution-price impact and fees are disjoint.
Execution impact includes the simulated spread/slippage and notional rounding;
it is not a measured exchange slippage estimate. No marks are refreshed here.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_UP
import fcntl
import json
from pathlib import Path
from typing import Any, Mapping

from Crypto.fixture_sim.contracts import _assert_simulation_only
from Crypto.research_accounting import number
from Crypto.round_trip_capital import (
    MONEY_QUANTUM, ROUND_TRIP_CAPITAL_POLICY, RoundTripCapitalLedger,
    _canonical_value, _money, _sha256,
)

CONTRACT = "tradingagent.crypto.paper_loss_attribution.v1"
ZERO = Decimal(0)


def _time(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.utcoffset() != timedelta(0):
        raise ValueError("attribution_utc_required")
    return parsed


def attribute_snapshot(snapshot: Mapping[str, Any], *, mark_times: Mapping[str, str],
                       as_of: datetime) -> dict[str, Any]:
    """Independently reconcile a validated same-epoch snapshot, never mark live."""
    _assert_simulation_only()
    now = _time(as_of)
    policy = ROUND_TRIP_CAPITAL_POLICY
    if (snapshot.get("account_id") != policy.account_id
            or snapshot.get("authority_id") != policy.authority_id
            or snapshot.get("generation") != policy.generation
            or snapshot.get("currency") != policy.currency
            or snapshot.get("aggregate_with_prior_generations") is not False
            or any(snapshot.get(key) is not False for key in (
                "real_trading_enabled", "execution_authority", "production_eligible"))):
        raise ValueError("attribution_account_boundary_invalid")
    initial = number(snapshot["initial_cash"], positive=True)
    if initial != policy.initial_cash:
        raise ValueError("attribution_opening_mismatch")
    orders = list(snapshot["orders"].values())
    orders.sort(key=lambda row: (_time(row["execution_slot"]), row["symbol"], row["intent_id"]))
    groups: dict[str, dict[str, Any]] = {}
    seen, receipt_ids, latest_by_symbol = set(), set(), {}
    for order in orders:
        symbol, side = order["symbol"], order["side"]
        at = _time(order["execution_slot"])
        if (side not in {"buy", "sell"} or symbol not in {"BTCUSDT", "ETHUSDT"}
                or at > now or order["intent_id"] in seen or order["receipt_id"] in receipt_ids):
            raise ValueError("attribution_order_identity_invalid")
        seen.add(order["intent_id"])
        receipt_ids.add(order["receipt_id"])
        if symbol in latest_by_symbol and at <= latest_by_symbol[symbol]:
            raise ValueError("attribution_order_time_invalid")
        latest_by_symbol[symbol] = at
        group = groups.setdefault(symbol, {
            "symbol": symbol, "buy_notional": ZERO, "sell_notional": ZERO,
            "reference_buys": ZERO, "reference_sells": ZERO, "fees": ZERO,
            "model_spread_cost": ZERO, "model_slippage_and_rounding_cost": ZERO,
            "quantity": ZERO, "entry_notional": ZERO, "entry_fee": ZERO,
            "realized_net": ZERO, "buy_count": 0, "sell_count": 0,
            "rejected_count": 0, "closed_position_count": 0,
            "profitable_sell_legs": 0, "losing_sell_legs": 0,
            "holding_hours_on_sell": [], "first_trade": None, "last_trade": None,
        })
        qty, notional, fee = (number(order[key]) for key in ("filled_quantity", "notional", "fee"))
        if min(qty, notional, fee) < 0:
            raise ValueError("attribution_negative_fill")
        status = order["status"]
        if status == "fixture_rejected":
            if qty or notional or fee:
                raise ValueError("attribution_rejected_fill_nonzero")
            group["rejected_count"] += 1
            continue
        if status not in {"fixture_simulated", "fixture_partially_simulated"} or qty <= 0:
            raise ValueError("attribution_fill_status_invalid")
        price = number(order["average_price"], positive=True)
        # The writer's reference_price is ALREADY the simulated fill price.
        # Use the recorded quote midpoint, not that misleading field, to avoid
        # reporting zero modeled execution cost by construction.
        bid, ask = (number(order[key], positive=True) for key in ("quote_bid", "quote_ask"))
        if bid > ask:
            raise ValueError("attribution_quote_crossed")
        reference = (bid + ask) / 2
        side_quote = ask if side == "buy" else bid
        sign = Decimal(1) if side == "buy" else Decimal(-1)
        group["model_spread_cost"] += sign * qty * (side_quote - reference)
        group["model_slippage_and_rounding_cost"] += sign * (notional - qty * side_quote)
        if abs(notional - qty * price) > MONEY_QUANTUM:
            raise ValueError("attribution_notional_mismatch")
        if abs(fee - notional * number(order["fee_rate"])) > MONEY_QUANTUM:
            raise ValueError("attribution_fee_mismatch")
        group["fees"] += fee
        group[f"{side}_count"] += 1
        group["first_trade"] = group["first_trade"] or at
        group["last_trade"] = at
        if side == "buy":
            if group["quantity"]:
                raise ValueError("attribution_duplicate_open")
            group["quantity"], group["entry_notional"], group["entry_fee"] = qty, notional, fee
            group["entry_time"] = at
            group["buy_notional"] += notional
            group["reference_buys"] += qty * reference
        else:
            if qty > group["quantity"]:
                raise ValueError("attribution_sell_without_inventory")
            fraction = qty / group["quantity"]
            basis = _money(group["entry_notional"] * fraction)
            entry_fee = _money(group["entry_fee"] * fraction, rounding=ROUND_UP)
            realized = notional - fee - basis - entry_fee
            group["realized_net"] += realized
            group["profitable_sell_legs"] += int(realized > 0)
            group["losing_sell_legs"] += int(realized < 0)
            group["holding_hours_on_sell"].append(Decimal(str((at - group["entry_time"]).total_seconds())) / 3600)
            group["quantity"] -= qty
            group["entry_notional"] -= basis
            group["entry_fee"] -= entry_fee
            group["sell_notional"] += notional
            group["reference_sells"] += qty * reference
            group["closed_position_count"] += int(group["quantity"] == 0)
    for symbol in snapshot["positions"]:
        if symbol not in groups:
            raise ValueError("attribution_position_without_orders")
    valuation_times = []
    for symbol, group in groups.items():
        quantity = group["quantity"]
        position = snapshot["positions"].get(symbol)
        if quantity:
            if position is None or number(position["quantity"]) != quantity:
                raise ValueError("attribution_position_mismatch")
            if (number(position["entry_notional"]) != group["entry_notional"]
                    or number(position["entry_fee"]) != group["entry_fee"]):
                raise ValueError("attribution_position_basis_mismatch")
            mark = number(snapshot["marks"][symbol], positive=True)
            mark_at = _time(mark_times[symbol])
            if mark_at > now or mark_at < group["last_trade"]:
                raise ValueError("attribution_mark_time_invalid")
            valuation_times.append(mark_at)
            group["mark_at"] = mark_at
            group["open_holding_hours"] = Decimal(str((mark_at - group["entry_time"]).total_seconds())) / 3600
            group["position_value"] = quantity * mark
        else:
            if position is not None:
                raise ValueError("attribution_unexpected_position")
            group["position_value"] = ZERO
            group["mark_at"] = None
            group["open_holding_hours"] = None
        group["price_movement_at_quote_mid"] = group["reference_sells"] - group["reference_buys"] + group["position_value"]
        group["execution_price_impact_cost"] = group["buy_notional"] - group["reference_buys"] + group["reference_sells"] - group["sell_notional"]
        group["net_pnl"] = group["price_movement_at_quote_mid"] - group["execution_price_impact_cost"] - group["fees"]
        group["unrealized_net_including_entry_fee"] = group["position_value"] - group["entry_notional"] - group["entry_fee"] if quantity else ZERO
        group["net_contribution_over_initial_cash"] = group["net_pnl"] / initial
        group["turnover_over_initial_cash"] = (group["buy_notional"] + group["sell_notional"]) / initial
        group.pop("entry_time", None)
    def total(key: str) -> Decimal:
        return sum((group[key] for group in groups.values()), ZERO)
    equity, cash = number(snapshot["equity"]), number(snapshot["cash"])
    residuals = {
        "cash": cash - (initial + total("sell_notional") - total("buy_notional") - total("fees")),
        "equity": equity - cash - total("position_value"),
        "fees": number(snapshot["fees"]) - total("fees"),
        "realized": number(snapshot["realized_pnl"]) - total("realized_net"),
        "net_pnl": equity - initial - total("net_pnl"),
        "realized_plus_unrealized": equity - initial - total("realized_net") - total("unrealized_net_including_entry_fee"),
    }
    # Writer rounds monetary snapshots at 1e-8; never hide a material residual.
    tolerance = MONEY_QUANTUM * 2
    if any(abs(value) > tolerance for value in residuals.values()):
        raise ValueError("attribution_reconciliation_failed")
    times = [_time(value) for value in mark_times.values()]
    if any(value > now for value in times):
        raise ValueError("attribution_future_source")
    valuation_at = min(valuation_times) if valuation_times else (max(times) if times else None)
    report = {
        "contract": CONTRACT, "authority": "none", "research_only": True,
        "read_only": True, "real_trading_enabled": False, "promotion_authorized": False,
        "current_account_pnl_claim": False, "checked_at": now,
        "source_snapshot_at": valuation_at,
        "source_age_seconds": int((now - valuation_at).total_seconds()) if valuation_at else None,
        "freshness": "unknown" if valuation_at is None else "fresh_snapshot" if now - valuation_at <= timedelta(minutes=30) else "dated_snapshot",
        "account_id": policy.account_id, "generation": policy.generation,
        "initial_cash": initial, "equity": equity, "net_pnl": equity - initial,
        "net_return": equity / initial - 1, "realized_net": total("realized_net"),
        "unrealized_net_including_entry_fee": total("unrealized_net_including_entry_fee"),
        "price_movement_at_quote_mid": total("price_movement_at_quote_mid"),
        "price_pnl_after_execution_before_fees": total("price_movement_at_quote_mid") - total("execution_price_impact_cost"),
        "model_spread_cost": total("model_spread_cost"),
        "model_slippage_and_rounding_cost": total("model_slippage_and_rounding_cost"),
        "execution_price_impact_cost": total("execution_price_impact_cost"), "fees": total("fees"),
        "symbol_attribution": sorted(groups.values(), key=lambda row: row["net_pnl"]),
        "reconciliation_residuals": residuals, "reconciliation_tolerance": tolerance,
        "snapshot_sha256": _sha256(snapshot), "orders_sha256": _sha256(snapshot["orders"]),
        "limitations": ["single_epoch_since_opening_not_aggregated", "source_marks_not_refreshed",
            "execution_impact_combines_model_spread_slippage_and_rounding",
            "sell_leg_counts_not_independent_round_trip_win_rate", "descriptive_attribution_not_causal_alpha"],
    }
    return _canonical_value(report)


def read_attribution(capital_root: Path, *, as_of: datetime | None = None) -> dict[str, Any]:
    """Read an already validated runtime snapshot under its existing shared lock."""
    _assert_simulation_only()
    now = as_of or datetime.now(timezone.utc)
    ledger = RoundTripCapitalLedger(capital_root)
    ledger._assert_safe_paths()
    with ledger.lock_path.open("r", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        try:
            summary = ledger._runtime_state_read_only()
            raw = json.loads(ledger.runtime_state_path.read_text(encoding="utf-8"))
            if raw["sequence"] != summary["head_sequence"] or raw["checksum"] != summary["head_checksum"]:
                raise ValueError("attribution_snapshot_changed")
            state = ledger._writer_state_restore(raw["writer_state"])
            snapshot = ledger._snapshot(state)
            result = attribute_snapshot(snapshot, mark_times=raw["writer_state"]["last_slot_by_symbol"], as_of=now)
            result["source"] = {"kind": "verified_runtime_snapshot", "head_sequence": summary["head_sequence"],
                "head_checksum": summary["head_checksum"], "runtime_state_sha256": raw["state_sha256"],
                "root": str(capital_root), "full_ledger_replayed_this_read": False}
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    result["report_sha256"] = _sha256(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capital-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(read_attribution(args.capital_root), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
