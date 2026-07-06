#!/usr/bin/env python3
"""Double-entry simulated ledger with FIFO tax lots and audit exports."""

from __future__ import annotations

import csv
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.execution.real_trading_gate import validate_real_trading_enabled
from shared.execution.sim_engine import SimFill, SimOrder


DEFAULT_LEDGER_ROOT = Path(__file__).resolve().parent.parent / "logs" / "sim_ledger"
AUDIT_SCOPE_KEYS = (
    "exclude_from_dashboard",
    "dashboard_excluded",
    "excluded_from_dashboard",
    "run_context",
    "run_mode",
    "run_source",
    "sample_type",
)
PROVENANCE_KEYS = (
    "strategy_name",
    "style_name",
    "signal_source",
    "reason",
    "conviction",
    "score",
    "belief_score",
    "model_probability",
    "market_probability",
    "edge",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _add_cny_fields(payload: dict[str, Any]) -> None:
    fx_to_cny = _safe_float(payload.get("fx_to_cny"), 0.0)
    if fx_to_cny <= 0:
        return
    for key in (
        "capital_base",
        "cash",
        "equity",
        "total_equity",
        "market_value",
        "pnl",
        "total_pnl",
        "realized_pnl",
        "unrealized_pnl",
        "benchmark_pnl",
        "pnl_vs_benchmark",
    ):
        value = payload.get(key)
        if value is None:
            continue
        payload[f"{key}_cny"] = round(_safe_float(value) * fx_to_cny, 8)


def _copy_audit_scope_fields(target: dict[str, Any], *sources: dict[str, Any]) -> None:
    for source in sources:
        for key in AUDIT_SCOPE_KEYS:
            value = source.get(key)
            if value not in (None, ""):
                target[key] = value


def _copy_provenance_fields(target: dict[str, Any], *sources: dict[str, Any]) -> None:
    for source in sources:
        for key in PROVENANCE_KEYS:
            value = source.get(key)
            if value not in (None, ""):
                target[key] = value


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@dataclass
class LedgerLeg:
    account: str
    debit: float = 0.0
    credit: float = 0.0


@dataclass
class LedgerEntry:
    event_type: str
    order_id: str = ""
    fill_id: str = ""
    symbol: str = ""
    timestamp: str = field(default_factory=_now_iso)
    entry_id: str = field(default_factory=lambda: f"LED-{uuid.uuid4().hex[:12]}")
    legs: list[LedgerLeg] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        debits = round(sum(leg.debit for leg in self.legs), 8)
        credits = round(sum(leg.credit for leg in self.legs), 8)
        if debits != credits:
            raise ValueError(f"double-entry imbalance: debits={debits} credits={credits}")

    def as_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "entry_id": self.entry_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "order_id": self.order_id,
            "fill_id": self.fill_id,
            "symbol": self.symbol,
            "legs": [asdict(leg) for leg in self.legs],
            "metadata": self.metadata,
            "capital_layer": "simulated",
            "real_execution": False,
        }


