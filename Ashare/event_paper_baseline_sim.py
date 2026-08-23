"""Portfolio-level paper baseline over the lockup sell_off signal stream.

Research-only L4 foundation.  The per-event statistics lane (shadow replay,
strata, margin studies) measures single-event windows; this script turns the
SAME signal definition into an actual portfolio NAV curve — equal-split
sizing, cash constraints, round-trip costs, daily mark-to-market — so the
north-star measuring stick has a measured baseline instead of none.

Conventions inherited unchanged from the existing lane:
  * signal = lockup expiry classified ``sell_off`` by the production shadow
    factor rule (pre window return <= ``SELL_OFF_THRESHOLD``), where the pre
    return ends at the close BEFORE the event session (matching
    ``event_catalyst_shadow``).
  * entry at the event-session close, exit ``POST_HORIZON_SESSIONS`` stock
    sessions later (tracker post-window), adjusted closes throughout.
  * regime label via ``regime_bucket`` from the strata study (10-session
    index return ending AT the event-day close, exact calendar-day match).
  * costs: ``COST_BPS_ROUNDTRIP_DEFAULT`` split half per side on notional.

Cache-only (no network).  Never writes to SampleJournal or any ledger —
this is a research simulator, NOT the capital-backed paper chain.  All
outputs are research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_paper_baseline_sim.py [--cache DIR] [--cost-bps X]
"""

from __future__ import annotations

import argparse
import bisect
import csv
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_catalyst_shadow import SELL_OFF_THRESHOLD  # noqa: E402
from Ashare.event_calendar_lockup_strata import (  # noqa: E402
    COST_BPS_ROUNDTRIP_DEFAULT,
    load_index_series,
    regime_bucket,
)

SIM_START = "20180101"
PRE_WINDOW_SESSIONS = 10  # shadow factor DEFAULT_PRE_WINDOW_SESSIONS
POST_HORIZON_SESSIONS = 5  # tracker lockup post-window
INITIAL_CASH_CNY = 1_000_000.0
MIN_ALLOC_CNY = 5_000.0


class BaselineSimError(RuntimeError):
    """Fail-closed simulation failure with a stable reason code."""


def _parse_day(raw: str):
    return datetime.strptime(raw, "%Y%m%d").date()


def load_events(cache: Path) -> tuple[list[dict[str, object]], dict[str, int]]:
    """Lockup events anchored at float_date since SIM_START.

    Same-(code, float_date) holder rows collapse to the max float_ratio;
    inverted rows (float_date < ann_date) and unparseable ratios are skipped
    and COUNTED."""
    path = cache / "share_float.csv"
    if not path.exists():
        raise BaselineSimError("cache_missing:share_float.csv")
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fields = next(reader)
        code_i = fields.index("ts_code")
        ann_i = fields.index("ann_date")
        float_i = fields.index("float_date")
        ratio_i = fields.index("float_ratio")
        best: dict[tuple[str, str], float] = {}
        skipped_ratio = 0
        skipped_inverted = 0
        for row in reader:
            ann_day = row[ann_i]
            float_day = row[float_i]
            if float_day < SIM_START or float_day < ann_day:
                if float_day < ann_day:
                    skipped_inverted += 1
                continue
            try:
                ratio = float(row[ratio_i])
            except ValueError:
                skipped_ratio += 1
                continue
            key = (row[code_i], float_day)
            best[key] = max(best.get(key, 0.0), ratio)
    events = [
        {"ts_code": code, "float_date": day, "float_ratio": best[(code, day)]}
        for code, day in sorted(best, key=lambda k: (k[1], k[0]))
    ]
    stats = {
        "skipped_bad_ratio_rows": skipped_ratio,
        "skipped_inverted_rows": skipped_inverted,
    }
    if not events:
        raise BaselineSimError("events_empty")
    return events, stats


class StockBook:
    """Ascending adjusted-close series for one symbol."""

    __slots__ = ("days", "closes")

    def __init__(self, days: list[str], closes: list[float]) -> None:
        self.days = days
        self.closes = closes

    def mark(self, day: str) -> float | None:
        pos = bisect.bisect_right(self.days, day) - 1
        return self.closes[pos] if pos >= 0 else None


