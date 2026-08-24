"""Portfolio-level overlay scan: restrict entries to pre-event buyback-active.

DESCRIPTIVE research on top of the locked slot-portfolio baseline
(``event_paper_baseline_sim.py``, reused unmodified) — the symmetric
companion to ``event_pledge_portfolio_overlay.py``.  The #458/#460
watch-list item says the rule arm's QUALIFYING bucket is
``rule[active]`` (ongoing buybacks = management absorbing unlock
supply, mean AND win rate both above no_records).  This script runs the
SAME slot engine four ways in one process:

- ``all``         every sell_off signal (baseline reference)
- ``all_active``  all restricted to pre-event repurchase ``active``
- ``rule``        weak-market x non-band rule arm (baseline reference)
- ``rule_active`` rule restricted to pre-event repurchase ``active``

Primary (declared before numbers print): ``rule_active`` vs ``rule``
monthly-mean and worst-month change; secondary drawdown / win rate.
Coverage caveat: only ~25% of signals have any in-window repurchase
record, so the active-restricted arms are SPARSE by construction —
slot idling is part of the measured portfolio effect, not a bug.
Path-dependent slot engine: deltas are PORTFOLIO effects, not per-trade
attribution.  Watch list != deployment candidate — no pre-registered
gate moves; deployment would need fresh pre-registration plus rolling
validation.

Usage::

    python3 Ashare/event_repurchase_portfolio_overlay.py [--cache DIR]
        [--cost-bps X]
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_paper_baseline_sim import (  # noqa: E402
    COST_BPS_ROUNDTRIP_DEFAULT,
    INITIAL_CASH_CNY,
    SIM_START,
    build_signals,
    benchmark_return,
    load_events,
    load_index_series,
    load_stock_books,
    max_drawdown,
    monthly_net_returns,
    rule_arm_filter,
    run_portfolio,
)
from Ashare.event_repurchase_prelockup_study import (  # noqa: E402
    repurchase_buckets_for_events,
)


class RepurchaseOverlayError(RuntimeError):
    """Fail-closed overlay failure with a stable reason code."""


def keep_active(
    signals: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Signals whose pre-event repurchase bucket IS ``active``."""

    return [s for s in signals if s.get("repurchase_bucket") == "active"]


def overlay_arms(
    signals: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    """The four same-process comparison arms (references first)."""

    rule_signals = [s for s in signals if rule_arm_filter(s)]
    return {
        "all": signals,
        "all_active": keep_active(signals),
        "rule": rule_signals,
        "rule_active": keep_active(rule_signals),
    }


def _arm_stats(
    name: str,
    arm_signals: list[dict[str, object]],
    global_days: list[str],
    books: dict,
    index_pairs: list,
    cost_bps: float,
) -> dict[str, object]:
    run = run_portfolio(arm_signals, global_days, books, cost_bps=cost_bps)
    nav = run["nav"]
    months = monthly_net_returns(nav, base=INITIAL_CASH_CNY)
    monthly_vals = [r for _, r in months]
    bench = (
        benchmark_return(index_pairs, nav[0][0], nav[-1][0]) if nav else None
    )
    return {
        "signals": len(arm_signals),
        "closed_positions": run["closed_positions"],
        "win_rate": run["win_rate"],
        "total_net_return": nav[-1][1] / INITIAL_CASH_CNY - 1.0 if nav else 0.0,
        "max_drawdown": max_drawdown(nav),
        "monthly_mean": sum(monthly_vals) / len(monthly_vals) if monthly_vals else 0.0,
        "monthly_worst": min(monthly_vals) if monthly_vals else 0.0,
        "benchmark_total_return": bench,
        "nav": nav,
    }


def _fmt_pct(value: float) -> str:
    return f"{value * 100.0:+.2f}%"


def run_overlay(
    cache: Path, cost_bps: float = COST_BPS_ROUNDTRIP_DEFAULT
) -> dict[str, object]:
    index_pairs = load_index_series(cache)
    global_days = [
        d.strftime("%Y%m%d")
        for d, _ in index_pairs
        if d.strftime("%Y%m%d") >= SIM_START
    ]
    if len(global_days) < 12:
        raise RepurchaseOverlayError("index_history_too_short")
    events, _event_stats = load_events(cache)
    books, uncovered = load_stock_books(cache)
    signals, _sig_stats = build_signals(
        events, books, index_pairs, global_days[-1]
    )
    if not signals:
        raise RepurchaseOverlayError("signals_empty")

    buckets = repurchase_buckets_for_events(
        cache,  # fail-closed inside: repurchase_cache_missing
        [(str(s["ts_code"]), str(s["entry_day"])) for s in signals],
    )
    for s in signals:
        s["repurchase_bucket"] = buckets[(str(s["ts_code"]), str(s["entry_day"]))]

    arms = overlay_arms(signals)
    stats = {
        name: _arm_stats(name, arm, global_days, books, index_pairs, cost_bps)
        for name, arm in arms.items()
    }

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "universe_uncovered_symbols": uncovered,
        "active_all": len(arms["all_active"]),
        "active_rule": len(arms["rule_active"]),
        "arms": stats,
    }

    print("## 组合层叠加探针：规则臂仅保留事前回购 active（research_only，非晋级证据）")
    print("- 观察名单纪律：描述性扫描，不移动任何预注册门柱、不作部署候选；"
          "槽位路径相关——差值是组合效应非逐笔归因；active 覆盖仅约 1/4，"
          "受限臂稀疏属测量对象本身（空转槽位计入组合效应）")
    print(f"- 成本 {cost_bps}bps 往返；active 数 all {results['active_all']}"
          f" / rule {results['active_rule']}")

    header = (
        f"{'arm':<12} {'signals':>7} {'trades':>6} {'win':>6} "
        f"{'total_net':>10} {'m_mean':>8} {'m_worst':>8} {'max_dd':>8}"
    )
    print("\n" + header)
    for name in ("all", "all_active", "rule", "rule_active"):
        s = stats[name]
        print(
            f"{name:<12} {s['signals']:>7} {s['closed_positions']:>6} "
            f"{float(s['win_rate']):>6.3f} {_fmt_pct(float(s['total_net_return'])):>10} "
            f"{float(s['monthly_mean']) * 100:>7.2f}% "
            f"{float(s['monthly_worst']) * 100:>7.2f}% "
            f"{float(s['max_drawdown']) * 100:>7.2f}%"
        )
    print("- 主对比（读前已声明）：rule_active vs rule 的月均净与最差月变化；"
          "次看回撤与胜率。两方向都可能成立，无门柱判定")

    for path_name, arm in (
        ("paper_repoverlay_nav_all_active.csv", "all_active"),
        ("paper_repoverlay_nav_rule_active.csv", "rule_active"),
    ):
        # Reference-arm NAVs already sit on disk from the pledge overlay.
        with (cache / path_name).open("w", newline="", encoding="utf-8") as h:
            writer = csv.writer(h)
            writer.writerow(["trade_date", "equity_cny", "research_only"])
            writer.writerows(
                (d, f"{v:.2f}", "not_promotion_evidence")
                for d, v in stats[arm]["nav"]
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--cost-bps", type=float, default=COST_BPS_ROUNDTRIP_DEFAULT)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    run_overlay(cache, cost_bps=args.cost_bps)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RepurchaseOverlayError as exc:
        print(f"REPURCHASE_OVERLAY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
