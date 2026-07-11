#!/usr/bin/env python3
"""A-share capital-tier experiment ledgers.

The active production account uses only the canonical capital. Historical
capital experiments run only when callers explicitly pass ``tiers``; the
default production refresh does not create parallel 100k/200k current books.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Sequence

from Ashare.capital_plan import plan_capital
from Ashare.epoch_review import validate_review_authority
from shared.execution import local_sim_ledger
from shared.execution.sim_account_epoch import (
    read_epoch_state,
    require_authoritative_epoch_metadata,
)
from shared.markets.sim_capital import default_sim_capital
from shared.review.sample_quality import strategy_valid_trades


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_TRADES = ROOT / "shared" / "logs" / "local_sim" / "local_sim_trades.jsonl"
DEFAULT_TIER_ROOT = ROOT / "shared" / "logs" / "local_sim_tiers"
DEFAULT_REVIEW_DIR = ROOT / "shared" / "review" / "ashare"


def _primary_capital() -> float:
    """Return the canonical A-share simulated capital in CNY."""
    return round(default_sim_capital("ashare"), 6)


def _experiment_tiers() -> tuple[float, ...]:
    """Return active experiment tiers; production has none by default."""
    return ()


# Explicit historical analysis can still pass ``tiers=(100_000, 200_000)``.
EXPERIMENT_TIERS = _experiment_tiers()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _tier_account(capital: float) -> str:
    return f"ashare_{int(capital)}"


def _commission(amount: float) -> float:
    return round(max(amount * 0.00025, 5.0), 2)


def _stamp_duty(amount: float, side: str) -> float:
    return round(amount * 0.0005, 2) if side == "sell" else 0.0


def _net_amount(amount: float, side: str) -> tuple[float, float, float]:
    commission = _commission(amount)
    stamp = _stamp_duty(amount, side)
    if side == "buy":
        return round(amount + commission + stamp, 2), commission, stamp
    return round(amount - commission - stamp, 2), commission, stamp


def _buy_quantity(source: dict[str, Any], capital: float, cash_available: float) -> int:
    price = _safe_float(source.get("filled_price") or source.get("avg_price") or source.get("requested_price"))
    if price <= 0:
        return 0
    source_amount = _safe_float(source.get("amount"), _safe_float(source.get("quantity")) * price)
    canonical_capital = _primary_capital()
    target_amount = max(0.0, source_amount * capital / canonical_capital) if canonical_capital > 0 else 0.0
    quantity = int(target_amount // price)
    quantity = (quantity // 100) * 100
    while quantity > 0:
        amount = round(quantity * price, 2)
        required, _, _ = _net_amount(amount, "buy")
        if required <= cash_available + 1e-9:
            return quantity
        quantity -= 100
    return 0


def _sell_quantity(source: dict[str, Any], capital: float, position_qty: int) -> int:
    source_qty = _safe_int(source.get("quantity"))
    if source_qty <= 0 or position_qty <= 0:
        return 0
    canonical_capital = _primary_capital()
    scaled = int(source_qty * capital / canonical_capital) if canonical_capital > 0 else 0
    scaled = (scaled // 100) * 100
    if scaled <= 0:
        scaled = min(position_qty, 100)
    return min(position_qty, scaled)


def _trade_row(source: dict[str, Any], *, account: str, quantity: int, side: str) -> dict[str, Any]:
    price = round(_safe_float(source.get("filled_price") or source.get("avg_price") or source.get("requested_price")), 4)
    amount = round(quantity * price, 2)
    net_amount, commission, stamp = _net_amount(amount, side)
    row = dict(source)
    row.update(
        {
            "trade_id": f"{source.get('trade_id') or source.get('order_id') or 'tier'}:{account}",
            "order_id": f"{source.get('order_id') or source.get('trade_id') or 'tier'}:{account}",
            "idempotency_key": f"{source.get('idempotency_key') or source.get('order_id') or source.get('trade_id') or 'tier'}:{account}",
            "account": account,
            "quantity": quantity,
            "filled_price": price,
            "avg_price": price,
            "amount": amount,
            "commission": commission,
            "stamp_duty": stamp,
            "net_amount": net_amount,
            "side": side,
            "status": "filled",
            "source": "ashare_capital_tier_experiment",
            "tier_experiment": True,
        }
    )
    return row


def _build_tier_capital_plan(
    ledger: dict[str, Any],
    candidates: Sequence[dict[str, Any]] | None = None,
    market_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate an independent capital plan for a single tier account.

    Uses the tier's own replayed cash and positions, not the 200k account's
    plan.  The plan is scaled to the tier's total capital.
    """
    pnl = ledger.get("pnl") or {}
    positions = pnl.get("positions") or {}
    holdings = [
        {"ts_code": code, "value": _safe_float(pos.get("market_value"), 0.0)}
        for code, pos in positions.items()
    ]
    cash_available = _safe_float(pnl.get("cash_available"), ledger["capital"])
    capital = float(ledger["capital"])
    plan = plan_capital(
        holdings,
        cash_available,
        candidates=candidates,
        dynamic=True,
        market_context=market_context or {},
        total_capital=capital,
    )
    plan_dict = plan.to_dict()
    plan_dict["total_capital"] = round(capital, 2)
    plan_dict["account"] = ledger["account"]
    plan_dict["cash_available"] = round(cash_available, 2)
    return plan_dict