def refresh_share_float(cache: Path) -> int:
    """Append per-stock share_float history for priced symbols missing from
    the cache (the top-1000 expansion pulled bars but not unlock tables).
    One Tushare call per missing symbol; idempotent — codes already present
    are never re-fetched.  Returns the number of symbols fetched."""
    from Ashare.event_calendar_fetch import call_api

    path = cache / "share_float.csv"
    if not path.exists():
        raise BaselineSimError("cache_missing:share_float.csv")
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fields = next(reader)
        code_i = fields.index("ts_code")
        known_codes = {row[code_i] for row in reader}
    wanted = {
        f"{p.stem.removeprefix('daily_')[:6]}.{p.stem.removeprefix('daily_')[6:]}"
        for p in cache.glob("daily_*.csv")
    }
    missing = sorted(wanted - known_codes)
    if not missing:
        return 0
    fetched_rows: list[list] = []
    for idx, code in enumerate(missing):
        _f, rows = call_api("share_float", {"ts_code": code})
        fetched_rows.extend(rows)
        if (idx + 1) % 50 == 0:
            print(f"refresh_share_float {idx + 1}/{len(missing)}", flush=True)
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        for row in fetched_rows:
            writer.writerow(row)
    print(f"refresh_share_float done: {len(missing)} symbols, "
          f"{len(fetched_rows)} rows appended", flush=True)
    return len(missing)


def load_stock_books(cache: Path) -> tuple[dict[str, StockBook], int]:
    """Cache-only load of every symbol with both daily_ and adjfactor_ CSVs.

    Symbols missing either file are counted as uncovered — never fetched,
    never fatal while any book loads."""
    books: dict[str, StockBook] = {}
    uncovered = 0
    for daily_path in sorted(cache.glob("daily_*.csv")):
        stem = daily_path.stem.removeprefix("daily_")
        ts_code = f"{stem[:6]}.{stem[6:]}"
        adj_path = cache / f"adjfactor_{stem}.csv"
        if not adj_path.exists():
            uncovered += 1
            continue
        factors: dict[str, float] = {}
        with adj_path.open(encoding="utf-8") as handle:
            reader = csv.reader(handle)
            fields = next(reader)
            d_i = fields.index("trade_date")
            f_i = fields.index("adj_factor")
            for row in reader:
                factors[row[d_i]] = float(row[f_i])
        rows: list[tuple[str, float]] = []
        complete = True
        with daily_path.open(encoding="utf-8") as handle:
            reader = csv.reader(handle)
            fields = next(reader)
            d_i = fields.index("trade_date")
            c_i = fields.index("close")
            for row in reader:
                factor = factors.get(row[d_i])
                if factor is None or factor <= 0.0:
                    complete = False
                    break
                close = float(row[c_i])
                if close <= 0.0:
                    complete = False
                    break
                rows.append((row[d_i], close * factor))
        if not complete or not rows:
            uncovered += 1
            continue
        rows.sort(key=lambda item: item[0])
        books[ts_code] = StockBook(
            [d for d, _ in rows], [c for _, c in rows]
        )
    if not books:
        raise BaselineSimError("stock_books_empty")
    return books, uncovered


