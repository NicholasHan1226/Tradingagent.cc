"""Multi-family paper baseline: lockup rule arm + earnings_neg relief.

Extends the #423 portfolio baseline to a second signal family so the
measuring stick sees DIVERSIFICATION, not just the single strongest lane.
Same engine (``run_portfolio``), same locked slot convention, same costs —
only the signal streams differ:

  * ``lockup``  — the #423 sell_off stream filtered by the practice rule
    (weak market x avoid the 3–5% float band), rebuilt unchanged;
  * ``earnings_neg`` — formal disclosures whose earliest prior forecast is
    negative (预减 family), the 利空出尽 relief structure (+85bps gross
    post-5d in the 2026-08-23 study).  Anchor = the study's ``pre_date``
    convention; entry rolls FORWARD to the first session on/after the
    anchor (weekend announcements must not enter at Friday's close —
    that would be lookahead), exit five sessions later;
  * ``combined`` — both streams merged, one cash pool, first look at
    whether the families' drawdowns actually miss each other.

Cache-only (no network).  Never writes to SampleJournal or any ledger.
research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_multifamily_baseline.py [--cache DIR] [--cost-bps X]
"""

from __future__ import annotations

import argparse
import bisect
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_calendar_earnings_groups import (  # noqa: E402
    load_disclosure_events,
    load_forecast_directions,
)
from Ashare.event_calendar_lockup_strata import (  # noqa: E402
    COST_BPS_ROUNDTRIP_DEFAULT,
    load_index_series,
    regime_bucket,
)
from Ashare.event_paper_baseline_sim import (  # noqa: E402
    INITIAL_CASH_CNY,
    POST_HORIZON_SESSIONS,
    SIM_START,
    _parse_day,
    build_signals,
    load_events,
    load_stock_books,
    max_drawdown,
    monthly_net_returns,
    rule_arm_filter,
    run_portfolio,
)


class MultifamilyBaselineError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


