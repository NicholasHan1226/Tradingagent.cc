#!/usr/bin/env python3
"""Crypto P1 daily shadow reporting.

Reports are recap-only. They never send orders, read exchange accounts, or
promote a real-capital action.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from Crypto.common import CryptoConfig, load_crypto_config
from shared.markets.base_tools import BaseReport
from shared.markets.safety import reject_real_execution_payload


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default


def _is_trigger(record: dict[str, Any]) -> bool:
    status = str(record.get("status") or record.get("trigger_status") or "").lower()
    if status in {"triggered", "pending", "filled"}:
        return True
    return bool(record.get("triggered") or record.get("trigger_alert"))


class CryptoDailyReport(BaseReport):
    """Daily shadow recap with the Crypto no-empty-trigger delivery rule."""

    def __init__(
        self,
        config: CryptoConfig | None = None,
        *,
        records: Iterable[dict[str, Any]] | None = None,
    ) -> None:
        self.records = [dict(row) for row in (records or [])]
        resolved = config or load_crypto_config()
        super().__init__("crypto", resolved)
        self._reject_real_payloads(self.records, context="CryptoDailyReport.records")

    def render_daily(self, date: str) -> dict[str, Any]:
        rows = [row for row in self.records if self._matches_date(row, date)]
        triggers = [row for row in rows if _is_trigger(row)]
        pnl = sum(_safe_float(row.get("pnl") or row.get("realized_pnl") or row.get("floating_pnl")) for row in rows)
        notional = sum(_safe_float(row.get("notional") or row.get("market_value")) for row in rows)
        open_positions = [row for row in rows if str(row.get("position_status") or "").lower() in {"open", "holding"}]

        result = {
            "market": "crypto",
            "date": date,
            "capital_layer": "shadow",
            "account_type": "shadow",
            "status": "ok",
            "signal_count": len(rows),
            "trigger_count": len(triggers),
            "open_position_count": len(open_positions),
            "pnl": round(pnl, 6),
            "notional": round(notional, 6),
            "triggers": triggers,
            "no_empty_trigger_rule": True,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        result["delivery"] = self.delivery_policy(result)
        return result

    def render_scorecard(self, date: str) -> dict[str, Any]:
        rows = [row for row in self.records if self._matches_date(row, date)]
        wins = sum(1 for row in rows if _safe_float(row.get("pnl")) > 0)
        losses = sum(1 for row in rows if _safe_float(row.get("pnl")) < 0)
        return {
            "market": "crypto",
            "date": date,
            "capital_layer": "shadow",
            "sample_count": len(rows),
            "win_count": wins,
            "loss_count": losses,
            "win_rate": round(wins / len(rows), 6) if rows else 0.0,
            "avg_belief_score": round(
                sum(_safe_float(row.get("belief_score")) for row in rows) / len(rows),
                6,
            )
            if rows
            else 0.0,
        }

    def delivery_policy(self, result: dict[str, Any]) -> dict[str, Any]:
        reject_real_execution_payload(result, context="CryptoDailyReport.delivery")
        trigger_count = int(result.get("trigger_count") or 0)
        if trigger_count <= 0 and self.config.reporting.notify_on_trigger_only:
            return {
                "send": False,
                "status": "no_send",
                "reason": "no_trigger_no_empty_report",
            }
        return {"send": True, "status": "ready", "reason": "trigger_present"}

    @staticmethod
    def _matches_date(row: dict[str, Any], date: str) -> bool:
        raw = str(row.get("trade_date") or row.get("date") or row.get("created_at") or "")
        compact = date.replace("-", "")
        return raw.startswith(date) or raw.startswith(compact)

    @staticmethod
    def _reject_real_payloads(records: Iterable[dict[str, Any]], *, context: str) -> None:
        for row in records:
            reject_real_execution_payload(row, context=context)


__all__ = ["CryptoDailyReport"]
