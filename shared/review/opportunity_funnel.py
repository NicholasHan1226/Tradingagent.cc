#!/usr/bin/env python3
"""Append-only opportunity funnel events for dashboard/review playback."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVENT_LOG_RELATIVE_PATH = Path("shared/review/opportunities/funnel_events.jsonl")

STAGE_SEQUENCE = {
    "发现": 1,
    "研判": 2,
    "风控": 3,
    "待确认": 4,
    "结果": 5,
}

STAGE_ALIASES = {
    "discover": "发现",
    "discovered": "发现",
    "scan": "发现",
    "market_scan": "发现",
    "found": "发现",
    "进入": "发现",
    "发现": "发现",
    "research": "研判",
    "score": "研判",
    "scored": "研判",
    "debate": "研判",
    "debated": "研判",
    "formed": "研判",
    "研判": "研判",
    "评分": "研判",
    "risk": "风控",
    "risk_checked": "风控",
    "riskcheck": "风控",
    "blocked": "风控",
    "rejected": "风控",
    "风控": "风控",
    "pending": "待确认",
    "waiting": "待确认",
    "triggered": "待确认",
    "queued": "待确认",
    "待确认": "待确认",
    "待执行": "待确认",
    "result": "结果",
    "filled": "结果",
    "executed": "结果",
    "missed": "结果",
    "cancelled": "结果",
    "canceled": "结果",
    "expired": "结果",
    "partial": "结果",
    "failed": "结果",
    "结果": "结果",
    "成交": "结果",
    "机会": "结果",
    "复盘": "结果",
}

STATUS_ALIASES = {
    "enter": "进入",
    "entered": "进入",
    "discover": "进入",
    "discovered": "进入",
    "found": "进入",
    "进入": "进入",
    "pass": "通过",
    "passed": "通过",
    "ok": "通过",
    "approved": "通过",
    "通过": "通过",
    "pending": "等待",
    "waiting": "等待",
    "queued": "等待",
    "triggered": "等待",
    "等待": "等待",
    "filled": "成交",
    "executed": "成交",
    "done": "成交",
    "成交": "成交",
    "missed": "机会",
    "opportunity": "机会",
    "机会": "机会",
    "blocked": "拦截",
    "reject": "拦截",
    "rejected": "拦截",
    "failed": "拦截",
    "partial": "拦截",
    "拦截": "拦截",
    "review": "复盘",
    "reviewed": "复盘",
    "expired": "复盘",
    "cancelled": "复盘",
    "canceled": "复盘",
    "复盘": "复盘",
}

TERMINAL_STATUSES = {"成交", "机会", "拦截", "复盘"}


def event_log_path(root: Path | str, path: Path | str | None = None) -> Path:
    """Return the canonical backend event log path."""
    if path is not None:
        return Path(path)
    return Path(root) / EVENT_LOG_RELATIVE_PATH


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_stage(value: Any, *, status: Any = None) -> str:
    raw = _clean(value)
    if raw:
        mapped = STAGE_ALIASES.get(raw.lower()) or STAGE_ALIASES.get(raw)
        if mapped:
            return mapped
    status_text = _clean(status)
    if status_text:
        mapped = STAGE_ALIASES.get(status_text.lower()) or STAGE_ALIASES.get(status_text)
        if mapped:
            return mapped
    return "发现"


def normalize_status(value: Any, *, stage: str) -> str:
    raw = _clean(value)
    if raw:
        mapped = STATUS_ALIASES.get(raw.lower()) or STATUS_ALIASES.get(raw)
        if mapped:
            return mapped
    if stage == "发现":
        return "进入"
    if stage in {"研判", "风控"}:
        return "通过"
    if stage == "待确认":
        return "等待"
    return "复盘"


def normalize_event_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize one raw event row into the frontend-compatible JSONL schema."""
    symbol = _first_text(row, "symbol", "ts_code", "code")
    if not symbol:
        raise ValueError("opportunity event requires symbol or ts_code")
    market = _first_text(row, "market", "asset_class", default=_infer_market(symbol))
    opportunity_id = _first_text(row, "opportunity_id", "opportunityId", "signal_id", "trace_id", "order_id", "card_id", default=symbol)
    timestamp = _first_text(row, "timestamp", "at", "ts", "created_at", "updated_at", default=now_iso())
    stage = normalize_stage(row.get("stage"), status=row.get("status"))
    status = normalize_status(row.get("status"), stage=stage)
    sequence = _safe_int(row.get("sequence"), STAGE_SEQUENCE[stage])
    label = _first_text(row, "label", default=_default_label(stage, status))
    reason = _first_text(row, "reason", "message", "failure_reason", default="")
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    terminal = _safe_bool(row.get("terminal"), stage == "结果" or status in TERMINAL_STATUSES)
    normalized = {
        "event_id": _first_text(row, "event_id", "id", default=""),
        "opportunity_id": opportunity_id,
        "symbol": symbol,
        "market": market,
        "stage": stage,
        "status": status,
        "sequence": sequence,
        "label": label,
        "timestamp": timestamp,
        "terminal": terminal,
        "source": _first_text(row, "source", default="opportunity_funnel_writer"),
        "metadata": metadata,
    }
    latency = _safe_number(row.get("latency_minutes", row.get("latencyMinutes")))
    if latency is not None:
        normalized["latency_minutes"] = latency
    if reason:
        normalized["reason"] = reason
    if not normalized["event_id"]:
        normalized["event_id"] = stable_event_id(normalized)
    return normalized