def load_negative_disclosure_events(
    cache: Path,
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """(symbol, pre_date) disclosure events with negative prior forecasts.

    Sample set = every priced symbol in the cache (ts_code reconstructed
    from the daily_ file stems)."""
    ts_codes = {
        f"{p.stem.removeprefix('daily_')[:6]}."
        f"{p.stem.removeprefix('daily_')[6:]}"
        for p in cache.glob("daily_*.csv")
    }
    forecasts = load_forecast_directions(cache)
    buckets, counts = load_disclosure_events(cache, ts_codes, forecasts)
    events = sorted(set(buckets.get("forecast_negative", [])))
    if not events:
        raise MultifamilyBaselineError("negative_disclosures_empty")
    return events, counts


def build_disclosure_signals(
    events: list[tuple[str, str]],
    books: dict,
    index_pairs: list[tuple[object, float]],
    last_global_day: str,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    """Relief signals from disclosure anchors; entry rolls FORWARD.

    The announcement may land on a non-session day; entering at the last
    session BEFORE it would trade on information that does not exist yet,
    so the entry is the first session ON/AFTER the anchor."""
    index_days = {d.strftime("%Y%m%d") for d, _ in index_pairs}
    signals: list[dict[str, object]] = []
    skipped_no_cache = 0
    skipped_truncated = 0
    for code, anchor in events:
        book = books.get(code)
        if book is None:
            skipped_no_cache += 1
            continue
        pos = bisect.bisect_right(book.days, anchor)
        if pos >= len(book.days):
            skipped_truncated += 1
            continue
        j = pos + POST_HORIZON_SESSIONS
        if j >= len(book.days) or book.days[j] > last_global_day:
            skipped_truncated += 1
            continue
        entry_day = book.days[pos]
        regime = (
            regime_bucket(index_pairs, _parse_day(entry_day))
            if entry_day in index_days
            else "unknown"
        )
        signals.append(
            {
                "ts_code": code,
                "anchor": anchor,
                "entry_day": entry_day,
                "exit_day": book.days[j],
                "entry_price": book.closes[pos],
                "exit_price": book.closes[j],
                "float_ratio": None,
                "pre_return": None,
                "regime": regime,
            }
        )
    stats = {
        "skipped_no_cache": skipped_no_cache,
        "skipped_truncated": skipped_truncated,
    }
    return signals, stats


def _arm_row(name: str, signals: list[dict[str, object]], run: dict[str, object],
             nav: list[tuple[str, float]]) -> dict[str, object]:
    months = monthly_net_returns(nav, base=INITIAL_CASH_CNY)
    monthly_vals = [r for _, r in months]
    return {
        "arm": name,
        "signals": len(signals),
        "closed_positions": run["closed_positions"],
        "win_rate": run["win_rate"],
        "total_net_return": nav[-1][1] / INITIAL_CASH_CNY - 1.0 if nav else 0.0,
        "max_drawdown": max_drawdown(nav),
        "monthly_mean": sum(monthly_vals) / len(monthly_vals) if monthly_vals else 0.0,
        "monthly_worst": min(monthly_vals) if monthly_vals else 0.0,
    }


def _fmt_pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def run_study(cache: Path, cost_bps: float = COST_BPS_ROUNDTRIP_DEFAULT) -> dict[str, object]:
    index_pairs = load_index_series(cache)
    global_days = [
        d.strftime("%Y%m%d")
        for d, _ in index_pairs
        if d.strftime("%Y%m%d") >= SIM_START
    ]
    if len(global_days) < POST_HORIZON_SESSIONS * 2 + 2:
        raise MultifamilyBaselineError("index_history_too_short")

    events_lockup, _stats_lockup = load_events(cache)
    books, uncovered = load_stock_books(cache)
    lockup_signals, _sig_stats = build_signals(
        events_lockup, books, index_pairs, global_days[-1]
    )
    neg_events, neg_counts = load_negative_disclosure_events(cache)
    neg_signals, neg_stats = build_disclosure_signals(
        neg_events, books, index_pairs, global_days[-1]
    )

    arms: dict[str, list[dict[str, object]]] = {
        "lockup_rule": [s for s in lockup_signals if rule_arm_filter(s)],
        "earnings_neg_all": neg_signals,
        # Descriptive exploratory arm: does the weak-market conditioning that
        # carries the lockup lane transfer to the relief structure?
        "earnings_neg_weak": [
            s for s in neg_signals if s["regime"] == "weak"
        ],
        "combined": [
            s for s in lockup_signals if rule_arm_filter(s)
        ] + neg_signals,
    }

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "universe_books": len(books),
        "universe_uncovered_symbols": uncovered,
        "earnings_neg_counts": neg_counts,
        "arms": {},
    }
    print("## 多信号族组合基线（research_only，非晋级证据）")
    print(f"- 覆盖：{len(books)} 只个股；负向披露事件 n={len(neg_events)} "
          f"分组计数 {neg_counts}；成本 {cost_bps}bps 往返")
    header = (f"{'arm':<17} {'closed':>6} {'总净':>9} {'月均净':>8} "
              f"{'最差月':>8} {'回撤':>8} {'胜率':>6}")
    print(header)
    rows_out: list[dict[str, object]] = []
    for name, arm in arms.items():
        arm_sorted = sorted(
            arm, key=lambda s: (str(s["entry_day"]), str(s["ts_code"]))
        )
        if not arm_sorted:
            results["arms"][name] = {"signals": 0}  # type: ignore[index]
            continue
        run = run_portfolio(arm_sorted, global_days, books, cost_bps=cost_bps)
        nav: list[tuple[str, float]] = run["nav"]  # type: ignore[assignment]
        row = _arm_row(name, arm_sorted, run, nav)
        rows_out.append(row)
        results["arms"][name] = row  # type: ignore[index]
        print(
            f"{name:<17} {row['closed_positions']:>6} "
            f"{_fmt_pct(float(row['total_net_return'])):>9} "
            f"{_fmt_pct(float(row['monthly_mean'])):>8} "
            f"{_fmt_pct(float(row['monthly_worst'])):>8} "
            f"{_fmt_pct(float(row['max_drawdown'])):>8} "
            f"{row['win_rate']:>6.3f}"
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
    except MultifamilyBaselineError as exc:
        print(f"MULTIFAMILY_BASELINE_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
