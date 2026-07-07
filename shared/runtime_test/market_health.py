#!/usr/bin/env python3
"""Market runtime health checks.

Default mode is read-only: no orders, no emails, no state mutation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import request

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_MINI_HEALTH_URL = "http://127.0.0.1:9865/health"
DEFAULT_SHAREDSIGNALS_API_URL = "http://127.0.0.1:8082"
STALE_SIGNAL_MINUTES = 60
VALID_ASHARE_RE = re.compile(r"^(000|001|002|003|300|301|600|601|603|605|688|689)\d{3}(\.(SZ|SH))?$", re.I)
INVALID_ASHARE_RE = re.compile(r"\b(?:200\d{3}\.SZ|900\d{3}\.SH)\b", re.I)
REQUIRED_TEMPLATES = [
    "daily_report.py",
    "weekly_report.py",
    "trade_receipt.py",
    "trading_signal.py",
    "system_health.py",
]
SIM_MARKETS = tuple(
    item.strip().lower()
    for item in os.environ.get("TRADINGAGENT_SIM_MARKETS", "ashare,crypto,pm,us,cn_futures").split(",")
    if item.strip()
)
SIM_LOG_NAMES = {
    "ashare": "ashare_sim.log",
    "crypto": "crypto_sim.log",
    "pm": "pm_sim.log",
    "us": "us_sim.log",
    "hk": "hk_sim.log",
    "cn_futures": "cn_futures_sim.log",
}
SIM_WRAPPERS = {
    "ashare": "job_ashare_sim_exec.sh",
    "crypto": "job_crypto_sim.sh",
    "pm": "job_pm_sim.sh",
    "us": "job_us_sim.sh",
    "hk": "job_hk_sim.sh",
    "cn_futures": "job_cn_futures_sim.sh",
}
DEFAULT_SIM_SYMBOLS = {
    "crypto": ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"),
    "us": ("TSLA", "NVDA", "META", "AMZN", "GOOGL", "AMD", "NFLX", "AVGO", "COIN", "PLTR"),
    "hk": ("00700.HK", "09988.HK", "03690.HK", "09618.HK", "00005.HK", "00388.HK"),
}
CRYPTO_ONE_BAR_THRESHOLD = 0.012
CRYPTO_LOOKBACK_THRESHOLD = 0.025


@dataclass
class Check:
    name: str
    status: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    severity: str = "error"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_ashare_trading_day(current: datetime) -> bool:
    try:
        from Ashare.t_plus_1 import is_trading_day

        return bool(is_trading_day(current.strftime("%Y%m%d")))
    except Exception:
        return current.weekday() < 5


def _market_session_state(market: str, now: datetime | None = None) -> dict[str, Any]:
    """Return whether today's production samples should already exist."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=8)))
    weekday = current.weekday()
    minutes = current.hour * 60 + current.minute
    in_session = False
    samples_expected_today = False
    if market == "ashare":
        trading_day = _is_ashare_trading_day(current)
        windows = ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60))
        in_session = trading_day and any(start <= minutes <= end for start, end in windows)
        samples_expected_today = trading_day and minutes >= 9 * 60 + 30
    elif market == "cn_futures":
        day_session = weekday < 5 and 9 * 60 <= minutes <= 15 * 60
        night_session = weekday < 5 and 21 * 60 <= minutes <= 23 * 60 + 59
        early_session = 1 <= weekday <= 5 and 0 <= minutes <= 2 * 60 + 30
        in_session = day_session or night_session or early_session
        samples_expected_today = (
            (weekday < 5 and minutes >= 9 * 60)
            or (1 <= weekday <= 5 and minutes <= 2 * 60 + 30)
        )
    return {
        "timezone": "Asia/Shanghai",
        "local_time": current.isoformat(timespec="seconds"),
        "in_session": in_session,
        "samples_expected_today": samples_expected_today,
    }


def _load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _safe_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and number > 0 else 0.0


def _price(row: dict[str, Any]) -> float:
    for key in ("latest_price", "price", "close", "last_price", "market_price", "yes_price", "probability"):
        value = _safe_float(row.get(key))
        if value > 0:
            return value
    return 0.0


