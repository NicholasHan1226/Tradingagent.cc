"""Stock-level own-margin state study (phase two of the margin crowding lane).

#426 established that MARKET-level margin crowding carries information at the
signal level but none at the portfolio level.  Phase two asks whether a
symbol's OWN financing-balance trajectory separates its sell_off outcomes:
per-symbol ``rzye`` 20-session change, bucketed with the SAME fixed edges as
the market layer (no sample quantile fitting), attached under the SAME
strict-prior publication lag (a balance dated D is first usable at D+1's
open, so an entry on E reads the last value strictly before E).

Population = the #423 lockup sell_off stream unchanged, so every readout is
directly comparable to #426.  Cache-only.  research_only /
not_promotion_evidence.

Usage::

    python3 Ashare/event_margin_own_stock_study.py [--cache DIR]
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
    classify_margin_state,
    cross_tab,
)
from Ashare.event_paper_baseline_sim import (  # noqa: E402
    SIM_START,
    build_signals,
    load_events,
    load_stock_books,
    load_index_series,
    max_drawdown,
    monthly_net_returns,
    INITIAL_CASH_CNY,
    run_portfolio,
)

DETAIL_DIRNAME = "margin_detail_daily"
OWN_MARGIN_LOOKBACK_SESSIONS = 20  # same window as the market-layer change


class OwnMarginStudyError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


def load_symbol_margin_series(
    cache: Path, symbols: set[str]
) -> dict[str, tuple[list[str], list[float]]]:
    """Per-symbol (days, rzye) from the daily detail dump, universe-filtered.

    One pass over ``<cache>/margin_detail_daily/*.csv``; rows for symbols
    outside ``symbols`` are dropped during parse to bound memory.  The day
    lists come out sorted because the filenames are session-dated."""
    detail_dir = cache / DETAIL_DIRNAME
    if not detail_dir.is_dir():
        raise OwnMarginStudyError(f"detail_dir_missing:{detail_dir}")
    series: dict[str, tuple[list[str], list[float]]] = {}
    for path in sorted(detail_dir.glob("*.csv")):
        day = path.stem
        with path.open(newline="", encoding="utf-8") as handle:
            import csv

            reader = csv.DictReader(handle)
            for row in reader:
                code = row.get("ts_code")
                if code not in symbols:
                    continue
                raw = row.get("rzye")
                if not raw:
                    continue
                days, values = series.setdefault(code, ([], []))
                days.append(day)
                values.append(float(raw))
    return series


def attach_own_margin_states(
    signals: list[dict[str, object]],
    series: dict[str, tuple[list[str], list[float]]],
    lookback: int = OWN_MARGIN_LOOKBACK_SESSIONS,
) -> dict[str, int]:
    """Annotate each signal with its own-balance bucket, in place.

    Strict prior: ``bisect_left(entry_day) - 1`` is the latest session whose
    balance was published BEFORE the entry opened; the change compares that
    value against ``lookback`` sessions earlier.  Signals without enough
    coverage get ``own_state="insufficient_history"`` and are counted."""
    stats = {
        "missing_series": 0,
        "insufficient_history": 0,
        "attached": 0,
    }
    for signal in signals:
        entry = str(signal["entry_day"])
        book = series.get(str(signal["ts_code"]))
        if book is None:
            signal["own_state"] = "insufficient_history"
            stats["missing_series"] += 1
            continue
        days, values = book
        pos = bisect.bisect_left(days, str(entry))
        if pos - 1 < lookback:
            signal["own_state"] = "insufficient_history"
            stats["insufficient_history"] += 1
            continue
        change = values[pos - 1] / values[pos - 1 - lookback] - 1.0
        signal["own_state"] = classify_margin_state(change)
        signal["own_change"] = change
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

    series = load_symbol_margin_series(
        cache, {str(s["ts_code"]) for s in signals}
    )
    own_stats = attach_own_margin_states(signals, series)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "own_attach_stats": own_stats,
        "universe_uncovered_symbols": uncovered,
    }

    print("## 个股自身融资余额状态研究（research_only，非晋级证据）")
    print(f"- 信号总数 {len(signals)}；覆盖序列 {len(series)} 只；"
          f"附加统计 {own_stats}；成本 {cost_bps}bps 往返")

    labeled = [s for s in signals if s["own_state"] != "insufficient_history"]
    tab = cross_tab(labeled, cost_bps=cost_bps, key="own_state")
    results["signal_level_cross_tab"] = tab
    print("\n### 信号层交叉表（净 bps / 胜率）")
    print(f"{'bucket':<12} {'n':>6} {'mean_net_bps':>13} {'win_rate':>9}")
    for label, cell in tab.items():
        n = cell["n"]
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        print(
            f"{label:<12} {n:>6} "
            f"{'—' if mean_bps is None else f'{float(mean_bps):+.1f}':>13} "
            f"{'—' if win is None else f'{float(win):.3f}':>9}"
        )

    def _portfolio_row(name: str, arm: list[dict[str, object]]) -> None:
        arm_sorted = sorted(arm, key=lambda s: (str(s["entry_day"]), str(s["ts_code"])))
        if len(arm_sorted) < 10:
            results["portfolio"][name] = {"signals": len(arm_sorted)}  # type: ignore[index]
            print(f"{name:<22} {len(arm_sorted):>5}   （样本不足，不跑组合）")
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
            f"{name:<22} {row['closed_positions']:>5} "
            f"{_fmt_pct(float(row['total_net_return'])):>9} "
            f"{_fmt_pct(float(row['monthly_mean'])):>8} "
            f"{_fmt_pct(float(row['monthly_worst'])):>8} "
            f"{_fmt_pct(float(row['max_drawdown'])):>8} "
            f"{row['win_rate']:>6.3f}"
        )

    results["portfolio"] = {}  # type: ignore[assignment]
    print("\n### 组合层（同槽位口径对照）")
    print(f"{'arm':<22} {'closed':>5} {'总净':>9} {'月均净':>8} {'最差月':>8} {'回撤':>8} {'胜率':>6}")
    _portfolio_row("pooled_labeled", labeled)
    for label in ("deleverage", "neutral", "expansion"):
        _portfolio_row(
            f"own_{label}",
            [s for s in signals if s["own_state"] == label],  # type: ignore[arg-type]
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
    except OwnMarginStudyError as exc:
        print(f"OWN_MARGIN_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
