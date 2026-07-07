#!/usr/bin/env python3
"""Read-only A-share pre-open dry run for the simulated trading chain."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Ashare.adapter import AshareAdapter
from Ashare.capital_plan import TOTAL_CAPITAL, plan_capital
from Ashare.sim_executor import _is_supported_ashare_code, _market_session_rejection
from shared.data.reader import TradingagentDataReader
from shared.notify import email_sender
from shared.orchestrator import _account_available_cash, _account_capital, _account_positions, _latest_price, _score_diagnostics
from shared.runtime_test.ashare_opening_validator import DEFAULT_SQLITE_DB, validate_pre_open
from shared.screening.six_dimension_scorer import score_universe


CN_TZ = timezone(timedelta(hours=8))
LATEST = ROOT / "shared/runtime_test/ashare_preopen_dry_run_latest.json"
HISTORY = ROOT / "shared/runtime_test/ashare_preopen_dry_run_history.jsonl"
CANDIDATE_THRESHOLD = 0.55
MIN_SYMBOLS = 1000
DEFAULT_SCORE_LIMIT = 10


def _now_cn(value: str | None = None) -> datetime:
    if not value:
        return datetime.now(CN_TZ)
    parsed = datetime.fromisoformat(value.replace(" ", "T", 1))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _trade_date(now: datetime) -> str:
    return now.strftime("%Y%m%d")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def _status_rank(status: str) -> int:
    return {"pass": 0, "ok": 0, "warn": 1, "degraded": 1, "fail": 2, "critical": 2}.get(str(status).lower(), 1)


def _overall_status(sections: list[dict[str, Any]]) -> str:
    worst = max((_status_rank(str(section.get("status") or "warn")) for section in sections), default=1)
    return "fail" if worst >= 2 else ("warn" if worst == 1 else "pass")


def _compact_scores(scored: list[tuple[str, dict[str, float]]], *, limit: int = 10) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol, scores in scored[: max(1, limit)]:
        rows.append(
            {
                "symbol": symbol,
                "combined": round(_safe_float(scores.get("combined")), 4),
                "macro": round(_safe_float(scores.get("macro")), 4),
                "event": round(_safe_float(scores.get("event")), 4),
                "fundamental": round(_safe_float(scores.get("fundamental")), 4),
                "capital": round(_safe_float(scores.get("capital")), 4),
                "technical": round(_safe_float(scores.get("technical")), 4),
                "sentiment": round(_safe_float(scores.get("sentiment")), 4),
            }
        )
    return rows


def _latest_liquid_universe_from_read_model(
    sqlite_db: Path,
    date: str,
    *,
    limit: int,
) -> list[str]:
    if not sqlite_db.exists():
        return []
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{sqlite_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        daily_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(market_bars_daily)").fetchall()
        }
        if not daily_columns:
            return []
        has_amount = "amount" in daily_columns
        has_assets = bool(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='market_assets'").fetchone())
        amount_expr = "COALESCE(b.amount, 0)" if has_amount else "0"
        join_assets = "LEFT JOIN market_assets a ON a.market=b.market AND a.symbol=b.symbol" if has_assets else ""
        name_expr = "COALESCE(a.name, '')" if has_assets else "''"
        status_expr = "COALESCE(a.status, '')" if has_assets else "''"
        rows = conn.execute(
            f"""
            WITH latest AS (
                SELECT MAX(trade_date) AS trade_date
                FROM market_bars_daily
                WHERE market='Ashare'
                  AND trade_date <= ?
                  AND close > 0
            )
            SELECT b.symbol AS symbol,
                   b.close AS close,
                   {amount_expr} AS amount,
                   {name_expr} AS name,
                   {status_expr} AS status
            FROM market_bars_daily b
            {join_assets}
            WHERE b.market='Ashare'
              AND b.trade_date = (SELECT trade_date FROM latest)
              AND b.close > 0
            ORDER BY {amount_expr} DESC, b.symbol ASC
            LIMIT ?
            """,
            (date, max(1, int(limit) * 4)),
        ).fetchall()
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()

    symbols: list[str] = []
    seen: set[str] = set()
    for row in rows:
        symbol = str(row["symbol"] or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        if not _is_supported_ashare_code(symbol):
            continue
        name = str(row["name"] or "").upper()
        status = str(row["status"] or "").lower()
        if "ST" in name or "退" in name or status in {"suspended", "halted", "delisted", "inactive"}:
            continue
        amount = _safe_float(row["amount"], 0.0)
        if amount > 0 and amount * 1000.0 < 50_000_000.0:
            continue
        seen.add(symbol)
        symbols.append(symbol)
        if len(symbols) >= max(1, int(limit)):
            break
    return symbols


def _build_candidate_pool(
    *,
    reader: Any,
    sqlite_db: Path,
    date: str,
    score_limit: int,
) -> dict[str, Any]:
    universe = _latest_liquid_universe_from_read_model(sqlite_db, date, limit=score_limit)
    limited = universe[: max(1, int(score_limit))]
    scored = score_universe(date=date, universe=limited, data_reader=reader, market="ashare")
    scores_by_symbol = {symbol: scores for symbol, scores in scored}
    candidates = [
        {
            "ts_code": symbol,
            "code": symbol,
            "combined": _safe_float(scores.get("combined")),
            "score": _safe_float(scores.get("combined")),
            "scores": scores,
        }
        for symbol, scores in scored
        if _safe_float(scores.get("combined")) >= CANDIDATE_THRESHOLD
    ]
    watch = [
        symbol
        for symbol, scores in scored
        if 0.45 <= _safe_float(scores.get("combined")) < CANDIDATE_THRESHOLD
    ][:20]
    return {
        "status": "pass" if candidates else "warn",
        "reason": "candidate_layer_ready" if candidates else "no_candidate_layer_after_scoring",
        "universe_source": "sharedsignals_read_model_latest_liquid_daily",
        "universe_count": len(universe),
        "scored_count": len(scored),
        "score_universe_limit": max(1, int(score_limit)),
        "candidate_threshold": CANDIDATE_THRESHOLD,
        "candidate_count": len(candidates),
        "watch_count": len(watch),
        "top_candidates": _compact_scores([(row["ts_code"], row["scores"]) for row in candidates], limit=10),
        "top_scored": _compact_scores(scored, limit=10),
        "score_diagnostics": _score_diagnostics(scores_by_symbol),
        "candidates_for_plan": candidates,
    }


def _build_capital_plan(adapter: AshareAdapter, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    account = adapter.get_sim_account()
    config = adapter.get_strategy_config()
    positions = _account_positions(account, config)
    capital = _account_capital(account, config)
    if capital <= 0:
        capital = float(TOTAL_CAPITAL)
    cash = _account_available_cash(account, config, capital, positions)
    plan = plan_capital(
        positions,
        cash,
        candidates=candidates,
        dynamic=True,
        market_context={"risk_rejection_rate": 0.0, "data_issue_rate": 0.0},
        total_capital=capital,
    ).to_dict()
    plan.update(
        {
            "status": "pass" if int(plan.get("target_positions") or 0) > 0 or positions else "warn",
            "account": account.get("account") if isinstance(account, dict) else "ashare_sim",
            "total_capital": round(capital, 2),
            "cash_available": round(cash, 2),
            "existing_position_count": len({str(row.get("ts_code") or row.get("symbol") or "") for row in positions if isinstance(row, dict)} - {""}),
            "source": account.get("source") if isinstance(account, dict) else "",
            "snapshot_synced_at": account.get("snapshot_synced_at") if isinstance(account, dict) else "",
        }
    )
    if int(plan.get("target_positions") or 0) <= 0 and not positions:
        plan["reason"] = "capital_plan_defensive_no_new_buy"
    else:
        plan["reason"] = "capital_plan_ready"
    return plan


def _execution_gate(
    *,
    reader: Any,
    date: str,
    candidate: dict[str, Any] | None,
    capital_plan: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    if not candidate:
        return {
            "status": "warn",
            "ready": False,
            "reason": "no_candidate_for_synthetic_order",
            "blockers": ["no_candidate"],
            "warnings": warnings,
            "synthetic_order": {},
            "market_session_check": {
                "would_execute_now": False,
                "message": _market_session_rejection({"now": now.isoformat(timespec="seconds")}) or "regular_session",
            },
        }

    symbol = str(candidate.get("ts_code") or candidate.get("code") or "")
    if not _is_supported_ashare_code(symbol):
        blockers.append("unsupported_ashare_code")
    price = _latest_price(reader, "ashare", symbol, date, 0.0)
    if price <= 0:
        blockers.append("missing_or_non_positive_price")
    budgets = capital_plan.get("position_budget_by_symbol") if isinstance(capital_plan.get("position_budget_by_symbol"), dict) else {}
    budget = _safe_float(budgets.get(symbol), 0.0)
    if budget <= 0:
        suggested = capital_plan.get("suggested_buys") if isinstance(capital_plan.get("suggested_buys"), list) else []
        for row in suggested:
            if not isinstance(row, dict):
                continue
            if str(row.get("code") or row.get("ts_code") or "") == symbol:
                budget = _safe_float(row.get("allocation"), 0.0)
                break
    if budget <= 0:
        blockers.append("no_position_budget")
    quantity = int(budget // max(price, 1e-9))
    quantity = (quantity // 100) * 100
    if quantity <= 0:
        blockers.append("quantity_below_100_lot")
    session_message = _market_session_rejection({"now": now.isoformat(timespec="seconds")})
    if session_message:
        warnings.append("outside_regular_session_now_expected_for_preopen")
    order = {
        "ts_code": symbol,
        "side": "buy",
        "quantity": quantity,
        "price": round(price, 4),
        "budget": round(budget, 2),
        "trade_date": date,
        "market": "ashare",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "candidate_pool_layer": "candidate",
        "execution_source": "ashare_candidate_layer",
        "dry_run": True,
    }
    return {
        "status": "pass" if not blockers else "fail",
        "ready": not blockers,
        "reason": "synthetic_order_gate_ready" if not blockers else "synthetic_order_gate_blocked",
        "blockers": blockers,
        "warnings": warnings,
        "synthetic_order": order,
        "market_session_check": {
            "would_execute_now": not bool(session_message),
            "message": session_message or "regular_session",
        },
    }


def run_preopen_dry_run(
    *,
    now: datetime | None = None,
    sqlite_db: Path = DEFAULT_SQLITE_DB,
    reader: Any | None = None,
    score_limit: int | None = None,
) -> dict[str, Any]:
    current = now or _now_cn()
    date = _trade_date(current)
    data_reader = reader or TradingagentDataReader()
    adapter = AshareAdapter(reader=data_reader)
    resolved_score_limit = int(
        score_limit
        or os.environ.get("ASHARE_PREOPEN_DRY_RUN_SCORE_LIMIT", "")
        or DEFAULT_SCORE_LIMIT
    )

    data = validate_pre_open(sqlite_db=sqlite_db, now=current, min_symbols=MIN_SYMBOLS)
    data_status = str(data.get("status") or "warn").lower()
    if data.get("reason") in {"pre_open_daily_bars_missing", "pre_open_daily_bars_stale"}:
        data_status = "fail"
    data_section = {
        "status": data_status,
        "reason": data.get("reason"),
        "symbol_count": data.get("symbol_count"),
        "latest_trade_date": data.get("latest_trade_date"),
        "latest_daily_age_days": data.get("latest_daily_age_days"),
        "max_daily_age_days": data.get("max_daily_age_days"),
    }

    candidate_pool = _build_candidate_pool(
        reader=data_reader,
        sqlite_db=sqlite_db,
        date=date,
        score_limit=resolved_score_limit,
    )
    capital_plan = _build_capital_plan(adapter, candidate_pool["candidates_for_plan"])
    top_candidate = candidate_pool["candidates_for_plan"][0] if candidate_pool["candidates_for_plan"] else None
    execution_gate = _execution_gate(
        reader=data_reader,
        date=date,
        candidate=top_candidate,
        capital_plan=capital_plan,
        now=current,
    )

    sections = [data_section, candidate_pool, capital_plan, execution_gate]
    blockers: list[str] = []
    warnings: list[str] = []
    for section_name, section in (
        ("data", data_section),
        ("candidate_pool", candidate_pool),
        ("capital_plan", capital_plan),
        ("execution_gate", execution_gate),
    ):
        status = str(section.get("status") or "warn").lower()
        if status == "fail":
            blockers.append(f"{section_name}:{section.get('reason') or 'failed'}")
        elif status == "warn":
            warnings.append(f"{section_name}:{section.get('reason') or 'warning'}")
    warnings.extend(str(item) for item in execution_gate.get("warnings", []) if item)

    report = {
        "report_type": "ashare_preopen_dry_run",
        "market": "ashare",
        "generated_at": current.isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "trade_date": date,
        "status": _overall_status(sections),
        "read_only": True,
        "dry_run": True,
        "real_trading_enabled": False,
        "writes_excluded": ["signals", "ledger", "pending", "review"],
        "data": data_section,
        "candidate_pool": {key: value for key, value in candidate_pool.items() if key != "candidates_for_plan"},
        "capital_plan": capital_plan,
        "execution_gate": execution_gate,
        "blockers": blockers,
        "warnings": warnings,
        "next_actions": _next_actions(blockers, warnings),
    }
    return report


def _next_actions(blockers: list[str], warnings: list[str]) -> list[str]:
    if blockers:
        actions: list[str] = []
        if any("data:" in item for item in blockers):
            actions.append("先修复 SharedSignals A股日线/覆盖数据，再进入开盘模拟。")
        if any("candidate_pool:" in item for item in blockers) or any("candidate_pool:" in item for item in warnings):
            actions.append("复核 A股候选池阈值、流动性过滤和六维打分输入。")
        if any("capital_plan:" in item for item in blockers):
            actions.append("复核模拟账户快照、现金和持仓数量。")
        if any("execution_gate:" in item for item in blockers):
            actions.append("复核候选价格、100股整手、来源字段和执行门禁。")
        return actions or ["复核失败项后重跑盘前 dry-run。"]
    if warnings:
        return ["允许开盘继续观察；若候选为空，系统会安全空跑并记录原因。"]
    return ["盘前 dry-run 通过，等待开盘后的 5 分钟数据与模拟成交验收。"]


def write_outputs(report: dict[str, Any], *, append_history: bool = True) -> None:
    LATEST.parent.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if append_history:
        with HISTORY.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, ensure_ascii=False) + "\n")


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"A股盘前 dry-run：{report.get('status')}",
        f"时间：{report.get('generated_at')}",
        f"交易日：{report.get('trade_date')}",
        f"数据：{report.get('data', {}).get('status')}；最新日线={report.get('data', {}).get('latest_trade_date')}；覆盖={report.get('data', {}).get('symbol_count')}",
        f"候选池：{report.get('candidate_pool', {}).get('status')}；候选={report.get('candidate_pool', {}).get('candidate_count')}；已打分={report.get('candidate_pool', {}).get('scored_count')}",
        f"资金计划：{report.get('capital_plan', {}).get('status')}；现金={report.get('capital_plan', {}).get('cash_available')}；目标持仓={report.get('capital_plan', {}).get('target_positions')}；风险模式={report.get('capital_plan', {}).get('risk_mode')}",
        f"执行门禁：{report.get('execution_gate', {}).get('status')}；ready={report.get('execution_gate', {}).get('ready')}",
    ]
    blockers = report.get("blockers") if isinstance(report.get("blockers"), list) else []
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    if blockers:
        lines.append("阻断：" + "；".join(str(item) for item in blockers))
    if warnings:
        lines.append("提醒：" + "；".join(str(item) for item in warnings))
    lines.append("下一步：")
    lines.extend(f"- {item}" for item in report.get("next_actions", []))
    return "\n".join(lines)


def _send_alert(report: dict[str, Any], rendered_text: str) -> dict[str, Any]:
    status = str(report.get("status") or "warn")
    subject = f"[TradingAgent][A股盘前dry-run] {status} {report.get('trade_date', '')}"
    html = (
        "<!DOCTYPE html><html><body>"
        "<h2>TradingAgent A股盘前 dry-run 异常</h2>"
        f"<pre style=\"white-space:pre-wrap;font-family:-apple-system,'PingFang SC',sans-serif;\">{rendered_text}</pre>"
        "</body></html>"
    )
    return email_sender.send_email(
        email_sender.CHANNELS["system"]["to"],
        subject,
        rendered_text,
        html,
        channel="system",
        rate_limit_type=f"ashare_preopen_dry_run:{status}",
    )


def maybe_send_alert(report: dict[str, Any], rendered_text: str, send_on: str) -> dict[str, Any]:
    status = str(report.get("status") or "warn")
    should_send = send_on == "warn" and status != "pass"
    should_send = should_send or (send_on == "fail" and status == "fail")
    if not should_send:
        return {"status": "skipped", "reason": "ashare_preopen_dry_run_pass_or_send_disabled"}
    return _send_alert(report, rendered_text)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only A-share pre-open dry run.")
    parser.add_argument("--now", default=None)
    parser.add_argument("--sqlite-db", type=Path, default=DEFAULT_SQLITE_DB)
    parser.add_argument("--score-limit", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--send-on", choices=["warn", "fail", "never"], default="never")
    parser.add_argument("--no-write", action="store_true", help="Do not write latest/history report files.")
    parser.add_argument("--exit-zero", action="store_true", help="Return 0 after reporting so cron does not retry identical alerts.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_preopen_dry_run(
        now=_now_cn(args.now) if args.now else None,
        sqlite_db=args.sqlite_db,
        score_limit=args.score_limit,
    )
    rendered = render_text(report)
    email_result = maybe_send_alert(report, rendered, args.send_on)
    report["email"] = email_result
    if not args.no_write:
        write_outputs(report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    else:
        print(rendered)
        if email_result.get("status") not in {"skipped", "rate_limited"}:
            print(f"邮件: {email_result.get('status')} -> {email_result.get('to')}")
    if args.exit_zero:
        return 0
    return 2 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
