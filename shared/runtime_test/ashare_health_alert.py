#!/usr/bin/env python3
"""Run A-share health checks and email system channel only on anomaly."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import Any

from shared.notify import email_sender
from shared.runtime_test.market_health import ROOT, run_ashare_health

HISTORY = ROOT / "shared/runtime_test/ashare_health_history.jsonl"
LATEST = ROOT / "shared/runtime_test/ashare_health_latest.json"


def _append_history(payload: dict[str, Any]) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _plain_summary(result: dict[str, Any]) -> str:
    bad = [c for c in result.get("checks", []) if c.get("status") != "pass"]
    lines = [
        f"A股健康检查状态: {result.get('overall_status')}",
        f"生成时间: {result.get('generated_at')}",
        f"通过/警告/失败: {result.get('summary', {}).get('pass')}/{result.get('summary', {}).get('warn')}/{result.get('summary', {}).get('fail')}",
    ]
    for check in bad[:12]:
        lines.append(f"- {check.get('name')}: {check.get('status')} - {check.get('message')}")
    return "\n".join(lines)


def _send_alert(result: dict[str, Any]) -> dict[str, Any]:
    status = result.get("overall_status")
    critical = status == "fail"
    failed = [c for c in result.get("checks", []) if c.get("status") == "fail"]
    warned = [c for c in result.get("checks", []) if c.get("status") == "warn"]
    data = {
        "overall_status": "critical" if critical else "degraded",
        "summary": _plain_summary(result),
        "collection": {
            "status": "ok",
            "sources": "SharedSignals API / MarketGraph API / Hermes health",
            "last_update": result.get("generated_at", "--"),
            "gaps": ", ".join(c.get("name", "") for c in failed + warned) or "无",
        },
        "pipeline": {
            "status": "failed" if critical else "degraded",
            "stages": "A股 runtime health",
            "failed_stages": ", ".join(c.get("name", "") for c in failed) or "无",
            "last_run": result.get("generated_at", "--"),
        },
        "integrity": {
            "status": "failed" if critical else "partial",
            "checks_passed": result.get("summary", {}).get("pass", 0),
            "checks_failed": result.get("summary", {}).get("fail", 0),
            "details": _plain_summary(result).replace("\n", "<br>"),
        },
    }
    subject = f"[A股][系统] 健康检查异常 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    return email_sender.send_template_email("system_health", data, subject=subject)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A-share health alert runner")
    parser.add_argument("--mini-health-url", default="http://127.0.0.1:9865/health")
    parser.add_argument("--send-on", choices=["warn", "fail", "never"], default="warn")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    result = run_ashare_health(mini_health_url=args.mini_health_url)
    LATEST.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _append_history(result)

    should_send = args.send_on == "warn" and result.get("overall_status") != "pass"
    should_send = should_send or (args.send_on == "fail" and result.get("overall_status") == "fail")
    email_result = {"status": "skipped", "reason": "health_pass_or_send_disabled"}
    if should_send:
        email_result = _send_alert(result)
    print(json.dumps({"health": result, "email": email_result}, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
