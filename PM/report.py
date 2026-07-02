#!/usr/bin/env python3
"""PM P1 daily Brier and PnL reporting."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from PM.common import PMConfig, load_pm_config
from shared.markets.base_tools import BaseReport
from shared.markets.safety import reject_real_execution_payload


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default


def _probability(row: dict[str, Any]) -> float:
    value = row.get("prediction", row.get("probability", row.get("model_probability", row.get("yes_price", 0.5))))
    return max(0.0, min(1.0, _safe_float(value, 0.5)))


def _outcome(row: dict[str, Any]) -> int | None:
    value = row.get("actual", row.get("outcome", row.get("resolved_outcome")))
    if value in (None, ""):
        return None
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "win", "resolved_yes"}:
        return 1
    if raw in {"0", "false", "no", "n", "loss", "resolved_no"}:
        return 0
    return 1 if bool(value) else 0


class PMDailyReport(BaseReport):
    """Daily prediction-market report with Brier and shadow PnL metrics."""

    def __init__(
        self,
        config: PMConfig | None = None,
        *,
        records: Iterable[dict[str, Any]] | None = None,
    ) -> None:
        self.pm_config = config or load_pm_config()
        self.pm_config.validate()
        self.records = [dict(row) for row in (records or [])]
        super().__init__("pm", self.pm_config.to_market_tool_config())
        for row in self.records:
            reject_real_execution_payload(row, context="PMDailyReport.records")

    def render_daily(self, date: str) -> dict[str, Any]:
        rows = [row for row in self.records if self._matches_date(row, date)]
        resolved = [row for row in rows if _outcome(row) is not None]
        brier_values = [(_probability(row) - float(_outcome(row))) ** 2 for row in resolved]
        pnl = sum(_safe_float(row.get("pnl")) for row in rows)
        result = {
            "market": "pm",
            "date": date,
            "capital_layer": "shadow",
            "account_type": "shadow",
            "status": "ok",
            "signal_count": len(rows),
            "resolved_count": len(resolved),
            "brier_score": round(sum(brier_values) / len(brier_values), 6) if brier_values else None,
            "pnl": round(pnl, 6),
            "currency": self.pm_config.capital.currency,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        result["delivery"] = self.delivery_policy(result)
        return result

    def render_scorecard(self, date: str) -> dict[str, Any]:
        rows = [row for row in self.records if self._matches_date(row, date)]
        resolved = [row for row in rows if _outcome(row) is not None]
        wins = sum(1 for row in resolved if (_probability(row) >= 0.5) == bool(_outcome(row)))
        return {
            "market": "pm",
            "date": date,
            "capital_layer": "shadow",
            "sample_count": len(rows),
            "resolved_count": len(resolved),
            "accuracy": round(wins / len(resolved), 6) if resolved else 0.0,
            "pnl": round(sum(_safe_float(row.get("pnl")) for row in rows), 6),
        }

    def delivery_policy(self, result: dict[str, Any]) -> dict[str, Any]:
        reject_real_execution_payload(result, context="PMDailyReport.delivery")
        if int(result.get("signal_count") or 0) <= 0 and self.config.reporting.notify_on_trigger_only:
            return {"send": False, "status": "no_send", "reason": "empty_pm_report"}
        return {"send": True, "status": "ready", "reason": "pm_daily_recap"}

    @staticmethod
    def _matches_date(row: dict[str, Any], date: str) -> bool:
        raw = str(row.get("trade_date") or row.get("date") or row.get("created_at") or "")
        compact = date.replace("-", "")
        return raw.startswith(date) or raw.startswith(compact)


__all__ = ["PMDailyReport"]