def build_tier_ledger(
    source_trades: list[dict[str, Any]],
    *,
    capital: float,
    mark_prices: dict[str, float] | None = None,
) -> dict[str, Any]:
    account = _tier_account(capital)
    cash = float(capital)
    positions: dict[str, int] = {}
    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for source in strategy_valid_trades(source_trades):
        symbol = str(source.get("ts_code") or source.get("symbol") or "").strip().upper()
        side = str(source.get("side") or "").strip().lower()
        if not symbol or side not in {"buy", "sell"}:
            continue
        if side == "buy":
            quantity = _buy_quantity(source, capital, cash)
            if quantity <= 0:
                skipped.append({"source_trade_id": source.get("trade_id"), "symbol": symbol, "side": side, "reason": "tier_cash_or_lot_size_insufficient"})
                continue
            row = _trade_row(source, account=account, quantity=quantity, side=side)
            cash -= _safe_float(row.get("net_amount"))
            positions[symbol] = positions.get(symbol, 0) + quantity
            generated.append(row)
            continue
        quantity = _sell_quantity(source, capital, positions.get(symbol, 0))
        if quantity <= 0:
            skipped.append({"source_trade_id": source.get("trade_id"), "symbol": symbol, "side": side, "reason": "no_tier_position_to_sell"})
            continue
        row = _trade_row(source, account=account, quantity=quantity, side=side)
        cash += _safe_float(row.get("net_amount"))
        positions[symbol] = max(0, positions.get(symbol, 0) - quantity)
        generated.append(row)
    pnl = local_sim_ledger._replay_account(  # noqa: SLF001
        generated,
        account,
        mark_prices=mark_prices,
        starting_cash=capital,
    )
    return {
        "account": account,
        "capital": capital,
        "trade_count": len(generated),
        "skipped_count": len(skipped),
        "trades": generated,
        "pnl": pnl,
        "skipped": skipped,
    }


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(str(tmp), str(path))
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def write_tier_ledgers(
    *,
    source_trades_path: Path | str | None = None,
    tier_root: Path | str | None = None,
    review_dir: Path | str | None = None,
    tiers: tuple[float, ...] | None = None,
    candidates: Sequence[dict[str, Any]] | None = None,
    market_context: dict[str, Any] | None = None,
    mark_prices: dict[str, float] | None = None,
) -> dict[str, Any]:
    authority = require_authoritative_epoch_metadata(read_epoch_state())
    epoch_fields = {
        "capital_epoch": int(authority["capital_epoch"]),
        "capital_cny": float(authority["capital_cny"]),
        "epoch_cutover_timestamp": str(authority["cutover_timestamp"]),
    }
    source_path = Path(source_trades_path) if source_trades_path is not None else DEFAULT_SOURCE_TRADES
    output_root = Path(tier_root) if tier_root is not None else DEFAULT_TIER_ROOT
    review_path = Path(review_dir) if review_dir is not None else DEFAULT_REVIEW_DIR
    source_trades = _read_jsonl(source_path)
    current_source_trades: list[dict[str, Any]] = []
    source_authority_rejections: dict[str, int] = {}
    for row in source_trades:
        valid, reason = validate_review_authority(row, epoch_fields)
        if valid:
            current_source_trades.append(row)
        else:
            source_authority_rejections[reason] = source_authority_rejections.get(reason, 0) + 1
    experiment_tiers = tuple(tiers) if tiers is not None else _experiment_tiers()
    accounts: list[dict[str, Any]] = []
    for capital in experiment_tiers:
        ledger = build_tier_ledger(current_source_trades, capital=float(capital), mark_prices=mark_prices)
        ledger["trades"] = [{**row, **epoch_fields} for row in ledger["trades"]]
        capital_plan = _build_tier_capital_plan(ledger, candidates=candidates, market_context=market_context)
        account_dir = output_root / ledger["account"]
        trades_path = account_dir / "local_sim_trades.jsonl"
        _atomic_write(
            trades_path,
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in ledger["trades"]),
        )
        _atomic_write_json(
            account_dir / "local_sim_pnl.json",
            {**epoch_fields, ledger["account"]: ledger["pnl"]},
        )
        _atomic_write_json(
            account_dir / "local_sim_positions.json",
            {**epoch_fields, ledger["account"]: ledger["pnl"].get("positions", {})},
        )
        _atomic_write_json(
            account_dir / "capital_plan.json",
            {**epoch_fields, ledger["account"]: capital_plan},
        )
        accounts.append(
            {
                **epoch_fields,
                "account": ledger["account"],
                "capital": ledger["capital"],
                "trade_count": ledger["trade_count"],
                "skipped_count": ledger["skipped_count"],
                "pnl": ledger["pnl"],
                "capital_plan": capital_plan,
                "ledger_dir": str(account_dir.relative_to(ROOT)) if str(account_dir).startswith(str(ROOT)) else str(account_dir),
            }
        )
    manifest = {
        **epoch_fields,
        "market": "ashare",
        "primary_capital": round(_primary_capital(), 2),
        "source_trades": str(source_path.relative_to(ROOT)) if str(source_path).startswith(str(ROOT)) else str(source_path),
        "tier_root": str(output_root.relative_to(ROOT)) if str(output_root).startswith(str(ROOT)) else str(output_root),
        "accounts": accounts,
        "source_trade_count": len(current_source_trades),
        "current_source_trade_count": len(current_source_trades),
        "source_authority_rejection_count": sum(source_authority_rejections.values()),
        "source_authority_rejections": source_authority_rejections,
        "read_only_source": True,
        "real_trading_enabled": False,
    }
    _atomic_write_json(review_path / "tier_experiments_latest.json", manifest)
    return manifest


def _read_candidates(path: Path | str | None) -> list[dict[str, Any]] | None:
    if path is None:
        return None
    candidate_path = Path(path)
    if not candidate_path.exists():
        return None
    try:
        text = candidate_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text.startswith("["):
        try:
            payload = json.loads(text)
            return [item for item in payload if isinstance(item, dict)]
        except json.JSONDecodeError:
            return None
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows or None


def _read_market_context(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-trades-path", type=Path, default=DEFAULT_SOURCE_TRADES)
    parser.add_argument("--tier-root", type=Path, default=DEFAULT_TIER_ROOT)
    parser.add_argument("--review-dir", type=Path, default=DEFAULT_REVIEW_DIR)
    parser.add_argument("--candidates-path", type=Path, default=None)
    parser.add_argument("--market-context", type=str, default="")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    report = write_tier_ledgers(
        source_trades_path=args.source_trades_path,
        tier_root=args.tier_root,
        review_dir=args.review_dir,
        candidates=_read_candidates(args.candidates_path),
        market_context=_read_market_context(args.market_context),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
