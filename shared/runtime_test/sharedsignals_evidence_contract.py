#!/usr/bin/env python3
"""Read-only SharedSignals evidence API contract checks for TradingAgent.

This verifies the consumer boundary only. It does not collect data, write to
SharedSignals, or require every evidence endpoint to be populated at all times.
Empty evidence rows are reported as evidence debt so TradingAgent can degrade
to neutral scoring without hiding the gap.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any


DEFAULT_API_URL = os.environ.get("SHAREDSIGNALS_API_URL", "http://127.0.0.1:8082")
DEFAULT_SYMBOL = os.environ.get("TRADINGAGENT_CONTRACT_SYMBOL", "600000.SH")


@dataclass
class EndpointCheck:
    name: str
    endpoint: str
    status: str
    summary: str
    row_count: int = 0
    sample_keys: list[str] = field(default_factory=list)
    required_keys_missing: list[str] = field(default_factory=list)
    error: str = ""


def _date_window(as_of: str, days: int) -> tuple[str, str]:
    end = datetime.strptime(as_of, "%Y%m%d")
    start = end - timedelta(days=max(1, days) - 1)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _fetch(base_url: str, endpoint: str, params: dict[str, str], timeout: float) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v})
    url = f"{base_url.rstrip('/')}{endpoint}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    data = payload.get("data") if isinstance(payload, dict) else None
    if data is None:
        data = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("response data is not a list")
    return [dict(row) for row in data if isinstance(row, dict)]


def _check_endpoint(
    *,
    name: str,
    endpoint: str,
    params: dict[str, str],
    required_keys: set[str],
    base_url: str,
    timeout: float,
    strict_empty: bool,
) -> EndpointCheck:
    try:
        rows = _fetch(base_url, endpoint, params, timeout)
    except (OSError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
        return EndpointCheck(
            name=name,
            endpoint=endpoint,
            status="fail",
            summary="api_unavailable_or_invalid",
            error=f"{exc.__class__.__name__}: {exc}",
        )

    sample_keys = sorted(rows[0].keys()) if rows else []
    missing = sorted(key for key in required_keys if rows and key not in rows[0])
    if rows and missing:
        return EndpointCheck(
            name=name,
            endpoint=endpoint,
            status="fail",
            summary="schema_missing_required_keys",
            row_count=len(rows),
            sample_keys=sample_keys,
            required_keys_missing=missing,
        )
    if not rows:
        return EndpointCheck(
            name=name,
            endpoint=endpoint,
            status="warn" if strict_empty else "pass",
            summary="evidence_debt_empty_rows",
            row_count=0,
            sample_keys=[],
        )
    return EndpointCheck(
        name=name,
        endpoint=endpoint,
        status="pass",
        summary="ok",
        row_count=len(rows),
        sample_keys=sample_keys,
    )


def run_contract_check(
    *,
    api_url: str = DEFAULT_API_URL,
    as_of: str | None = None,
    symbol: str = DEFAULT_SYMBOL,
    timeout: float = 8.0,
    strict_empty: bool = False,
) -> dict[str, Any]:
    as_of = (as_of or datetime.now().strftime("%Y%m%d")).replace("-", "")
    start_120, end = _date_window(as_of, 120)
    start_30, _ = _date_window(as_of, 30)
    start_14, _ = _date_window(as_of, 14)
    checks = [
        _check_endpoint(
            name="macro",
            endpoint="/macro",
            params={"start": start_120, "end": end},
            required_keys={"factor_name", "value"},
            base_url=api_url,
            timeout=timeout,
            strict_empty=strict_empty,
        ),
        _check_endpoint(
            name="events",
            endpoint="/events",
            params={"market": "Ashare", "end": end},
            required_keys={"event_time", "event_type"},
            base_url=api_url,
            timeout=timeout,
            strict_empty=strict_empty,
        ),
        _check_endpoint(
            name="sentiment",
            endpoint="/sentiment",
            params={"start": start_14, "end": end},
            required_keys={"event_time", "event_type"},
            base_url=api_url,
            timeout=timeout,
            strict_empty=strict_empty,
        ),
        _check_endpoint(
            name="capital_flow",
            endpoint="/capital_flow",
            params={"ts_code": symbol, "start": start_30, "end": end},
            required_keys={"factor_name", "value"},
            base_url=api_url,
            timeout=timeout,
            strict_empty=strict_empty,
        ),
    ]
    status_rank = {"pass": 0, "warn": 1, "fail": 2}
    worst = max(status_rank.get(check.status, 1) for check in checks)
    evidence_debts = [asdict(check) for check in checks if check.summary == "evidence_debt_empty_rows"]
    return {
        "overall_status": "fail" if worst >= 2 else ("warn" if worst == 1 else "pass"),
        "api_url": api_url,
        "as_of": as_of,
        "symbol": symbol,
        "strict_empty": strict_empty,
        "evidence_debt_count": len(evidence_debts),
        "evidence_debts": evidence_debts,
        "checks": [asdict(check) for check in checks],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check SharedSignals evidence API contract for TradingAgent.")
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--as-of", default="")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--strict-empty", action="store_true", help="Treat empty evidence endpoints as warn.")
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_contract_check(
        api_url=args.api_url,
        as_of=args.as_of or None,
        symbol=args.symbol,
        timeout=args.timeout,
        strict_empty=bool(args.strict_empty),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if report["overall_status"] != "fail" else 2


if __name__ == "__main__":
    raise SystemExit(main())
