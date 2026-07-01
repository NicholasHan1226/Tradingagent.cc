#!/usr/bin/env python3
"""Market runtime health checks.

Default mode is read-only: no orders, no emails, no state mutation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import request

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MINI_HEALTH_URL = "http://127.0.0.1:9865/health"
VALID_ASHARE_RE = re.compile(r"^(000|001|002|003|300|301|600|601|603|605|688|689)\d{3}(\.(SZ|SH))?$", re.I)
INVALID_ASHARE_RE = re.compile(r"\b(?:200\d{3}\.SZ|900\d{3}\.SH)\b", re.I)
REQUIRED_TEMPLATES = [
    "daily_report.py",
    "weekly_report.py",
    "trade_receipt.py",
    "trading_signal.py",
    "system_health.py",
]


@dataclass
class Check:
    name: str
    status: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    severity: str = "error"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _iter_json_files(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(path.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)


def _count_json_files(path: Path) -> int:
    return len(_iter_json_files(path))


def _status(ok: bool, warn: bool = False) -> str:
    if ok:
        return "pass"
    return "warn" if warn else "fail"


def _check_ashare_universe() -> Check:
    try:
        from shared.data.reader import SharedSignalsReader

        reader = SharedSignalsReader()
        try:
            assets = reader.get_assets("Ashare") or reader.get_assets("ashare")
        finally:
            reader.close()
        symbols = [str(row.get("symbol") or "").strip() for row in assets if isinstance(row, dict) and row.get("symbol")]
        regular = [symbol for symbol in symbols if VALID_ASHARE_RE.match(symbol)]
        excluded = [symbol for symbol in symbols if symbol not in regular]
        ok = bool(regular)
        return Check(
            "ashare_universe",
            _status(ok),
            "A股普通股票资产入口正常" if ok else "A股普通股票资产入口异常",
            {
                "asset_count": len(symbols),
                "regular_ashare_count": len(regular),
                "excluded_non_regular_count": len(excluded),
                "excluded_sample": excluded[:20],
                "sample": regular[:20],
            },
        )
    except Exception as exc:  # noqa: BLE001
        return Check("ashare_universe", "fail", "A股资产入口检查失败", {"error": f"{exc.__class__.__name__}: {exc}"})

def _check_shadow_ledger() -> Check:
    files = [
        ROOT / "shared/logs/shadow/shadow_trades.jsonl",
        ROOT / "shared/logs/shadow/shadow_pnl.json",
        ROOT / "shared/logs/shadow/shadow_positions.json",
    ]
    matches: dict[str, int] = {}
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        matches[str(path.relative_to(ROOT))] = len(INVALID_ASHARE_RE.findall(text))
    pnl = _load_json(ROOT / "shared/logs/shadow/shadow_pnl.json", {}) or {}
    ashare = pnl.get("ashare_shadow", {}) if isinstance(pnl, dict) else {}
    required = ["realized_pnl", "unrealized_pnl", "market_value", "total_pnl", "valuation_source"]
    missing = [key for key in required if key not in ashare]
    ok = all(count == 0 for count in matches.values()) and not missing
    return Check(
        "ashare_shadow_ledger",
        _status(ok),
        "A股影子账本干净且有收益口径" if ok else "A股影子账本仍有污染或口径缺失",
        {
            "invalid_ashare_code_matches": matches,
            "ashare_pnl": {key: ashare.get(key) for key in ["total_trades", "market_value", "realized_pnl", "unrealized_pnl", "total_pnl", "valuation_source"]},
            "missing_pnl_fields": missing,
        },
    )


def _market_layer_counts(base: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for state in ["pending", "claimed", "running", "filled", "failed", "expired", "cancelled"]:
        counts[state] = _count_json_files(base / state)
    return counts


def _check_signal_queues() -> Check:
    execution = _market_layer_counts(ROOT / "signals")
    shadow = _market_layer_counts(ROOT / "signals/shadow")
    leaked_shadow = []
    for state in ["pending", "claimed", "running", "failed", "expired", "cancelled", "filled"]:
        for path in _iter_json_files(ROOT / "signals" / state):
            try:
                card = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                card = {}
            if str(card.get("capital_layer") or "").lower() == "shadow" or path.name.startswith("SHADOW-"):
                leaked_shadow.append(str(path.relative_to(ROOT)))
    ok = execution["pending"] == 0 and execution["claimed"] == 0 and execution["running"] == 0 and not leaked_shadow
    return Check(
        "signal_queue_isolation",
        _status(ok),
        "执行队列与影子队列已隔离" if ok else "执行队列存在待处理/影子污染",
        {"execution_queue": execution, "shadow_queue": shadow, "leaked_shadow_sample": leaked_shadow[:20]},
    )


def _check_mini_health(url: str = DEFAULT_MINI_HEALTH_URL) -> Check:
    try:
        with request.urlopen(url, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        pending = int(data.get("pending", 0))
        in_progress = int(data.get("in_progress", 0))
        expired = int(data.get("expired_pending", 0))
        halted = bool(data.get("halted")) or data.get("execution_status") == "halted"
        ok = data.get("status") == "ok" and pending == 0 and in_progress == 0 and expired == 0 and not halted
        return Check(
            "mini_hermes_health",
            _status(ok),
            "mini/Hermes 执行桥 ready" if ok else "mini/Hermes 执行桥不可用或有积压",
            {"url": url, "health": data},
        )
    except Exception as exc:  # noqa: BLE001
        return Check("mini_hermes_health", "fail", "mini/Hermes 健康口不可达", {"url": url, "error": f"{exc.__class__.__name__}: {exc}"})


def _check_simulated_position_sync() -> Check:
    path = ROOT / "signals/positions/simulated_ashare_positions.json"
    data = _load_json(path, {}) or {}
    positions = data.get("positions") or data.get("holdings") or []
    if isinstance(positions, dict):
        position_count = len(positions)
        sample = list(positions.items())[:5]
    elif isinstance(positions, list):
        position_count = len(positions)
        sample = positions[:5]
    else:
        position_count = 0
        sample = []
    ok = path.exists() and position_count >= 0
    return Check(
        "ashare_sim_position_sync",
        _status(ok),
        "A股模拟持仓快照可读" if ok else "A股模拟持仓快照缺失",
        {"path": str(path.relative_to(ROOT)), "position_count": position_count, "sample": sample, "mtime": path.stat().st_mtime if path.exists() else None},
        severity="warn",
    )


def _check_email_templates() -> Check:
    template_dir = ROOT / "shared/notify/email_templates"
    missing = [name for name in REQUIRED_TEMPLATES if not (template_dir / name).exists()]
    channels = _load_json(ROOT / "shared/notify/logs/email_rate_limit.json", {})
    latest_sent = []
    log = ROOT / "shared/notify/logs/emails_sent.jsonl"
    if log.exists():
        rows = [line for line in log.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        for line in rows[-50:]:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if item.get("status") == "sent":
                latest_sent.append({key: item.get(key) for key in ["to", "from", "subject", "provider", "status_code", "attempted_at"]})
    ok = not missing
    return Check(
        "email_templates_and_delivery",
        _status(ok, warn=True),
        "邮件模板存在，最近发送记录可复盘" if ok else "邮件模板缺失",
        {"missing_templates": missing, "latest_sent": latest_sent[-5:], "rate_limit_state_keys": len(channels) if isinstance(channels, dict) else 0},
        severity="warn",
    )


def _check_local_sim_ledger() -> Check:
    try:
        from shared.execution import local_sim_ledger

        trades_path = local_sim_ledger.LOCAL_SIM_TRADES
        pnl_path = local_sim_ledger.LOCAL_SIM_PNL
        invalid_matches = 0
        if trades_path.exists():
            for line in trades_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "200" in line or "900" in line:
                    invalid_matches += 1
        pnl = _load_json(pnl_path, {}) if pnl_path.exists() else {}
        ok = invalid_matches == 0
        return Check(
            "ashare_server_local_sim",
            _status(ok),
            "服务器本地模拟盘备份账本可用" if ok else "服务器本地模拟盘备份账本存在异常 A股代码",
            {
                "trade_log_exists": trades_path.exists(),
                "pnl_exists": pnl_path.exists(),
                "accounts": sorted(pnl.keys()) if isinstance(pnl, dict) else [],
                "invalid_code_matches": invalid_matches,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return Check("ashare_server_local_sim", "fail", "服务器本地模拟盘备份账本不可用", {"error": f"{exc.__class__.__name__}: {exc}"})


def _check_failure_receipts() -> Check:
    failed = _iter_json_files(ROOT / "signals/failed")
    receipts = ROOT.parent / "MarketGraph/outputs/sim_execution_receipts.jsonl"
    latest_receipts = []
    if receipts.exists():
        rows = [line for line in receipts.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        for line in rows[-20:]:
            try:
                item = json.loads(line)
            except Exception:
                continue
            latest_receipts.append({key: item.get(key) for key in ["id", "signal_id", "code", "status", "success", "filled_qty", "message"]})
    ok = receipts.exists() and bool(latest_receipts)
    return Check(
        "failure_receipts",
        _status(ok, warn=True),
        "失败/回执记录可复盘" if ok else "失败/回执记录不足",
        {"failed_count": len(failed), "receipt_path_exists": receipts.exists(), "latest_receipts": latest_receipts[-5:]},
        severity="warn",
    )


def run_ashare_health(*, mini_health_url: str = DEFAULT_MINI_HEALTH_URL) -> dict[str, Any]:
    checks = [
        _check_ashare_universe(),
        _check_shadow_ledger(),
        _check_signal_queues(),
        _check_mini_health(mini_health_url),
        _check_simulated_position_sync(),
        _check_local_sim_ledger(),
        _check_email_templates(),
        _check_failure_receipts(),
    ]
    failed = [check for check in checks if check.status == "fail"]
    warned = [check for check in checks if check.status == "warn"]
    overall = "fail" if failed else ("warn" if warned else "pass")
    return {
        "market": "ashare",
        "generated_at": _now_iso(),
        "overall_status": overall,
        "summary": {
            "pass": sum(1 for check in checks if check.status == "pass"),
            "warn": len(warned),
            "fail": len(failed),
        },
        "checks": [check.__dict__ for check in checks],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tradings market health checks")
    parser.add_argument("--market", default="ashare", choices=["ashare"], help="market to check")
    parser.add_argument("--mini-health-url", default=DEFAULT_MINI_HEALTH_URL)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    if args.market == "ashare":
        result = run_ashare_health(mini_health_url=args.mini_health_url)
    else:  # pragma: no cover
        raise ValueError(args.market)
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=False))
    return 0 if result["overall_status"] != "fail" else 2


if __name__ == "__main__":
    raise SystemExit(main())
