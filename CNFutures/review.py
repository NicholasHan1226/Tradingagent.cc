#!/usr/bin/env python3
"""Append-only review records for CN futures simulation runs."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REVIEW_PATH = (
    Path(__file__).resolve().parents[1]
    / "shared"
    / "review"
    / "data"
    / "cn_futures_sim_reviews.jsonl"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    styles: dict[str, dict[str, Any]] = defaultdict(lambda: {"filled_count": 0, "fee": 0.0, "margin_required": 0.0})
    for record in records:
        style = str(record.get("style") or "unknown")
        receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
        raw = receipt.get("raw_response") if isinstance(receipt, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        if str(receipt.get("status", "")).lower() == "filled":
            styles[style]["filled_count"] += 1
        styles[style]["fee"] += float(receipt.get("fee") or 0.0)
        styles[style]["margin_required"] += float(raw.get("margin_required") or 0.0)
    return {
        "filled_count": sum(item["filled_count"] for item in styles.values()),
        "styles": {style: dict(values) for style, values in styles.items()},
    }


def append_review(
    *,
    date: str,
    market: str,
    records: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    path: Path | None = None,
) -> dict[str, Any]:
    """Append one run review and return the persisted payload."""

    summary = summarize_records(records)
    payload: dict[str, Any] = {
        "date": date,
        "market": market,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "state": "degraded" if errors else "ok",
        "record_count": len(records),
        "error_count": len(errors),
        "errors": errors,
        "generated_at": _now_iso(),
        **summary,
    }
    target = path or DEFAULT_REVIEW_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return payload


__all__ = ["DEFAULT_REVIEW_PATH", "append_review", "summarize_records"]
