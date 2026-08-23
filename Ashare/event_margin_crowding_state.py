"""Market-level margin-crowding state layer over the lockup sell_off stream.

Task #19 (literature Top1 from the 2026-08-24 evidence scan): use aggregate
margin-balance momentum (rzye 20-session change) as an explicit conditioning
STATE for the existing lockup signal, instead of relying on the 10-session
index-return regime alone.  Two readouts:

  * cross-tab of rule-arm signal outcomes by margin-state bucket
    (full sample AND post-2016 — the leverage bubble poisons pre-2016 means);
  * portfolio rerun with entries restricted to margin-favorable buckets,
    through the SAME engine and locked slot convention as #423/#425.

Lookahead discipline: the rzye value dated day D is published the NEXT
morning, so an entry at day-E close may only use states whose margin session
is STRICTLY BEFORE E.  Bucket edges are FIXED thresholds mirroring the lane's
REGIME_BINS convention (-2% / +2%), not sample quantiles — no in-sample edge
fitting.

Cache-only (no network).  Never writes to SampleJournal or any ledger.
research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_margin_crowding_state.py [--cache DIR]
"""

from __future__ import annotations

import argparse
import bisect
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_calendar_lockup_strata import (  # noqa: E402
    COST_BPS_ROUNDTRIP_DEFAULT,
)
from Ashare.event_margin_flow_research import (  # noqa: E402
    daily_totals,
    load_cached_aggregate,
    margin_features,
)
from Ashare.event_paper_baseline_sim import (  # noqa: E402
    INITIAL_CASH_CNY,
    SIM_START,
    build_signals,
    load_events,
    load_stock_books,
    monthly_net_returns,
    max_drawdown,
    rule_arm_filter,
    run_portfolio,
)
from Ashare.event_calendar_lockup_strata import load_index_series  # noqa: E402

MARGIN_START = "20100401"
POST_SUBSAMPLE_START = "20160101"  # skip the 2015 leverage bubble in means
# Fixed bucket edges on the 20-session aggregate rzye change (same shape as
# REGIME_BINS): deleveraging / neutral / expansion.
MARGIN_STATE_BINS: tuple[tuple[float, float, str], ...] = (
    (-1.0, -0.02, "deleverage"),
    (-0.02, 0.02, "neutral"),
    (0.02, 10.0, "expansion"),
)


class CrowdingStateError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


def classify_margin_state(change: float) -> str:
    for low, high, label in MARGIN_STATE_BINS:
        if low <= change < high:
            return label
    return "unknown"


def load_margin_states(cache: Path) -> tuple[list[str], dict[str, float]]:
    """(sorted margin session days, {day: 20-session rzye change})."""
    fields, rows = load_cached_aggregate(cache)
    totals = daily_totals(fields, rows)
    features = margin_features(totals)
    days = [day for day, _short, _long in features]
    states = {day: long_chg for day, _short, long_chg in features}
    if not days:
        raise CrowdingStateError("margin_features_empty")
    return days, states


def attach_margin_states(
    signals: list[dict[str, object]],
    margin_days: list[str],
    states: dict[str, float],
) -> tuple[list[dict[str, object]], int]:
    """Mutate copies? No — annotate in place; count signals without a state."""
    missing = 0
    for signal in signals:
        pos = bisect.bisect_left(margin_days, str(signal["entry_day"])) - 1
        if pos < 0:
            signal["margin_state"] = None
            missing += 1
            continue
        day = margin_days[pos]
        signal["margin_state"] = classify_margin_state(states[day])
        signal["margin_state_day"] = day
    return signals, missing


def net_trade_return(signal: dict[str, object], cost_bps: float) -> float:
    gross = float(signal["exit_price"]) / float(signal["entry_price"]) - 1.0
    return gross - cost_bps / 1e4


def cross_tab(
    signals: list[dict[str, object]], cost_bps: float
) -> dict[str, dict[str, object]]:
    """Per margin-state bucket: n / mean net bps / win rate."""
    buckets: dict[str, list[float]] = {}
    for signal in signals:
        label = signal.get("margin_state")
        if label is None:
            continue
        buckets.setdefault(str(label), []).append(net_trade_return(signal, cost_bps))
    out: dict[str, dict[str, object]] = {}
    for label in ("deleverage", "neutral", "expansion"):
        rets = buckets.get(label, [])
        out[label] = {
            "n": len(rets),
            "mean_net_bps": (sum(rets) / len(rets)) * 1e4 if rets else None,
            "win_rate": (sum(1 for r in rets if r > 0.0) / len(rets)) if rets else None,
        }
    return out


