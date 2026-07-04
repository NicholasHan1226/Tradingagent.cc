#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from shared.notify.email_sender import send_email, send_template_email
from shared.notify.email_templates import CHANNELS, wrap_html

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "shared"
DAILY_BRIEF_MARKETS = ("Ashare", "Crypto", "US", "PM")
DAILY_BRIEF_CAPITAL_BASE = 100000.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def trade_date() -> str:
    """Return current trading date in Beijing time (UTC+8).

    At early morning hours (00:00-08:00 BJT), the trading date is still the
    previous calendar day because markets have not opened yet.
    """
    import zoneinfo
    try:
        bj_tz = zoneinfo.ZoneInfo("Asia/Shanghai")
    except Exception:
        bj_tz = timezone(timedelta(hours=8))
    now_bj = datetime.now(bj_tz)
    # Before 08:00 BJT, use previous day as trade_date
    if now_bj.hour < 8:
        now_bj = now_bj - timedelta(days=1)
    return now_bj.strftime("%Y%m%d")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def write_markdown(path: Path, title: str, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    lines = [
        f"# {title}",
        "",
        f"- generated_at: {payload['generated_at']}",
        f"- job: {payload['job']}",
        f"- state: {payload['state']}",
        f"- note: {payload['note']}",
        "",
        "```json",
        json.dumps(payload, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _read_last_jsonl(path: Path) -> dict[str, Any]:
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except FileNotFoundError:
        return {}
    for line in reversed(lines):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                rows.append(data)
    except FileNotFoundError:
        return []
    return rows


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, str):
            value = value.strip().rstrip("%")
        result = float(value)
        if result != result:
            return default
        return result
    except (TypeError, ValueError):
        return default


def _compact_date(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:8]


def _normalize_market(value: Any) -> str:
    raw = str(value or "").strip().lower()
    mapping = {
        "ashare": "Ashare",
        "a_share": "Ashare",
        "a-share": "Ashare",
        "cn": "Ashare",
        "china": "Ashare",
        "crypto": "Crypto",
        "digital_asset": "Crypto",
        "us": "US",
        "usa": "US",
        "pm": "PM",
        "polymarket": "PM",
        "prediction_market": "PM",
    }
    return mapping.get(raw, str(value or "unknown") or "unknown")


def _market_from_symbol(symbol: Any) -> str:
    raw = str(symbol or "").strip().upper()
    if raw.endswith((".SH", ".SZ", ".BJ")):
        return "Ashare"
    if raw.startswith("PM-"):
        return "PM"
    if "-" in raw or raw.endswith("USDT"):
        return "Crypto"
    return "unknown"


def _trade_time_hhmm(row: dict[str, Any]) -> str:
    raw = str(row.get("created_at") or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 12:
        return digits[8:12]
    if "T" in raw:
        return raw.split("T", 1)[1][:5].replace(":", "")
    if " " in raw:
        return raw.split(" ", 1)[1][:5].replace(":", "")
    return ""


def _is_morning_trade(row: dict[str, Any]) -> bool:
    hhmm = _trade_time_hhmm(row)
    return bool(hhmm) and hhmm < "1135"


def _load_position_snapshot(as_of_date: str) -> list[dict[str, Any]]:
    try:
        from shared.accounting import position_ledger

        rows = position_ledger.get_positions(capital_layer="all")
    except Exception:
        return []

    snapshot: list[dict[str, Any]] = []
    as_of = _compact_date(as_of_date)
    for row in rows:
        entry_date = _compact_date(row.get("entry_date"))
        if entry_date and entry_date > as_of:
            continue
        entry = dict(row)
        entry["market"] = _normalize_market(entry.get("market")) if entry.get("market") else _market_from_symbol(entry.get("ts_code"))
        snapshot.append(entry)
    return snapshot


def _load_shadow_trades_for_date(target_date: str) -> list[dict[str, Any]]:
    from shared.review import daily_review as review_driver

    rows = []
    for row in review_driver.load_shadow_trades(target_date):
        entry = dict(row)
        entry["market"] = _normalize_market(entry.get("market")) if entry.get("market") else _market_from_symbol(entry.get("ts_code"))
        rows.append(entry)
    return rows


def _market_matches(row: dict[str, Any], market: str) -> bool:
    target = _normalize_market(market)
    row_market = _normalize_market(row.get("market")) if row.get("market") else _market_from_symbol(row.get("ts_code"))
    return row_market == target


def _position_notional(row: dict[str, Any]) -> float:
    cost_basis = _safe_float(row.get("cost_basis"))
    if cost_basis:
        return abs(cost_basis)
    quantity = _safe_float(row.get("quantity"))
    price = _safe_float(row.get("avg_price") or row.get("cost") or row.get("price"))
    return abs(quantity * price)


def _position_current_price(row: dict[str, Any]) -> float:
    for key in ("current_price", "last_close", "close", "price", "avg_price", "cost"):
        price = _safe_float(row.get(key))
        if price > 0:
            return price
    return 0.0


def _positions_for_market(positions: list[dict[str, Any]], market: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in positions:
        entry = dict(row)
        if not entry.get("market"):
            entry["market"] = _market_from_symbol(entry.get("ts_code"))
        if _market_matches(entry, market):
            rows.append(entry)
    return rows


def _latest_strategy_configs(markets: tuple[str, ...] = DAILY_BRIEF_MARKETS) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    snapshots: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for market in markets:
        try:
            adapter = get_market_adapter(market)
            config = adapter.get_strategy_config()
        except Exception as exc:  # noqa: BLE001
            errors.append({"market": market, "error": f"{exc.__class__.__name__}: {exc}"})
            continue
        if not isinstance(config, dict):
            errors.append({"market": market, "error": "strategy config is not a dict"})
            continue
        encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        strategies = config.get("strategies") if isinstance(config.get("strategies"), dict) else {}
        snapshots.append({
            "market": _normalize_market(config.get("market") or market),
            "adapter": adapter.__class__.__name__,
            "version_hash": hashlib.sha256(encoded).hexdigest()[:16],
            "strategy_count": len(strategies),
            "strategies": sorted(strategies),
            "portfolio_method": config.get("portfolio_method", "unknown"),
            "regime": config.get("regime", "unknown"),
            "max_candidates": config.get("max_candidates"),
            "shadow_capital": config.get("shadow_capital"),
            "market_rules": config.get("market_rules", {}),
        })
    return snapshots, errors


def _state_from_errors(errors: list[Any], *, no_input: bool = False) -> str:
    return "degraded" if errors or no_input else "ok"


def _count_signals(trades: list[dict[str, Any]]) -> int:
    signal_ids = {
        str(row.get("signal_id", "")).strip()
        for row in trades
        if str(row.get("signal_id", "")).strip()
    }
    return len(signal_ids) if signal_ids else len(trades)


def _trade_notional(trades: list[dict[str, Any]]) -> float:
    return sum(abs(_safe_float(row.get("quantity")) * _safe_float(row.get("price"))) for row in trades)


def _pnl_pct_from_trades(pnl_value: Any, trades: list[dict[str, Any]]) -> float:
    notional = _trade_notional(trades)
    pnl = _safe_float(pnl_value)
    return round(pnl / notional, 6) if notional else 0.0


def _market_trade_summary(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for market in DAILY_BRIEF_MARKETS:
        grouped[market] = {
            "market": market,
            "trade_count": 0,
            "signal_count": 0,
            "pnl": 0.0,
            "state": "idle",
        }
    for row in trades:
        market = _normalize_market(row.get("market")) if row.get("market") else _market_from_symbol(row.get("ts_code"))
        if market not in grouped:
            grouped[market] = {
                "market": market,
                "trade_count": 0,
                "signal_count": 0,
                "pnl": 0.0,
                "state": "idle",
            }
        grouped[market]["trade_count"] += 1
        grouped[market]["pnl"] = round(grouped[market]["pnl"] + _safe_float(row.get("pnl")), 6)
        grouped[market]["state"] = "active"
    for market, summary in grouped.items():
        market_trades = [row for row in trades if _normalize_market(row.get("market")) == market]
        summary["signal_count"] = _count_signals(market_trades)
    return grouped


def _position_market_summary(positions: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in positions:
        market = _normalize_market(row.get("market")) if row.get("market") else _market_from_symbol(row.get("ts_code"))
        counts[market] = counts.get(market, 0) + 1
    return counts


def _shadow_layer_review(review_payload: dict[str, Any]) -> dict[str, Any]:
    layer_reviews = review_payload.get("capital_layer_reviews")
    if isinstance(layer_reviews, dict):
        shadow = layer_reviews.get("shadow")
        if isinstance(shadow, dict):
            return shadow
    if review_payload.get("capital_layer") == "shadow":
        return review_payload
    return {}


def _positions_for_template(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for row in positions[:12]:
        quantity = int(_safe_float(row.get("quantity")))
        avg_price = _safe_float(row.get("avg_price") or row.get("cost"))
        rendered.append({
            "ts_code": row.get("ts_code", ""),
            "name": row.get("market", ""),
            "quantity": quantity or "",
            "cost": avg_price,
            "last_close": avg_price,
            "current_price": avg_price,
            "pnl_pct": _safe_float(row.get("pnl_pct")),
        })
    return rendered


def _capital_summary(positions: list[dict[str, Any]]) -> dict[str, Any]:
    allocated = sum(_safe_float(row.get("cost_basis")) for row in positions)
    available = max(0.0, DAILY_BRIEF_CAPITAL_BASE - allocated)
    return {
        "available": round(available, 2),
        "allocated": round(allocated, 2),
        "reserve": round(available, 2),
        "reverse_repo": 0.0,
    }


def _market_focus_rows(
    trades: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    layer_review: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    review_market_map = {}
    if isinstance(layer_review, dict):
        review_market_map = layer_review.get("market_reviews") or {}
    trade_summary = _market_trade_summary(trades)
    position_counts = _position_market_summary(positions)
    rows: list[dict[str, str]] = []
    for market in DAILY_BRIEF_MARKETS:
        summary = trade_summary.get(market, {"trade_count": 0, "signal_count": 0, "pnl": 0.0, "state": "idle"})
        market_review = review_market_map.get(market) if isinstance(review_market_map, dict) else {}
        pnl_value = _safe_float((market_review or {}).get("pnl"), summary.get("pnl", 0.0))
        signal_count = int((market_review or {}).get("signal_count", summary.get("signal_count", 0)) or 0)
        trade_count = int((market_review or {}).get("trades", summary.get("trade_count", 0)) or 0)
        position_count = int(position_counts.get(market, 0))
        direction = "活跃" if trade_count or position_count else "待观察"
        reason = (
            f"signals={signal_count}, trades={trade_count}, positions={position_count}, "
            f"pnl={pnl_value:.4f}"
        )
        rows.append({"sector": market, "direction": direction, "reason": reason})
    return rows


def _build_system_health(
    trades: list[dict[str, Any]],
    midday_review: dict[str, Any],
    nightly_review: dict[str, Any],
) -> dict[str, Any]:
    checks = {
        "shadow_trades": bool(trades),
        "midday_review": bool(midday_review),
        "nightly_review": bool(nightly_review),
        "daily_log": bool(_read_last_jsonl(SHARED / "review/data/daily_reviews.jsonl")),
    }
    ok_count = sum(1 for value in checks.values() if value)
    label = "healthy" if ok_count >= 3 else "degraded" if ok_count >= 2 else "cold_start"
    latest_review = nightly_review or midday_review
    latest_session = str(latest_review.get("session") or "--")
    detail = ", ".join(f"{name}={'ok' if ok else 'missing'}" for name, ok in checks.items())
    return {
        "status": label,
        "ok_count": ok_count,
        "checks": checks,
        "latest_review_session": latest_session,
        "detail": detail,
    }


def _summary_text(title: str, trade_date_value: str, extra_lines: list[str]) -> str:
    lines = [f"{title} {trade_date_value}"] + [line for line in extra_lines if line]
    return "\n".join(lines)


def _afternoon_plan_rows(plan: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for target in plan.get("reduce", []) or []:
        rows.append({"action": "减仓", "target": str(target), "condition": "触及止损或动量衰减"})
    for target in plan.get("add", []) or []:
        rows.append({"action": "加仓", "target": str(target), "condition": "信号未充分兑现"})
    for target in plan.get("watch", []) or []:
        rows.append({"action": "观察", "target": str(target), "condition": "等待下午确认"})
    notes = str(plan.get("notes", "") or "").strip()
    if notes:
        rows.append({"action": "备注", "target": "全市场", "condition": notes})
    return rows


def _trade_rows(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in trades:
        rows.append({
            "ts_code": row.get("ts_code", ""),
            "side": row.get("side", ""),
            "quantity": _safe_float(row.get("quantity")),
            "price": _safe_float(row.get("price")),
            "pnl": _safe_float(row.get("pnl")),
        })
    return rows


def _parse_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    cleaned = raw.strip("[]")
    candidates = [
        cleaned,
        cleaned.replace("Z", "+00:00"),
    ]
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y%m%dT%H%M%S%z"):
        try:
            return datetime.strptime(cleaned, fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _format_trade_day(value: str) -> str:
    compact = _compact_date(value)
    if len(compact) == 8:
        return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
    return str(value or "")


def _week_window(anchor_trade_date: str) -> tuple[str, str]:
    anchor = datetime.strptime(_compact_date(anchor_trade_date), "%Y%m%d")
    week_start = anchor - timedelta(days=anchor.weekday())
    return week_start.strftime("%Y%m%d"), anchor.strftime("%Y%m%d")


def _iter_trade_dates(start_trade_date: str, end_trade_date: str) -> list[str]:
    start = datetime.strptime(_compact_date(start_trade_date), "%Y%m%d")
    end = datetime.strptime(_compact_date(end_trade_date), "%Y%m%d")
    dates: list[str] = []
    current = start
    while current <= end:
        dates.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return dates


def _market_scope_for_job(job_name: str) -> str | None:
    lowered = str(job_name or "").lower()
    mapping = {
        "job_us_weekly": "US",
        "job_crypto_weekly": "Crypto",
        "job_pm_weekly": "PM",
        "job_ashare_weekly": "Ashare",
    }
    return mapping.get(lowered)


def _load_week_shadow_trades(anchor_trade_date: str, market: str | None = None) -> tuple[list[dict[str, Any]], str, str]:
    from shared.review import daily_review as review_driver

    week_start, week_end = _week_window(anchor_trade_date)
    rows: list[dict[str, Any]] = []
    for one_day in _iter_trade_dates(week_start, week_end):
        for row in review_driver.load_shadow_trades(one_day):
            entry = dict(row)
            entry["market"] = _normalize_market(entry.get("market")) if entry.get("market") else _market_from_symbol(entry.get("ts_code"))
            if market and entry["market"] != market:
                continue
            rows.append(entry)
    return rows, week_start, week_end


def _weekly_strategy_rows(
    strategy_stats: dict[str, dict[str, Any]],
    week_trades: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    grouped_notional: dict[str, float] = {}
    for trade in week_trades:
        strategy = str(trade.get("strategy") or "unattributed")
        grouped_notional[strategy] = grouped_notional.get(strategy, 0.0) + abs(
            _safe_float(trade.get("quantity")) * _safe_float(trade.get("price"))
        )
    for name, stats in strategy_stats.items():
        notional = grouped_notional.get(name, 0.0)
        pnl = _safe_float(stats.get("pnl"))
        rows.append({
            "name": name,
            "trades": int(stats.get("trades", 0) or 0),
            "win_rate": _safe_float(stats.get("win_rate")),
            "pnl": pnl,
            "pnl_pct": round(pnl / notional, 6) if notional else 0.0,
        })
    rows.sort(key=lambda item: (_safe_float(item.get("pnl")), _safe_float(item.get("win_rate"))), reverse=True)
    return rows


def _weekly_daily_rows(week_trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in week_trades:
        trade_day = _compact_date(trade.get("trade_date") or trade.get("created_at") or "")
        grouped.setdefault(trade_day, []).append(trade)
    rows: list[dict[str, Any]] = []
    for trade_day in sorted(grouped):
        trades = grouped[trade_day]
        pnl = sum(_safe_float(item.get("pnl")) for item in trades)
        rows.append({
            "date": _format_trade_day(trade_day),
            "pnl": round(pnl, 6),
            "pnl_pct": _pnl_pct_from_trades(pnl, trades),
        })
    return rows


def _weekly_trends(layer_review: dict[str, Any]) -> dict[str, str]:
    dimensions = layer_review.get("dimension_effectiveness") or {}
    ranked = sorted(
        ((str(name), _safe_float(value)) for name, value in dimensions.items() if str(name) != "unattributed"),
        key=lambda item: item[1],
        reverse=True,
    )
    leaders = ", ".join(f"{name}({_safe_float(value):+.2f})" for name, value in ranked[:2]) or "暂无显著强势维度"
    laggards = ", ".join(f"{name}({_safe_float(value):+.2f})" for name, value in ranked[-2:] if _safe_float(value) < 0) or "暂无显著弱势维度"
    promotes = len(layer_review.get("strategies_to_promote") or [])
    eliminates = len(layer_review.get("strategies_to_eliminate") or [])
    adjustments = len(layer_review.get("conditions_to_adjust") or [])
    return {
        "market": (
            f"shadow 周胜率 {_safe_float(layer_review.get('week_win_rate')):.0%}, "
            f"周交易 {int(layer_review.get('week_trade_count', 0) or 0)} 笔"
        ),
        "sectors": f"强势维度: {leaders}; 弱势维度: {laggards}",
        "capital_flow": f"升级候选 {promotes} 个, 降级候选 {eliminates} 个, 条件调参 {adjustments} 项",
    }


def _weekly_next_week_rows(layer_review: dict[str, Any], strategy_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for name in layer_review.get("strategies_to_promote") or []:
        rows.append({"focus": f"策略 {name}", "action": "保持正反馈，准备升级到更高权重或模拟盘验证"})
    for name in layer_review.get("strategies_to_eliminate") or []:
        rows.append({"focus": f"策略 {name}", "action": "连续两周偏弱，下周降权或临时下线观察"})
    for name in layer_review.get("conditions_to_adjust") or []:
        rows.append({"focus": f"条件 {name}", "action": "条件贡献转负，下周收紧触发或延后执行"})
    if not rows and strategy_rows:
        leader = strategy_rows[0]
        rows.append({
            "focus": f"策略 {leader['name']}",
            "action": "延续本周最强策略，同时观察是否还能维持正胜率",
        })
    if not rows:
        rows.append({"focus": "全市场", "action": "本周样本不足，继续积累 shadow 交易并等待下周复盘"})
    return rows[:8]


def _compose_weekly_email_data(
    week_start: str,
    week_end: str,
    week_trades: list[dict[str, Any]],
    layer_review: dict[str, Any],
) -> dict[str, Any]:
    total_pnl = _safe_float(layer_review.get("week_pnl"))
    strategy_rows = _weekly_strategy_rows(layer_review.get("strategy_win_rates") or {}, week_trades)
    next_week = _weekly_next_week_rows(layer_review, strategy_rows)
    promote_names = ", ".join(layer_review.get("strategies_to_promote") or []) or "无"
    eliminate_names = ", ".join(layer_review.get("strategies_to_eliminate") or []) or "无"
    data = {
        "week_range": f"{_format_trade_day(week_start)} ~ {_format_trade_day(week_end)}",
        "date": _format_trade_day(week_end),
        "weekly_pnl": total_pnl,
        "weekly_pnl_pct": _pnl_pct_from_trades(total_pnl, week_trades),
        "benchmark_pnl_pct": 0.0,
        "strategy_stats": strategy_rows,
        "daily_pnl": _weekly_daily_rows(week_trades),
        "trends": _weekly_trends(layer_review),
        "next_week": next_week,
        "summary": _summary_text(
            "周度复盘",
            _format_trade_day(week_end),
            [
                f"周胜率={_safe_float(layer_review.get('week_win_rate')):.2%}",
                f"周PnL={total_pnl:.4f}",
                f"升级候选={promote_names}",
                f"降级候选={eliminate_names}",
            ],
        ),
    }
    data.update(_ops_template_fields())
    return data


def _latest_ops_report() -> dict[str, Any]:
    return _read_json(SHARED / "review/ops/tradings_ops_latest.json")


def _ops_template_fields() -> dict[str, Any]:
    report = _latest_ops_report()
    if not report:
        return {"ops_status": "missing", "ops_summary": "暂无运维报告"}
    totals = (report.get("queue_summary") or {}).get("totals") or {}
    receipts = report.get("receipt_integrity") or {}
    shadow_totals = (report.get("shadow_queue_summary") or {}).get("totals") or {}
    failures = report.get("failure_summary") or {}
    return {
        "ops_status": report.get("overall_status", "unknown"),
        "ops_generated_at": report.get("generated_at", ""),
        "ops_queue_summary": totals,
        "ops_shadow_queue_summary": shadow_totals,
        "ops_receipt_integrity": receipts,
        "ops_failure_summary": failures.get("by_category") or {},
        "ops_recommendations": report.get("recommendations") or [],
        "ops_summary": (
            f"status={report.get('overall_status', 'unknown')}, "
            f"pending={totals.get('pending', 0)}, running={totals.get('running', 0)}, "
            f"failed={totals.get('failed', 0)}, expired={totals.get('expired', 0)}, "
            f"shadow_pending={shadow_totals.get('pending', 0)}, "
            f"receipt_invalid={receipts.get('invalid', 0)}"
        ),
    }

def _cron_log_snapshot() -> dict[str, Any]:
    log_dir = SHARED / "logs" / "cron"
    records: list[dict[str, Any]] = []
    latest_dt: datetime | None = None
    failed_jobs: list[str] = []
    failure_keywords = (" traceback", "traceback", " exception", " failed", " error")
    benign_markers = (
        " success ",
        "skipped=already_running",
        '"state": "ok"',
        "'state': 'ok'",
        '"errors": []',
        "'errors': []",
    )
    scan_tail_lines = 80
    for path in sorted(log_dir.glob("*.log")):
        try:
            lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        except FileNotFoundError:
            continue
        if not lines:
            continue
        failed = False
        reasons: list[str] = []
        for line in lines[-scan_tail_lines:]:
            lowered = line.lower()
            if any(marker in lowered for marker in benign_markers):
                failed = False
                reasons = []
                continue
            if any(keyword in lowered for keyword in failure_keywords) and "0 error" not in lowered:
                failed = True
                reasons.append(line[:160])
        first_token = lines[-1].split("]", 1)[0].lstrip("[")
        line_dt = _parse_datetime(first_token)
        if line_dt and (latest_dt is None or line_dt > latest_dt):
            latest_dt = line_dt
        if failed:
            failed_jobs.append(path.stem)
        records.append({
            "job": path.stem,
            "failed": failed,
            "line_count": len(lines),
            "scan_tail_lines": min(len(lines), scan_tail_lines),
            "sample": reasons[:2] or [lines[-1][:160]],
        })
    return {
        "jobs_checked": len(records),
        "records": records,
        "failed_jobs": sorted(set(failed_jobs)),
        "latest_run": latest_dt.isoformat(timespec="seconds") if latest_dt else "",
    }


def _latest_self_heal_snapshot() -> dict[str, Any]:
    actions = _read_jsonl_dicts(SHARED / "review/heal/self_heal_actions.jsonl")
    report = _read_json(SHARED / "review/heal/heal_report.json")
    latest = report or (actions[-1] if actions else {})
    return {
        "latest": latest,
        "actions_count": len(actions),
        "latest_cycle_at": str(latest.get("cycle_at") or latest.get("generated_at") or ""),
        "issues_found": int(latest.get("issues_found", 0) or 0),
        "issues_fixed": int(latest.get("issues_fixed", 0) or 0),
        "issues_escalated": int(latest.get("issues_escalated", 0) or 0),
        "rule_updates": latest.get("rule_updates") or [],
    }


def _consecutive_zero_signal_periods(rows: list[dict[str, Any]]) -> int:
    streak = 0
    for row in reversed(rows):
        if row.get("capital_layer") not in ("", None, "shadow"):
            continue
        signal_count = row.get("signal_count")
        if signal_count is None:
            signal_count = ((row.get("capital_layer_reviews") or {}).get("shadow") or {}).get("signal_count")
        if signal_count is None:
            continue
        if int(signal_count or 0) > 0:
            break
        streak += 1
    return streak


def _self_heal_context() -> dict[str, Any]:
    current_trade_date = trade_date()
    trades = _load_shadow_trades_for_date(current_trade_date)
    positions = _load_position_snapshot(current_trade_date)
    cron_snapshot = _cron_log_snapshot()
    daily_rows = _read_jsonl_dicts(SHARED / "review/data/daily_reviews.jsonl")

    latest_activity = [
        dt
        for dt in (
            [_parse_datetime(item.get("created_at")) for item in trades]
            + [_parse_datetime(cron_snapshot.get("latest_run"))]
        )
        if dt is not None
    ]
    if latest_activity:
        age_minutes = max(0.0, (datetime.now(timezone.utc) - max(latest_activity)).total_seconds() / 60.0)
    else:
        age_minutes = 999.0

    total_pnl = sum(_safe_float(row.get("pnl")) for row in trades)
    pipeline_errors = {
        record["job"]: {"runs": 1, "errors": 1 if record.get("failed") else 0}
        for record in cron_snapshot.get("records", [])
    }
    position_rows = []
    for row in positions:
        weight_pct = _safe_float(row.get("weight_pct"))
        if not weight_pct:
            cost_basis = _safe_float(row.get("cost_basis"))
            if not cost_basis:
                cost_basis = abs(_safe_float(row.get("quantity")) * _safe_float(row.get("avg_price") or row.get("cost")))
            weight_pct = round(cost_basis / DAILY_BRIEF_CAPITAL_BASE * 100, 4) if cost_basis else 0.0
        position_rows.append({
            "ts_code": row.get("ts_code"),
            "weight_pct": weight_pct,
        })
    return {
        "data_age_minutes": round(age_minutes, 2),
        "pipeline_errors": pipeline_errors,
        "intraday_pnl_pct": _pnl_pct_from_trades(total_pnl, trades),
        "periods_without_signal": _consecutive_zero_signal_periods(daily_rows),
        "positions": position_rows,
        "freeze_active": False,
        "in_sample_tuning_detected": False,
    }


def _system_health_email_data(self_heal_snapshot: dict[str, Any], cron_snapshot: dict[str, Any]) -> dict[str, Any]:
    failed_jobs = cron_snapshot.get("failed_jobs") or []
    issues_found = int(self_heal_snapshot.get("issues_found", 0) or 0)
    issues_escalated = int(self_heal_snapshot.get("issues_escalated", 0) or 0)
    actions_count = int(self_heal_snapshot.get("actions_count", 0) or 0)
    if issues_escalated or failed_jobs:
        overall_status = "critical"
    elif issues_found or not actions_count:
        overall_status = "degraded"
    else:
        overall_status = "healthy"
    collection_status = "ok" if actions_count else "degraded"
    pipeline_status = "critical" if failed_jobs else "ok"
    integrity_status = "passed" if not issues_escalated else "failed"
    gaps = []
    if not actions_count:
        gaps.append("self_heal log missing")
    if failed_jobs:
        gaps.append("cron failures detected")
    return {
        "overall_status": overall_status,
        "date": trade_date(),
        "collection": {
            "status": collection_status,
            "sources": f"self_heal_actions={actions_count}, cron_logs={int(cron_snapshot.get('jobs_checked', 0) or 0)}",
            "last_update": self_heal_snapshot.get("latest_cycle_at") or cron_snapshot.get("latest_run") or "--",
            "gaps": ", ".join(gaps) if gaps else "无",
        },
        "pipeline": {
            "status": pipeline_status,
            "stages": int(cron_snapshot.get("jobs_checked", 0) or 0),
            "failed_stages": ", ".join(failed_jobs) if failed_jobs else "无",
            "last_run": cron_snapshot.get("latest_run") or "--",
        },
        "integrity": {
            "status": integrity_status,
            "checks_passed": max(0, 3 - len(failed_jobs) - issues_escalated),
            "checks_failed": len(failed_jobs) + issues_escalated,
            "details": (
                f"issues_found={issues_found}, issues_fixed={int(self_heal_snapshot.get('issues_fixed', 0) or 0)}, "
                f"rule_updates={len(self_heal_snapshot.get('rule_updates') or [])}"
            ),
        },
        "summary": _summary_text(
            "系统健康",
            trade_date(),
            [
                f"overall={overall_status}",
                f"cron_failed={len(failed_jobs)}",
                f"self_heal_escalated={issues_escalated}",
            ],
        ),
    }


def _attribution_rows(layer_review: dict[str, Any]) -> list[dict[str, Any]]:
    attribution = layer_review.get("attribution") or {}
    by_dimension = attribution.get("by_dimension") or {}
    rows = [
        {"factor": str(name), "contribution": _safe_float(value)}
        for name, value in by_dimension.items()
        if name != "unattributed"
    ]
    rows.sort(key=lambda item: abs(_safe_float(item.get("contribution"))), reverse=True)
    return rows[:6]


def _comparison_rows(layer_review: dict[str, Any]) -> list[dict[str, str]]:
    comparisons = layer_review.get("comparisons") or {}
    vs_goals = comparisons.get("vs_goals") or {}
    vs_benchmark = comparisons.get("vs_benchmark") or {}
    vs_last = comparisons.get("vs_last_period") or {}
    return [
        {
            "action": "对比目标",
            "target": "达标" if vs_goals.get("all_goals_met") else "未达标",
            "reason": f"stage={vs_goals.get('stage', '--')}",
        },
        {
            "action": "对比基准",
            "target": "跑赢" if vs_benchmark.get("beat_benchmark") else "未跑赢",
            "reason": f"excess={_safe_float(vs_benchmark.get('excess_return')):.4f}",
        },
        {
            "action": "对比上一周期",
            "target": "改善" if vs_last.get("improved") else "未改善",
            "reason": f"delta={_safe_float(vs_last.get('delta')):.4f}",
        },
    ]


def _tomorrow_plan_rows(layer_review: dict[str, Any]) -> list[dict[str, str]]:
    next_day = layer_review.get("next_day_plan") or {}
    rows = _comparison_rows(layer_review)
    if next_day.get("tighten_stops"):
        rows.append({"action": "收紧止损", "target": "组合", "reason": "胜率未达阶段目标"})
    for name in next_day.get("reduce_dimensions", []) or []:
        rows.append({"action": "降权维度", "target": str(name), "reason": "当日归因为负"})
    for name in next_day.get("reduce_strategies", []) or []:
        rows.append({"action": "降权策略", "target": str(name), "reason": "当日归因为负"})
    notes = str(next_day.get("notes", "") or "").strip()
    if notes:
        rows.append({"action": "备注", "target": "下日", "reason": notes})
    return rows


def _compose_morning_email_data(
    trade_date_value: str,
    trades: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    midday_review: dict[str, Any],
    nightly_review: dict[str, Any],
) -> dict[str, Any]:
    latest_review = nightly_review or midday_review
    latest_layer_review = _shadow_layer_review(latest_review)
    health = _build_system_health(trades, midday_review, nightly_review)
    signal_count = _count_signals(trades)
    market_rows = _market_focus_rows(trades, positions, latest_layer_review)
    return {
        "trade_date": trade_date_value,
        "date": trade_date_value,
        "holdings": _positions_for_template(positions),
        "capital": _capital_summary(positions),
        "market_outlook": {
            "regime": f"shadow {sum(1 for row in market_rows if row['direction'] == '活跃')}/{len(DAILY_BRIEF_MARKETS)} 市场活跃",
            "trend": health["status"],
            "key_levels": f"signals={signal_count}; trades={len(trades)}; latest_review={health['latest_review_session']}",
        },
        "sector_focus": market_rows,
        "strategy": [
            {"name": "系统健康", "action": health["status"], "target": health["detail"]},
            {"name": "今日信号", "action": str(signal_count), "target": f"shadow trades={len(trades)}"},
            {
                "name": "最近复盘",
                "action": health["latest_review_session"],
                "target": str((latest_layer_review.get("next_day_plan") or {}).get("notes") or "--"),
            },
        ],
        "summary": _summary_text(
            "盘前规划",
            trade_date_value,
            [
                f"4市场 shadow 状态已汇总，今日 signal count={signal_count}",
                f"系统健康={health['status']}",
            ],
        ),
    }


def _compose_midday_email_data(
    trade_date_value: str,
    trades: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    review_payload: dict[str, Any],
) -> dict[str, Any]:
    morning_trades = [row for row in trades if _is_morning_trade(row)]
    layer_review = _shadow_layer_review(review_payload)
    morning_pnl = _safe_float(layer_review.get("realized_pnl"), _safe_float(layer_review.get("pnl")))
    win_rate = _safe_float(layer_review.get("win_rate"))
    return {
        "trade_date": trade_date_value,
        "date": trade_date_value,
        "morning_pnl": morning_pnl,
        "morning_pnl_pct": _pnl_pct_from_trades(morning_pnl, morning_trades),
        "morning_trades": _trade_rows(morning_trades),
        "holdings": _positions_for_template(positions),
        "afternoon_plan": _afternoon_plan_rows(layer_review.get("afternoon_plan") or {}),
        "summary": _summary_text(
            "午盘复盘",
            trade_date_value,
            [
                f"上午胜率={win_rate:.2%}",
                f"上午PnL={morning_pnl:.4f}",
                f"signal_count={int(layer_review.get('signal_count', 0) or 0)}",
            ],
        ),
    }


def _compose_nightly_email_data(
    trade_date_value: str,
    trades: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    review_payload: dict[str, Any],
) -> dict[str, Any]:
    layer_review = _shadow_layer_review(review_payload)
    total_pnl = _safe_float(layer_review.get("pnl"))
    comparisons = layer_review.get("comparisons") or {}
    vs_benchmark = comparisons.get("vs_benchmark") or {}
    data = {
        "trade_date": trade_date_value,
        "date": trade_date_value,
        "total_pnl": total_pnl,
        "total_pnl_pct": _pnl_pct_from_trades(total_pnl, trades),
        "benchmark_pnl_pct": 0.0,
        "trades": _trade_rows(trades),
        "attribution": _attribution_rows(layer_review),
        "holdings": _positions_for_template(positions),
        "tomorrow_plan": _tomorrow_plan_rows(layer_review),
        "summary": _summary_text(
            "收盘日报",
            trade_date_value,
            [
                f"今日PnL={total_pnl:.4f}",
                f"vs goals={'met' if (comparisons.get('vs_goals') or {}).get('all_goals_met') else 'miss'}",
                f"vs benchmark={'beat' if vs_benchmark.get('beat_benchmark') else 'lag'}",
                f"vs last={'improved' if (comparisons.get('vs_last_period') or {}).get('improved') else 'down'}",
            ],
        ),
    }
    data.update(_ops_template_fields())
    return data


class StubMarketAdapter:
    """Default no-op adapter until market-specific adapters land."""

    def __init__(self, market: str) -> None:
        self.market = market

    def get_universe(self, date: str) -> list[str]:
        return []

    def get_market(self) -> str:
        return self.market

    def map_symbol_to_reader(self, symbol: str) -> tuple[str, str]:
        return self.market, symbol

    def get_strategy_config(self) -> dict[str, Any]:
        return {
            "shadow_capital": 100000.0,
            "portfolio_method": "conviction_weighted",
            "regime": "unknown",
            "max_candidates": 20,
            "default_price": 1.0,
            "default_volatility": 0.20,
        }

    def get_shadow_account(self) -> str:
        return f"{self.market.lower()}_shadow_stub"


def _build_ashare_adapter() -> Any:
    try:
        from Ashare.adapter import AshareAdapter
        return AshareAdapter()
    except Exception:
        return StubMarketAdapter("Ashare")


def _build_crypto_adapter() -> Any:
    try:
        from Crypto.adapter import CryptoAdapter
        return CryptoAdapter()
    except Exception:
        return StubMarketAdapter("Crypto")


def _build_us_adapter() -> Any:
    try:
        from US.adapter import USAdapter
        return USAdapter()
    except Exception:
        return StubMarketAdapter("US")


MARKET_ADAPTERS: dict[str, Any] = {
    "Ashare": _build_ashare_adapter(),
    "Crypto": _build_crypto_adapter(),
    "US": _build_us_adapter(),
    "PM": StubMarketAdapter("PM"),
}


def _register_default_adapters() -> None:
    try:
        from PM.adapter import PMAdapter
    except Exception:
        return
    register_market_adapter("PM", PMAdapter())


def register_market_adapter(market: str, adapter: Any) -> None:
    MARKET_ADAPTERS[market] = adapter
    get_market = getattr(adapter, "get_market", None)
    if callable(get_market):
        try:
            canonical = str(get_market()).strip()
        except Exception:
            canonical = ""
        if canonical and canonical != market:
            MARKET_ADAPTERS[canonical] = adapter


def get_market_adapter(market: str) -> Any:
    return MARKET_ADAPTERS.get(market) or StubMarketAdapter(market)


def _pm_price_to_close(row: dict[str, Any]) -> float:
    for key in ("yes_price", "last_price", "price", "implied_probability", "probability"):
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price == price:
            return max(0.0, min(1.0, price))
    return 0.5


class PMReaderBridge:
    """Expose PM probability prices through the orchestrator's bar interface."""

    def __init__(self, reader: Any) -> None:
        self.reader = reader

    def __getattr__(self, name: str) -> Any:
        return getattr(self.reader, name)

    def get_bars_daily(self, market: str, symbol: str, start: object = None, end: object = None) -> list[dict[str, Any]]:
        if str(market).lower() != "pm":
            return self.reader.get_bars_daily(market, symbol, start, end)
        rows = self.reader.get_pm_prices(symbol, start, end)
        bars: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            bar = dict(row)
            bar["close"] = _pm_price_to_close(row)
            bars.append(bar)
        return bars


def _pm_score_stock(symbol: str, date: str, data_reader: Any = None) -> dict[str, Any]:
    from PM.scoring import score_market

    return score_market(symbol, date, data_reader=data_reader)


def _crypto_score_stock(symbol: str, date: str, data_reader: Any = None) -> dict[str, Any]:
    from shared.screening.six_dimension_scorer import score_stock

    return score_stock("crypto", symbol, data_reader, date)


def _us_score_stock(symbol: str, date: str, data_reader: Any = None) -> dict[str, Any]:
    from shared.screening.six_dimension_scorer import score_stock

    return score_stock("us", symbol, data_reader, date)


def _pm_orchestrator_deps() -> Any:
    from shared.orchestrator import _default_deps

    deps = _default_deps()
    deps.score_stock = _pm_score_stock
    return deps


def _crypto_orchestrator_deps() -> Any:
    from shared.orchestrator import _default_deps

    deps = _default_deps()
    deps.score_stock = _crypto_score_stock
    return deps


def _us_orchestrator_deps() -> Any:
    from shared.orchestrator import _default_deps

    deps = _default_deps()
    deps.score_stock = _us_score_stock
    return deps


def run_market_watch(job_name: str, market: str, output_rel: str, phase: str) -> dict[str, Any]:
    adapter = get_market_adapter(market)
    config = adapter.get_strategy_config()
    current_trade_date = trade_date()
    universe = adapter.get_universe(current_trade_date)
    payload = {
        "job": job_name,
        "state": "orchestrated",
        "generated_at": now_iso(),
        "capital_layer": "shadow",
        "market": adapter.get_market(),
        "account": adapter.get_shadow_account(),
        "phase": phase,
        "trade_date": current_trade_date,
        "universe_count": len(universe),
        "sample_universe": universe[:10],
        "strategies": sorted(config.get("strategies", {})),
        "market_rules": config.get("market_rules", {}),
    }
    append_jsonl(SHARED / output_rel, payload)
    return payload


def run_shadow_orchestrator(job_name: str, market: str) -> dict[str, Any]:
    from shared.data.reader import TradingagentDataReader
    from shared.orchestrator import run_shadow_loop

    adapter = get_market_adapter(market)
    reader: Any = TradingagentDataReader()
    deps = None
    adapter_market = str(adapter.get_market()).lower()
    if str(market).upper() == "PM" or adapter_market == "pm":
        reader = PMReaderBridge(reader)
        deps = _pm_orchestrator_deps()
    elif str(market).upper() == "CRYPTO" or adapter_market == "crypto":
        deps = _crypto_orchestrator_deps()
    elif str(market).upper() == "US" or adapter_market == "us":
        deps = _us_orchestrator_deps()
    result = run_shadow_loop(adapter, trade_date(), reader, deps=deps)
    result.update({"job": job_name, "state": result.get("state", "ok"), "generated_at": now_iso()})
    append_jsonl(SHARED / "logs/orchestrator_shadow_runs.jsonl", result)
    return result


def run_sim_orchestrator(job_name: str, market: str) -> dict[str, Any]:
    from shared.data.reader import TradingagentDataReader
    from shared.orchestrator import run_sim_loop

    adapter = get_market_adapter(market)
    reader: Any = TradingagentDataReader()
    deps = None
    adapter_market = str(adapter.get_market()).lower()
    if str(market).upper() == "PM" or adapter_market == "pm":
        reader = PMReaderBridge(reader)
        deps = _pm_orchestrator_deps()
    elif str(market).upper() == "CRYPTO" or adapter_market == "crypto":
        deps = _crypto_orchestrator_deps()
    elif str(market).upper() == "US" or adapter_market == "us":
        deps = _us_orchestrator_deps()
    result = run_sim_loop(adapter, trade_date(), reader, deps=deps)
    result.update({"job": job_name, "state": result.get("state", "ok"), "generated_at": now_iso()})
    append_jsonl(SHARED / "logs/orchestrator_sim_runs.jsonl", result)
    return result


_register_default_adapters()


def run_all_market_trading_signals() -> dict[str, Any]:
    results = [
        run_shadow_orchestrator(f"job_trading_signals_{market.lower()}", market)
        for market in ("Ashare", "Crypto", "US", "PM")
    ]
    payload = {
        "job": "job_trading_signals",
        "state": "degraded" if any(item.get("state") == "degraded" for item in results) else "ok",
        "generated_at": now_iso(),
        "capital_layer": "shadow",
        "results": results,
    }
    append_jsonl(SHARED / "logs/orchestrator_shadow_runs.jsonl", payload)
    return payload


def run_daily_brief_morning() -> dict[str, Any]:
    current_trade_date = trade_date()
    trades = _load_shadow_trades_for_date(current_trade_date)
    positions = _load_position_snapshot(current_trade_date)
    midday_review = _read_last_jsonl(SHARED / "review/daily/midday_review.jsonl")
    nightly_review = _read_last_jsonl(SHARED / "review/daily/daily_brief.jsonl")
    email_data = _compose_morning_email_data(current_trade_date, trades, positions, midday_review, nightly_review)
    email_result = send_template_email("pre_market_plan", email_data)
    health = _build_system_health(trades, midday_review, nightly_review)
    payload = {
        "job": "job_daily_brief_morning",
        "phase": "morning",
        "state": "email_sent" if email_result.get("status") == "sent" else "saved_local",
        "generated_at": now_iso(),
        "trade_date": current_trade_date,
        "capital_layer": "shadow",
        "signal_count": _count_signals(trades),
        "shadow_trade_count": len(trades),
        "system_health": health,
        "market_statuses": _market_focus_rows(trades, positions, _shadow_layer_review(nightly_review or midday_review)),
        "email_notification": email_result,
        "email_data": email_data,
    }
    write_json(SHARED / "review/daily/morning_brief.json", payload)
    return payload


def run_daily_brief_day() -> dict[str, Any]:
    from shared.review import daily_review as review_driver

    current_trade_date = trade_date()
    trades = _load_shadow_trades_for_date(current_trade_date)
    positions = _load_position_snapshot(current_trade_date)
    result = review_driver.run_daily_review(current_trade_date, session="lunch")
    email_data = _compose_midday_email_data(current_trade_date, trades, positions, result)
    email_result = send_template_email("midday_review", email_data)
    result.update({
        "job": "job_daily_brief_day",
        "state": "email_sent" if email_result.get("status") == "sent" else "saved_local",
        "phase": "lunch",
        "generated_at": now_iso(),
        "trade_date": current_trade_date,
        "capital_layer": "shadow",
        "shadow_trade_count": len(trades),
        "email_notification": email_result,
        "email_data": email_data,
    })
    append_jsonl(SHARED / "review/daily/midday_review.jsonl", result)
    return result


def run_daily_brief_night() -> dict[str, Any]:
    from shared.review import daily_review as review_driver

    current_trade_date = trade_date()
    trades = _load_shadow_trades_for_date(current_trade_date)
    positions = _load_position_snapshot(current_trade_date)
    result = review_driver.run_daily_review(current_trade_date, session="close")
    email_data = _compose_nightly_email_data(current_trade_date, trades, positions, result)
    email_result = send_template_email("daily_report", email_data)
    result.update({
        "job": "job_daily_brief_night",
        "state": "email_sent" if email_result.get("status") == "sent" else "saved_local",
        "phase": "close",
        "generated_at": now_iso(),
        "trade_date": current_trade_date,
        "capital_layer": "shadow",
        "shadow_trade_count": len(trades),
        "email_notification": email_result,
        "email_data": email_data,
    })
    append_jsonl(SHARED / "review/daily/daily_brief.jsonl", result)
    return result


def run_signal_sweep_expired() -> dict[str, Any]:
    from shared.execution.signal_state_machine import SignalStateMachine

    sweeps: dict[str, Any] = {}
    state = "ok"
    for label, signals_dir in (
        ("execution", ROOT / "signals"),
        ("shadow", ROOT / "signals" / "shadow"),
    ):
        try:
            sweeps[label] = SignalStateMachine(signals_dir).sweep_expired()
        except Exception as exc:  # noqa: BLE001
            sweeps[label] = {
                "status": "error",
                "expired_count": 0,
                "expired": [],
                "message": f"{exc.__class__.__name__}: {exc}",
            }
            state = "degraded"
    result = {
        "job": "job_signal_sweep_expired",
        "state": state,
        "generated_at": now_iso(),
        "trade_date": trade_date(),
        "execution": sweeps.get("execution", {}),
        "shadow": sweeps.get("shadow", {}),
        "status": "error" if state == "degraded" else "ok",
        "expired_count": int((sweeps.get("execution") or {}).get("expired_count", 0) or 0)
        + int((sweeps.get("shadow") or {}).get("expired_count", 0) or 0),
    }
    append_jsonl(SHARED / "logs/cron/signal_sweep_expired.jsonl", result)
    return result


def run_self_heal() -> dict[str, Any]:
    from shared.review.self_heal_loop import run_heal_cycle

    signal_sweep = run_signal_sweep_expired()
    result = run_heal_cycle(_self_heal_context())
    state = "critical" if int(result.get("issues_escalated", 0) or 0) else "healed" if int(result.get("issues_found", 0) or 0) else "healthy"
    result.update({
        "job": "job_self_heal",
        "state": state,
        "generated_at": now_iso(),
        "signal_sweep_expired": signal_sweep,
    })
    append_jsonl(SHARED / "review/heal/self_heal_actions.jsonl", result)
    return result


def run_self_heal_night() -> dict[str, Any]:
    from shared.review.self_heal_loop import run_heal_cycle

    signal_sweep = run_signal_sweep_expired()
    context = _self_heal_context()
    thresholds = {
        "data_stale_minutes": 180,
        "error_rate_pct": 5,
        "pnl_drawdown_pct": 4,
        "signal_starvation_periods": 2,
        "position_cap_pct": 12,
    }
    result = run_heal_cycle(context, thresholds=thresholds)
    result.update({
        "job": "job_self_heal_night",
        "state": "degraded" if int(result.get("issues_escalated", 0) or 0) else "ok",
        "generated_at": now_iso(),
        "mode": "deep_night",
        "context": context,
        "thresholds": thresholds,
        "signal_sweep_expired": signal_sweep,
    })
    write_json(SHARED / "review/heal/heal_report.json", result)
    return result


def run_weekly_review(job_name: str, output_rel: str) -> dict[str, Any]:
    from shared.review.weekly_review import review_week

    market_scope = _market_scope_for_job(job_name)
    week_trades, week_start, week_end = _load_week_shadow_trades(trade_date(), market=market_scope)
    result = review_week(week_trades)
    layer_key = market_scope and "shadow" or "shadow"
    layer_review = (result.get("capital_layer_reviews") or {}).get(layer_key) or {}
    email_data = _compose_weekly_email_data(week_start, week_end, week_trades, layer_review)
    email_result = send_template_email("weekly_report", email_data)
    result.update({
        "job": job_name,
        "state": "email_sent" if email_result.get("status") == "sent" else "saved_local",
        "generated_at": now_iso(),
        "capital_layer": "shadow",
        "market": market_scope or "all",
        "week_start": week_start,
        "week_end": week_end,
        "shadow_trade_count": len(week_trades),
        "email_notification": email_result,
        "email_data": email_data,
    })
    write_json(SHARED / output_rel, result)
    return result


def run_attribution(job_name: str, output_rel: str) -> dict[str, Any]:
    from shared.review.attribution import attribute, attribute_pct

    current_trade_date = trade_date()
    trades = _load_shadow_trades_for_date(current_trade_date)
    attribution = attribute(trades)
    attribution_pct = attribute_pct(trades)
    payload = {
        "job": job_name,
        "state": "ok",
        "generated_at": now_iso(),
        "trade_date": current_trade_date,
        "capital_layer": "shadow",
        "shadow_trade_count": len(trades),
        "attribution": attribution,
        "attribution_pct": attribution_pct,
    }
    append_jsonl(SHARED / output_rel, payload)
    return payload


def run_strategy_version() -> dict[str, Any]:
    snapshots, errors = _latest_strategy_configs()
    payload = {
        "job": "job_strategy_version",
        "state": _state_from_errors(errors),
        "generated_at": now_iso(),
        "trade_date": trade_date(),
        "capital_layer": "shadow",
        "market_count": len(snapshots),
        "strategy_count": sum(int(item.get("strategy_count", 0) or 0) for item in snapshots),
        "versions": snapshots,
        "errors": errors,
    }
    append_jsonl(SHARED / "review/strategies/strategy_version.jsonl", payload)
    return payload


def run_pm_risk() -> dict[str, Any]:
    from shared.accounting import position_ledger
    from shared.risk.black_swan import check_black_swan
    from shared.risk.position_monitor import check_positions, filter_actions

    errors: list[dict[str, str]] = []
    try:
        positions = position_ledger.get_positions(capital_layer="all")
    except Exception as exc:  # noqa: BLE001
        positions = []
        errors.append({"source": "position_ledger.get_positions", "error": f"{exc.__class__.__name__}: {exc}"})
    pm_positions = _positions_for_market(positions, "PM")
    current_prices = {
        str(row.get("ts_code")): _position_current_price(row)
        for row in pm_positions
        if row.get("ts_code") and _position_current_price(row) > 0
    }
    total_notional = sum(_position_notional(row) for row in pm_positions)
    total_exposure = min(1.0, total_notional / DAILY_BRIEF_CAPITAL_BASE) if DAILY_BRIEF_CAPITAL_BASE else 0.0
    portfolio = {
        "positions": pm_positions,
        "current_prices": current_prices,
        "total_exposure": total_exposure,
        "high_water": max(total_notional, 1.0),
        "current_value": total_notional,
        "daily_pnl_pct": 0.0,
        "regime": "pm_probability_market",
    }
    try:
        position_signals = check_positions(pm_positions, current_prices, regime="pm_probability_market")
        action_signals = filter_actions(position_signals)
    except Exception as exc:  # noqa: BLE001
        position_signals = []
        action_signals = []
        errors.append({"source": "position_monitor", "error": f"{exc.__class__.__name__}: {exc}"})
    market_data = {
        "market_change_pct": 0.0,
        "policy_shock": None,
        "liquidity_stress": False,
    }
    try:
        black_swan = check_black_swan(market_data)
    except Exception as exc:  # noqa: BLE001
        black_swan = {}
        errors.append({"source": "black_swan", "error": f"{exc.__class__.__name__}: {exc}"})
    payload = {
        "job": "job_pm_risk",
        "state": _state_from_errors(errors),
        "generated_at": now_iso(),
        "trade_date": trade_date(),
        "capital_layer": "shadow",
        "market": "PM",
        "position_count": len(pm_positions),
        "portfolio": portfolio,
        "position_signals": position_signals,
        "action_signals": action_signals,
        "black_swan": black_swan,
        "errors": errors,
    }
    append_jsonl(SHARED / "risk/pm/pm_risk_report.jsonl", payload)
    return payload


def run_stress_test() -> dict[str, Any]:
    from shared.adversarial.stress_test import stress_test, worst_case

    positions = _load_position_snapshot(trade_date())
    shadow_positions = [
        row for row in positions
        if str(row.get("capital_layer") or "shadow").strip().lower() == "shadow"
    ]
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for row in shadow_positions:
        ts_code = str(row.get("ts_code") or "").strip()
        if not ts_code:
            continue
        try:
            per_symbol = stress_test(ts_code)
        except Exception as exc:  # noqa: BLE001
            errors.append({"ts_code": ts_code, "error": f"{exc.__class__.__name__}: {exc}"})
            continue
        for item in per_symbol:
            enriched = dict(item)
            enriched["capital_layer"] = "shadow"
            enriched["quantity"] = row.get("quantity")
            enriched["cost_basis"] = row.get("cost_basis")
            results.append(enriched)
    payload = {
        "job": "job_stress_test",
        "state": _state_from_errors(errors, no_input=not shadow_positions),
        "generated_at": now_iso(),
        "trade_date": trade_date(),
        "capital_layer": "shadow",
        "position_count": len(shadow_positions),
        "results": results,
        "worst_case": worst_case(results),
        "errors": errors,
    }
    write_json(SHARED / "risk/reports/stress_test_report.json", payload)
    return payload


def run_auto_position() -> dict[str, Any]:
    from shared.accounting import capital_ledger, position_ledger
    from shared.portfolio.position_sizer import size_positions_batch

    errors: list[dict[str, str]] = []
    try:
        capital_balances = {
            layer: capital_ledger.get_capital_balance(capital_layer=layer)
            for layer in ("real", "simulated", "shadow")
        }
    except Exception as exc:  # noqa: BLE001
        capital_balances = {}
        errors.append({"source": "capital_ledger.get_capital_balance", "error": f"{exc.__class__.__name__}: {exc}"})
    try:
        positions = position_ledger.get_positions(capital_layer="all")
    except Exception as exc:  # noqa: BLE001
        positions = []
        errors.append({"source": "position_ledger.get_positions", "error": f"{exc.__class__.__name__}: {exc}"})

    shadow_balance = capital_balances.get("shadow", {}) if isinstance(capital_balances, dict) else {}
    capital_base = max(
        _safe_float(shadow_balance.get("total_inflow")),
        _safe_float(shadow_balance.get("balance")),
        DAILY_BRIEF_CAPITAL_BASE,
    )
    candidates: list[dict[str, Any]] = []
    for row in positions:
        ts_code = str(row.get("ts_code") or "").strip()
        if not ts_code:
            continue
        notional = _position_notional(row)
        current_weight = notional / capital_base if capital_base else 0.0
        candidates.append({
            "ts_code": ts_code,
            "belief_score": max(0.10, min(0.90, 0.70 - current_weight)),
            "volatility": _safe_float(row.get("volatility"), 0.20),
            "capital_layer": row.get("capital_layer", "shadow"),
            "current_weight": round(current_weight, 6),
            "current_notional": round(notional, 2),
        })
    target_positions = size_positions_batch(candidates, regime="unknown")
    by_code = {item["ts_code"]: item for item in candidates}
    plan_rows: list[dict[str, Any]] = []
    for target in target_positions:
        source = by_code.get(str(target.get("ts_code")), {})
        current_weight = _safe_float(source.get("current_weight"))
        target_weight = _safe_float(target.get("position_size_pct"))
        delta = round(target_weight - current_weight, 6)
        if delta > 0.01:
            action = "consider_add_shadow"
        elif delta < -0.01:
            action = "consider_reduce_shadow"
        else:
            action = "hold"
        plan_rows.append({
            **target,
            "capital_layer": source.get("capital_layer", "shadow"),
            "current_weight": current_weight,
            "target_weight": target_weight,
            "delta_weight": delta,
            "target_notional": round(target_weight * capital_base, 2),
            "action": action,
        })
    payload = {
        "job": "job_auto_position",
        "state": _state_from_errors(errors),
        "generated_at": now_iso(),
        "trade_date": trade_date(),
        "capital_layer": "shadow",
        "capital_base": round(capital_base, 2),
        "capital_balances": capital_balances,
        "source_position_count": len(positions),
        "positions": plan_rows,
        "errors": errors,
    }
    append_jsonl(SHARED / "accounting/position_plan.jsonl", payload)
    return payload


def run_alert() -> dict[str, Any]:
    self_heal_snapshot = _latest_self_heal_snapshot()
    cron_snapshot = _cron_log_snapshot()
    email_data = _system_health_email_data(self_heal_snapshot, cron_snapshot)
    email_result = send_template_email("system_health", email_data, channel="system")
    result = {
        "job": "job_alert",
        "state": "email_sent" if email_result.get("status") == "sent" else "saved_local",
        "generated_at": now_iso(),
        "trade_date": trade_date(),
        "self_heal": self_heal_snapshot,
        "cron_health": cron_snapshot,
        "email_notification": email_result,
        "email_data": email_data,
    }
    append_jsonl(SHARED / "notify/logs/alert_log.jsonl", result)
    return result


def _market_review_snapshot(job_name: str, market: str, output_rel: str) -> dict[str, Any]:
    from shared.review.attribution import attribute

    current_trade_date = trade_date()
    trades = [
        row for row in _load_shadow_trades_for_date(current_trade_date)
        if _market_matches(row, market)
    ]
    payload = {
        "job": job_name,
        "state": "ok",
        "generated_at": now_iso(),
        "trade_date": current_trade_date,
        "capital_layer": "shadow",
        "market": _normalize_market(market),
        "shadow_trade_count": len(trades),
        "attribution": attribute(trades),
    }
    append_jsonl(SHARED / output_rel, payload)
    return payload


def run_pm_optimize() -> dict[str, Any]:
    snapshots, errors = _latest_strategy_configs(("PM",))
    config = snapshots[0] if snapshots else {}
    strategy_count = int(config.get("strategy_count", 0) or 0)
    payload = {
        "job": "job_pm_optimize",
        "state": _state_from_errors(errors),
        "generated_at": now_iso(),
        "trade_date": trade_date(),
        "capital_layer": "shadow",
        "market": "PM",
        "strategy_version": config.get("version_hash", ""),
        "strategy_count": strategy_count,
        "params": {
            "portfolio_method": config.get("portfolio_method", "pm_probability_weighted"),
            "max_positions": (config.get("market_rules") or {}).get("max_positions", 20),
            "max_candidates": config.get("max_candidates", 20),
            "shadow_capital": config.get("shadow_capital", 50000.0),
        },
        "errors": errors,
    }
    write_json(SHARED / "review/pm/pm_optimize_params.json", payload)
    append_jsonl(SHARED / "review/pm/pm_optimize_params_history.jsonl", payload)
    return payload


def run_pm_promote() -> dict[str, Any]:
    from shared.review.weekly_review import review_week

    week_trades, week_start, week_end = _load_week_shadow_trades(trade_date(), market="PM")
    result = review_week(week_trades)
    shadow = (result.get("capital_layer_reviews") or {}).get("shadow") or {}
    payload = {
        "job": "job_pm_promote",
        "state": "ok",
        "generated_at": now_iso(),
        "capital_layer": "shadow",
        "market": "PM",
        "week_start": week_start,
        "week_end": week_end,
        "shadow_trade_count": len(week_trades),
        "strategies_to_promote": shadow.get("strategies_to_promote", []),
        "strategies_to_eliminate": shadow.get("strategies_to_eliminate", []),
        "review": result,
    }
    append_jsonl(SHARED / "review/pm/pm_promotion.jsonl", payload)
    return payload


def _pending_signal_orders() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pending_dir = ROOT / "signals" / "pending"
    for path in sorted(pending_dir.glob("*.json")):
        data = _read_json(path)
        if data:
            data.setdefault("source_path", str(path.relative_to(ROOT)))
            rows.append(data)
    return rows


def run_gate_review(job_name: str, output_rel: str, phase: str) -> dict[str, Any]:
    from shared.risk.pre_trade_check import check

    orders = _pending_signal_orders()
    positions = _load_position_snapshot(trade_date())
    total_notional = sum(_position_notional(row) for row in positions)
    portfolio = {
        "positions": positions,
        "total_exposure": min(1.0, total_notional / DAILY_BRIEF_CAPITAL_BASE) if DAILY_BRIEF_CAPITAL_BASE else 0.0,
        "daily_pnl_pct": 0.0,
    }
    decisions: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for order in orders:
        try:
            decision = check(order, portfolio)
        except Exception as exc:  # noqa: BLE001
            decision = {"approved": False, "error": f"{exc.__class__.__name__}: {exc}"}
            errors.append({"order_id": str(order.get("order_id", "")), "error": decision["error"]})
        decisions.append({
            "order_id": order.get("order_id", ""),
            "ts_code": order.get("ts_code", ""),
            "decision": decision,
        })
    payload = {
        "job": job_name,
        "state": _state_from_errors(errors),
        "generated_at": now_iso(),
        "trade_date": trade_date(),
        "phase": phase,
        "capital_layer": "shadow",
        "pending_order_count": len(orders),
        "decisions": decisions,
        "errors": errors,
    }
    append_jsonl(SHARED / output_rel, payload)
    return payload


def run_cross_market_review() -> dict[str, Any]:
    from shared.review.attribution import attribute

    current_trade_date = trade_date()
    trades = _load_shadow_trades_for_date(current_trade_date)
    market_reviews = {}
    for market in DAILY_BRIEF_MARKETS:
        scoped = [row for row in trades if _market_matches(row, market)]
        market_reviews[market] = {
            "trade_count": len(scoped),
            "pnl": round(sum(_safe_float(row.get("pnl")) for row in scoped), 6),
            "attribution": attribute(scoped),
        }
    payload = {
        "job": "job_cross_market_review",
        "state": "ok",
        "generated_at": now_iso(),
        "trade_date": current_trade_date,
        "capital_layer": "shadow",
        "market_reviews": market_reviews,
    }
    append_jsonl(SHARED / "review/cross/cross_market_review.jsonl", payload)
    return payload


def run_backtest_report() -> dict[str, Any]:
    daily_rows = _read_jsonl_dicts(SHARED / "review/data/daily_reviews.jsonl")
    shadow_rows = [
        row for row in daily_rows
        if str(row.get("capital_layer") or "shadow").strip().lower() == "shadow"
    ]
    recent_rows = shadow_rows[-20:]
    payload = {
        "job": "job_backtest_report",
        "state": "ok",
        "generated_at": now_iso(),
        "capital_layer": "shadow",
        "sample_count": len(recent_rows),
        "total_pnl": round(sum(_safe_float(row.get("pnl")) for row in recent_rows), 6),
        "avg_hit_rate": round(
            sum(_safe_float(row.get("hit_rate")) for row in recent_rows) / len(recent_rows),
            6,
        ) if recent_rows else 0.0,
        "recent_reviews": recent_rows,
    }
    write_json(SHARED / "review/backtest/backtest_report.json", payload)
    return payload


def run_research_report() -> dict[str, Any]:
    versions = _read_jsonl_dicts(SHARED / "review/strategies/strategy_version.jsonl")
    attribution = _read_last_jsonl(SHARED / "review/attribution/strategy_attribution.jsonl")
    payload = {
        "job": "job_research_report",
        "state": "ok",
        "generated_at": now_iso(),
        "capital_layer": "shadow",
        "note": "research report assembled from current strategy version and attribution evidence",
        "latest_strategy_version": versions[-1] if versions else {},
        "latest_attribution": attribution,
    }
    write_markdown(SHARED / "review/research/research_report.md", "job_research_report", payload)
    return payload


def run_pm_report() -> dict[str, Any]:
    positions = _positions_for_market(_load_position_snapshot(trade_date()), "PM")
    trades = [
        row for row in _load_shadow_trades_for_date(trade_date())
        if _market_matches(row, "PM")
    ]
    risk = run_pm_risk()
    payload = {
        "job": "job_pm_report",
        "state": "ok" if risk.get("state") == "ok" else "degraded",
        "generated_at": now_iso(),
        "trade_date": trade_date(),
        "capital_layer": "shadow",
        "market": "PM",
        "position_count": len(positions),
        "shadow_trade_count": len(trades),
        "positions": positions,
        "trades": trades,
        "risk": risk,
    }
    append_jsonl(SHARED / "notify/pm/pm_report.jsonl", payload)
    return payload


def _format_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "--"


def _build_email_notify_payload() -> tuple[str, str, str]:
    morning = _read_json(SHARED / "review/daily/morning_brief.json")
    midday = _read_last_jsonl(SHARED / "review/daily/midday_review.jsonl")
    nightly = _read_last_jsonl(SHARED / "review/daily/daily_brief.jsonl")

    review_sources = [
        ("晨间简报", morning),
        ("午盘复盘", midday),
        ("收盘复盘", nightly),
    ]
    available = [name for name, payload in review_sources if payload]
    subject = f"tradingagent 每日汇总 {trade_date()}"

    lines = [
        f"交易日: {trade_date()}",
        f"可用复盘: {', '.join(available) if available else '无'}",
        "",
    ]
    html_sections = []

    for title, payload in review_sources:
        if not payload:
            lines.append(f"{title}: 暂无产物")
            html_sections.append(
                f"<div style=\"margin-bottom:16px;\"><h3>{title}</h3><p>暂无产物</p></div>"
            )
            continue

        layer_reviews = payload.get("capital_layer_reviews") or {}
        shadow = layer_reviews.get("shadow") or {}
        summary_bits = [
            f"state={payload.get('state', '--')}",
            f"signals={shadow.get('signal_count', '--')}",
            f"hit_rate={_format_pct(shadow.get('hit_rate'))}",
            f"pnl={shadow.get('pnl', '--')}",
            f"positions={shadow.get('position_count', '--')}",
        ]
        lines.append(f"{title}: " + ", ".join(summary_bits))
        html_sections.append(
            "".join([
                "<div style=\"margin-bottom:16px;\">",
                f"<h3>{title}</h3>",
                "<ul>",
                f"<li>状态: {payload.get('state', '--')}</li>",
                f"<li>信号数: {shadow.get('signal_count', '--')}</li>",
                f"<li>命中率: {_format_pct(shadow.get('hit_rate'))}</li>",
                f"<li>盈亏: {shadow.get('pnl', '--')}</li>",
                f"<li>持仓数: {shadow.get('position_count', '--')}</li>",
                "</ul>",
                "</div>",
            ])
        )


    ops = _latest_ops_report()
    if ops:
        ops_totals = (ops.get("queue_summary") or {}).get("totals") or {}
        ops_receipts = ops.get("receipt_integrity") or {}
        ops_shadow = ((ops.get("shadow_queue_summary") or {}).get("totals") or {})
        ops_failures = ((ops.get("failure_summary") or {}).get("by_category") or {})
        ops_line = (
            f"运维报告: status={ops.get('overall_status', '--')}, "
            f"pending={ops_totals.get('pending', 0)}, running={ops_totals.get('running', 0)}, "
            f"failed={ops_totals.get('failed', 0)}, expired={ops_totals.get('expired', 0)}, "
            f"shadow_pending={ops_shadow.get('pending', 0)}, "
            f"receipt_invalid={ops_receipts.get('invalid', 0)}"
        )
        lines.append(ops_line)
        html_sections.append(
            "".join([
                "<div style=\"margin-bottom:16px;\">",
                "<h3>运维报告</h3>",
                "<ul>",
                f"<li>状态: {ops.get('overall_status', '--')}</li>",
                f"<li>执行队列: pending={ops_totals.get('pending', 0)}, running={ops_totals.get('running', 0)}, filled={ops_totals.get('filled', 0)}, failed={ops_totals.get('failed', 0)}, expired={ops_totals.get('expired', 0)}</li>",
                f"<li>影子队列: pending={ops_shadow.get('pending', 0)}, running={ops_shadow.get('running', 0)}, filled={ops_shadow.get('filled', 0)}, failed={ops_shadow.get('failed', 0)}, expired={ops_shadow.get('expired', 0)}</li>",
                f"<li>回执: total={ops_receipts.get('total', 0)}, signed={ops_receipts.get('signed', 0)}, unsigned={ops_receipts.get('unsigned', 0)}, invalid={ops_receipts.get('invalid', 0)}</li>",
                f"<li>失败分类: {json.dumps(ops_failures, ensure_ascii=False)}</li>",
                "</ul>",
                "</div>",
            ])
        )

    body = "\n".join(lines)
    html_body = wrap_html(
        f"每日汇总 | {trade_date()}",
        "Daily Summary",
        "".join(html_sections) or "<p>暂无可发送的复盘内容。</p>",
    )
    return subject, body, html_body


def run_ashare_night_calibration() -> dict[str, Any]:
    """Nightly A-share calibration after research/backtest/data backfill.

    This is not a second trading review: no new A-share trades arrive after close.
    It refreshes evidence files and records the next-day calibration package without
    sending a duplicate daily report email.
    """
    current_trade_date = trade_date()
    attribution = run_attribution("job_ashare_night_attribution", "review/attribution/ashare_night_attribution.jsonl")
    strategy_version = run_strategy_version()
    research = run_research_report()
    backtest = run_backtest_report()
    latest_daily = _read_last_jsonl(SHARED / "review/daily/daily_brief.jsonl")
    closing_scan = _read_json(Path("/opt/investment/MarketGraph/outputs/ashare_closing_buy_candidates.json"))
    payload = {
        "job": "job_ashare_night_calibration",
        "state": "ok",
        "phase": "night_calibration",
        "generated_at": now_iso(),
        "trade_date": current_trade_date,
        "capital_layer": "shadow",
        "note": "No new A-share trades after 15:00; this package refreshes attribution/backtest/research evidence and next-day plan inputs without sending a duplicate trading review email.",
        "latest_close_review": latest_daily,
        "closing_scan": closing_scan,
        "attribution": attribution,
        "strategy_version": strategy_version,
        "research_report": research,
        "backtest_report": backtest,
    }
    append_jsonl(SHARED / "review/daily/night_calibration.jsonl", payload)
    write_json(SHARED / "review/daily/night_calibration_latest.json", payload)
    write_markdown(SHARED / "review/daily/night_calibration.md", "job_ashare_night_calibration", payload)
    return payload



def run_ops_report() -> dict[str, Any]:
    from shared.runtime_test.ops_report import build_ops_report, send_alert_if_needed, write_report
    report = build_ops_report()
    paths = write_report(report)
    email = send_alert_if_needed(report, "fail")
    return {
        "job": "job_ops_report",
        "generated_at": now_iso(),
        "state": report.get("overall_status", "unknown"),
        "written_paths": paths,
        "email": email,
    }


def _resolve_daily_summary_recipient() -> str:
    return os.environ.get(
        "TRADINGS_EMAIL_RECIPIENT",
        os.environ.get(
            "TRADINGS_DAILY_RECIPIENT",
            CHANNELS["trading"]["to"],
        ),
    )


def run_email_notify() -> dict[str, Any]:
    subject, body, html_body = _build_email_notify_payload()
    result = send_email(
        _resolve_daily_summary_recipient(),
        subject,
        body,
        html_body,
        channel="trading",
    )
    result.update({
        "job": "job_email_notify",
        "generated_at": now_iso(),
        "trade_date": trade_date(),
        "state": "sent" if result.get("status") == "sent" else "saved_local",
    })
    return result


JOB_HANDLERS: dict[str, Any] = {
    "job_trading_signals": run_all_market_trading_signals,
    "job_premarket_signals": lambda: run_market_watch(
        "job_premarket_signals",
        "Ashare",
        "signals/premarket_signals.jsonl",
        "premarket",
    ),
    "job_ashare_sim_exec": lambda: run_sim_orchestrator("job_ashare_sim_exec", "Ashare"),
    "job_us_premarket": lambda: run_market_watch(
        "job_us_premarket",
        "US",
        "signals/us/us_premarket_signals.jsonl",
        "premarket",
    ),
    "job_us_hourly": lambda: run_market_watch(
        "job_us_hourly",
        "US",
        "signals/us/us_intraday_signals.jsonl",
        "hourly",
    ),
    "job_us_postclose": lambda: _market_review_snapshot(
        "job_us_postclose",
        "US",
        "review/us/us_postclose.jsonl",
    ),
    "job_us_signal_review": lambda: _market_review_snapshot(
        "job_us_signal_review",
        "US",
        "review/us/us_signal_review.jsonl",
    ),
    "job_crypto_daily": lambda: run_shadow_orchestrator("job_crypto_daily", "Crypto"),
    "job_crypto_weekly": lambda: run_weekly_review("job_crypto_weekly", "review/crypto/crypto_weekly_review.json"),
    "job_pm_forward": lambda: run_market_watch(
        "job_pm_forward",
        "PM",
        "signals/pm/pm_forward_signals.jsonl",
        "forward",
    ),
    "job_pm_optimize": run_pm_optimize,
    "job_pm_promote": run_pm_promote,
    "job_daily_brief_morning": run_daily_brief_morning,
    "job_daily_brief_day": run_daily_brief_day,
    "job_daily_brief_night": run_daily_brief_night,
    "job_ashare_night_calibration": run_ashare_night_calibration,
    "job_self_heal": run_self_heal,
    "job_self_heal_night": run_self_heal_night,
    "job_signal_sweep_expired": run_signal_sweep_expired,
    "job_weekly_review": lambda: run_weekly_review("job_weekly_review", "review/weekly/weekly_review.json"),
    "job_us_weekly": lambda: run_weekly_review("job_us_weekly", "review/us/us_weekly_review.json"),
    "job_strategy_attribution": lambda: run_attribution("job_strategy_attribution", "review/attribution/strategy_attribution.jsonl"),
    "job_factor_attribution": lambda: run_attribution("job_factor_attribution", "review/attribution/factor_attribution.jsonl"),
    "job_strategy_version": run_strategy_version,
    "job_pm_risk": run_pm_risk,
    "job_stress_test": run_stress_test,
    "job_auto_position": run_auto_position,
    "job_gate_review_night": lambda: run_gate_review("job_gate_review_night", "risk/gate/gate_decisions.jsonl", "night"),
    "job_gate_review_day": lambda: run_gate_review("job_gate_review_day", "risk/gate/gate_intraday.jsonl", "day"),
    "job_cross_market_review": run_cross_market_review,
    "job_backtest_report": run_backtest_report,
    "job_research_report": run_research_report,
    "job_pm_report": run_pm_report,
    "job_alert": run_alert,
    "job_email_notify": run_email_notify,
    "job_ops_report": run_ops_report,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    args = parser.parse_args()

    if args.job in JOB_HANDLERS:
        payload = JOB_HANDLERS[args.job]()
    else:
        raise SystemExit(f"unknown job: {args.job}")

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
