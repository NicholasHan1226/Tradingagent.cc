"""Portfolio-level overlay scan: Pursue pair as slot filters (#16).

DESCRIPTIVE research on top of the locked slot-portfolio baseline
(``event_paper_baseline_sim.py``, reused unmodified): the #47 forward
program's two Pursue conditions -- pre-unlock own-history valuation
percentile ``low_le25`` and holder-type ``incentive`` -- are applied as
slot-portfolio filters.  Same four-arm pattern as the pledge overlay
(``event_pledge_portfolio_overlay.py``), one process:

- ``rule``            weak-market x non-band reference arm
- ``rule_val``        rule ∩ low_le25
- ``rule_holdertype`` rule ∩ incentive
- ``rule_pair``       rule ∩ both

Labels come from the two study engines' frozen side-table helpers
(``valuation_buckets_for_entries`` / ``holdertype_buckets_for_entries``)
keyed on (ts_code, float_date) identity — the exact keying the engines
and tracker use; no re-derivation here.  Missing valuation shard or
unknown holder batch excludes a signal from the respective pursue arm
(tracker convention: unlabeled is honest).  Paired same-process deltas;
the slot engine is path-dependent so deltas are PORTFOLIO effects, not
per-trade attribution.  Watch list != deployment candidate — this scan
moves no pre-registered gate and is not promotion evidence.

Usage::

    python3 Ashare/event_valholdtype_portfolio_overlay.py [--cache DIR]
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
from Ashare.event_pledge_portfolio_overlay import _arm_stats  # noqa: E402
from Ashare.event_pursue_labels import (  # noqa: E402
    PURSUE_HOLDERTYPE,
    PURSUE_VALUATION,
    attach_pursue_labels,
)


class ValHoldOverlayError(RuntimeError):
    """Fail-closed overlay failure with a stable reason code."""


def overlay_arms(
    signals: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    """The four same-process comparison arms (reference first)."""

    def _keep(s: dict[str, object], want_val: bool, want_hold: bool) -> bool:
        if not rule_arm_filter(s):
            return False
        if want_val and s.get("valuation_bucket") != PURSUE_VALUATION:
            return False
        if want_hold and s.get("holdertype_bucket") != PURSUE_HOLDERTYPE:
            return False
        return True

    return {
        "rule": [s for s in signals if rule_arm_filter(s)],
        "rule_val": [s for s in signals if _keep(s, True, False)],
        "rule_holdertype": [s for s in signals if _keep(s, False, True)],
        "rule_pair": [s for s in signals if _keep(s, True, True)],
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
        raise ValHoldOverlayError("index_history_too_short")
    events, _event_stats = load_events(cache)
    books, uncovered = load_stock_books(cache)
    signals, _sig_stats = build_signals(
        events, books, index_pairs, global_days[-1]
    )
    if not signals:
        raise ValHoldOverlayError("signals_empty")

    attach_pursue_labels(signals, cache)

    rule_signals = [s for s in signals if rule_arm_filter(s)]
    arms = overlay_arms(signals)
    stats = {
        name: _arm_stats(name, arm, global_days, books, index_pairs, cost_bps)
        for name, arm in arms.items()
    }

    from collections import Counter

    rule_val_mix = Counter(str(s.get("valuation_bucket")) for s in rule_signals)
    rule_hold_mix = Counter(str(s.get("holdertype_bucket")) for s in rule_signals)
    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "universe_uncovered_symbols": uncovered,
        "rule_arm_label_coverage": {
            "valuation": dict(rule_val_mix),
            "holdertype": dict(rule_hold_mix),
            "pursue_pair_n": len(arms["rule_pair"]),
        },
        "arms": stats,
    }

    print("## 组合层叠加探针：Pursue 对（估值低位×激励持有）作槽位过滤器"
          "（research_only，非晋级证据）")
    print("- 观察名单纪律：描述性扫描，不移动任何预注册门柱、不作部署候选；"
          "槽位路径相关——剔除交易改变现金轨迹，差值是组合效应非逐笔归因；"
          "标签缺档按剔除处理（未标注是诚实，误标注不是）")
    print(f"- 成本 {cost_bps}bps 往返；rule 臂 {len(rule_signals)} 条中 "
          f"val=low_le25 {rule_val_mix.get(PURSUE_VALUATION, 0)} / "
          f"hold=incentive {rule_hold_mix.get(PURSUE_HOLDERTYPE, 0)} / "
          f"双条件 {len(arms['rule_pair'])}")

    header = (
        f"{'arm':<17} {'signals':>7} {'trades':>6} {'win':>6} "
        f"{'total_net':>10} {'m_mean':>8} {'m_worst':>8} {'max_dd':>8}"
    )
    print("\n" + header)
    for name in ("rule", "rule_val", "rule_holdertype", "rule_pair"):
        s = stats[name]
        print(
            f"{name:<17} {s['signals']:>7} {s['closed_positions']:>6} "
            f"{float(s['win_rate']):>6.3f} {_fmt_pct(float(s['total_net_return'])):>10} "
            f"{float(s['monthly_mean']) * 100:>7.2f}% "
            f"{float(s['monthly_worst']) * 100:>7.2f}% "
            f"{float(s['max_drawdown']) * 100:>7.2f}%"
        )
    print("- 主对比（读前已声明）：rule_pair vs rule 的月均净与最差月变化；"
          "次看两个单条件臂的边际贡献。两方向都可能成立，无门柱判定")

    for path_name, arm in (
        ("paper_valhold_nav_rule.csv", "rule"),
        ("paper_valhold_nav_rule_val.csv", "rule_val"),
        ("paper_valhold_nav_rule_holdertype.csv", "rule_holdertype"),
        ("paper_valhold_nav_rule_pair.csv", "rule_pair"),
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
    except ValHoldOverlayError as exc:
        print(f"VALHOLD_OVERLAY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
