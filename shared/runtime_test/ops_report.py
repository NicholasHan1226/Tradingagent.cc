#!/usr/bin/env python3
"""tradingagent operations report: queues, failures, receipts and PnL."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "shared"
SIGNALS = ROOT / "signals"
OUT_DIR = SHARED / "review" / "ops"
LATEST = OUT_DIR / "tradings_ops_latest.json"
HISTORY = OUT_DIR / "tradings_ops_history.jsonl"
MARKETS = ("ashare", "pm", "us", "crypto", "cn_futures")
CHECKSUM_KEYS = {"payload_sha256", "receipt_sha256", "checksum", "sha256"}
RECEIPT_CHECKSUM_KEYS = ("receipt_sha256", "checksum", "sha256")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return rows
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows


def canonical_json(payload: dict[str, Any], drop_checksums: bool = False) -> bytes:
    data = {k: v for k, v in payload.items() if not (drop_checksums and k in CHECKSUM_KEYS)}
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def payload_sha256(payload: dict[str, Any], drop_checksums: bool = False) -> str:
    return hashlib.sha256(canonical_json(payload, drop_checksums)).hexdigest()


def market_of(card: dict[str, Any]) -> str:
    raw = str(card.get("market") or card.get("asset_class") or card.get("source_market") or "").lower()
    code = str(card.get("ts_code") or card.get("code") or card.get("symbol") or "").upper()
    if raw in MARKETS:
        return raw
    if raw in {"ashare", "a", "cn"} or code.endswith((".SH", ".SZ", ".BJ")):
        return "ashare"
    if raw in {"predictionmarkets", "polymarket", "pm"}:
        return "pm"
    if raw in {"cn_futures", "futures", "cnfutures"} or code.startswith("SIM-CNF-"):
        return "cn_futures"
    if raw in {"us", "usa"}:
        return "us"
    if raw in {"crypto", "cryptocurrency"} or "USDT" in code:
        return "crypto"
    return "unknown"


def cards(state: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((SIGNALS / state).glob("*.json")):
        data = read_json(path)
        if data:
            data.setdefault("_path", str(path.relative_to(ROOT)))
            rows.append(data)
    return rows



def shadow_cards(state: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((SIGNALS / "shadow" / state).glob("*.json")):
        data = read_json(path)
        if data:
            data.setdefault("_path", str(path.relative_to(ROOT)))
            rows.append(data)
    return rows


def shadow_queue_summary() -> dict[str, Any]:
    states = ("pending", "claimed", "running", "filled", "failed", "expired", "cancelled")
    by_market: dict[str, Any] = {m: {state: 0 for state in states} for m in MARKETS}
    by_market["unknown"] = {state: 0 for state in states}
    totals = {state: 0 for state in states}
    for state in states:
        for card in shadow_cards(state):
            market = market_of(card)
            by_market.setdefault(market, {s: 0 for s in states})[state] += 1
            totals[state] += 1
    return {"totals": totals, "by_market": by_market}

def queue_summary() -> dict[str, Any]:
    states = ("pending", "claimed", "running", "filled", "failed", "expired", "cancelled")
    by_market: dict[str, Any] = {m: {state: 0 for state in states} for m in MARKETS}
    by_market["unknown"] = {state: 0 for state in states}
    totals = {state: 0 for state in states}
    for state in states:
        for card in cards(state):
            market = market_of(card)
            by_market.setdefault(market, {s: 0 for s in states})[state] += 1
            totals[state] += 1
    return {"totals": totals, "by_market": by_market}


def classify_failure(card: dict[str, Any]) -> str:
    receipt = card.get("receipt") if isinstance(card.get("receipt"), dict) else {}
    text = " ".join(str(x or "") for x in [
        card.get("posthoc_reason"), card.get("reason"), card.get("status"), card.get("message"),
        receipt.get("status"), receipt.get("message"), receipt.get("failure_category"),
        receipt.get("confirmation_status"),
    ]).lower()
    if "posthoc" in text or "false_positive" in text or "假阳" in text:
        return "posthoc_false_positive"
    if "unsupported" in text or "不支持" in text:
        return "code_unsupported"
    if "timeout" in text or "timed out" in text or "超时" in text:
        return "timeout"
    if "max_attempt" in text or "attempt" in text and "exceed" in text:
        return "max_attempts_exceeded"
    if "position" in text or "持仓" in text:
        return "position_mismatch"
    if "confirm" in text or "unconfirmed" in text or "未确认" in text:
        return "confirmation_unverified"
    if "vision" in text or "button" in text or "click" in text or "gui" in text or "ax" in text:
        return "gui_automation_failure"
    if "network" in text or "sync" in text or "webhook" in text:
        return "network_or_sync"
    if card.get("_path", "").startswith("signals/expired"):
        return "expired"
    return "other"


def failure_review() -> dict[str, Any]:
    source_states = ("failed", "expired", "cancelled")
    by_category: Counter[str] = Counter()
    by_market: dict[str, Counter[str]] = defaultdict(Counter)
    examples: list[dict[str, Any]] = []
    total = 0
    for state in source_states:
        for card in cards(state):
            total += 1
            category = classify_failure(card)
            market = market_of(card)
            by_category[category] += 1
            by_market[market][category] += 1
            if len(examples) < 12 and state in {"failed", "expired"}:
                receipt = card.get("receipt") if isinstance(card.get("receipt"), dict) else {}
                examples.append({
                    "state": state,
                    "market": market,
                    "category": category,
                    "order_id": card.get("order_id") or card.get("id") or receipt.get("order_id"),
                    "symbol": card.get("ts_code") or card.get("symbol") or card.get("code"),
                    "message": str(card.get("message") or receipt.get("message") or card.get("reason") or "")[:180],
                })
    return {
        "total_reviewed": total,
        "by_category": dict(by_category),
        "by_market": {m: dict(c) for m, c in by_market.items()},
        "examples": examples,
    }


def receipt_integrity(paths: list[Path] | None = None) -> dict[str, Any]:
    paths = paths or [
        ROOT.parent / "MarketGraph" / "outputs" / "sim_execution_receipts.jsonl",
        SIGNALS / "sim_execution_receipts.jsonl",
    ]
    total = signed = unsigned = invalid = payload_linked = 0
    by_path: list[dict[str, Any]] = []
    for path in paths:
        rows = read_jsonl(path)
        path_invalid = path_signed = path_unsigned = path_payload_linked = 0
        for row in rows:
            total += 1
            if row.get("payload_sha256"):
                payload_linked += 1
                path_payload_linked += 1
            embedded = next((str(row.get(k) or "") for k in RECEIPT_CHECKSUM_KEYS if row.get(k)), "")
            if not embedded:
                unsigned += 1
                path_unsigned += 1
                continue
            if embedded == payload_sha256(row, drop_checksums=True):
                signed += 1
                path_signed += 1
            else:
                invalid += 1
                path_invalid += 1
        by_path.append({
            "path": str(path),
            "rows": len(rows),
            "signed": path_signed,
            "unsigned": path_unsigned,
            "invalid": path_invalid,
            "payload_linked": path_payload_linked,
        })
    return {"total": total, "signed": signed, "unsigned": unsigned, "invalid": invalid, "payload_linked": payload_linked, "by_path": by_path}



def reviewed_summary() -> dict[str, Any]:
    root = ROOT / "signals_archive" / "reviewed"
    batches: list[dict[str, Any]] = []
    totals = {"failed": 0, "expired": 0}
    if not root.exists():
        return {"batch_count": 0, "totals": totals, "latest_batches": []}
    for batch in sorted([p for p in root.iterdir() if p.is_dir()], reverse=True):
        counts = {"failed": len(list((batch / "failed").glob("*.json"))), "expired": len(list((batch / "expired").glob("*.json")))}
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
        manifest = read_json(batch / "manifest.json")
        batches.append({
            "batch_id": batch.name,
            "record_count": manifest.get("record_count", sum(counts.values())),
            "reason": manifest.get("reason", ""),
            "counts": counts,
            "generated_at": manifest.get("generated_at", ""),
        })
    return {"batch_count": len(batches), "totals": totals, "latest_batches": batches[:5]}

def pnl_summary() -> dict[str, Any]:
    local: dict[str, Any] = {}
    try:
        from shared.execution.local_sim_ledger import get_local_sim_pnl
        local = get_local_sim_pnl()
    except Exception as exc:  # pragma: no cover - defensive runtime report
        local = {"error": str(exc)}
    shadow = read_json(SHARED / "logs" / "shadow" / "shadow_pnl.json")
    attribution = read_json(SHARED / "review" / "attribution" / "strategy_attribution_latest.json")
    if not attribution:
        attribution = read_json(SHARED / "review" / "attribution" / "strategy_attribution.json")
    return {"server_local_sim": local, "shadow": shadow, "strategy_attribution": attribution}


def cn_futures_review_summary(path: Path | None = None) -> dict[str, Any]:
    """Summarize CNFutures append-only simulated reviews for ops/dashboard consumers."""

    review_path = path or (SHARED / "review" / "data" / "cn_futures_sim_reviews.jsonl")
    rows = read_jsonl(review_path)
    latest = rows[-1] if rows else {}
    style_totals: dict[str, dict[str, Any]] = defaultdict(lambda: {"filled_count": 0, "error_count": 0})
    error_counter: Counter[str] = Counter()
    for row in rows:
        styles = row.get("styles") if isinstance(row.get("styles"), dict) else {}
        for style, values in styles.items():
            if isinstance(values, dict):
                style_totals[str(style)]["filled_count"] += int(values.get("filled_count") or 0)
        error_summary = row.get("error_summary") if isinstance(row.get("error_summary"), dict) else {}
        by_error = error_summary.get("by_error") if isinstance(error_summary.get("by_error"), dict) else {}
        for name, count in by_error.items():
            error_counter[str(name)] += int(count or 0)
        by_style = error_summary.get("by_style") if isinstance(error_summary.get("by_style"), dict) else {}
        for style, values in by_style.items():
            if isinstance(values, dict):
                style_totals[str(style)]["error_count"] += int(values.get("error_count") or 0)
    return {
        "path": str(review_path),
        "exists": review_path.exists(),
        "review_rows": len(rows),
        "latest_generated_at": latest.get("generated_at", ""),
        "latest_state": latest.get("state", ""),
        "latest_date": latest.get("date", ""),
        "latest_record_count": int(latest.get("record_count") or 0) if latest else 0,
        "latest_filled_count": int(latest.get("filled_count") or 0) if latest else 0,
        "latest_error_count": int(latest.get("error_count") or 0) if latest else 0,
        "latest_error_summary": latest.get("error_summary") if isinstance(latest.get("error_summary"), dict) else {},
        "latest_style_health": latest.get("style_health") if isinstance(latest.get("style_health"), dict) else {},
        "style_totals": {style: dict(values) for style, values in style_totals.items()},
        "top_errors": dict(error_counter.most_common(10)),
    }


def overall_status(queue: dict[str, Any], failures: dict[str, Any], receipts: dict[str, Any]) -> str:
    totals = queue.get("totals") or {}
    shadow_totals = (queue.get("shadow_totals") or {})
    if int(receipts.get("invalid", 0) or 0) > 0:
        return "fail"
    if int(totals.get("running", 0) or 0) > 0:
        return "fail"
    if int(totals.get("failed", 0) or 0) > 0 or int(totals.get("expired", 0) or 0) > 0:
        return "warn"
    if int(totals.get("pending", 0) or 0) > 200:
        return "warn"
    return "pass"


def build_ops_report() -> dict[str, Any]:
    queue = queue_summary()
    shadow_queue = shadow_queue_summary()
    failures = failure_review()
    receipts = receipt_integrity()
    pnl = pnl_summary()
    cn_futures = cn_futures_review_summary()
    reviewed = reviewed_summary()
    status = overall_status(queue, failures, receipts)
    return {
        "generated_at": now_iso(),
        "report_type": "tradings_ops",
        "overall_status": status,
        "queue_summary": queue,
        "shadow_queue_summary": shadow_queue,
        "failure_summary": failures,
        "receipt_integrity": receipts,
        "pnl_summary": pnl,
        "cn_futures_review_summary": cn_futures,
        "reviewed_summary": reviewed,
        "recommendations": recommendations(status, queue, failures, receipts),
    }


def recommendations(status: str, queue: dict[str, Any], failures: dict[str, Any], receipts: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    totals = queue.get("totals") or {}
    shadow_totals = (queue.get("shadow_totals") or {})
    if int(receipts.get("invalid", 0) or 0) > 0:
        notes.append("回执校验失败：需要检查 mini/Hermes 回执是否被截断或字段被改写。")
    if int(totals.get("running", 0) or 0) > 0:
        notes.append("存在 running 状态订单：需要确认执行器是否卡在处理中或回调未写回。")
    if int(totals.get("failed", 0) or 0) > 0:
        top = failures.get("by_category") or {}
        top_name = max(top, key=top.get) if top else "unknown"
        notes.append(f"存在失败订单：优先复盘最高频原因 {top_name}。")
    if int(totals.get("pending", 0) or 0) > 200:
        notes.append("执行 pending 队列过大：需要检查过期清理或同日去重是否失效。")
    if not notes and status == "pass":
        notes.append("当前队列、回执和失败分类未发现阻塞级异常。")
    return notes


def write_report(report: dict[str, Any]) -> dict[str, str]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False) + "\n")
    return {"latest": str(LATEST), "history": str(HISTORY)}


def _email_text(report: dict[str, Any]) -> tuple[str, str, str]:
    status = report.get("overall_status", "unknown")
    totals = (report.get("queue_summary") or {}).get("totals") or {}
    shadow_totals = (report.get("shadow_queue_summary") or {}).get("totals") or {}
    failures = report.get("failure_summary") or {}
    receipts = report.get("receipt_integrity") or {}
    subject = f"tradingagent 系统异常复盘 {status} {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    lines = [
        f"状态: {status}",
        f"生成时间: {report.get('generated_at')}",
        f"执行队列: pending={totals.get('pending', 0)}, running={totals.get('running', 0)}, filled={totals.get('filled', 0)}, failed={totals.get('failed', 0)}, expired={totals.get('expired', 0)}",
        f"影子队列: pending={shadow_totals.get('pending', 0)}, running={shadow_totals.get('running', 0)}, filled={shadow_totals.get('filled', 0)}, failed={shadow_totals.get('failed', 0)}, expired={shadow_totals.get('expired', 0)}",
        f"回执: total={receipts.get('total', 0)}, signed={receipts.get('signed', 0)}, unsigned={receipts.get('unsigned', 0)}, invalid={receipts.get('invalid', 0)}",
        f"失败分类: {json.dumps(failures.get('by_category') or {}, ensure_ascii=False)}",
        "建议:",
    ]
    lines.extend(f"- {item}" for item in report.get("recommendations") or [])
    body = "\n".join(lines)
    html = "<br/>".join(line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for line in lines)
    return subject, body, f"<!DOCTYPE html><html><body><pre>{html}</pre></body></html>"


def send_alert_if_needed(report: dict[str, Any], send_on: str = "fail") -> dict[str, Any]:
    status = str(report.get("overall_status") or "unknown")
    levels = {"pass": 0, "warn": 1, "fail": 2}
    threshold = levels.get(send_on, 99)
    if send_on == "never" or levels.get(status, 0) < threshold:
        return {"status": "skipped", "reason": f"status={status}, send_on={send_on}"}
    from shared.notify.email_sender import send_email
    subject, body, html = _email_text(report)
    return send_email("soc@coze.email", subject, body, html, channel="system", rate_limit_type="tradings_ops_report")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--send-on", choices=("never", "warn", "fail"), default="never")
    args = parser.parse_args()
    report = build_ops_report()
    paths = write_report(report)
    email = send_alert_if_needed(report, args.send_on)
    report["written_paths"] = paths
    report["email"] = email
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