def build_signals(
    events: list[dict[str, object]],
    books: dict[str, StockBook],
    index_pairs: list[tuple[object, float]],
    last_global_day: str,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    """Classify sell_off signals and pre-compute their entry/exit legs.

    Classification mirrors ``event_catalyst_shadow``: the pre return ends at
    the close BEFORE the event session (pos-1 vs pos-1-PRE_WINDOW on the
    symbol's own ascending grid)."""
    index_days = {d.strftime("%Y%m%d") for d, _ in index_pairs}
    signals: list[dict[str, object]] = []
    skipped_no_cache = 0
    skipped_short_history = 0
    skipped_truncated = 0
    for event in events:
        code = str(event["ts_code"])
        float_day = str(event["float_date"])
        book = books.get(code)
        if book is None:
            skipped_no_cache += 1
            continue
        pos = bisect.bisect_right(book.days, float_day) - 1
        if pos < PRE_WINDOW_SESSIONS + 1:
            skipped_short_history += 1
            continue
        pre = book.closes[pos - 1] / book.closes[pos - 1 - PRE_WINDOW_SESSIONS] - 1.0
        if pre > SELL_OFF_THRESHOLD:
            continue
        j = pos + POST_HORIZON_SESSIONS
        if j >= len(book.days) or book.days[j] > last_global_day:
            skipped_truncated += 1
            continue
        regime = (
            regime_bucket(index_pairs, _parse_day(float_day))
            if float_day in index_days
            else "unknown"
        )
        signals.append(
            {
                "ts_code": code,
                "float_date": float_day,
                "entry_day": book.days[pos],
                "exit_day": book.days[j],
                "entry_price": book.closes[pos],
                "exit_price": book.closes[j],
                "float_ratio": event["float_ratio"],
                "pre_return": pre,
                "regime": regime,
            }
        )
    stats = {
        "skipped_no_cache": skipped_no_cache,
        "skipped_short_history": skipped_short_history,
        "skipped_truncated": skipped_truncated,
    }
    return signals, stats


def rule_arm_filter(signal: dict[str, object]) -> bool:
    """Practice rule from the strata study: weak market AND avoid the 3–5%
    float-ratio band.  Unknown regime or unknown ratio excludes (matches the
    tracker's rule-subset convention)."""
    if signal["regime"] != "weak":
        return False
    ratio = signal["float_ratio"]
    if ratio is None:
        return False
    return not (3.0 <= float(ratio) < 5.0)


def run_portfolio(
    arm_signals: list[dict[str, object]],
    global_days: list[str],
    books: dict[str, StockBook],
    initial_cash: float = INITIAL_CASH_CNY,
    cost_bps: float = COST_BPS_ROUNDTRIP_DEFAULT,
) -> dict[str, object]:
    """Equal-split paper portfolio: exits first each day, then same-day
    entries split the available cash evenly, then mark-to-market."""
    cost_rate = (cost_bps / 2.0) / 1e4
    global_pos = {d: i for i, d in enumerate(global_days)}
    entries: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
    for signal in arm_signals:
        day = str(signal["entry_day"])
        if day not in global_pos:
            i = bisect.bisect_right(global_days, day)
            if i >= len(global_days):
                continue
            day = global_days[i]
        entries[day].append(signal)

    def _roll_exit(day: str) -> str | None:
        if day in global_pos:
            return day
        i = bisect.bisect_right(global_days, day)
        return global_days[i] if i < len(global_days) else None

    cash = initial_cash
    positions: list[dict[str, object]] = []
    nav: list[tuple[str, float]] = []
    trades: list[float] = []
    skipped_no_cash = 0
    for day in global_days:
        surviving: list[dict[str, object]] = []
        for posn in positions:
            if posn["exit_day"] == day:
                gross = float(posn["shares"]) * float(posn["exit_price"])
                fee = gross * cost_rate
                cash += gross - fee
                trades.append(gross - fee - float(posn["spend"]))
            else:
                surviving.append(posn)
        positions = surviving
        batch = entries.get(day)
        if batch:
            alloc = cash / len(batch)
            for signal in batch:
                spend = min(alloc, cash)
                if spend < MIN_ALLOC_CNY or spend <= 0.0:
                    skipped_no_cash += 1
                    continue
                fee = spend * cost_rate
                shares = (spend - fee) / float(signal["entry_price"])
                cash -= spend
                exit_day = _roll_exit(str(signal["exit_day"]))
                if exit_day is None:
                    # Exit beyond the sim span cannot happen (build_signals
                    # guards it); fail closed rather than leak a ghost leg.
                    raise BaselineSimError("exit_day_out_of_span")
                positions.append(
                    {
                        "code": signal["ts_code"],
                        "shares": shares,
                        "spend": spend,
                        "exit_day": exit_day,
                        "exit_price": signal["exit_price"],
                    }
                )
        equity = cash + sum(
            float(posn["shares"]) * (_books_mark(books, str(posn["code"]), day) or 0.0)
            for posn in positions
        )
        nav.append((day, equity))
    win_rate = (
        sum(1 for pnl in trades if pnl > 0.0) / len(trades) if trades else 0.0
    )
    return {
        "nav": nav,
        "trades": trades,
        "entries_attempted": len(arm_signals),
        "skipped_no_cash": skipped_no_cash,
        "closed_positions": len(trades),
        "win_rate": win_rate,
    }


def _books_mark(books: dict[str, StockBook], code: str, day: str) -> float | None:
    book = books.get(code)
    return book.mark(day) if book is not None else None


def monthly_net_returns(
    nav: list[tuple[str, float]], base: float | None = None
) -> list[tuple[str, float]]:
    """Calendar-month net returns from a NAV series.  The first month is
    measured against `base` (the starting capital) when given, else against
    the first NAV point."""
    if not nav:
        return []
    month_end: dict[str, float] = {}
    order: list[str] = []
    for day, value in nav:
        month = day[:6]
        if month not in month_end:
            order.append(month)
        month_end[month] = value
    out: list[tuple[str, float]] = []
    prev = base if base is not None else nav[0][1]
    for month in order:
        value = month_end[month]
        out.append((month, value / prev - 1.0))
        prev = value
    return out


def max_drawdown(nav: list[tuple[str, float]]) -> float:
    peak = float("-inf")
    worst = 0.0
    for _, value in nav:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def benchmark_return(index_pairs: list[tuple[object, float]], first_day: str, last_day: str) -> float | None:
    days = [d.strftime("%Y%m%d") for d, _ in index_pairs]
    lo = bisect.bisect_right(days, first_day) - 1
    hi = bisect.bisect_right(days, last_day) - 1
    if lo < 0 or hi < 0 or hi <= lo:
        return None
    return index_pairs[hi][1] / index_pairs[lo][1] - 1.0


def _fmt_pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def run_study(cache: Path, cost_bps: float = COST_BPS_ROUNDTRIP_DEFAULT) -> dict[str, object]:
    index_pairs = load_index_series(cache)
    global_days = [d.strftime("%Y%m%d") for d, _ in index_pairs if d.strftime("%Y%m%d") >= SIM_START]
    if len(global_days) < PRE_WINDOW_SESSIONS + POST_HORIZON_SESSIONS + 2:
        raise BaselineSimError("index_history_too_short")

    events, event_stats = load_events(cache)
    books, uncovered = load_stock_books(cache)
    signals, signal_stats = build_signals(events, books, index_pairs, global_days[-1])
    if not signals:
        raise BaselineSimError("signals_empty")

    arms = {
        "all": signals,
        "rule": [s for s in signals if rule_arm_filter(s)],
    }
    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "initial_cash_cny": INITIAL_CASH_CNY,
        "cost_bps_roundtrip": cost_bps,
        "universe_books": len(books),
        "universe_uncovered_symbols": uncovered,
        "events_total": len(events),
        "signal_stats": {**event_stats, **signal_stats},
        "arms": {},
    }
    for name, arm_signals in arms.items():
        run = run_portfolio(arm_signals, global_days, books, cost_bps=cost_bps)
        nav: list[tuple[str, float]] = run["nav"]  # type: ignore[assignment]
        months = monthly_net_returns(nav, base=INITIAL_CASH_CNY)
        monthly_vals = [r for _, r in months]
        path = cache / f"paper_baseline_nav_{name}.csv"
        bench = (
            benchmark_return(index_pairs, nav[0][0], nav[-1][0]) if nav else None
        )
        results["arms"][name] = {  # type: ignore[index]
            "signals": len(arm_signals),
            "entries_attempted": run["entries_attempted"],
            "skipped_no_cash": run["skipped_no_cash"],
            "closed_positions": run["closed_positions"],
            "win_rate": run["win_rate"],
            "total_net_return": nav[-1][1] / INITIAL_CASH_CNY - 1.0 if nav else 0.0,
            "max_drawdown": max_drawdown(nav),
            "monthly_count": len(months),
            "monthly_mean": sum(monthly_vals) / len(monthly_vals) if monthly_vals else 0.0,
            "monthly_median": sorted(monthly_vals)[len(monthly_vals) // 2] if monthly_vals else 0.0,
            "monthly_worst": min(monthly_vals) if monthly_vals else 0.0,
            "benchmark_total_return": bench,
            "monthly": months,
            "nav_path": str(path),
        }
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["trade_date", "equity_cny", "research_only"])
            writer.writerows((d, f"{v:.2f}", "not_promotion_evidence") for d, v in nav)
    _render(results)
    return results