class SimLedger:
    """Append-only simulated accounting ledger."""

    def __init__(self, ledger_root: Path | str | None = None, *, starting_cash: float = 0.0) -> None:
        self.root = Path(ledger_root) if ledger_root is not None else DEFAULT_LEDGER_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self.trade_journal_path = self.root / "trade_journal.jsonl"
        self.cash_ledger_path = self.root / "cash_ledger.jsonl"
        self.double_entry_path = self.root / "double_entry.jsonl"
        self.mtm_path = self.root / "daily_mark_to_market.jsonl"
        self.positions_path = self.root / "positions.json"
        self.tax_lots_path = self.root / "tax_lots.json"
        if starting_cash:
            state = self._load_state()
            if not state.get("_initialized"):
                self.record_deposit(starting_cash, note="initial simulated capital")
                state = self._load_state()
                state["_initialized"] = True
                self._save_state(state)

    def record_deposit(self, amount: float, *, timestamp: str | None = None, note: str = "") -> dict[str, Any]:
        amount = round(_safe_float(amount), 8)
        if amount <= 0:
            raise ValueError("deposit amount must be positive")
        entry = LedgerEntry(
            event_type="deposit",
            timestamp=timestamp or _now_iso(),
            legs=[
                LedgerLeg("cash", debit=amount),
                LedgerLeg("external_capital", credit=amount),
            ],
            metadata={"note": note},
        )
        self._append_entry(entry)
        cash_event = {"timestamp": entry.timestamp, "event_type": "deposit", "amount": amount, "note": note}
        _append_jsonl(self.cash_ledger_path, cash_event)
        state = self._load_state()
        state["cash"] = round(_safe_float(state.get("cash")) + amount, 8)
        self._save_state(state)
        return {**cash_event, "entry_id": entry.entry_id}

    def record_withdrawal(self, amount: float, *, timestamp: str | None = None, note: str = "") -> dict[str, Any]:
        amount = round(_safe_float(amount), 8)
        if amount <= 0:
            raise ValueError("withdrawal amount must be positive")
        entry = LedgerEntry(
            event_type="withdrawal",
            timestamp=timestamp or _now_iso(),
            legs=[
                LedgerLeg("external_capital", debit=amount),
                LedgerLeg("cash", credit=amount),
            ],
            metadata={"note": note},
        )
        self._append_entry(entry)
        cash_event = {"timestamp": entry.timestamp, "event_type": "withdrawal", "amount": -amount, "note": note}
        _append_jsonl(self.cash_ledger_path, cash_event)
        state = self._load_state()
        state["cash"] = round(_safe_float(state.get("cash")) - amount, 8)
        self._save_state(state)
        return {**cash_event, "entry_id": entry.entry_id}

    def record_fill(
        self,
        order: SimOrder | dict[str, Any],
        fill: SimFill | dict[str, Any],
        *,
        fees: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        order_payload = asdict(order) if isinstance(order, SimOrder) else dict(order)
        fill_payload = asdict(fill) if isinstance(fill, SimFill) else dict(fill)
        side = str(order_payload.get("side") or "").lower()
        symbol = str(order_payload.get("symbol") or "")
        qty = round(_safe_float(fill_payload.get("fill_qty")), 8)
        price = round(_safe_float(fill_payload.get("fill_price")), 8)
        if side not in {"buy", "sell"}:
            raise ValueError(f"unsupported side: {side}")
        if not symbol or qty <= 0 or price <= 0:
            raise ValueError("fill requires symbol, positive qty and positive price")
        fee_payload = dict(fees or {})
        fee_total = round(_safe_float(fee_payload.get("total")), 8)
        notional = round(qty * price, 8)
        stamp_duty = round(_safe_float(fee_payload.get("stamp_duty")), 8)
        timestamp = timestamp or str(fill_payload.get("fill_time") or _now_iso())

        state = self._load_state()
        positions = state.setdefault("positions", {})
        lots = state.setdefault("tax_lots", {})
        realized_pnl = 0.0
        cost_basis_closed = 0.0
        if side == "buy":
            self._apply_buy(positions, lots, symbol, qty, price, fee_total, timestamp, order_payload, stamp_duty)
            legs = [
                LedgerLeg("inventory", debit=notional),
                LedgerLeg("fees_expense", debit=fee_total),
                LedgerLeg("cash", credit=round(notional + fee_total, 8)),
            ]
            cash_delta = round(-(notional + fee_total), 8)
        else:
            realized_pnl, cost_basis_closed = self._apply_sell(
                positions, lots, symbol, qty, price, fee_total, timestamp, order_payload, stamp_duty
            )
            gross_before_fees = round(realized_pnl + fee_total, 8)
            legs = [
                LedgerLeg("cash", debit=round(notional - fee_total, 8)),
                LedgerLeg("fees_expense", debit=fee_total),
            ]
            if gross_before_fees >= 0:
                legs.extend([
                    LedgerLeg("inventory", credit=cost_basis_closed),
                    LedgerLeg("trading_gain", credit=gross_before_fees),
                ])
            else:
                legs.extend([
                    LedgerLeg("trading_loss", debit=abs(gross_before_fees)),
                    LedgerLeg("inventory", credit=cost_basis_closed),
                ])
            cash_delta = round(notional - fee_total, 8)

        state["cash"] = round(_safe_float(state.get("cash")) + cash_delta, 8)
        self._save_state(state)

        entry = LedgerEntry(
            event_type=f"trade_{side}",
            order_id=str(order_payload.get("order_id") or fill_payload.get("order_id") or ""),
            fill_id=str(fill_payload.get("fill_id") or ""),
            symbol=symbol,
            timestamp=timestamp,
            legs=legs,
            metadata={
                "quantity": qty,
                "price": price,
                "notional": notional,
                "fees": fee_payload,
                "realized_pnl": round(realized_pnl, 8),
                "cost_basis_closed": round(cost_basis_closed, 8),
            },
        )
        _copy_audit_scope_fields(entry.metadata, order_payload, fill_payload)
        _copy_provenance_fields(entry.metadata, order_payload, fill_payload)
        self._append_entry(entry)
        journal = {
            "timestamp": timestamp,
            "order_id": entry.order_id,
            "fill_id": entry.fill_id,
            "symbol": symbol,
            "side": side,
            "fill_qty": qty,
            "fill_price": price,
            "notional": notional,
            "fees": fee_payload,
            "realized_pnl": round(realized_pnl, 8),
            "capital_layer": "simulated",
        }
        _copy_audit_scope_fields(journal, order_payload, fill_payload)
        _copy_provenance_fields(journal, order_payload, fill_payload)
        if order_payload.get("outcome") not in (None, ""):
            journal["outcome"] = str(order_payload.get("outcome")).lower()
        if order_payload.get("market_id") not in (None, ""):
            journal["market_id"] = str(order_payload.get("market_id"))
        _append_jsonl(self.trade_journal_path, journal)
        _append_jsonl(self.cash_ledger_path, {"timestamp": timestamp, "event_type": f"trade_{side}", "amount": cash_delta, "symbol": symbol})
        return {**journal, "entry_id": entry.entry_id}

    def daily_mark_to_market(
        self,
        prices: dict[str, float],
        *,
        date: str,
        benchmark_return: float = 0.0,
        target_return_pct: float = 0.0,
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self._load_state()
        positions = state.get("positions", {})
        market_value = 0.0
        unrealized = 0.0
        realized = 0.0
        missing_mark_count = 0
        open_position_count = 0
        position_rows: list[dict[str, Any]] = []
        for symbol, position in positions.items():
            qty = _safe_float(position.get("quantity"))
            if qty <= 1e-12:
                realized += _safe_float(position.get("realized_pnl"))
                continue
            avg = _safe_float(position.get("avg_cost"))
            mark = _safe_float(prices.get(symbol), 0.0)
            if mark <= 0:
                mark = avg
                missing_mark_count += 1
            realized += _safe_float(position.get("realized_pnl"))
            open_position_count += 1
            value = round(qty * mark, 8)
            pnl = round((mark - avg) * qty, 8)
            market_value += value
            unrealized += pnl
            position_rows.append({
                "symbol": symbol,
                "quantity": qty,
                "avg_cost": avg,
                "mark_price": mark,
                "market_value": value,
                "unrealized_pnl": pnl,
            })
        cash = _safe_float(state.get("cash"))
        equity = round(cash + market_value, 8)
        realized = round(realized, 8)
        unrealized = round(unrealized, 8)
        total_pnl = round(realized + unrealized, 8)
        capital_base = self._external_capital_base(fallback=round(equity - total_pnl, 8))
        prior_rows = self._read_jsonl(self.mtm_path)
        prior_equities = [
            _safe_float(row.get("equity") or row.get("total_equity"))
            for row in prior_rows
            if _safe_float(row.get("equity") or row.get("total_equity")) > 0
        ]
        high_water = max(prior_equities + [equity, capital_base])
        drawdown_pct = round(((high_water - equity) / high_water) * 100, 6) if high_water > 0 else 0.0
        return_pct = round((total_pnl / capital_base) * 100, 6) if abs(capital_base) > 1e-12 else 0.0
        benchmark_pnl = round(equity * _safe_float(benchmark_return), 8)
        benchmark_return_pct = round(_safe_float(benchmark_return) * 100, 6)
        trade_count = len(self._read_jsonl(self.trade_journal_path))
        payload = {
            "date": date,
            "timestamp": _now_iso(),
            "cash": cash,
            "market_value": round(market_value, 8),
            "equity": equity,
            "total_equity": equity,
            "capital_base": capital_base,
            "pnl": total_pnl,
            "total_pnl": total_pnl,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "return_pct": return_pct,
            "target_return_pct": round(_safe_float(target_return_pct), 6),
            "max_drawdown_pct": drawdown_pct,
            "benchmark_return": benchmark_return,
            "benchmark_return_pct": benchmark_return_pct,
            "benchmark_pnl": benchmark_pnl,
            "pnl_vs_benchmark": round(total_pnl - benchmark_pnl, 8),
            "open_position_count": open_position_count,
            "missing_mark_count": missing_mark_count,
            "trade_count": trade_count,
            "pnl_source": "sim_ledger_mark_to_market" if missing_mark_count == 0 else "sim_ledger_cost_fallback",
            "source": "sim_ledger_daily_mark_to_market",
            "account_type": "simulated",
            "positions": position_rows,
            "capital_layer": "simulated",
            "real_execution": False,
        }
        if extra_fields:
            payload.update(extra_fields)
        _add_cny_fields(payload)
        _append_jsonl(self.mtm_path, payload)
        return payload

    def export_json(self, output_path: Path | str) -> Path:
        output = Path(output_path)
        payload = {
            "state": self._load_state(),
            "trade_journal": self._read_jsonl(self.trade_journal_path),
            "cash_ledger": self._read_jsonl(self.cash_ledger_path),
            "double_entry": self._read_jsonl(self.double_entry_path),
            "daily_mark_to_market": self._read_jsonl(self.mtm_path),
        }
        _write_json(output, payload)
        return output

    def export_csv(self, output_dir: Path | str) -> dict[str, Path]:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        exports = {
            "trade_journal": output / "trade_journal.csv",
            "cash_ledger": output / "cash_ledger.csv",
            "double_entry": output / "double_entry.csv",
        }
        for name, path in exports.items():
            rows = self._read_jsonl(getattr(self, f"{name}_path"))
            self._write_csv(path, rows)
        return exports

    def current_state(self) -> dict[str, Any]:
        return self._load_state()

    def total_pnl(self, prices: dict[str, float] | None = None) -> dict[str, Any]:
        """Return realized PnL plus mark-to-market unrealized PnL for open positions.

        ``prices`` maps symbol -> mark price. Missing symbols are marked at cost
        (unrealized = 0) to avoid optimistic marks when no live price is available.
        """
        state = self._load_state()
        positions = state.get("positions", {})
        cash = _safe_float(state.get("cash"))
        realized = 0.0
        unrealized = 0.0
        market_value = 0.0
        open_count = 0
        missing_mark_count = 0
        pnl_samples: list[float] = []
        for symbol, position in positions.items():
            realized_pnl = _safe_float(position.get("realized_pnl"))
            realized += realized_pnl
            if abs(realized_pnl) > 1e-12:
                pnl_samples.append(round(realized_pnl, 8))
            qty = _safe_float(position.get("quantity"))
            avg = _safe_float(position.get("avg_cost"))
            if qty <= 1e-12:
                continue
            open_count += 1
            mark = _safe_float(prices.get(symbol) if prices else None)
            if mark <= 0:
                mark = avg
                missing_mark_count += 1
            market_value += round(mark * qty, 8)
            position_unrealized = round((mark - avg) * qty, 8)
            unrealized += position_unrealized
            if abs(position_unrealized) > 1e-12:
                pnl_samples.append(position_unrealized)
        equity = cash + market_value
        return {
            "realized_pnl": round(realized, 8),
            "unrealized_pnl": round(unrealized, 8),
            "total_pnl": round(realized + unrealized, 8),
            "cash": round(cash, 8),
            "market_value": round(market_value, 8),
            "equity": round(equity, 8),
            "open_position_count": open_count,
            "missing_mark_count": missing_mark_count,
            "pnl_samples": pnl_samples,
        }

    def to_real(self) -> dict[str, Any]:
        validate_real_trading_enabled()
        return {
            "adapter": "real_accounting_ledger_placeholder",
            "ledger_root": str(self.root),
            "requires_broker_statement_reconcile": True,
        }

    def _append_entry(self, entry: LedgerEntry) -> None:
        _append_jsonl(self.double_entry_path, entry.as_dict())

    def _load_state(self) -> dict[str, Any]:
        return _read_json(self.positions_path, {"cash": 0.0, "positions": {}, "tax_lots": {}})

    def _save_state(self, state: dict[str, Any]) -> None:
        _write_json(self.positions_path, state)
        _write_json(self.tax_lots_path, state.get("tax_lots", {}))

    def _external_capital_base(self, *, fallback: float) -> float:
        capital_base = 0.0
        found_capital_event = False
        for event in self._read_jsonl(self.cash_ledger_path):
            event_type = str(event.get("event_type") or "").lower()
            if event_type not in {"deposit", "withdrawal"}:
                continue
            found_capital_event = True
            capital_base += _safe_float(event.get("amount"))
        return round(capital_base if found_capital_event else fallback, 8)

    def _apply_buy(
        self,
        positions: dict[str, Any],
        lots: dict[str, Any],
        symbol: str,
        qty: float,
        price: float,
        fees: float,
        timestamp: str,
        order: dict[str, Any],
        stamp_duty: float,
    ) -> None:
        position = positions.setdefault(symbol, {"quantity": 0.0, "avg_cost": 0.0, "realized_pnl": 0.0})
        if order.get("outcome") not in (None, ""):
            position["outcome"] = str(order.get("outcome")).lower()
        if order.get("market_id") not in (None, ""):
            position["market_id"] = str(order.get("market_id"))
        old_qty = _safe_float(position.get("quantity"))
        old_avg = _safe_float(position.get("avg_cost"))
        total_cost = (old_qty * old_avg) + (qty * price) + fees
        new_qty = old_qty + qty
        position["quantity"] = round(new_qty, 8)
        position["avg_cost"] = round(total_cost / new_qty, 8)
        position["realized_pnl"] = round(_safe_float(position.get("realized_pnl")), 8)
        lot = {
            "lot_id": f"LOT-{uuid.uuid4().hex[:12]}",
            "symbol": symbol,
            "quantity": qty,
            "remaining_qty": qty,
            "cost_per_unit": round((qty * price + fees) / qty, 8),
            "opened_at": timestamp,
            "order_id": order.get("order_id", ""),
            "stamp_duty_paid": stamp_duty,
        }
        lots.setdefault(symbol, []).append(lot)

    def _apply_sell(
        self,
        positions: dict[str, Any],
        lots: dict[str, Any],
        symbol: str,
        qty: float,
        price: float,
        fees: float,
        timestamp: str,
        order: dict[str, Any],
        stamp_duty: float,
    ) -> tuple[float, float]:
        position = positions.get(symbol)
        if not position or _safe_float(position.get("quantity")) + 1e-12 < qty:
            raise ValueError(f"insufficient simulated position for {symbol}")
        remaining = qty
        cost_basis = 0.0
        symbol_lots = lots.setdefault(symbol, [])
        for lot in symbol_lots:
            if remaining <= 1e-12:
                break
            available = _safe_float(lot.get("remaining_qty"))
            close_qty = min(available, remaining)
            lot["remaining_qty"] = round(available - close_qty, 8)
            remaining = round(remaining - close_qty, 8)
            cost_basis += close_qty * _safe_float(lot.get("cost_per_unit"))
        lots[symbol] = [lot for lot in symbol_lots if _safe_float(lot.get("remaining_qty")) > 1e-12]
        if remaining > 1e-12:
            raise ValueError(f"FIFO lot state cannot close {qty} {symbol}")

        proceeds = qty * price
        realized = round(proceeds - cost_basis - fees, 8)
        new_qty = round(_safe_float(position.get("quantity")) - qty, 8)
        position["quantity"] = max(0.0, new_qty)
        position["realized_pnl"] = round(_safe_float(position.get("realized_pnl")) + realized, 8)
        if position["quantity"] <= 0:
            position["avg_cost"] = 0.0
        else:
            remaining_cost = sum(
                _safe_float(lot.get("remaining_qty")) * _safe_float(lot.get("cost_per_unit"))
                for lot in lots.get(symbol, [])
            )
            position["avg_cost"] = round(remaining_cost / position["quantity"], 8)
        tax_event = {
            "lot_id": f"TAX-{uuid.uuid4().hex[:12]}",
            "symbol": symbol,
            "closed_qty": qty,
            "closed_at": timestamp,
            "order_id": order.get("order_id", ""),
            "stamp_duty_paid": stamp_duty,
            "realized_pnl": realized,
        }
        lots.setdefault(f"{symbol}:closed_tax_lots", []).append(tax_event)
        return realized, round(cost_basis, 8)

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rows.append(json.loads(line))
        return rows

    @staticmethod
    def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        keys = sorted({key for row in rows for key in row.keys()})
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=keys)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: json.dumps(row.get(key), ensure_ascii=False) if isinstance(row.get(key), (dict, list)) else row.get(key) for key in keys})


__all__ = ["LedgerEntry", "LedgerLeg", "SimLedger"]
