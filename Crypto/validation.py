#!/usr/bin/env python3
"""Crypto P1 forward validation metrics."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from Crypto.common import CryptoConfig, load_crypto_config
from shared.markets.safety import assert_no_real_execution, reject_real_execution_payload


def _date_key(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    date_part = raw.split("T", 1)[0].split(" ", 1)[0]
    if "-" in date_part:
        parts = date_part.split("-")
        if len(parts) >= 3:
            return f"{parts[0].zfill(4)}{parts[1].zfill(2)}{parts[2].zfill(2)}"
    compact = "".join(ch for ch in date_part if ch.isdigit())
    return compact.zfill(8) if compact else ""


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default


class CryptoForwardValidation:
    """Out-of-sample validation and sample-quality scorecard for Crypto."""

    def __init__(
        self,
        config: CryptoConfig | None = None,
        *,
        records: Iterable[dict[str, Any]] | None = None,
        train_end: str | None = None,
    ) -> None:
        self.config = config or load_crypto_config()
        assert_no_real_execution(self.config)
        self.records = [dict(row) for row in (records or [])]
        self.train_end = train_end
        for row in self.records:
            reject_real_execution_payload(row, context="CryptoForwardValidation.records")

    def evaluate(self, as_of: str | None = None) -> dict[str, Any]:
        oos_rows = self.oos_records(as_of)
        pnl_values = [_safe_float(row.get("pnl") or row.get("return")) for row in oos_rows]
        wins = sum(1 for value in pnl_values if value > 0)
        losses = sum(1 for value in pnl_values if value < 0)
        hit_rows = [row for row in oos_rows if "direction_hit" in row or "hit" in row]
        hit_count = sum(1 for row in hit_rows if bool(row.get("direction_hit", row.get("hit"))))
        quality = self.sample_quality(as_of)
        return {
            "market": "crypto",
            "capital_layer": "shadow",
            "account_type": "shadow",
            "as_of": as_of,
            "train_end": self.train_end,
            "oos_count": len(oos_rows),
            "win_rate": round(wins / len(pnl_values), 6) if pnl_values else 0.0,
            "loss_count": losses,
            "total_pnl": round(sum(pnl_values), 6),
            "avg_pnl": round(sum(pnl_values) / len(pnl_values), 6) if pnl_values else 0.0,
            "direction_hit_rate": round(hit_count / len(hit_rows), 6) if hit_rows else 0.0,
            "sample_quality": quality,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def oos_records(self, as_of: str | None = None) -> list[dict[str, Any]]:
        rows = []
        train_end_key = _date_key(self.train_end)
        as_of_key = _date_key(as_of)
        for row in self.records:
            date = _date_key(row.get("trade_date") or row.get("date") or "")
            if train_end_key and (not date or date <= train_end_key):
                continue
            if as_of_key and date and date > as_of_key:
                continue
            rows.append(row)
        return rows

    def sample_quality(self, as_of: str | None = None) -> dict[str, Any]:
        rows = self.oos_records(as_of)
        symbols = {str(row.get("symbol") or row.get("ts_code") or "") for row in rows if row.get("symbol") or row.get("ts_code")}
        strategies = {str(row.get("strategy") or row.get("strategy_name") or "") for row in rows if row.get("strategy") or row.get("strategy_name")}
        triggered = sum(1 for row in rows if bool(row.get("triggered")) or str(row.get("status", "")).lower() in {"triggered", "filled"})
        score = 0
        score += 30 if len(rows) >= 30 else int(len(rows) / 30 * 30)
        score += 25 if len(symbols) >= 3 else int(len(symbols) / 3 * 25)
        score += 20 if len(strategies) >= 2 else int(len(strategies) / 2 * 20)
        score += 25 if triggered >= 3 else int(triggered / 3 * 25)
        return {
            "sample_count": len(rows),
            "unique_symbol_count": len(symbols),
            "strategy_count": len(strategies),
            "triggered_count": triggered,
            "score": min(100, score),
            "grade": "ready" if score >= 75 else "thin" if score >= 45 else "insufficient",
        }


__all__ = ["CryptoForwardValidation"]
