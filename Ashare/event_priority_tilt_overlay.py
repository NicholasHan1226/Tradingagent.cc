"""Portfolio-level overlay scan: priority tilt instead of hard filters.

DESCRIPTIVE research on top of the locked slot-portfolio baseline
(``event_paper_baseline_sim.py``, reused unmodified).  The three earlier
overlays (#33 pledge exclusion, #34 repurchase active, #51 Pursue pair)
converged on one structural lesson: moderate/high-coverage conditions
carry per-trade quality but DIE as hard filters because dropped slots
idle capital.  Both #34/#51 named the realistic form: keep every slot
deployed and TILT the fill order toward labelled candidates.

Mechanism this scan tests (engine fact, not an assumption): batches are
grouped by entry day and consumed in INPUT-LIST ORDER; when cash or the
concurrency cap binds, later-in-batch signals are skipped
(``skipped_no_cash`` / ``skipped_capped``).  Reordering the input list
therefore implements priority tilt with ZERO engine changes.

Pre-read facts (measured before any tilted run): the rule arm shows
near-zero natural scarcity -- 1/357 entries skipped for cash, max daily
batch 5 against 10 slots -- so an UNCAPPED tilt is expected to be inert;
that arm is kept anyway as an honest demonstration.  The informative
regime is ``max_concurrent=4`` (near the arm's natural mean occupancy),
where the cap actually binds.

Arms (references first):

- ``rule``           uncapped reference
- ``rule_tilt``      uncapped, Pursue-pair-first order (expected inert)
- ``rule_cap4``      capped reference
- ``rule_cap4_tilt`` capped + tilt   <-- MAIN CONTRAST vs rule_cap4

Tilt ranking (frozen before reading): rank 0 = both Pursue labels
(valuation ``low_le25`` AND holdertype ``incentive``), rank 1 = exactly
one, rank 2 = neither or unlabeled; stable sort so ties keep original
order.  Labels come from the two study engines' frozen side-table
helpers keyed (ts_code, float_date); unlabeled ranks LAST (honest).

Paired same-process deltas; the engine is path-dependent so deltas are
PORTFOLIO effects.  Watch list != deployment candidate — moves no gate.

Usage::

    python3 Ashare/event_priority_tilt_overlay.py [--cache DIR]
        [--cost-bps X] [--cap N]
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
    rule_arm_filter,
    run_portfolio,
)
from Ashare.event_pledge_portfolio_overlay import _arm_stats  # noqa: E402
from Ashare.event_pursue_labels import (  # noqa: E402
    PURSUE_HOLDERTYPE,
    PURSUE_VALUATION,
    attach_pursue_labels,
)


class TiltOverlayError(RuntimeError):
    """Fail-closed overlay failure with a stable reason code."""


def tilt_rank(signal: dict[str, object]) -> int:
    """Frozen ranking: both Pursue labels first, one next, none last."""

    has_val = signal.get("valuation_bucket") == PURSUE_VALUATION
    has_hold = signal.get("holdertype_bucket") == PURSUE_HOLDERTYPE
    if has_val and has_hold:
        return 0
    if has_val or has_hold:
        return 1
    return 2


def tilt_order(
    signals: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Stable re-sort by tilt rank; original order inside equal ranks."""

    return sorted(signals, key=tilt_rank)


