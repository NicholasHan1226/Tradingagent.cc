#!/usr/bin/env python3
"""Sync signal cards into explicit opportunity funnel events for the dashboard."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from shared.review.opportunity_funnel import (
    STAGE_SEQUENCE,
    event_log_path,
    normalize_event_row,
    read_event_rows,
)

ROOT = Path(__file__).resolve().parents[2]
SIGNALS = ROOT / "signals"
SIGNAL_STATES = ("pending", "claimed", "running", "filled", "partial", "failed", "expired", "cancelled")


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def signal_cards(root: Path) -> list[tuple[str, Path, dict[str, Any]]]:
    signals = root / "signals"
    rows: list[tuple[str, Path, dict[str, Any]]] = []
    for state in SIGNAL_STATES:
        for path in sorted((signals / state).glob("*.json")):
            card = read_json(path)
            if card:
                rows.append((state, path, card))
    return rows


def sync_opportunity_funnel_events(root: Path | str = ROOT, *, apply: bool = False) -> dict[str, Any]:
    project_root = Path(root)
    existing_ids = {str(row.get("event_id") or "") for row in read_event_rows(project_root)}
    planned: list[dict[str, Any]] = []
    duplicate_count = 0
    cards = signal_cards(project_root)
    for bucket, path, card in cards:
        for event in events_from_signal_card(bucket, card):
            if event["event_id"] in existing_ids:
                duplicate_count += 1
                continue
            planned.append(event)
            existing_ids.add(str(event["event_id"]))

    if apply and planned:
        target = event_log_path(project_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            for row in planned:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")

    by_stage: dict[str, int] = {stage: 0 for stage in STAGE_SEQUENCE}
    by_status: dict[str, int] = {}
    for row in planned:
        by_stage[row["stage"]] = by_stage.get(row["stage"], 0) + 1
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    return {
        "applied": apply,
        "cards_reviewed": len(cards),
        "events_planned": len(planned),
        "events_written": len(planned) if apply else 0,
        "events_skipped_existing": duplicate_count,
        "target_path": str(event_log_path(project_root)),
        "by_stage": by_stage,
        "by_status": by_status,
    }


def events_from_signal_card(bucket: str, card: dict[str, Any]) -> list[dict[str, Any]]:
    symbol = _first(card, "ts_code", "symbol", "code")
    if not symbol:
        return []
    market = _first(card, "market", "asset_class", default=_infer_market(symbol))
    opportunity_id = _first(card, "order_id", "idempotency_key", "signal_id", "id", default=symbol)
    base_metadata = {
        "bucket": bucket,
        "strategy_name": _first(card, "strategy_name", "method", "strategy"),
        "capital_layer": _first(card, "capital_layer", "account_type"),
        "source": _first(card, "source", "signal_source"),
    }
    specs: list[tuple[str, str, str, str, str]] = [
        ("发现", "进入", "发现机会", _first(card, "timestamp", "created_at", "received_at"), ""),
    ]
    scored_at = _first(card, "debated_at", "scored_at")
    if scored_at or _has_score(card):
        specs.append(("研判", "通过", "形成理由", scored_at or _first(card, "timestamp", "created_at"), ""))
    risk_at = _risk_checked_at(card)
    if risk_at or isinstance(card.get("risk_check"), dict):
        risk_passed = _risk_passed(card)
        specs.append(("风控", "通过" if risk_passed else "拦截", "风险检查", risk_at or scored_at, _risk_reason(card)))
    final_stage = _final_stage(bucket, card)
    if final_stage:
        specs.append(final_stage)

    rows: list[dict[str, Any]] = []
    for stage, status, label, timestamp, reason in specs:
        rows.append(
            normalize_event_row(
                {
                    "opportunity_id": opportunity_id,
                    "symbol": symbol,
                    "market": market,
                    "stage": stage,
                    "status": status,
                    "label": label,
                    "timestamp": timestamp or _first(card, "timestamp", "created_at", "filled_at"),
                    "reason": reason,
                    "metadata": base_metadata,
                }
            )
        )
    return rows


def _final_stage(bucket: str, card: dict[str, Any]) -> tuple[str, str, str, str, str] | None:
    status = str(card.get("status") or bucket).strip().lower()
    timestamp = _first(card, "filled_at", "fill_time", "triggered_at", "updated_at", "timestamp")
    if bucket in {"filled"} or status in {"filled", "executed"}:
        return ("结果", "成交", "形成收益", timestamp, "")
    if bucket in {"partial", "failed"} or status in {"partial", "failed", "rejected"}:
        return ("结果", "拦截", "风险拦截", timestamp, _first(card, "failure_reason", "reason", "message"))
    if bucket in {"expired", "cancelled"} or status in {"expired", "cancelled", "canceled"}:
        return ("结果", "复盘", "机会复盘", timestamp, _first(card, "cancel_reason", "reason", "message"))
    if bucket in {"pending", "claimed", "running"} or status in {"pending", "claimed", "running"}:
        return ("待确认", "等待", "等待确认", _first(card, "triggered_at", "timestamp"), "")
    return None


def _first(card: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = card.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def _has_score(card: dict[str, Any]) -> bool:
    return any(key in card for key in ("score", "confidence", "belief_score", "scores")) or isinstance(card.get("signal"), dict)


def _risk_checked_at(card: dict[str, Any]) -> str:
    risk = card.get("risk_check") if isinstance(card.get("risk_check"), dict) else {}
    return _first(card, "risk_checked_at", default=str(risk.get("checked_at") or risk.get("timestamp") or "").strip())


def _risk_passed(card: dict[str, Any]) -> bool:
    risk = card.get("risk_check") if isinstance(card.get("risk_check"), dict) else {}
    if "passed" in risk:
        return bool(risk.get("passed"))
    return str(card.get("status") or "").lower() not in {"failed", "rejected", "partial"}


def _risk_reason(card: dict[str, Any]) -> str:
    risk = card.get("risk_check") if isinstance(card.get("risk_check"), dict) else {}
    return str(risk.get("reason") or card.get("failure_reason") or card.get("reason") or card.get("message") or "").strip()


def _infer_market(symbol: str) -> str:
    upper = symbol.upper()
    if upper.endswith((".SH", ".SZ", ".BJ")):
        return "A-share"
    if upper.endswith(".HK"):
        return "HK"
    if upper.endswith(".US"):
        return "US"
    if "USD" in upper or "USDT" in upper or "PERP" in upper:
        return "Crypto"
    return "All Markets"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    payload = sync_opportunity_funnel_events(Path(args.root), apply=bool(args.apply))
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
