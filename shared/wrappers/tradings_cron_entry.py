#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.notify.email_sender import send_email
from shared.notify.email_templates import wrap_html

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "shared"


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
    return placeholder(
        "job_daily_brief_morning",
        "review/daily/morning_brief.json",
        "晨间简报骨架已迁入 Tradings；待接入 overnight_state 实数与邮件模板。",
        fmt="json",
        extra={"capital_layer": "shadow"},
    )


def run_daily_brief_day() -> dict[str, Any]:
    from shared.orchestrator import run_daily_review

    result = run_daily_review("all", trade_date(), "lunch")
    result.update({
        "job": "job_daily_brief_day",
        "state": "orchestrated",
        "phase": "lunch",
        "generated_at": now_iso(),
    })
    append_jsonl(SHARED / "review/daily/midday_review.jsonl", result)
    return result


def run_daily_brief_night() -> dict[str, Any]:
    from shared.orchestrator import run_daily_review

    result = run_daily_review("all", trade_date(), "close")
    result.update({
        "job": "job_daily_brief_night",
        "state": "orchestrated",
        "phase": "close",
        "generated_at": now_iso(),
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
