#!/usr/bin/env python3
"""US P1 daily shadow report tooling."""

from __future__ import annotations

from typing import Any

from shared.markets.base_tools import BaseReport
from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import assert_no_live_broker, reject_real_execution_payload
from US.common import USConfig


class USDailyReport(BaseReport):
    """Render a Markdown daily report for US shadow activity."""

    def __init__(self, config: MarketToolConfig | None = None) -> None:
        cfg = config or USConfig()
        super().__init__("us", cfg)
        assert_no_live_broker(cfg)

    def render_daily(
        self,
        date: str,
        *,
        shadow_records: list[dict[str, Any]] | None = None,
        validation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        records = list(shadow_records or [])
        for record in records:
            reject_real_execution_payload(record, context="USDailyReport.shadow_record")
        if validation:
            reject_real_execution_payload(validation, context="USDailyReport.validation")

        scorecard = self.render_scorecard(date, shadow_records=records)
        markdown = [
            f"# US Daily Shadow Report - {date}",
            "",
            "- Market: US equities",
            "- Capital layer: shadow",
            "- Currency: USD",
            f"- Shadow records: {len(records)}",
            f"- Validation: {(validation or {}).get('status', 'not_available')}",
            "",
            "## Shadow Signals",
            "",
            "| Symbol | Strategy | Side | Score |",
            "|---|---|---|---:|",
        ]
        if records:
            for record in records:
                markdown.append(
                    "| {symbol} | {strategy} | {side} | {score:.4f} |".format(
                        symbol=str(record.get("symbol") or ""),
                        strategy=str(record.get("strategy_name") or record.get("strategy") or "unknown"),
                        side=str(record.get("side") or ""),
                        score=_to_float(record.get("score")),
                    )
                )
        else:
            markdown.append("| - | no_shadow_signal | - | 0.0000 |")

        return {
            "market": "us",
            "date": date,
            "status": "ok",
            "currency": "USD",
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
            "market": "us",
            "date": date,
            "currency": "USD",
            "capital_layer": "shadow",
            "total": len(records),
            "avg_score": round(sum(scores) / len(scores), 6) if scores else 0.0,
            "strategies": sorted(
                {str(record.get("strategy_name") or record.get("strategy") or "unknown") for record in records}
            ),
            "real_execution": False,
        }

    def delivery_policy(self, result: dict[str, Any]) -> dict[str, Any]:
        reject_real_execution_payload(result, context="USDailyReport.result")
        return {
            "send": False,
            "channel": "none",
            "reason": "p1_report_render_only",
            "real_execution": False,
        }


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
