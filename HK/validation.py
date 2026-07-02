#!/usr/bin/env python3
"""HK P1 forward validation for shadow strategies."""

from __future__ import annotations

from typing import Any

from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import assert_no_live_broker, assert_no_real_execution, reject_real_execution_payload
from HK.common import HKConfig


class HKForwardValidation:
    """Out-of-sample validation using HKD-denominated results."""

    def __init__(self, config: MarketToolConfig | None = None, *, train_end: str | None = None) -> None:
        self.config = config or HKConfig()
        assert_no_real_execution(self.config)
        assert_no_live_broker(self.config)
        self.train_end = train_end
        self.market = "hk"
        self.currency = "HKD"

    def validate(self, records: list[dict[str, Any]], *, as_of: str) -> dict[str, Any]:
        checked = []
        train_end_key = _date_key(self.train_end)
        as_of_key = _date_key(as_of)
        for record in list(records or []):
            reject_real_execution_payload(record, context="HKForwardValidation.record")
            date = _date_key(record.get("trade_date") or record.get("signal_date") or record.get("date") or record.get("as_of"))
            if train_end_key and (not date or date <= train_end_key):
                continue
            if as_of_key and date and date > as_of_key:
                continue
            checked.append(record)

        returns = [_to_float(record.get("return_pct")) for record in checked]
        pnl_values = [_to_float(record.get("pnl")) for record in checked]
        positive = [value for value in returns if value > 0]
        return {
            "market": "hk",
            "as_of": as_of,
            "train_end": self.train_end,
            "status": "ok",
            "validation_type": "out_of_sample",
            "currency": "HKD",
            "capital_layer": "shadow",
            "total": len(checked),
            "positive": len(positive),
            "positive_rate": len(positive) / len(checked) if checked else 0.0,
            "avg_return_pct": round(sum(returns) / len(returns), 6) if returns else 0.0,
            "total_pnl": round(sum(pnl_values), 6),
            "passed": bool(checked) and len(positive) / len(checked) >= self.config.promotion.min_positive_days_pct,
            "real_execution": False,
        }


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if result == result else float(default)


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
