#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.notify.email_sender import send_email, send_template_email
from shared.notify.email_templates import wrap_html

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "shared"
DAILY_BRIEF_MARKETS = ("Ashare", "Crypto", "US", "PM")
DAILY_BRIEF_CAPITAL_BASE = 100000.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def trade_date() -> str:
    return datetime.now().strftime("%Y%m%d")


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
    return {
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


def placeholder(job: str, output_rel: str, note: str, fmt: str = "jsonl", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "job": job,
        "state": "planned_only",
        "generated_at": now_iso(),
        "note": note,
    }
    if extra:
        payload.update(extra)
    output = SHARED / output_rel
    if fmt == "json":
        write_json(output, payload)
    elif fmt == "md":
        write_markdown(output, job, payload)
    else:
        append_jsonl(output, payload)
    return payload


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
    from shared.data.reader import TradingsDataReader
    from shared.orchestrator import run_shadow_loop

    adapter = get_market_adapter(market)
    reader: Any = TradingsDataReader()
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


def run_self_heal() -> dict[str, Any]:
    from shared.review.self_heal_loop import run_heal_cycle

    result = run_heal_cycle({})
    result.update({"job": "job_self_heal", "state": "scaffolded", "generated_at": now_iso()})
    append_jsonl(SHARED / "review/heal/self_heal_actions.jsonl", result)
    return result


def run_self_heal_night() -> dict[str, Any]:
    from shared.review.self_heal_loop import run_heal_cycle

    result = run_heal_cycle({})
    result.update({
        "job": "job_self_heal_night",
        "state": "scaffolded",
        "generated_at": now_iso(),
        "mode": "deep_night",
    })
    write_json(SHARED / "review/heal/heal_report.json", result)
    return result


def run_weekly_review(job_name: str, output_rel: str) -> dict[str, Any]:
    from shared.review.weekly_review import review_week

    result = review_week([])
    result.update({"job": job_name, "state": "scaffolded", "generated_at": now_iso()})
    write_json(SHARED / output_rel, result)
    return result


def run_attribution(job_name: str, output_rel: str) -> dict[str, Any]:
    from shared.review.attribution import attribute_pct

    result = attribute_pct([])
    payload = {
        "job": job_name,
        "state": "scaffolded",
        "generated_at": now_iso(),
        "attribution": result,
    }
    append_jsonl(SHARED / output_rel, payload)
    return payload


def run_strategy_version() -> dict[str, Any]:
    return placeholder(
        "job_strategy_version",
        "review/strategies/strategy_version.jsonl",
        "策略版本快照 wrapper 已迁移；待对接真实 strategy_params 源。",
    )


def run_pm_risk() -> dict[str, Any]:
    from shared.risk.patrol import patrol

    result = patrol({})
    result.update({"job": "job_pm_risk", "state": "scaffolded", "generated_at": now_iso()})
    append_jsonl(SHARED / "risk/pm/pm_risk_report.jsonl", result)
    return result


def run_stress_test() -> dict[str, Any]:
    from shared.adversarial.stress_test import stress_test, worst_case

    results = stress_test("PLACEHOLDER")
    payload = {
        "job": "job_stress_test",
        "state": "scaffolded",
        "generated_at": now_iso(),
        "results": results,
        "worst_case": worst_case(results),
    }
    write_json(SHARED / "risk/reports/stress_test_report.json", payload)
    return payload


def run_auto_position() -> dict[str, Any]:
    from shared.portfolio.position_sizer import size_positions_batch

    payload = {
        "job": "job_auto_position",
        "state": "scaffolded",
        "generated_at": now_iso(),
        "positions": size_positions_batch([], regime="unknown"),
        "note": "仓位规划 wrapper 已迁移；待接入 capital_ledger 与 positions 实盘输入。",
    }
    append_jsonl(SHARED / "accounting/position_plan.jsonl", payload)
    return payload


def run_alert() -> dict[str, Any]:
    from shared.notify.alert_router import check_self_heal_status

    result = check_self_heal_status()
    result.update({"job": "job_alert", "state": "scaffolded", "generated_at": now_iso()})
    append_jsonl(SHARED / "notify/logs/alert_log.jsonl", result)
    return result


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
    subject = f"Tradings 每日汇总 {trade_date()}"

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

    body = "\n".join(lines)
    html_body = wrap_html(
        f"每日汇总 | {trade_date()}",
        "Daily Summary",
        "".join(html_sections) or "<p>暂无可发送的复盘内容。</p>",
    )
    return subject, body, html_body


def run_email_notify() -> dict[str, Any]:
    subject, body, html_body = _build_email_notify_payload()
    result = send_email(
        "Leocozy@coze.email",
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
    "job_ashare_sim_exec": lambda: run_shadow_orchestrator("job_ashare_sim_exec", "Ashare"),
    "job_us_shadow_exec": lambda: run_shadow_orchestrator("job_us_shadow_exec", "US"),
    "job_us_shadow": lambda: run_shadow_orchestrator("job_us_shadow", "US"),
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
    "job_crypto_shadow_exec": lambda: run_shadow_orchestrator("job_crypto_shadow_exec", "Crypto"),
    "job_crypto_shadow": lambda: run_shadow_orchestrator("job_crypto_shadow", "Crypto"),
    "job_crypto_daily": lambda: run_shadow_orchestrator("job_crypto_daily", "Crypto"),
    "job_pm_shadow": lambda: run_shadow_orchestrator("job_pm_shadow", "PM"),
    "job_daily_brief_morning": run_daily_brief_morning,
    "job_daily_brief_day": run_daily_brief_day,
    "job_daily_brief_night": run_daily_brief_night,
    "job_self_heal": run_self_heal,
    "job_self_heal_night": run_self_heal_night,
    "job_weekly_review": lambda: run_weekly_review("job_weekly_review", "review/weekly/weekly_review.json"),
    "job_us_weekly": lambda: run_weekly_review("job_us_weekly", "review/us/us_weekly_review.json"),
    "job_strategy_attribution": lambda: run_attribution("job_strategy_attribution", "review/attribution/strategy_attribution.jsonl"),
    "job_factor_attribution": lambda: run_attribution("job_factor_attribution", "review/attribution/factor_attribution.jsonl"),
    "job_strategy_version": run_strategy_version,
    "job_pm_risk": run_pm_risk,
    "job_stress_test": run_stress_test,
    "job_auto_position": run_auto_position,
    "job_alert": run_alert,
    "job_email_notify": run_email_notify,
}


PLACEHOLDER_SPECS: dict[str, tuple[str, str, str]] = {
    "job_premarket_signals": ("signals/premarket_signals.jsonl", "jsonl", "待接入隔夜事件与评分后生成 A 股盘前信号。"),
    "job_us_postclose": ("review/us/us_postclose.jsonl", "jsonl", "待接入 US close data 与当日信号聚合。"),
    "job_crypto_weekly": ("signals/crypto/crypto_weekly_signals.jsonl", "jsonl", "待接入中期 crypto 事件与参数。"),
    "job_pm_forward": ("signals/pm/pm_forward_signals.jsonl", "jsonl", "待接入 pm_shadow 与 pm_prices。"),
    "job_pm_optimize": ("strategies/pm/pm_optimize_params.json", "json", "待接入 PM bayesian/weight adjustment 参数优化。"),
    "job_pm_promote": ("review/pm/pm_promotion.jsonl", "jsonl", "待接入 PM 晋级评估输入。"),
    "job_gate_review_night": ("risk/gate/gate_decisions.jsonl", "jsonl", "夜间 gate_review wrapper 已拆出；待迁移原 MarketGraph/tools/gate_review.py 逻辑。"),
    "job_gate_review_day": ("risk/gate/gate_intraday.jsonl", "jsonl", "日间 gate_review wrapper 已拆出；待迁移盘中门禁裁决逻辑。"),
    "job_us_signal_review": ("review/us/us_signal_review.jsonl", "jsonl", "待接入美股信号命中率统计。"),
    "job_cross_market_review": ("review/cross/cross_market_review.jsonl", "jsonl", "待接入跨市场联动兑现数据。"),
    "job_backtest_report": ("review/backtest/backtest_report.json", "json", "待接入反事实回测结果。"),
    "job_research_report": ("review/research/research_report.md", "md", "待接入 research_findings 汇总。"),
    "job_pm_report": ("notify/pm/pm_report.jsonl", "jsonl", "待接入 Polymarket 持仓与成交报告。"),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    args = parser.parse_args()

    if args.job in JOB_HANDLERS:
        payload = JOB_HANDLERS[args.job]()
    elif args.job in PLACEHOLDER_SPECS:
        output_rel, fmt, note = PLACEHOLDER_SPECS[args.job]
        payload = placeholder(args.job, output_rel, note, fmt=fmt)
    else:
        raise SystemExit(f"unknown job: {args.job}")

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
