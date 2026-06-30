#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


MARKET_ADAPTERS: dict[str, Any] = {
    "Ashare": StubMarketAdapter("Ashare"),
    "Crypto": StubMarketAdapter("Crypto"),
    "US": StubMarketAdapter("US"),
    "PM": StubMarketAdapter("PM"),
}


def register_market_adapter(market: str, adapter: Any) -> None:
    MARKET_ADAPTERS[market] = adapter


def get_market_adapter(market: str) -> Any:
    return MARKET_ADAPTERS.get(market) or StubMarketAdapter(market)


def run_shadow_orchestrator(job_name: str, market: str) -> dict[str, Any]:
    from shared.data.reader import TradingsDataReader
    from shared.orchestrator import run_shadow_loop

    result = run_shadow_loop(get_market_adapter(market), trade_date(), TradingsDataReader())
    result.update({"job": job_name, "state": result.get("state", "ok"), "generated_at": now_iso()})
    append_jsonl(SHARED / "logs/orchestrator_shadow_runs.jsonl", result)
    return result


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
    result.update({"job": "job_daily_brief_day", "state": "orchestrated", "generated_at": now_iso()})
    append_jsonl(SHARED / "review/daily/midday_review.jsonl", result)
    return result


def run_daily_brief_night() -> dict[str, Any]:
    from shared.orchestrator import run_daily_review

    result = run_daily_review("all", trade_date(), "close")
    result.update({"job": "job_daily_brief_night", "state": "orchestrated", "generated_at": now_iso()})
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


def run_email_notify() -> dict[str, Any]:
    return placeholder(
        "job_email_notify",
        "notify/logs/emails_sent.jsonl",
        "邮件汇总 wrapper 已迁移；待接入 notify/email_templates 与真实发送通道。",
    )


JOB_HANDLERS: dict[str, Any] = {
    "job_trading_signals": run_all_market_trading_signals,
    "job_us_shadow_exec": lambda: run_shadow_orchestrator("job_us_shadow_exec", "US"),
    "job_us_shadow": lambda: run_shadow_orchestrator("job_us_shadow", "US"),
    "job_crypto_shadow_exec": lambda: run_shadow_orchestrator("job_crypto_shadow_exec", "Crypto"),
    "job_crypto_shadow": lambda: run_shadow_orchestrator("job_crypto_shadow", "Crypto"),
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
    "job_ashare_sim_exec": ("executions/sim/sim_exec_log.jsonl", "jsonl", "待接入 active_conditions 与 quotes 后执行 A 股模拟单。"),
    "job_us_premarket": ("signals/us/us_premarket_signals.jsonl", "jsonl", "待接入美股日线与事件流。"),
    "job_us_hourly": ("signals/us/us_intraday_signals.jsonl", "jsonl", "待接入美股盘中行情。"),
    "job_us_postclose": ("review/us/us_postclose.jsonl", "jsonl", "待接入 US close data 与当日信号聚合。"),
    "job_crypto_daily": ("signals/crypto/crypto_daily_signals.jsonl", "jsonl", "待接入 crypto_klines 与 regime。"),
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
