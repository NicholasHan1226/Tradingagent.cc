"""Portfolio-level overlay scan: exclude pre-event high-pledge entries.

DESCRIPTIVE research on top of the locked slot-portfolio baseline
(``event_paper_baseline_sim.py``, reused unmodified): the #464/#467
watch-list item says the rule arm should AVOID names whose latest
pre-event pledge snapshot is >= 20%.  This script quantifies what that
exclusion would have done AT THE PORTFOLIO LEVEL by running the SAME
slot engine four ways in one process:

- ``all``            every sell_off signal (baseline reference)
- ``all_ex_high``    all minus pre-event high-pledge entries
- ``rule``           weak-market x non-band rule arm (baseline reference)
- ``rule_ex_high``   rule minus pre-event high-pledge entries

Paired same-process deltas avoid cross-run drift.  The slot engine is
path-dependent: dropping trades changes the cash trajectory, so the
delta is a PORTFOLIO effect, not per-trade attribution.  Watch list !=
deployment candidate — this scan moves no pre-registered gate and is
not promotion evidence; a real deployment would need its own fresh
pre-registration plus rolling validation.

Usage::

    python3 Ashare/event_pledge_portfolio_overlay.py [--cache DIR]
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
from Ashare.event_pledge_prelockup_study import (  # noqa: E402
    attach_pledge_states,
    load_pledge_index,
)


class PledgeOverlayError(RuntimeError):
    """Fail-closed overlay failure with a stable reason code."""


def drop_high_pledge(
    signals: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Signals whose pre-event pledge bucket is NOT ``high`` (order kept)."""

    return [s for s in signals if s.get("pledge_bucket") != "high"]


def overlay_arms(
    signals: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    """The four same-process comparison arms (references first)."""

    rule_signals = [s for s in signals if rule_arm_filter(s)]
    return {
        "all": signals,
        "all_ex_high": drop_high_pledge(signals),
        "rule": rule_signals,
        "rule_ex_high": drop_high_pledge(rule_signals),
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
        raise PledgeOverlayError("index_history_too_short")
    events, _event_stats = load_events(cache)
    books, uncovered = load_stock_books(cache)
    signals, _sig_stats = build_signals(
        events, books, index_pairs, global_days[-1]
    )
    if not signals:
        raise PledgeOverlayError("signals_empty")

    index = load_pledge_index(cache)  # fail-closed: pledge_cache_missing
    attach_pledge_states(signals, index)

    rule_signals = [s for s in signals if rule_arm_filter(s)]
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
        "dropped_high_all": len(signals) - len(arms["all_ex_high"]),
        "dropped_high_rule": len(rule_signals) - len(arms["rule_ex_high"]),
        "arms": stats,
    }

    print("## 组合层叠加探针：排除解禁前高质押≥20%（research_only，非晋级证据）")
    print("- 观察名单纪律：描述性扫描，不移动任何预注册门柱、不作部署候选；"
          "槽位路径相关——剔除交易改变现金轨迹，差值是组合效应非逐笔归因")
    print(f"- 成本 {cost_bps}bps 往返；剔除数 all {results['dropped_high_all']}"
          f" / rule {results['dropped_high_rule']}")

    header = (
        f"{'arm':<13} {'signals':>7} {'trades':>6} {'win':>6} "
        f"{'total_net':>10} {'m_mean':>8} {'m_worst':>8} {'max_dd':>8}"
    )
    print("\n" + header)
    for name in ("all", "all_ex_high", "rule", "rule_ex_high"):
        s = stats[name]
        print(
            f"{name:<13} {s['signals']:>7} {s['closed_positions']:>6} "
            f"{float(s['win_rate']):>6.3f} {_fmt_pct(float(s['total_net_return'])):>10} "
            f"{float(s['monthly_mean']) * 100:>7.2f}% "
            f"{float(s['monthly_worst']) * 100:>7.2f}% "
            f"{float(s['max_drawdown']) * 100:>7.2f}%"
        )
    print("- 主对比（读前已声明）：rule_ex_high vs rule 的月均净与最差月变化；"
          "次看回撤与胜率。两方向都可能成立，无门柱判定")

    for path_name, arm in (
        ("paper_overlay_nav_all.csv", "all"),
        ("paper_overlay_nav_all_ex_high.csv", "all_ex_high"),
        ("paper_overlay_nav_rule.csv", "rule"),
        ("paper_overlay_nav_rule_ex_high.csv", "rule_ex_high"),
    ):
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
    except PledgeOverlayError as exc:
        print(f"PLEDGE_OVERLAY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
