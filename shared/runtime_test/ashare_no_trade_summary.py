#!/usr/bin/env python3
"""Summarize A-share simulated no-trade attribution logs."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CN_TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[2]
NO_TRADE_LOG = ROOT / "shared/logs/ashare_no_trade_explanations.jsonl"
TRADE_LOG = ROOT / "shared/logs/local_sim/local_sim_trades.jsonl"
LATEST_REPORT = ROOT / "shared/runtime_test/ashare_no_trade_summary_latest.json"


def _today_compact(now: datetime | None = None) -> str:
    current = now or datetime.now(CN_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CN_TZ)
    return current.astimezone(CN_TZ).strftime("%Y%m%d")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _compact_date(payload: dict[str, Any]) -> str:
    for key in ("date", "trade_date"):
        value = str(payload.get(key) or "").replace("-", "")[:8]
        if len(value) == 8 and value.isdigit():
            return value
    generated_at = str(payload.get("generated_at") or payload.get("timestamp") or payload.get("filled_at") or payload.get("created_at") or "")
    if len(generated_at) >= 10:
        value = generated_at[:10].replace("-", "")
        if len(value) == 8 and value.isdigit():
            return value
    return ""


def _explanation(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("no_trade_explanation")
    return value if isinstance(value, dict) else payload


def _count_range(rows: list[dict[str, Any]], key: str) -> dict[str, int | None]:
    values: list[int] = []
    for payload in rows:
        counts = _explanation(payload).get("counts")
        if not isinstance(counts, dict):
            continue
        try:
            values.append(int(counts.get(key) or 0))
        except (TypeError, ValueError):
            continue
    if not values:
        return {"min": None, "max": None, "latest": None}
    return {"min": min(values), "max": max(values), "latest": values[-1]}


def summarize_no_trade_log(path: Path = NO_TRADE_LOG, trade_date: str | None = None) -> dict[str, Any]:
    target_date = (trade_date or _today_compact()).replace("-", "")[:8]
    rows = [row for row in _read_jsonl(path) if _compact_date(row) == target_date]
    latest = rows[-1] if rows else {}
    latest_explanation = _explanation(latest) if latest else {}
    category_counts = Counter(str(_explanation(row).get("category") or "unknown") for row in rows)
    counts = latest_explanation.get("counts") if isinstance(latest_explanation.get("counts"), dict) else {}
    evidence_gaps: list[str] = []
    if rows and int(counts.get("candidates") or 0) > 0 and int(counts.get("orders") or 0) <= 0:
        if not latest_explanation.get("candidate_decision_trace"):
            evidence_gaps.append("candidate_decision_trace_missing")
        if not latest_explanation.get("capital_plan_decision"):
            evidence_gaps.append("capital_plan_decision_missing")
        if not latest_explanation.get("portfolio_decision"):
            evidence_gaps.append("portfolio_decision_missing")
    return {
        "report_type": "ashare_no_trade_summary",
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "date": target_date,
        "source": str(path),
        "row_count": len(rows),
        "category_counts": dict(category_counts),
        "latest_no_trade_log": latest_explanation,
        "count_ranges": {
            "universe": _count_range(rows, "universe"),
            "candidates": _count_range(rows, "candidates"),
            "orders": _count_range(rows, "orders"),
            "risk_rejections": _count_range(rows, "risk_rejections"),
            "skipped_candidates": _count_range(rows, "skipped_candidates"),
            "execution_skips": _count_range(rows, "execution_skips"),
        },
        "evidence_status": "incomplete" if evidence_gaps else ("ready" if rows else "no_rows"),
        "evidence_gaps": evidence_gaps,
        "trade_source_check": summarize_trade_source_check(TRADE_LOG, target_date),
        "read_only": True,
        "real_trading_enabled": False,
    }


def summarize_trade_source_check(path: Path = TRADE_LOG, trade_date: str | None = None) -> dict[str, Any]:
    target_date = (trade_date or _today_compact()).replace("-", "")[:8]
    rows = [row for row in _read_jsonl(path) if _compact_date(row) == target_date]
    filled_rows = [row for row in rows if _is_filled_trade(row)]
    invalid_rows: list[dict[str, Any]] = []
    missing_source_count = 0
    invalid_layer_count = 0

    for index, row in enumerate(filled_rows):
        source = str(row.get("execution_source") or "").strip()
        side = str(row.get("side") or "").strip().lower()
        layer = str(row.get("candidate_pool_layer") or "").strip().lower()
        issue: list[str] = []
        if not source:
            missing_source_count += 1
            issue.append("execution_source_missing")
        if side == "buy" and layer != "candidate":
            invalid_layer_count += 1
            issue.append("buy_candidate_layer_invalid")
        if side == "sell" and source != "ashare_rebalance_sell" and layer != "ashare_rebalance_sell":
            invalid_layer_count += 1
            issue.append("sell_source_invalid")
        if issue:
            invalid_rows.append({
                "index": index,
                "symbol": row.get("ts_code") or row.get("symbol"),
                "side": side or None,
                "execution_source": source or None,
                "candidate_pool_layer": layer or None,
                "issues": issue,
            })

    status = "no_rows" if not filled_rows else ("incomplete" if invalid_rows else "ready")
    return {
        "status": status,
        "filled_count": len(filled_rows),
        "missing_source_count": missing_source_count,
        "invalid_layer_count": invalid_layer_count,
        "invalid_rows": invalid_rows[:5],
    }


def _is_filled_trade(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "").lower()
    return status in {"filled", "executed"} or bool(row.get("filled_at"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-path", type=Path, default=NO_TRADE_LOG)
    parser.add_argument("--date", default=None, help="YYYYMMDD or YYYY-MM-DD; default today China time")
    parser.add_argument("--write-latest", action="store_true", help="Write shared/runtime_test/ashare_no_trade_summary_latest.json")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    report = summarize_no_trade_log(args.log_path, args.date)
    if args.write_latest:
        LATEST_REPORT.parent.mkdir(parents=True, exist_ok=True)
        LATEST_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