def _fmt(value: object) -> str:
    return f"{float(value):+.1f}" if value is not None else "n/a"


def run_study(cache: Path, cost_bps: float = COST_BPS_ROUNDTRIP_DEFAULT) -> dict[str, object]:
    index_pairs = load_index_series(cache)
    global_days = [
        d.strftime("%Y%m%d")
        for d, _ in index_pairs
        if d.strftime("%Y%m%d") >= SIM_START
    ]
    margin_days, states = load_margin_states(cache)

    events, _event_stats = load_events(cache)
    books, uncovered = load_stock_books(cache)
    signals, _signal_stats = build_signals(events, books, index_pairs, global_days[-1])
    if not signals:
        raise CrowdingStateError("signals_empty")

    rule_signals = [s for s in signals if rule_arm_filter(s)]
    attach_margin_states(rule_signals, margin_days, states)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "universe_books": len(books),
        "universe_uncovered_symbols": uncovered,
    }
    full_tab = cross_tab(rule_signals, cost_bps)
    post = [s for s in rule_signals if str(s["entry_day"]) >= POST_SUBSAMPLE_START]
    post_tab = cross_tab(post, cost_bps)
    results["cross_tab_full"] = full_tab
    results["cross_tab_post2016"] = post_tab

    print("## 融资拥挤度状态层 × 解禁规则臂（research_only，非晋级证据）")
    print(f"- 覆盖：{len(books)} 只个股；规则臂信号 n={len(rule_signals)}"
          f"（post-2016 子样本 n={len(post)}）；成本 {cost_bps}bps 往返")
    for title, tab in (("全样本", full_tab), ("post-2016", post_tab)):
        print(f"- [{title}] 按入场前融资余额20日变化分桶：")
        for label in ("deleverage", "neutral", "expansion"):
            cell = tab[label]
            print(f"    {label:<10} n={cell['n']:<5} "
                  f"净均={_fmt(cell['mean_net_bps'])}bps  胜率="
                  + (f"{float(cell['win_rate']):.3f}" if cell['win_rate'] is not None else "n/a"))

    # Portfolio reruns: baseline vs margin-conditioned entries.  The favorable
    # set is chosen AFTER reading the cross-tab — exploratory by construction.
    favorable = [
        label for label in ("deleverage", "neutral", "expansion")
        if (full_tab[label]["mean_net_bps"] is not None
            and float(full_tab[label]["mean_net_bps"]) > 0.0)
    ]
    results["favorable_states_full_sample"] = favorable
    conditioned = [
        s for s in rule_signals if s.get("margin_state") in favorable
    ]

    def _portfolio_row(name: str, arm: list[dict[str, object]]) -> None:
        if not arm:
            results[f"portfolio_{name}"] = {"signals": 0}
            print(f"- [{name}] 无信号，跳过组合复算")
            return
        run = run_portfolio(arm, global_days, books, cost_bps=cost_bps)
        nav: list[tuple[str, float]] = run["nav"]  # type: ignore[assignment]
        months = monthly_net_returns(nav, base=INITIAL_CASH_CNY)
        monthly_vals = [r for _, r in months]
        row = {
            "signals": len(arm),
            "closed_positions": run["closed_positions"],
            "win_rate": run["win_rate"],
            "total_net_return": nav[-1][1] / INITIAL_CASH_CNY - 1.0,
            "max_drawdown": max_drawdown(nav),
            "monthly_mean": sum(monthly_vals) / len(monthly_vals),
            "monthly_worst": min(monthly_vals),
        }
        results[f"portfolio_{name}"] = row
        print(
            f"- [{name}] n={len(arm)} 平仓={run['closed_positions']} "
            f"总净={row['total_net_return'] * 100:+.1f}%  "
            f"月均净={row['monthly_mean'] * 100:+.2f}%  "
            f"最差月={row['monthly_worst'] * 100:+.1f}%  "
            f"回撤={row['max_drawdown'] * 100:+.1f}%  笔胜率={row['win_rate']:.3f}"
        )

    _portfolio_row("rule_baseline", rule_signals)
    _portfolio_row(f"rule_margin_{'_'.join(favorable) or 'none'}", conditioned)
    # Standalone per-bucket portfolios: does CONCENTRATING on one margin
    # state beat the pooled baseline on risk-adjusted terms?
    for label in ("deleverage", "neutral", "expansion"):
        subset = [s for s in rule_signals if s.get("margin_state") == label]
        if subset:
            _portfolio_row(f"bucket_{label}", subset)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    run_study(cache)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CrowdingStateError as exc:
        print(f"MARGIN_CROWDING_STATE_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