def stable_event_id(row: dict[str, Any]) -> str:
    raw = "|".join(
        str(row.get(key, ""))
        for key in ("opportunity_id", "symbol", "market", "stage", "status", "timestamp")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def append_opportunity_event(
    root: Path | str,
    *,
    opportunity_id: str,
    symbol: str,
    market: str,
    stage: str,
    status: str,
    label: str | None = None,
    timestamp: str | None = None,
    sequence: int | None = None,
    latency_minutes: float | int | None = None,
    terminal: bool | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
    event_id: str | None = None,
    path: Path | str | None = None,
) -> dict[str, Any]:
    row = normalize_event_row(
        {
            "event_id": event_id,
            "opportunity_id": opportunity_id,
            "symbol": symbol,
            "market": market,
            "stage": stage,
            "status": status,
            "label": label,
            "timestamp": timestamp,
            "sequence": sequence,
            "latency_minutes": latency_minutes,
            "terminal": terminal,
            "reason": reason,
            "metadata": metadata or {},
        }
    )
    target = event_log_path(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return row


def read_event_rows(root: Path | str, path: Path | str | None = None) -> list[dict[str, Any]]:
    target = event_log_path(root, path)
    rows: list[dict[str, Any]] = []
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return rows
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _first_text(row: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        text = _clean(row.get(key))
        if text:
            return text
    return default


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _safe_number(value: Any) -> float | int | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return int(parsed) if parsed.is_integer() else parsed


def _safe_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


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


def _default_label(stage: str, status: str) -> str:
    if status == "成交":
        return "形成收益"
    if status == "机会":
        return "机会保留"
    if status == "拦截":
        return "风险拦截"
    if status == "复盘":
        return "进入复盘"
    if stage == "发现":
        return "发现机会"
    if stage == "研判":
        return "形成理由"
    if stage == "风控":
        return "通过风控"
    if stage == "待确认":
        return "等待确认"
    return "形成结果"


__all__ = [
    "EVENT_LOG_RELATIVE_PATH",
    "STAGE_SEQUENCE",
    "append_opportunity_event",
    "event_log_path",
    "normalize_event_row",
    "normalize_stage",
    "normalize_status",
    "read_event_rows",
    "stable_event_id",
]
