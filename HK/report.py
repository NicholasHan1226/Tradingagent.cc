#!/usr/bin/env python3
"""HK P1 daily shadow report tooling."""

from __future__ import annotations

from typing import Any

from shared.markets.base_tools import BaseReport
from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import assert_no_live_broker, reject_real_execution_payload
from HK.common import HKConfig


class HKDailyReport(BaseReport):
    """Render a Markdown daily report for HK shadow activity."""

    def __init__(self, config: MarketToolConfig | None = None, lot_sizes: dict[str, int] | None = None) -> None:
        cfg = config or HKConfig()
        super().__init__("hk", cfg)
        assert_no_live_broker(cfg)
        self.lot_sizes = dict(lot_sizes or {})

    def render_daily(
        self,
        date: str,
        *,
        shadow_records: list[dict[str, Any]] | None = None,
        validation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        records = list(shadow_records or [])
        for record in records:
            reject_real_execution_payload(record, context="HKDailyReport.shadow_record")
        if validation:
            reject_real_execution_payload(validation, context="HKDailyReport.validation")

        scorecard = self.render_scorecard(date, shadow_records=records)
        markdown = [
            f"# HK Daily Shadow Report - {date}",
            "",
            "- Market: HK equities",
            "- Capital layer: shadow",
            "- Currency: HKD",
            f"- Shadow records: {len(records)}",
            "",
            "## Shadow Signals",
            "",
            "| Symbol | Lot Size | Strategy | Side | Score |",
            "|---|---:|---|---|---:|",
        ]
        if records:
            for record in records:
                symbol = str(record.get("symbol") or "")
                markdown.append(
                    "| {symbol} | {lot_size} | {strategy} | {side} | {score:.4f} |".format(
                        symbol=symbol,
                        lot_size=self.lot_size(symbol),
                        strategy=str(record.get("strategy_name") or record.get("strategy") or "unknown"),
                        side=str(record.get("side") or ""),
                        score=_to_float(record.get("score")),
                    )
                )
        else:
            markdown.append("| - | 0 | no_shadow_signal | - | 0.0000 |")

        return {
            "market": "hk",
            "date": date,
            "status": "ok",
            "currency": "HKD",
            "capital_layer": "shadow",
            "real_execution": False,
            "shadow_count": len(records),
            "scorecard": scorecard,
            "validation": validation or {},
            "markdown": "\n".join(markdown),
        }

    def render_scorecard(
        self,
        date: str,
        *,
        shadow_records: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        records = list(shadow_records or [])
        scores = [_to_float(record.get("score")) for record in records]
        return {
            "market": "hk",
            "date": date,
            "currency": "HKD",
            "capital_layer": "shadow",
            "total": len(records),
            "avg_score": round(sum(scores) / len(scores), 6) if scores else 0.0,
            "lot_sizes": {str(record.get("symbol") or ""): self.lot_size(str(record.get("symbol") or "")) for record in records},
            "real_execution": False,
        }

    def delivery_policy(self, result: dict[str, Any]) -> dict[str, Any]:
        reject_real_execution_payload(result, context="HKDailyReport.result")
        return {
            "send": False,
            "channel": "none",
            "reason": "p1_report_render_only",
            "real_execution": False,
        }

    def lot_size(self, symbol: str) -> int:
        return int(self.lot_sizes.get(symbol.upper(), 100))


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