def _render(results: dict[str, object]) -> None:
    print("## 解禁 sell_off 信号组合级纸面基线（research_only，非晋级证据）")
    print(
        f"- 覆盖：{results['universe_books']} 只有缓存个股 / {results['events_total']} 个事件；"
        f"跳过统计 {results['signal_stats']}"
    )
    for name in ("all", "rule"):
        arm = results["arms"][name]  # type: ignore[index]
        assert isinstance(arm, dict)
        print(f"- [{name}] 信号 n={arm['signals']} 入场尝试={arm['entries_attempted']} "
              f"现金不足跳过={arm['skipped_no_cash']} 平仓={arm['closed_positions']}")
        print(f"    总净收益={_fmt_pct(float(arm['total_net_return']))}  "
              f"月均净={_fmt_pct(float(arm['monthly_mean']))}  "
              f"月中位={_fmt_pct(float(arm['monthly_median']))}  "
              f"最差月={_fmt_pct(float(arm['monthly_worst']))}  "
              f"最大回撤={_fmt_pct(float(arm['max_drawdown']))}  "
              f"笔胜率={arm['win_rate']:.3f}  "
              f"同期上证={_fmt_pct(float(arm['benchmark_total_return'])) if arm['benchmark_total_return'] is not None else 'n/a'}")
        monthly = arm["monthly"]
        assert isinstance(monthly, list)
        if monthly:
            line = "    月度净收益: " + " ".join(
                f"{m}:{r * 100:+.1f}%" for m, r in monthly
            )
            print(line)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--cost-bps", type=float, default=COST_BPS_ROUNDTRIP_DEFAULT)
    parser.add_argument(
        "--refresh-share-float",
        action="store_true",
        help="Fetch per-stock unlock tables for priced symbols missing from "
        "the cache before simulating (network; needs TUSHARE token env).",
    )
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    if args.refresh_share_float:
        refresh_share_float(cache)
    run_study(cache, cost_bps=args.cost_bps)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaselineSimError as exc:
        print(f"PAPER_BASELINE_SIM_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
