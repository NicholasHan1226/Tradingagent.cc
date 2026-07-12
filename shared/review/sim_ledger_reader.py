#!/usr/bin/env python3
"""Read simulated trade ledgers as review inputs.

The review layer treats these files as append-only sources of fills:
- shared/logs/sim_ledger/<market>/<style>/trade_journal.jsonl
- shared/logs/execution_lineages/<current-ashare-lineage>/local_sim_trades.jsonl
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shared.execution.local_sim_ledger import (
    LOCAL_SIM_TRADES as CURRENT_ASHARE_SIM_TRADES,
)
from shared.review.sample_quality import enrich_trade_sample

REVIEW_DIR = Path(__file__).resolve().parent
SHARED_DIR = REVIEW_DIR.parent
DEFAULT_SIM_LEDGER_ROOT = SHARED_DIR / "logs" / "sim_ledger"
DEFAULT_LOCAL_SIM_TRADES = CURRENT_ASHARE_SIM_TRADES
DEFAULT_REVIEW_MARKETS = ("ashare", "crypto", "pm", "us")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _compact_date(value: Any) -> str:
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else raw


def _date_in_range(value: Any, start_date: str, end_date: str) -> bool:
    compact = _compact_date(value)
    if not compact:
        return False
    start = _compact_date(start_date)
    end = _compact_date(end_date)
    return start <= compact <= end


def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        if not path.exists():
            return rows
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except OSError:
        return []
    return rows


def _normalize_market(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"a_share", "a-share", "cn", "china"}:
        return "ashare"
    if raw in {"usa"}:
        return "us"
    if raw in {"polymarket", "prediction_market", "prediction-market"}:
        return "pm"
    return raw or "unknown"


def _market_allowed(market: str, markets: set[str]) -> bool:
    return _normalize_market(market) in markets


def _markets_filter(markets: list[str] | tuple[str, ...] | set[str] | None) -> set[str]:
    raw_markets = markets if markets is not None else DEFAULT_REVIEW_MARKETS
    return {_normalize_market(item) for item in raw_markets}


def _infer_market_strategy(path: Path, ledger_root: Path) -> tuple[str, str]:
    try:
        parent = path.parent.relative_to(ledger_root)
    except ValueError:
        return "unknown", "simulated"
    parts = parent.parts
    if len(parts) >= 2:
        return _normalize_market(parts[0]), parts[1]
    if len(parts) == 1:
        return _normalize_market(parts[0]), "simulated"
    return "unknown", "simulated"


def _normalize_style_ledger_trade(
    row: dict[str, Any], path: Path, ledger_root: Path
) -> dict[str, Any]:
    market, strategy = _infer_market_strategy(path, ledger_root)
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    timestamp = row.get("timestamp") or row.get("created_at") or row.get("fill_time")
    return {
        "ts_code": row.get("symbol") or row.get("ts_code") or row.get("code") or "",
        "side": row.get("side") or "",
        "quantity": _safe_float(
            row.get("fill_qty") or row.get("quantity") or metadata.get("quantity")
        ),
        "price": _safe_float(
            row.get("fill_price") or row.get("price") or metadata.get("price")
        ),
        "pnl": _safe_float(
            row.get("realized_pnl") or row.get("pnl") or metadata.get("realized_pnl")
        ),
        "strategy": row.get("strategy") or strategy,
        "signal_id": row.get("order_id")
        or row.get("fill_id")
        or row.get("signal_id")
        or "",
        "order_id": row.get("order_id") or "",
        "fill_id": row.get("fill_id") or "",
        "created_at": timestamp or "",
        "trade_date": row.get("trade_date") or timestamp or "",
        "market": _normalize_market(row.get("market") or market),
        "capital_layer": "simulated",
        "source_ledger": str(path),
        "notional": _safe_float(row.get("notional")),
        "fees": row.get("fees") or metadata.get("fees") or {},
    }


def _normalize_local_sim_trade(row: dict[str, Any], path: Path) -> dict[str, Any]:
    return enrich_trade_sample(
        {
            "ts_code": row.get("ts_code") or row.get("symbol") or "",
            "side": row.get("side") or "",
            "quantity": _safe_float(row.get("quantity")),
            "filled_price": _safe_float(
                row.get("filled_price") or row.get("avg_price")
            ),
            "avg_price": _safe_float(row.get("avg_price")),
            "price": _safe_float(
                row.get("filled_price")
                or row.get("avg_price")
                or row.get("requested_price")
            ),
            "pnl": _safe_float(row.get("realized_pnl") or row.get("pnl")),
            "strategy": row.get("strategy") or row.get("source") or "server_local_sim",
            "signal_id": row.get("order_id")
            or row.get("idempotency_key")
            or row.get("trade_id")
            or "",
            "order_id": row.get("order_id") or "",
            "trade_id": row.get("trade_id") or "",
            "idempotency_key": row.get("idempotency_key") or "",
            "created_at": row.get("created_at") or "",
            "trade_timestamp_bj": row.get("trade_timestamp_bj") or "",
            "ashare_session_valid": row.get("ashare_session_valid"),
            "ashare_session_rejection": row.get("ashare_session_rejection") or "",
            "trade_date": row.get("trade_date") or row.get("created_at") or "",
            "market": _normalize_market(row.get("market") or "ashare"),
            "capital_layer": "simulated",
            "status": row.get("status") or "",
            "candidate_pool_layer": row.get("candidate_pool_layer") or "",
            "execution_source": row.get("execution_source") or "",
            "fill_price_source": row.get("fill_price_source") or "",
            "fill_price_source_class": row.get("fill_price_source_class") or "",
            "fill_evidence": row.get("fill_evidence")
            if isinstance(row.get("fill_evidence"), dict)
            else {},
            "capital_scope": row.get("capital_scope") or "",
            "retry_of": row.get("retry_of") or "",
            "retry_attempt": int(_safe_float(row.get("retry_attempt"))),
            "source_ledger": str(path),
            "notional": _safe_float(row.get("amount") or row.get("net_amount")),
            "fees": {
                "commission": _safe_float(row.get("commission")),
                "stamp_duty": _safe_float(row.get("stamp_duty")),
            },
        }
    )


def _load_style_ledger_trades(
    start_date: str,
    end_date: str,
    *,
    markets: set[str],
    ledger_root: Path,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(ledger_root.glob("**/trade_journal.jsonl")):
        market, strategy = _infer_market_strategy(path, ledger_root)
        if not _market_allowed(market, markets):
            continue
        if market == "ashare" and strategy != "ashare_sim":
            continue
        for row in _read_jsonl_dicts(path):
            trade_date = (
                row.get("trade_date") or row.get("timestamp") or row.get("created_at")
            )
            if not _date_in_range(trade_date, start_date, end_date):
                continue
            normalized = _normalize_style_ledger_trade(row, path, ledger_root)
            if _market_allowed(str(normalized.get("market")), markets):
                rows.append(normalized)
    return rows


def _load_local_sim_trades(
    start_date: str,
    end_date: str,
    *,
    markets: set[str],
    local_trades_path: Path,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if "ashare" not in markets:
        return rows
    for row in _read_jsonl_dicts(local_trades_path):
        trade_date = row.get("trade_date") or row.get("created_at")
        if not _date_in_range(trade_date, start_date, end_date):
            continue
        normalized = _normalize_local_sim_trade(row, local_trades_path)
        if str(normalized.get("status") or "filled").lower() in {
            "failed",
            "rejected",
            "cancelled",
        }:
            continue
        rows.append(normalized)
    return rows


def load_sim_trades_between(
    start_date: str,
    end_date: str,
    *,
    markets: list[str] | tuple[str, ...] | set[str] | None = None,
    ledger_root: Path | None = None,
    local_trades_path: Path | None = None,
) -> list[dict[str, Any]]:
    market_filter = _markets_filter(markets)
    root = ledger_root or DEFAULT_SIM_LEDGER_ROOT
    local_path = local_trades_path or DEFAULT_LOCAL_SIM_TRADES
    return _load_style_ledger_trades(
        start_date, end_date, markets=market_filter, ledger_root=root
    ) + _load_local_sim_trades(
        start_date, end_date, markets=market_filter, local_trades_path=local_path
    )


def load_sim_trades_for_date(
    trade_date: str,
    *,
    markets: list[str] | tuple[str, ...] | set[str] | None = None,
    ledger_root: Path | None = None,
    local_trades_path: Path | None = None,
) -> list[dict[str, Any]]:
    return load_sim_trades_between(
        trade_date,
        trade_date,
        markets=markets,
        ledger_root=ledger_root,
        local_trades_path=local_trades_path,
    )


def summarize_trade_sources(trades: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for trade in trades:
        source = str(
            trade.get("source_ledger") or trade.get("capital_layer") or "unknown"
        )
        counts[source] = counts.get(source, 0) + 1
    return counts