def run_overlay(
    cache: Path,
    cost_bps: float = COST_BPS_ROUNDTRIP_DEFAULT,
    cap: int = 4,
) -> dict[str, object]:
    index_pairs = load_index_series(cache)
    global_days = [
        d.strftime("%Y%m%d")
        for d, _ in index_pairs
        if d.strftime("%Y%m%d") >= SIM_START
    ]
    if len(global_days) < 12:
        raise TiltOverlayError("index_history_too_short")
    events, _event_stats = load_events(cache)
    books, uncovered = load_stock_books(cache)
    signals, _sig_stats = build_signals(
        events, books, index_pairs, global_days[-1]
    )
    if not signals:
        raise TiltOverlayError("signals_empty")

    attach_pursue_labels(signals, cache)

    rule_signals = [s for s in signals if rule_arm_filter(s)]
    tilted = tilt_order(rule_signals)
    n_rank = {
        "pair": sum(1 for s in rule_signals if tilt_rank(s) == 0),
        "single": sum(1 for s in rule_signals if tilt_rank(s) == 1),
        "none": sum(1 for s in rule_signals if tilt_rank(s) == 2),
    }

    def _run(name: str, arm: list[dict[str, object]], max_c: int | None):
        stats = _arm_stats(name, arm, global_days, books, index_pairs, cost_bps)
        if max_c is not None:
            rerun = run_portfolio(
                arm, global_days, books, cost_bps=cost_bps, max_concurrent=max_c
            )
            nav = rerun["nav"]
            from Ashare.event_paper_baseline_sim import (
                max_drawdown,
                monthly_net_returns,
            )

            months = [r for _, r in monthly_net_returns(nav, base=INITIAL_CASH_CNY)]
            stats.update(
                {
                    "closed_positions": rerun["closed_positions"],
                    "win_rate": rerun["win_rate"],
                    "total_net_return": nav[-1][1] / INITIAL_CASH_CNY - 1.0 if nav else 0.0,
                    "max_drawdown": max_drawdown(nav),
                    "monthly_mean": sum(months) / len(months) if months else 0.0,
                    "monthly_worst": min(months) if months else 0.0,
                    "skipped_capped": rerun["skipped_capped"],
                    "skipped_no_cash": rerun["skipped_no_cash"],
                    "nav": nav,
                }
            )
        else:
            stats["skipped_capped"] = None
            stats["skipped_no_cash"] = 0
        return stats

    arms_spec = {
        "rule": (rule_signals, None),
        "rule_tilt": (tilted, None),
        f"rule_cap{cap}": (rule_signals, cap),
        f"rule_cap{cap}_tilt": (tilted, cap),
    }
    stats = {name: _run(name, arm, mc) for name, (arm, mc) in arms_spec.items()}

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "universe_uncovered_symbols": uncovered,
        "tilt_mix_rule_arm": n_rank,
        "cap": cap,
        "arms": stats,
    }

    print("## 组合层叠加探针：优先级倾斜替代硬过滤（research_only，非晋级证据）")
    print("- #34/#51 结构教训的现实形态验证：槽位全部署、同日批次按标签优先"
          "排序；锁定引擎零改动（排序输入+既有 max_concurrent 参数）；"
          "预读事实=规则臂自然稀缺近零（1/357 现金拒入），无上限倾斜预期惰性，"
          f"信息量在有约束 regime（cap={cap}）")
    print(f"- 标签覆盖（rule 臂 {len(rule_signals)} 条）：双条件 "
          f"{n_rank['pair']} / 单条件 {n_rank['single']} / 无 {n_rank['none']}；"
          f"成本 {cost_bps}bps 往返")

    header = (
        f"{'arm':<18} {'trades':>6} {'win':>6} {'total_net':>10} "
        f"{'m_mean':>8} {'m_worst':>8} {'max_dd':>8} {'skip_cap':>8}"
    )
    print("\n" + header)
    for name in arms_spec:
        s = stats[name]
        sc = "—" if s["skipped_capped"] is None else str(s["skipped_capped"])
        print(
            f"{name:<18} {s['closed_positions']:>6} "
            f"{float(s['win_rate']):>6.3f} {_fmt_pct(float(s['total_net_return'])):>10} "
            f"{float(s['monthly_mean']) * 100:>7.2f}% "
            f"{float(s['monthly_worst']) * 100:>7.2f}% "
            f"{float(s['max_drawdown']) * 100:>7.2f}% {sc:>8}"
        )
    print(f"- 主对比（读前已声明）：rule_cap{cap}_tilt vs rule_cap{cap} 的月均净"
          "与最差月；次看无上限对是否确证惰性。两方向都可能成立，无门柱判定")

    for name in arms_spec:
        path_name = f"paper_tilt_nav_{name}.csv"
        with (cache / path_name).open("w", newline="", encoding="utf-8") as h:
            writer = csv.writer(h)
            writer.writerow(["trade_date", "equity_cny", "research_only"])
            writer.writerows(
                (d, f"{v:.2f}", "not_promotion_evidence")
                for d, v in stats[name]["nav"]
            )
    return results


def _fmt_pct(value: float) -> str:
    return f"{value * 100.0:+.2f}%"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--cost-bps", type=float, default=COST_BPS_ROUNDTRIP_DEFAULT)
    parser.add_argument("--cap", type=int, default=4)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    run_overlay(cache, cost_bps=args.cost_bps, cap=args.cap)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TiltOverlayError as exc:
        print(f"TILT_OVERLAY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
