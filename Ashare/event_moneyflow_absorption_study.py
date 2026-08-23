"""Order-flow absorption study: does WHO buys before a sell_off matter?

Pre-registered with the moneyflow fetcher (H1/H2): sell_off relief quality
may depend on LARGE-ORDER absorption ahead of the event — unlocked supply
meeting active large buyers differs from supply landing on retail bids.
This is a mechanism dimension distinct from both margin layers (#426 market,
#430 own-stock), and the first genuinely new data plane since the baseline.

Measure: per symbol, the 5-session pre-event cumulative large+extra-large
net buy amount, normalized by that symbol's trailing 20-session average
total turnover (scale-free ratio).  Strictly-prior windows only — sessions
on/after the entry day never enter either window.  Fixed bucket edges at
±0.10 (calibrated on fetched data: median −0.078, |ratio| ≥ 0.10 ≈ 53%),
same no-quantile-fitting convention as the margin lane.

Population = the #423 lockup sell_off stream.  Cache-only.
research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_moneyflow_absorption_study.py [--cache DIR]
        [--cost-bps X]
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
from Ashare.event_margin_crowding_state import (  # noqa: E402
    cross_tab,
)
from Ashare.event_paper_baseline_sim import (  # noqa: E402
    INITIAL_CASH_CNY,
    SIM_START,
    build_signals,
    load_events,
    load_index_series,
    load_stock_books,
    max_drawdown,
    monthly_net_returns,
    rule_arm_filter,
    run_portfolio,
)

MONEYFLOW_DIRNAME = "moneyflow_daily"
PRE_WINDOW_SESSIONS = 5     # pre-event absorption window (strictly prior)
TRAIL_WINDOW_SESSIONS = 20  # turnover normalizer window (strictly prior)
AMOUNT_FIELDS = (
    "buy_sm_amount", "sell_sm_amount",
    "buy_md_amount", "sell_md_amount",
    "buy_lg_amount", "sell_lg_amount",
    "buy_elg_amount", "sell_elg_amount",
)
LARGE_NET_FIELDS = ("buy_lg_amount", "buy_elg_amount",
                    "sell_lg_amount", "sell_elg_amount")
ABSORPTION_BUCKETS = ("outflow", "balanced", "inflow")
# Frozen fixed edges on the scale-free ratio; ±0.10 chosen from the fetched
# distribution (median -0.078), NOT per-sample quantiles.  Half-open bands:
# ratio <= -0.10 outflow · [-0.10, 0.10) balanced · >= 0.10 inflow.
ABSORPTION_OUTFLOW_EDGE = -0.10
ABSORPTION_INFLOW_EDGE = 0.10


class AbsorptionStudyError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


def classify_absorption(ratio: float) -> str:
    if ratio >= ABSORPTION_INFLOW_EDGE:
        return "inflow"
    if ratio <= ABSORPTION_OUTFLOW_EDGE:
        return "outflow"
    return "balanced"


def load_symbol_moneyflow(
    cache: Path, symbols: set[str]
) -> dict[str, tuple[list[str], list[float], list[float]]]:
    """Per-symbol (days, large_net_amt, total_amt), universe-filtered."""
    flow_dir = cache / MONEYFLOW_DIRNAME
    if not flow_dir.is_dir():
        raise AbsorptionStudyError(f"moneyflow_dir_missing:{flow_dir}")
    import csv

    series: dict[str, tuple[list[str], list[float], list[float]]] = {}
    for path in sorted(flow_dir.glob("*.csv")):
        day = path.stem
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                code = row.get("ts_code")
                if code not in symbols:
                    continue
                try:
                    net_lg = (
                        float(row["buy_lg_amount"]) + float(row["buy_elg_amount"])
                        - float(row["sell_lg_amount"]) - float(row["sell_elg_amount"])
                    )
                    total = sum(float(row[f]) for f in AMOUNT_FIELDS)
                except (KeyError, TypeError, ValueError):
                    continue  # malformed row: skip, do not guess
                days, nets, totals = series.setdefault(code, ([], [], []))
                days.append(day)
                nets.append(net_lg)
                totals.append(total)
    return series


def attach_absorption(
    signals: list[dict[str, object]],
    series: dict[str, tuple[list[str], list[float], list[float]]],
    pre_window: int = PRE_WINDOW_SESSIONS,
    trail_window: int = TRAIL_WINDOW_SESSIONS,
) -> dict[str, int]:
    """Annotate each signal with its pre-event absorption bucket, in place."""
    stats = {"missing_series": 0, "insufficient_history": 0, "attached": 0}
    need = max(pre_window, trail_window)
    for signal in signals:
        book = series.get(str(signal["ts_code"]))
        if book is None:
            signal["absorption_bucket"] = "insufficient_history"
            stats["missing_series"] += 1
            continue
        days, nets, totals = book
        pos = bisect.bisect_left(days, str(signal["entry_day"]))
        if pos < need:
            signal["absorption_bucket"] = "insufficient_history"
            stats["insufficient_history"] += 1
            continue
        avg_total = sum(totals[pos - trail_window:pos]) / trail_window
        if avg_total <= 0.0:
            signal["absorption_bucket"] = "insufficient_history"
            stats["insufficient_history"] += 1
            continue
        ratio = sum(nets[pos - pre_window:pos]) / avg_total
        signal["absorption_ratio"] = ratio
        signal["absorption_bucket"] = classify_absorption(ratio)
        stats["attached"] += 1
    return stats


def _fmt_pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def run_study(cache: Path, cost_bps: float = COST_BPS_ROUNDTRIP_DEFAULT) -> dict[str, object]:
    index_pairs = load_index_series(cache)
    global_days = [
        d.strftime("%Y%m%d")
        for d, _ in index_pairs
        if d.strftime("%Y%m%d") >= SIM_START
    ]
    events, _stats = load_events(cache)
    books, uncovered = load_stock_books(cache)
    signals, _sig_stats = build_signals(events, books, index_pairs, global_days[-1])

    series = load_symbol_moneyflow(cache, {str(s["ts_code"]) for s in signals})
    attach_stats = attach_absorption(signals, series)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "universe_uncovered_symbols": uncovered,
    }
    print("## 大单承接吸收研究（research_only，非晋级证据）")
    print(f"- 信号总数 {len(signals)}；覆盖序列 {len(series)} 只；"
          f"附加统计 {attach_stats}；成本 {cost_bps}bps 往返；"
          f"窗口：事前 {PRE_WINDOW_SESSIONS} 会话大单净额 / 前 "
          f"{TRAIL_WINDOW_SESSIONS} 会话均成交额，固定边界 ±0.10")

    labeled = [s for s in signals
               if s["absorption_bucket"] != "insufficient_history"]
    tab = cross_tab(labeled, cost_bps=cost_bps, key="absorption_bucket",
                    labels=ABSORPTION_BUCKETS)
    results["signal_level_cross_tab"] = tab
    print("\n### 信号层交叉表（净 bps / 胜率）")
    print(f"{'bucket':<10} {'n':>6} {'mean_net_bps':>13} {'win_rate':>9}")
    for label, cell in tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        print(
            f"{label:<10} {cell['n']:>6} "
            f"{'—' if mean_bps is None else f'{float(mean_bps):+.1f}':>13} "
            f"{'—' if win is None else f'{float(win):.3f}':>9}"
        )

    def _portfolio_row(name: str, arm: list[dict[str, object]]) -> None:
        arm_sorted = sorted(arm, key=lambda s: (str(s["entry_day"]), str(s["ts_code"])))
        if len(arm_sorted) < 10:
            results["portfolio"][name] = {"signals": len(arm_sorted)}  # type: ignore[index]
            print(f"{name:<26} {len(arm_sorted):>5}   （样本不足，不跑组合）")
            return
        run = run_portfolio(arm_sorted, global_days, books, cost_bps=cost_bps)
        nav = run["nav"]
        months = [r for _, r in monthly_net_returns(nav, base=INITIAL_CASH_CNY)]
        row = {
            "closed_positions": run["closed_positions"],
            "total_net_return": nav[-1][1] / INITIAL_CASH_CNY - 1.0,
            "monthly_mean": sum(months) / len(months),
            "monthly_worst": min(months),
            "max_drawdown": max_drawdown(nav),
            "win_rate": run["win_rate"],
        }
        results["portfolio"][name] = row  # type: ignore[index]
        print(
            f"{name:<26} {row['closed_positions']:>5} "
            f"{_fmt_pct(float(row['total_net_return'])):>9} "
            f"{_fmt_pct(float(row['monthly_mean'])):>8} "
            f"{_fmt_pct(float(row['monthly_worst'])):>8} "
            f"{_fmt_pct(float(row['max_drawdown'])):>8} "
            f"{row['win_rate']:>6.3f}"
        )

    results["portfolio"] = {}  # type: ignore[assignment]
    print("\n### 组合层（同槽位口径对照）")
    header = (f"{'arm':<26} {'closed':>5} {'总净':>9} {'月均净':>8} "
              f"{'最差月':>8} {'回撤':>8} {'胜率':>6}")
    print(header)
    _portfolio_row("pooled_labeled", labeled)
    for label in ABSORPTION_BUCKETS:
        _portfolio_row(
            f"absorb_{label}",
            [s for s in signals if s["absorption_bucket"] == label],  # type: ignore[arg-type]
        )

    rule = [s for s in signals if rule_arm_filter(s)]
    rule_labeled = [s for s in rule
                    if s["absorption_bucket"] != "insufficient_history"]
    rule_tab = cross_tab(rule_labeled, cost_bps=cost_bps, key="absorption_bucket",
                         labels=ABSORPTION_BUCKETS)
    results["rule_arm_cross_tab"] = rule_tab
    print("\n### rule 臂叠加交叉表（判定层）")
    for label, cell in rule_tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        print(
            f"rule[{label:<9}] n={cell['n']:>4} "
            f"{'—' if mean_bps is None else f'{float(mean_bps):+.1f}'}bps "
            f"{'—' if win is None else f'win={float(win):.3f}'}"
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--cost-bps", type=float, default=COST_BPS_ROUNDTRIP_DEFAULT)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    run_study(cache, cost_bps=args.cost_bps)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AbsorptionStudyError as exc:
        print(f"ABSORPTION_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