def _probability(row: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        value = _safe_float(row.get(key))
        if 0 < value < 1:
            return value
    return 0.0


def _explicit_trade_side(row: dict[str, Any]) -> str:
    for key in ("side", "action", "direction", "signal", "decision", "recommendation"):
        raw = str(row.get(key) or "").strip().lower()
        if raw in {"buy", "long", "open_long", "increase"}:
            return "buy"
        if raw in {"sell", "short", "open_short", "reduce", "close"}:
            return "sell"
    return ""


def _unwrap_rows(rows: Any) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if isinstance(rows, dict):
        rows = rows.get("data", [rows])
    if not isinstance(rows, list):
        return []
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        data = row.get("data")
        normalized.append(dict(data) if isinstance(data, dict) else dict(row))
    return normalized


def _latest_priced(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    priced = [row for row in rows if _price(row) > 0]
    if not priced:
        return None
    return priced[-1]


def _row_time(row: dict[str, Any]) -> str:
    return str(
        row.get("trade_date")
        or row.get("price_time")
        or row.get("latest_price_time")
        or row.get("collected_at")
        or row.get("open_time")
        or ""
    )


def _priced_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((row for row in rows if _price(row) > 0), key=_row_time)


def _pct_change(latest: float, previous: float) -> float:
    if previous <= 0:
        return 0.0
    return latest / previous - 1.0


def _crypto_momentum_diagnostic(symbol: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    priced = _priced_rows(rows)
    if not priced:
        return {
            "symbol": symbol,
            "rows": len(rows),
            "priced_rows": 0,
            "latest_price": 0.0,
            "one_bar_return": 0.0,
            "lookback_return": 0.0,
            "strategy_candidate": False,
            "reason": "crypto_klines_empty",
        }
    latest_price = _price(priced[-1])
    if len(priced) < 2:
        return {
            "symbol": symbol,
            "rows": len(rows),
            "priced_rows": len(priced),
            "latest_price": latest_price,
            "latest_time": _row_time(priced[-1]),
            "one_bar_return": 0.0,
            "lookback_return": 0.0,
            "strategy_candidate": False,
            "reason": "crypto_insufficient_priced_rows",
        }
    one_bar_return = _pct_change(latest_price, _price(priced[-2]))
    lookback_return = _pct_change(latest_price, _price(priced[0]))
    candidate = one_bar_return >= CRYPTO_ONE_BAR_THRESHOLD or lookback_return >= CRYPTO_LOOKBACK_THRESHOLD
    return {
        "symbol": symbol,
        "rows": len(rows),
        "priced_rows": len(priced),
        "latest_price": latest_price,
        "latest_time": _row_time(priced[-1]),
        "one_bar_return": round(one_bar_return, 6),
        "lookback_return": round(lookback_return, 6),
        "strategy_candidate": candidate,
        "reason": "crypto_strategy_candidate" if candidate else "crypto_momentum_threshold_not_met",
    }


def _file_age_minutes(path: Path) -> float | None:
    if not path.exists():
        return None
    return round(max(0.0, (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 60.0), 2)


def _parse_iso_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iter_json_files(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(path.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)


def _count_json_files(path: Path) -> int:
    return len(_iter_json_files(path))


def _compact_date_key(value: Any) -> str:
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _position_count_from_snapshot(payload: dict[str, Any]) -> int:
    positions = payload.get("positions") or payload.get("holdings") or []
    if isinstance(positions, dict):
        return len(positions)
    if isinstance(positions, list):
        return len(positions)
    return 0


def _position_count_from_positions_payload(payload: dict[str, Any]) -> int:
    if not isinstance(payload, dict):
        return 0
    total = 0
    for account_positions in payload.values():
        if isinstance(account_positions, dict):
            total += len(account_positions)
        elif isinstance(account_positions, list):
            total += len(account_positions)
    return total


def _execution_card_stale(path: Path, card: dict[str, Any]) -> dict[str, Any] | None:
    age = _file_age_minutes(path)
    today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    for key in ("valid_until", "trade_date", "date"):
        date_key = _compact_date_key(card.get(key))
        if date_key and date_key < today:
            return {"path": str(path.relative_to(ROOT)), "reason": f"{key}_expired", "date": date_key, "age_minutes": age}
    if age is not None and age > STALE_SIGNAL_MINUTES:
        return {"path": str(path.relative_to(ROOT)), "reason": "age_exceeded", "age_minutes": age}
    return None


def _status(ok: bool, warn: bool = False) -> str:
    if ok:
        return "pass"
    return "warn" if warn else "fail"


def _check_ashare_universe() -> Check:
    try:
        from shared.data.reader import SharedSignalsAPIClient, TradingagentDataReader

        api_url = os.environ.get("SHAREDSIGNALS_API_URL", DEFAULT_SHAREDSIGNALS_API_URL).strip() or DEFAULT_SHAREDSIGNALS_API_URL
        reader = TradingagentDataReader(api_client=SharedSignalsAPIClient(base_url=api_url))
        assets = reader.get_assets("Ashare") or reader.get_assets("ashare")
        symbols = [str(row.get("symbol") or "").strip() for row in assets if isinstance(row, dict) and row.get("symbol")]
        regular = [symbol for symbol in symbols if VALID_ASHARE_RE.match(symbol)]
        excluded = [symbol for symbol in symbols if symbol not in regular]
        ok = bool(regular) and not reader.degraded
        return Check(
            "ashare_universe",
            _status(ok, warn=bool(regular)),
            "A股普通股票资产入口正常" if ok else "A股普通股票资产入口异常",
            {
                "asset_count": len(symbols),
                "regular_ashare_count": len(regular),
                "excluded_non_regular_count": len(excluded),
                "excluded_sample": excluded[:20],
                "sample": regular[:20],
                "reader_degraded": reader.degraded,
                "reader_errors": reader.errors[-5:],
            },
        )
    except Exception as exc:  # noqa: BLE001
        return Check("ashare_universe", "fail", "A股资产入口检查失败", {"error": f"{exc.__class__.__name__}: {exc}"})

def _normalize_shadow_pnl_payload(pnl: dict[str, Any]) -> dict[str, Any]:
    ashare = pnl.get("ashare_shadow", {}) if isinstance(pnl, dict) else {}
    if isinstance(ashare, dict) and ashare:
        return ashare

    try:
        from shared.execution.shadow_broker import get_shadow_pnl

        replay = get_shadow_pnl("ashare_shadow", datetime.now(timezone.utc).strftime("%Y%m%d"), market="ashare")
    except Exception:
        replay = {}
    return {
        "total_trades": int(replay.get("total_trades") or 0),
        "market_value": 0.0,
        "realized_pnl": float(replay.get("realized_pnl") or 0.0),
        "unrealized_pnl": 0.0,
        "total_pnl": float(replay.get("pnl") or replay.get("realized_pnl") or 0.0),
        "valuation_source": "shadow_broker_replay",
    }


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
    ashare = _normalize_shadow_pnl_payload(pnl if isinstance(pnl, dict) else {})
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
    stale_execution = []
    for state in ["pending", "claimed", "running", "failed", "expired", "cancelled", "filled"]:
        for path in _iter_json_files(ROOT / "signals" / state):
            try:
                card = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                card = {}
            if str(card.get("capital_layer") or "").lower() == "shadow" or path.name.startswith("SHADOW-"):
                leaked_shadow.append(str(path.relative_to(ROOT)))
            if state in {"pending", "claimed", "running"} and isinstance(card, dict):
                stale = _execution_card_stale(path, card)
                if stale:
                    stale["state"] = state
                    stale_execution.append(stale)
    ok = execution["pending"] == 0 and execution["claimed"] == 0 and execution["running"] == 0 and not leaked_shadow and not stale_execution
    return Check(
        "signal_queue_isolation",
        _status(ok),
        "执行队列与影子队列已隔离" if ok else "执行队列存在待处理/影子污染/陈旧信号",
        {"execution_queue": execution, "shadow_queue": shadow, "leaked_shadow_sample": leaked_shadow[:20], "stale_execution_sample": stale_execution[:20]},
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


def _check_optional_mini_health(url: str = DEFAULT_MINI_HEALTH_URL) -> Check:
    enabled = os.environ.get("ASHARE_SIM_HERMES_ENABLED", "0").strip() == "1"
    if not enabled:
        return Check(
            "mini_hermes_optional",
            "pass",
            "Hermes/同花顺 GUI 第二路径未启用，不影响服务器本地模拟盘",
            {"url": url, "enabled": False, "primary_path": "server_local_sim"},
            severity="info",
        )

    raw = _check_mini_health(url)
    ok = raw.status == "pass"
    return Check(
        "mini_hermes_optional",
        "pass" if ok else "warn",
        "Hermes/同花顺 GUI 第二路径 ready" if ok else "Hermes/同花顺 GUI 第二路径异常，服务器本地模拟盘继续独立运行",
        {"url": url, "enabled": True, "raw_status": raw.status, "raw_summary": raw.summary, "raw_details": raw.details},
        severity="warn",
    )


def _check_simulated_position_sync() -> Check:
    path = ROOT / "signals/positions/simulated_ashare_positions.json"
    local_trades_path = ROOT / "shared/logs/local_sim/local_sim_trades.jsonl"
    local_trade_count = 0
    if local_trades_path.exists():
        local_trade_count = sum(1 for line in local_trades_path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
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
    snapshot_bootstrap_state = str(data.get("bootstrap_state") or "")
    no_trade_bootstrap = (not path.exists() and local_trade_count == 0) or (
        snapshot_bootstrap_state == "no_trades_yet" and local_trade_count == 0
    )
    ok = (path.exists() and position_count >= 0) or no_trade_bootstrap
    return Check(
        "ashare_sim_position_sync",
        _status(ok, warn=True),
        "A股模拟持仓快照可读" if path.exists() else "A股模拟盘暂无成交，持仓快照待首笔成交生成" if no_trade_bootstrap else "A股模拟持仓快照缺失",
        {
            "path": str(path.relative_to(ROOT)),
            "position_count": position_count,
            "sample": sample,
            "mtime": path.stat().st_mtime if path.exists() else None,
            "local_trade_count": local_trade_count,
            "bootstrap_state": snapshot_bootstrap_state or ("no_trades_yet" if no_trade_bootstrap else ""),
        },
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
        positions_path = local_sim_ledger.LOCAL_SIM_POSITIONS
        pnl_path = local_sim_ledger.LOCAL_SIM_PNL
        snapshot_path = local_sim_ledger.LOCAL_SIM_POSITIONS_SNAPSHOT
        invalid_matches = 0
        trade_rows = _ashare_local_sim_trade_rows(trades_path)
        for item in trade_rows:
            symbol = str(item.get("ts_code") or item.get("symbol") or item.get("code") or "").strip()
            if symbol and not VALID_ASHARE_RE.match(symbol):
                invalid_matches += 1
        sample_quality = _ashare_local_sample_quality(trades_path)
        pnl = _load_json(pnl_path, {}) if pnl_path.exists() else {}
        positions_payload = _load_json(positions_path, {}) if positions_path.exists() else {}
        snapshot = _load_json(snapshot_path, {}) if snapshot_path.exists() else {}
        position_count = _position_count_from_positions_payload(positions_payload if isinstance(positions_payload, dict) else {})
        snapshot_position_count = _position_count_from_snapshot(snapshot if isinstance(snapshot, dict) else {})
        consistency_errors: list[str] = []
        if trades_path.exists() and snapshot_path.exists() and position_count != snapshot_position_count:
            consistency_errors.append("position_count_mismatch")
        missing_cash_accounts: list[str] = []
        cash_mismatch_accounts: list[str] = []
        snapshot_pnl = snapshot.get("pnl") if isinstance(snapshot, dict) and isinstance(snapshot.get("pnl"), dict) else {}
        if isinstance(pnl, dict):
            for account, account_pnl in pnl.items():
                if not isinstance(account_pnl, dict):
                    continue
                local_cash = account_pnl.get("cash_available")
                if local_cash is None:
                    missing_cash_accounts.append(str(account))
                    continue
                snapshot_account = snapshot_pnl.get(account) if isinstance(snapshot_pnl.get(account), dict) else {}
                snapshot_cash = snapshot_account.get("cash_available")
                if snapshot_cash is not None and abs(_safe_float(local_cash) - _safe_float(snapshot_cash)) > 0.01:
                    cash_mismatch_accounts.append(str(account))
        if missing_cash_accounts:
            consistency_errors.append("cash_available_missing")
        if cash_mismatch_accounts:
            consistency_errors.append("cash_available_mismatch")
        invalid_strategy_samples = int(sample_quality.get("invalid_strategy_sample_count", 0) or 0)
        outside_session_only = _ashare_outside_session_only_samples(sample_quality)
        ok = invalid_matches == 0 and not consistency_errors and (invalid_strategy_samples == 0 or outside_session_only)
        status = _status(ok)
        severity = "error"
        summary = "服务器本地模拟盘备份账本可用"
        advisory = False
        if invalid_matches == 0 and not consistency_errors and outside_session_only:
            status = "pass"
            severity = "info"
            advisory = True
            summary = "服务器本地模拟盘账本可用；链路验证样本已隔离出策略口径"
        elif invalid_matches == 0 and not consistency_errors and invalid_strategy_samples > 0:
            status = "warn"
            severity = "warn"
            summary = "服务器本地模拟盘账本可用，但存在非策略样本，已从策略绩效/演化口径隔离"
        elif not ok:
            summary = "服务器本地模拟盘备份账本存在异常"
        return Check(
            "ashare_server_local_sim",
            status,
            summary,
            {
                "trade_log_exists": trades_path.exists(),
                "pnl_exists": pnl_path.exists(),
                "accounts": sorted(pnl.keys()) if isinstance(pnl, dict) else [],
                "invalid_code_matches": invalid_matches,
                "sample_quality": sample_quality,
                "position_count": position_count,
                "snapshot_position_count": snapshot_position_count,
                "consistency_errors": consistency_errors,
                "missing_cash_accounts": missing_cash_accounts,
                "cash_mismatch_accounts": cash_mismatch_accounts,
                "advisory": advisory,
            },
            severity=severity,
        )
    except Exception as exc:  # noqa: BLE001
        return Check("ashare_server_local_sim", "fail", "服务器本地模拟盘备份账本不可用", {"error": f"{exc.__class__.__name__}: {exc}"})


def _latest_ashare_capital_plan_row() -> tuple[Path | None, dict[str, Any]]:
    target_dir = ROOT / "shared/review/ashare"
    files = sorted(target_dir.glob("capital_plan_*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True) if target_dir.exists() else []
    for path in files:
        rows = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        for line in reversed(rows):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return path, payload
    return None, {}


def _check_ashare_capital_plan_alignment() -> Check:
    local_trades_path = ROOT / "shared/logs/local_sim/local_sim_trades.jsonl"
    local_trade_count = _count_jsonl_rows(local_trades_path)
    sample_quality = _ashare_local_sample_quality(local_trades_path)
    outside_session_only = _ashare_outside_session_only_samples(sample_quality)
    snapshot_path = ROOT / "signals/positions/simulated_ashare_positions.json"
    snapshot = _load_json(snapshot_path, {}) if snapshot_path.exists() else {}
    snapshot_count = _position_count_from_snapshot(snapshot if isinstance(snapshot, dict) else {})
    if local_trade_count == 0 and snapshot_count == 0:
        return Check(
            "ashare_capital_plan_alignment",
            "pass",
            "A股模拟盘尚无成交，资金计划持仓对账待首笔成交后启用",
            {"local_trade_count": local_trade_count, "snapshot_position_count": snapshot_count, "bootstrap_state": "no_trades_yet"},
            severity="warn",
        )

    plan_path, row = _latest_ashare_capital_plan_row()
    if not row:
        return Check(
            "ashare_capital_plan_alignment",
            "warn",
            "A股资金计划日志尚未生成，无法对账持仓数",
            {"local_trade_count": local_trade_count, "snapshot_position_count": snapshot_count},
            severity="warn",
        )

    capital_plan = row.get("capital_plan") if isinstance(row.get("capital_plan"), dict) else {}
    rebalance = row.get("rebalance") if isinstance(row.get("rebalance"), dict) else {}
    plan_count = capital_plan.get("existing_position_count", rebalance.get("existing_position_count"))
    plan_count_int = int(plan_count) if isinstance(plan_count, (int, float)) or str(plan_count).isdigit() else -1
    ok = plan_count_int == snapshot_count
    plan_ts = _parse_iso_datetime(row.get("generated_at"))
    snapshot_ts = _parse_iso_datetime(snapshot.get("synced_at")) if isinstance(snapshot, dict) else None
    plan_older_than_snapshot = bool(plan_ts and snapshot_ts and plan_ts < snapshot_ts)
    status = _status(ok)
    severity = "error" if not ok else "info"
    message = "A股资金计划与持仓快照对账一致"
    advisory = False
    if not ok and plan_older_than_snapshot and outside_session_only:
        status = "pass"
        severity = "info"
        advisory = True
        message = "A股资金计划早于链路验证样本快照；验证样本已隔离，不影响策略资金计划"
    elif not ok and plan_older_than_snapshot:
        status = "warn"
        severity = "warn"
        message = "A股资金计划早于最新持仓快照，等待下一轮资金计划刷新"
    elif not ok:
        message = "A股资金计划读取的持仓数与持仓快照不一致"
    return Check(
        "ashare_capital_plan_alignment",
        status,
        message,
        {
            "latest_capital_plan": str(plan_path.relative_to(ROOT)) if plan_path else "",
            "generated_at": row.get("generated_at"),
            "snapshot_synced_at": snapshot.get("synced_at") if isinstance(snapshot, dict) else "",
            "plan_older_than_snapshot": plan_older_than_snapshot,
            "local_trade_count": local_trade_count,
            "snapshot_position_count": snapshot_count,
            "capital_plan_position_count": plan_count_int,
            "cash_source": capital_plan.get("cash_source"),
            "sample_quality": sample_quality,
            "advisory": advisory,
        },
        severity=severity,
    )


def _check_failure_receipts() -> Check:
    failed = _iter_json_files(ROOT / "signals/failed")
    local_trades_path = ROOT / "shared/logs/local_sim/local_sim_trades.jsonl"
    local_trade_count = 0
    if local_trades_path.exists():
        local_trade_count = sum(1 for line in local_trades_path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
    sample_quality = _ashare_local_sample_quality(local_trades_path)
    outside_session_only = _ashare_outside_session_only_samples(sample_quality)
    receipt_paths = [ROOT / "signals/sim_execution_receipts.jsonl"]
    legacy_receipts = ROOT.parent / "MarketGraph/outputs/sim_execution_receipts.jsonl"
    if legacy_receipts.exists():
        receipt_paths.append(legacy_receipts)
    latest_receipts = []
    existing_paths = []
    for receipts in receipt_paths:
        if not receipts.exists():
            continue
        existing_paths.append(str(receipts))
        rows = [line for line in receipts.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        for line in rows[-20:]:
            try:
                item = json.loads(line)
            except Exception:
                continue
            latest_receipts.append({key: item.get(key) for key in ["id", "signal_id", "order_id", "code", "symbol", "status", "success", "filled_qty", "message", "receipt_sha256"]})
    no_receipt_expected = not failed and local_trade_count == 0
    validation_receipt_advisory = not failed and outside_session_only and not latest_receipts
    ok = (bool(existing_paths) and bool(latest_receipts)) or no_receipt_expected
    if validation_receipt_advisory:
        ok = True
    summary = "失败/回执记录可复盘"
    severity = "warn"
    advisory = False
    if latest_receipts:
        summary = "失败/回执记录可复盘"
    elif validation_receipt_advisory:
        summary = "链路验证样本无需策略回执，已隔离；真实策略成交仍要求回执"
        severity = "info"
        advisory = True
    elif no_receipt_expected:
        summary = "暂无失败或模拟成交，回执待首笔事件生成"
    else:
        summary = "失败/回执记录不足"
    return Check(
        "failure_receipts",
        _status(ok, warn=True),
        summary,
        {
            "failed_count": len(failed),
            "local_trade_count": local_trade_count,
            "receipt_path_exists": bool(existing_paths),
            "receipt_paths": existing_paths,
            "latest_receipts": latest_receipts[-5:],
            "bootstrap_state": "no_receipts_expected_yet" if no_receipt_expected else "",
            "sample_quality": sample_quality,
            "advisory": advisory,
        },
        severity=severity,
    )


def _installed_crontab_text() -> tuple[str, str]:
    commands = (["crontab", "-u", "marketgraph", "-l"], ["crontab", "-l"])
    errors: list[str] = []
    for command in commands:
        try:
            result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=5)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{' '.join(command)}: {exc.__class__.__name__}: {exc}")
            continue
        if result.returncode == 0:
            return result.stdout, ""
        errors.append(f"{' '.join(command)}: {result.stderr.strip() or result.stdout.strip()}")
    return "", "; ".join(error for error in errors if error)


def _latest_cron_result(market: str) -> dict[str, Any]:
    path = ROOT / "shared/logs/cron" / SIM_LOG_NAMES.get(market, f"{market}_sim.log")
    payload: dict[str, Any] | None = None
    if path.exists():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines[-200:]):
            start = line.find("{")
            if start < 0:
                continue
            try:
                parsed = json.loads(line[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and str(parsed.get("market") or "").lower() == market:
                payload = parsed
                break
    return {
        "path": str(path.relative_to(ROOT)),
        "exists": path.exists(),
        "age_minutes": _file_age_minutes(path),
        "payload": payload or {},
    }


def _count_jsonl_rows(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
    except OSError:
        return 0


def _ashare_local_sim_trade_rows(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or (ROOT / "shared/logs/local_sim/local_sim_trades.jsonl")
    rows: list[dict[str, Any]] = []
    if not target.exists():
        return rows
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _ashare_local_sample_quality(path: Path | None = None) -> dict[str, Any]:
    from shared.review.sample_quality import summarize_sample_quality

    quality_rows = [
        row
        for row in _ashare_local_sim_trade_rows(path)
        if any(row.get(key) for key in ("side", "status", "created_at", "execution_source", "candidate_pool_layer", "order_id", "trade_id"))
    ]
    return summarize_sample_quality(quality_rows)


def _ashare_outside_session_only_samples(sample_quality: dict[str, Any]) -> bool:
    total = int(sample_quality.get("total_count", 0) or 0)
    valid_count = int(sample_quality.get("strategy_sample_valid_count", 0) or 0)
    by_reason = sample_quality.get("by_reason") if isinstance(sample_quality.get("by_reason"), dict) else {}
    outside_count = int(by_reason.get("outside_ashare_regular_session", 0) or 0)
    return total > 0 and outside_count == total and valid_count == 0


def _sim_ledger_summary(market: str) -> dict[str, Any]:
    pnl: dict[str, Any] = {}
    try:
        from shared.review.pnl_summary import sim_ledger_pnl_summary

        pnl_result = sim_ledger_pnl_summary(markets=(market,))
        pnl = pnl_result.get(market, {})
    except Exception as exc:  # noqa: BLE001
        pnl = {"error": f"{exc.__class__.__name__}: {exc}"}
    if market == "ashare":
        path = ROOT / "shared/logs/local_sim/local_sim_trades.jsonl"
        return {
            "type": "server_local_sim_backup",
            "trade_rows": _count_jsonl_rows(path),
            "ledger_count": 1 if path.exists() else 0,
            "latest_file": str(path.relative_to(ROOT)),
            "latest_age_minutes": _file_age_minutes(path),
            "realized_pnl": round(_safe_float(pnl.get("realized_pnl")), 6),
            "unrealized_pnl": round(_safe_float(pnl.get("unrealized_pnl")), 6),
            "total_pnl": round(_safe_float(pnl.get("total_pnl")), 6),
            "pnl_source": pnl.get("pnl_source", ""),
        }
    if market == "cn_futures":
        review_path = ROOT / "shared/review/data/cn_futures_sim_reviews.jsonl"
        rows = []
        if review_path.exists():
            for line in review_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    rows.append(payload)
        latest = rows[-1] if rows else {}
        return {
            "type": "cn_futures_append_only_review",
            "trade_rows": sum(int(row.get("filled_count") or 0) for row in rows),
            "ledger_count": 1 if review_path.exists() else 0,
            "review_rows": len(rows),
            "latest_file": str(review_path.relative_to(ROOT)),
            "latest_age_minutes": _file_age_minutes(review_path),
            "latest_state": latest.get("state", ""),
            "latest_filled_count": int(latest.get("filled_count") or 0) if latest else 0,
            "latest_error_count": int(latest.get("error_count") or 0) if latest else 0,
            "latest_error_summary": latest.get("error_summary") if isinstance(latest.get("error_summary"), dict) else {},
            "latest_style_health": latest.get("style_health") if isinstance(latest.get("style_health"), dict) else {},
            "realized_pnl": round(_safe_float(pnl.get("realized_pnl")), 6),
            "unrealized_pnl": round(_safe_float(pnl.get("unrealized_pnl")), 6),
            "total_pnl": round(_safe_float(pnl.get("total_pnl")), 6),
            "pnl_source": pnl.get("pnl_source", ""),
        }

    files = sorted((ROOT / "shared/logs/sim_ledger" / market).glob("*/trade_journal.jsonl"))
    latest = max(files, key=lambda item: item.stat().st_mtime) if files else None
    return {
        "type": "style_sim_ledger",
        "trade_rows": sum(_count_jsonl_rows(path) for path in files),
        "ledger_count": len(files),
        "latest_file": str(latest.relative_to(ROOT)) if latest else "",
        "latest_age_minutes": _file_age_minutes(latest) if latest else None,
        "realized_pnl": round(_safe_float(pnl.get("realized_pnl")), 6),
        "unrealized_pnl": round(_safe_float(pnl.get("unrealized_pnl")), 6),
        "total_pnl": round(_safe_float(pnl.get("total_pnl")), 6),
        "pnl_source": pnl.get("pnl_source", ""),
    }


def _probe_market_data(market: str) -> dict[str, Any]:
    try:
        from shared.data.reader import SharedSignalsAPIClient, TradingagentDataReader

        api_url = os.environ.get("SHAREDSIGNALS_API_URL", DEFAULT_SHAREDSIGNALS_API_URL).strip() or DEFAULT_SHAREDSIGNALS_API_URL
        reader = TradingagentDataReader(api_client=SharedSignalsAPIClient(base_url=api_url))
        priced_rows: list[dict[str, Any]] = []
        asset_count = 0
        if market == "ashare":
            assets = reader.get_assets("Ashare") or reader.get_assets("ashare")
            asset_count = len(assets)
            regular = [
                row for row in assets
                if isinstance(row, dict) and VALID_ASHARE_RE.match(str(row.get("symbol") or row.get("ts_code") or ""))
            ]
            return {
                "status": "ok" if regular and not reader.degraded else ("warn" if regular else "fail"),
                "asset_count": asset_count,
                "priced_signal_count": 0,
                "regular_ashare_count": len(regular),
                "reader_degraded": reader.degraded,
                "reader_errors": reader.errors[-5:],
            }
        if market == "pm":
            from PM.probability_model import enrich_pm_rows
            from shared.wrappers.run_sim import _pm_strategy_signal

            rows = enrich_pm_rows(_unwrap_rows(reader.get_pm_markets(limit=10)))
            priced_rows = [row for row in rows if _price(row) > 0]
            modeled_rows = [
                row for row in priced_rows
                if _probability(row, ("model_probability", "model_prob", "fair_probability", "estimated_probability")) > 0
            ]
            explicit_rows = [row for row in rows if _explicit_trade_side(row)]
            candidate_rows = [row for row in priced_rows if _pm_strategy_signal(row)]
            if not rows:
                return {
                    "status": "warn",
                    "asset_count": 0,
                    "priced_signal_count": 0,
                    "modeled_signal_count": 0,
                    "explicit_signal_count": 0,
                    "reason": "pm_market_rows_empty",
                    "sample": [],
                    "reader_degraded": reader.degraded,
                    "reader_errors": reader.errors[-5:],
                }
            if not priced_rows:
                return {
                    "status": "warn",
                    "asset_count": len(rows),
                    "priced_signal_count": 0,
                    "modeled_signal_count": 0,
                    "explicit_signal_count": len(explicit_rows),
                    "reason": "pm_prices_missing",
                    "sample": [
                        {key: row.get(key) for key in ("symbol", "market_id", "trade_date", "price", "yes_price")}
                        for row in rows[:5]
                    ],
                    "reader_degraded": reader.degraded,
                    "reader_errors": reader.errors[-5:],
                }
            if not modeled_rows and not explicit_rows:
                return {
                    "status": "warn",
                    "asset_count": len(rows),
                    "priced_signal_count": len(priced_rows),
                    "modeled_signal_count": 0,
                    "explicit_signal_count": 0,
                    "reason": "pm_model_probability_missing",
                    "sample": [
                        {key: row.get(key) for key in ("symbol", "market_id", "trade_date", "price", "latest_price", "yes_price", "model_probability", "fair_probability", "estimated_probability")}
                        for row in priced_rows[:5]
                    ],
                    "reader_degraded": reader.degraded,
                    "reader_errors": reader.errors[-5:],
                }
            if modeled_rows and not explicit_rows and not candidate_rows:
                return {
                    "status": "warn",
                    "asset_count": len(rows),
                    "priced_signal_count": len(priced_rows),
                    "modeled_signal_count": len(modeled_rows),
                    "explicit_signal_count": 0,
                    "strategy_candidate_count": 0,
                    "reason": "pm_model_edge_below_threshold",
                    "sample": [
                        {key: row.get(key) for key in ("symbol", "market_id", "trade_date", "price", "latest_price", "yes_price", "model_probability", "model_source", "model_reason")}
                        for row in priced_rows[:5]
                    ],
                    "reader_degraded": reader.degraded,
                    "reader_errors": reader.errors[-5:],
                }
            asset_count = len(rows)
        elif market == "crypto":
            diagnostics: list[dict[str, Any]] = []
            for symbol in DEFAULT_SIM_SYMBOLS["crypto"]:
                rows = _unwrap_rows(reader.get_crypto_klines(symbol=symbol, limit=50))
                diagnostic = _crypto_momentum_diagnostic(symbol, rows)
                diagnostics.append(diagnostic)
                latest = _latest_priced(rows)
                if latest:
                    priced_rows.append(latest)
            strategy_candidates = [row for row in diagnostics if row.get("strategy_candidate")]
            if not priced_rows:
                return {
                    "status": "warn",
                    "asset_count": len(DEFAULT_SIM_SYMBOLS["crypto"]),
                    "priced_signal_count": 0,
                    "strategy_candidate_count": 0,
                    "reason": "crypto_klines_empty",
                    "momentum_thresholds": {
                        "one_bar_return": CRYPTO_ONE_BAR_THRESHOLD,
                        "lookback_return": CRYPTO_LOOKBACK_THRESHOLD,
                    },
                    "sample": diagnostics[:5],
                    "reader_degraded": reader.degraded,
                    "reader_errors": reader.errors[-5:],
                }
            if not strategy_candidates:
                return {
                    "status": "warn",
                    "asset_count": len(DEFAULT_SIM_SYMBOLS["crypto"]),
                    "priced_signal_count": len(priced_rows),
                    "strategy_candidate_count": 0,
                    "reason": "crypto_momentum_threshold_not_met",
                    "momentum_thresholds": {
                        "one_bar_return": CRYPTO_ONE_BAR_THRESHOLD,
                        "lookback_return": CRYPTO_LOOKBACK_THRESHOLD,
                    },
                    "sample": diagnostics[:5],
                    "reader_degraded": reader.degraded,
                    "reader_errors": reader.errors[-5:],
                }
            asset_count = len(DEFAULT_SIM_SYMBOLS["crypto"])
        elif market in {"us", "hk"}:
            end = datetime.now(timezone.utc).date()
            start = end - timedelta(days=10)
            market_name = "HK" if market == "hk" else "US"
            for symbol in DEFAULT_SIM_SYMBOLS[market]:
                latest = _latest_priced(
                    _unwrap_rows(
                        reader.get_market_data(
                            ts_code=symbol,
                            market=market_name,
                            start=start.strftime("%Y%m%d"),
                            end=end.strftime("%Y%m%d"),
                            freq="daily",
                        )
                    )
                )
                if latest:
                    priced_rows.append(latest)
            if market == "hk" and not priced_rows:
                proxy_rows: list[dict[str, Any]] = []
                for symbol in ("HSI",):
                    latest = _latest_priced(
                        _unwrap_rows(
                            reader.get_market_data(
                                ts_code=symbol,
                                market="Global",
                                start=start.strftime("%Y%m%d"),
                                end=end.strftime("%Y%m%d"),
                                freq="daily",
                            )
                        )
                    )
                    if latest:
                        item = dict(latest)
                        item["symbol"] = symbol
                        item["market_proxy_for"] = "HK"
                        proxy_rows.append(item)
                if proxy_rows:
                    return {
                        "status": "warn",
                        "asset_count": asset_count,
                        "priced_signal_count": 0,
                        "proxy_priced_signal_count": len(proxy_rows),
                        "proxy": "HSI",
                        "reason": "hk_stock_daily_missing_using_hsi_proxy",
                        "sample": [
                            {key: row.get(key) for key in ("symbol", "market", "trade_date", "price", "close", "latest_price", "market_proxy_for")}
                            for row in proxy_rows[:5]
                        ],
                        "reader_degraded": reader.degraded,
                        "reader_errors": reader.errors[-5:],
                    }
        elif market == "cn_futures":
            from CNFutures.adapter import CNFuturesAdapter, READER_MARKET

            adapter = CNFuturesAdapter(reader=None, universe_filter={"max_symbols": 5})
            symbols = adapter.get_intraday_universe(datetime.now(timezone.utc).strftime("%Y%m%d"))
            priced_rows = []
            latest_bar_time = ""
            for symbol in symbols[:5]:
                rows = adapter.get_bars_intraday(READER_MARKET, symbol, interval="5min")
                priced = [row for row in rows if _price(row) > 0]
                if priced:
                    priced_rows.append(priced[-1])
                    latest_bar_time = max(latest_bar_time, str(priced[-1].get("bar_time") or ""))
            session_state = _market_session_state(market)
            status = "ok" if priced_rows else ("warn" if symbols and session_state["samples_expected_today"] else "fail" if not symbols else "ok")
            reason = "" if priced_rows else ("futures_intraday_bars_missing" if symbols and session_state["samples_expected_today"] else "futures_intraday_waiting_for_next_session" if symbols else "futures_universe_missing")
            return {
                "status": status,
                "asset_count": len(symbols),
                "priced_signal_count": len(priced_rows),
                "latest_bar_time": latest_bar_time,
                "reason": reason,
                "market_session": session_state,
                "sample": [
                    {key: row.get(key) for key in ("symbol", "ts_code", "market", "trade_date", "bar_time", "close", "price")}
                    for row in priced_rows[:5]
                ],
                "reader_degraded": False,
                "reader_errors": [],
            }
        ok = bool(priced_rows) and not reader.degraded
        return {
            "status": "ok" if ok else ("warn" if priced_rows else "fail"),
            "asset_count": asset_count,
            "priced_signal_count": len(priced_rows),
            "sample": [
                {key: row.get(key) for key in ("symbol", "ts_code", "market_id", "trade_date", "price", "close", "latest_price", "yes_price")}
                for row in priced_rows[:5]
            ],
            "reader_degraded": reader.degraded,
            "reader_errors": reader.errors[-5:],
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "fail", "error": f"{exc.__class__.__name__}: {exc}"}


def _check_sim_market_loop(market: str, crontab_text: str = "", crontab_error: str = "") -> Check:
    data = _probe_market_data(market)
    ledger = _sim_ledger_summary(market)
    cron_result = _latest_cron_result(market)
    wrapper = SIM_WRAPPERS.get(market, "")
    cron_installed = bool(wrapper and wrapper in crontab_text)
    session_state = _market_session_state(market)
    samples_expected = bool(session_state.get("samples_expected_today"))

    hard_fail_reasons: list[str] = []
    warn_reasons: list[str] = []
    if not cron_installed:
        hard_fail_reasons.append("cron_missing")
    if data.get("status") == "fail":
        if market == "cn_futures":
            warn_reasons.append("futures_market_data_not_ready")
        else:
            hard_fail_reasons.append("market_data_missing")
    elif data.get("status") == "warn":
        warn_reasons.append("market_data_degraded")
    if market not in {"ashare", "cn_futures"} and int(ledger.get("trade_rows") or 0) <= 0:
        if market == "pm" and data.get("reason") in {"pm_market_rows_empty", "pm_prices_missing", "pm_model_probability_missing", "pm_model_edge_below_threshold"}:
            warn_reasons.append("pm_waiting_for_market_data")
        elif market == "crypto" and data.get("reason") in {
            "crypto_klines_empty",
            "crypto_insufficient_priced_rows",
            "crypto_momentum_threshold_not_met",
        }:
            warn_reasons.append("crypto_waiting_for_momentum_signal")
        else:
            hard_fail_reasons.append("sim_trade_ledger_empty")
    if market == "ashare" and int(ledger.get("trade_rows") or 0) <= 0 and samples_expected:
        warn_reasons.append("server_local_sim_has_no_production_trades_yet")
    if market == "cn_futures" and int(ledger.get("review_rows") or 0) <= 0 and samples_expected:
        warn_reasons.append("cn_futures_review_has_no_samples_yet")
    payload = cron_result.get("payload") or {}
    if payload and payload.get("status") not in {"ok", "market_closed"}:
        if market == "hk" and payload.get("status") == "no_data":
            hard_fail_reasons.append("latest_cron_no_data")
        else:
            warn_reasons.append(f"latest_cron_status={payload.get('status')}")
    elif not payload and market in {"crypto", "pm"}:
        warn_reasons.append("latest_cron_json_missing")
    if crontab_error and not crontab_text:
        warn_reasons.append("crontab_unreadable")

    status = "fail" if hard_fail_reasons else ("warn" if warn_reasons else "pass")
    return Check(
        f"{market}_sim_loop",
        status,
        f"{market} 模拟盘闭环{'正常' if status == 'pass' else '需要处理'}",
        {
            "market": market,
            "cron_installed": cron_installed,
            "wrapper": wrapper,
            "crontab_error": crontab_error,
            "data_probe": data,
            "ledger": ledger,
            "latest_cron_result": cron_result,
            "market_session": session_state,
            "fail_reasons": hard_fail_reasons,
            "warn_reasons": warn_reasons,
        },
        severity="error" if status == "fail" else ("warn" if status == "warn" else "info"),
    )


def run_sim_market_health(markets: tuple[str, ...] = SIM_MARKETS) -> dict[str, Any]:
    crontab_text, crontab_error = _installed_crontab_text()
    checks = [_check_sim_market_loop(market, crontab_text, crontab_error) for market in markets]
    failed = [check for check in checks if check.status == "fail"]
    warned = [check for check in checks if check.status == "warn"]
    overall = "fail" if failed else ("warn" if warned else "pass")
    return {
        "market": "all_sim",
        "generated_at": _now_iso(),
        "overall_status": overall,
        "summary": {
            "pass": sum(1 for check in checks if check.status == "pass"),
            "warn": len(warned),
            "fail": len(failed),
        },
        "checks": [check.__dict__ for check in checks],
    }


def run_all_health(*, mini_health_url: str = DEFAULT_MINI_HEALTH_URL) -> dict[str, Any]:
    ashare = run_ashare_health(mini_health_url=mini_health_url)
    sim = run_sim_market_health()
    checks = list(ashare["checks"]) + list(sim["checks"])
    failed = [check for check in checks if check.get("status") == "fail"]
    warned = [check for check in checks if check.get("status") == "warn"]
    overall = "fail" if failed else ("warn" if warned else "pass")
    return {
        "market": "all",
        "generated_at": _now_iso(),
        "overall_status": overall,
        "summary": {
            "pass": sum(1 for check in checks if check.get("status") == "pass"),
            "warn": len(warned),
            "fail": len(failed),
        },
        "sections": {"ashare": ashare, "sim_markets": sim},
        "checks": checks,
    }


def run_ashare_health(*, mini_health_url: str = DEFAULT_MINI_HEALTH_URL) -> dict[str, Any]:
    checks = [
        _check_ashare_universe(),
        _check_shadow_ledger(),
        _check_signal_queues(),
        _check_optional_mini_health(mini_health_url),
        _check_simulated_position_sync(),
        _check_local_sim_ledger(),
        _check_ashare_capital_plan_alignment(),
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


def run_cn_futures_health() -> dict[str, Any]:
    try:
        from shared.runtime_test.cn_futures_live_check import run_live_check

        return run_live_check()
    except Exception as exc:  # noqa: BLE001
        return {
            "market": "cn_futures",
            "generated_at": _now_iso(),
            "overall_status": "fail",
            "summary": {"pass": 0, "warn": 0, "fail": 1},
            "checks": [
                Check(
                    "cn_futures_live_chain",
                    "fail",
                    "CNFutures 只读健康检查入口失败",
                    {"error": f"{exc.__class__.__name__}: {exc}"},
                ).__dict__
            ],
            "real_trading_enabled": False,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="tradingagent market health checks")
    parser.add_argument("--market", default="ashare", choices=["ashare", "cn_futures", "sim", "all"], help="market scope to check")
    parser.add_argument("--mini-health-url", default=DEFAULT_MINI_HEALTH_URL)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    if args.market == "ashare":
        result = run_ashare_health(mini_health_url=args.mini_health_url)
    elif args.market == "cn_futures":
        result = run_cn_futures_health()
    elif args.market == "sim":
        result = run_sim_market_health()
    elif args.market == "all":
        result = run_all_health(mini_health_url=args.mini_health_url)
    else:  # pragma: no cover
        raise ValueError(args.market)
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=False))
    return 0 if result["overall_status"] != "fail" else 2


if __name__ == "__main__":
    raise SystemExit(main())
