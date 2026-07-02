#!/usr/bin/env python3
"""PM P1 forward validation: calibration and Brier tracking."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from PM.common import PMConfig, load_pm_config
from PM.report import _outcome, _probability, _safe_float
from shared.markets.safety import reject_real_execution_payload


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


class PMForwardValidation:
    """Calibration, Brier score, and PnL tracking for PM shadow records."""

    def __init__(
        self,
        config: PMConfig | None = None,
        *,
        records: Iterable[dict[str, Any]] | None = None,
        train_end: str | None = None,
    ) -> None:
        self.config = config or load_pm_config()
        self.config.validate()
        self.records = [dict(row) for row in (records or [])]
        self.train_end = train_end
        for row in self.records:
            reject_real_execution_payload(row, context="PMForwardValidation.records")

    def evaluate(self, as_of: str | None = None) -> dict[str, Any]:
        rows = self.oos_records(as_of)
        resolved = [row for row in rows if _outcome(row) is not None]
        brier_values = [(_probability(row) - float(_outcome(row))) ** 2 for row in resolved]
        calibration = self.calibration_bins(resolved)
        return {
            "market": "pm",
            "capital_layer": "shadow",
            "account_type": "shadow",
            "as_of": as_of,
            "train_end": self.train_end,
            "oos_count": len(rows),
            "resolved_count": len(resolved),
            "brier_score": round(sum(brier_values) / len(brier_values), 6) if brier_values else None,
            "pnl": round(sum(_safe_float(row.get("pnl")) for row in rows), 6),
            "calibration": calibration,
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

    @staticmethod
    def calibration_bins(rows: Iterable[dict[str, Any]], bin_count: int = 5) -> list[dict[str, Any]]:
        bins = [{"bin": idx, "count": 0, "avg_prediction": 0.0, "actual_rate": 0.0} for idx in range(bin_count)]
        for row in rows:
            prob = _probability(row)
            outcome = _outcome(row)
            if outcome is None:
                continue
            idx = min(bin_count - 1, int(prob * bin_count))
            bucket = bins[idx]
            bucket["count"] += 1
            bucket["avg_prediction"] += prob
            bucket["actual_rate"] += float(outcome)
        for bucket in bins:
            count = int(bucket["count"])
            if count:
                bucket["avg_prediction"] = round(float(bucket["avg_prediction"]) / count, 6)
                bucket["actual_rate"] = round(float(bucket["actual_rate"]) / count, 6)
        return bins


__all__ = ["PMForwardValidation"]
