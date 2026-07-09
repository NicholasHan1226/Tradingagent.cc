#!/usr/bin/env python3
"""TradingAgent gate for SharedSignals /source_status.

TradingAgent may continue on a yellow SharedSignals source status, but trading
jobs must fail closed when the data-source governance status is red or cannot
be checked.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any, Callable


HttpJsonFunc = Callable[[str, float], tuple[int, dict[str, Any]]]


def http_json(url: str, timeout_seconds: float) -> tuple[int, dict[str, Any]]:
    with urllib.request.urlopen(url, timeout=timeout_seconds) as resp:
        body = resp.read(65536).decode("utf-8", errors="replace")
        status_code = int(getattr(resp, "status", 200))
    payload = json.loads(body)
    return status_code, payload if isinstance(payload, dict) else {"data": payload}


def check_source_status(
    base_url: str,
    *,
    market: str = "",
    timeout_seconds: float = 3.0,
    http_json_func: HttpJsonFunc = http_json,
) -> dict[str, Any]:
    base = base_url.rstrip("/")
    url = f"{base}/source_status"
    try:
        status_code, payload = http_json_func(url, timeout_seconds)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {
            "status": "critical",
            "blocking": True,
            "source_status": "unknown",
            "url": url,
            "status_code": 0,
            "error": str(exc),
            "reason": "source_status_unreachable",
        }

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    source_status = str(data.get("status") or "unknown").lower() if isinstance(data, dict) else "unknown"
    summary = data.get("summary") if isinstance(data, dict) and isinstance(data.get("summary"), dict) else {}
    checks = data.get("checks") if isinstance(data, dict) and isinstance(data.get("checks"), list) else []
    market_key = _normalize_market(market)
    health_enrichment = _health_enrichment(base, market_key, checks, timeout_seconds, http_json_func)
    red_checks = [
        _enrich_check_with_health(check, health_enrichment)
        for check in checks
        if isinstance(check, dict) and str(check.get("status") or "").strip().lower() == "red"
    ]
    blocking_red_checks = [
        check
        for check in red_checks
        if _check_applies_to_market(check, market_key)
    ]
    if not (200 <= status_code < 300):
        status = "critical"
        blocking = True
    elif source_status == "green":
        status = "ok"
        blocking = False
    elif source_status == "yellow":
        status = "degraded"
        blocking = False
    elif source_status == "red" and market_key and red_checks and not blocking_red_checks:
        status = "degraded"
        blocking = False
    else:
        status = "critical"
        blocking = True

    return {
        "status": status,
        "blocking": blocking,
        "source_status": source_status,
        "url": url,
        "status_code": status_code,
        "summary": summary,
        "market": market_key,
        "red_check_count": len(red_checks),
        "blocking_red_check_count": len(blocking_red_checks),
        "blocking_checks": [str(check.get("name") or "") for check in blocking_red_checks],
        "health_enrichment": health_enrichment,
        "reason": "source_status_red_or_unknown" if blocking else ("source_status_red_unrelated_to_market" if source_status == "red" else ""),
    }


def _normalize_market(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"a_share", "a-share", "cn", "china", "ashares"}:
        return "ashare"
    if raw in {"futures", "cnfuture", "cn_futures", "china_futures"}:
        return "cn_futures"
    if raw in {"polymarket", "prediction_market", "prediction-market"}:
        return "pm"
    if raw in {"usa"}:
        return "us"
    return raw


def _market_from_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    for candidate in ("ashare", "a-share", "a_share", "crypto", "pm", "polymarket", "us", "usa", "cn_futures", "futures"):
        if candidate in text:
            return _normalize_market(candidate)
    return ""


def _check_applies_to_market(check: dict[str, Any], market: str) -> bool:
    if not market:
        return True
    evidence = check.get("evidence") if isinstance(check.get("evidence"), dict) else {}
    markets: set[str] = set()
    for key in ("market", "markets", "lane_market"):
        value = evidence.get(key)
        if isinstance(value, list):
            markets.update(_normalize_market(item) for item in value if _normalize_market(item))
        else:
            normalized = _normalize_market(value)
            if normalized:
                markets.add(normalized)
    for row in evidence.get("violations") or []:
        if isinstance(row, dict):
            normalized = _normalize_market(row.get("market"))
            if normalized:
                markets.add(normalized)
    inferred = _market_from_text(json.dumps(evidence, ensure_ascii=False))
    if inferred:
        markets.add(inferred)
    if markets:
        return market in markets
    return True


def _needs_health_enrichment(market: str, checks: list[Any]) -> bool:
    if not market:
        return False
    for check in checks:
        if not isinstance(check, dict):
            continue
        if str(check.get("name") or "") != "health_sla_summary":
            continue
        if str(check.get("status") or "").strip().lower() != "red":
            continue
        evidence = check.get("evidence") if isinstance(check.get("evidence"), dict) else {}
        if not evidence.get("violations"):
            return True
    return False


def _health_enrichment(
    base: str,
    market: str,
    checks: list[Any],
    timeout_seconds: float,
    http_json_func: HttpJsonFunc,
) -> dict[str, Any]:
    if not _needs_health_enrichment(market, checks):
        return {}
    url = f"{base}/health"
    try:
        status_code, payload = http_json_func(url, timeout_seconds)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"status": "unavailable", "url": url, "error": str(exc)}
    sla = payload.get("checks", {}).get("sla") if isinstance(payload.get("checks"), dict) else {}
    violations = sla.get("violations") if isinstance(sla, dict) and isinstance(sla.get("violations"), list) else []
    markets = sorted({_normalize_market(row.get("market")) for row in violations if isinstance(row, dict) and _normalize_market(row.get("market"))})
    return {
        "status": "ok" if 200 <= status_code < 300 else "unavailable",
        "url": url,
        "status_code": status_code,
        "violation_markets": markets,
        "violations": violations,
    }


def _enrich_check_with_health(check: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    if str(check.get("name") or "") != "health_sla_summary" or not health:
        return check
    enriched = dict(check)
    evidence = dict(enriched.get("evidence") if isinstance(enriched.get("evidence"), dict) else {})
    if health.get("violations"):
        evidence["violations"] = health["violations"]
    if health.get("violation_markets"):
        evidence["markets"] = health["violation_markets"]
    if evidence:
        enriched["evidence"] = evidence
    return enriched


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check SharedSignals /source_status for TradingAgent")
    parser.add_argument("--base-url", default="http://127.0.0.1:8082")
    parser.add_argument("--market", default="")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--require-not-red", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = check_source_status(args.base_url, market=args.market, timeout_seconds=args.timeout)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"{result['status']} source_status={result['source_status']} blocking={result['blocking']}")
    if args.require_not_red and result["blocking"]:
        return 2
    return 0 if result["status"] != "critical" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
