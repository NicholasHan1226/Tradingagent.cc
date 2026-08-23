"""Stock-level pre-lockup leverage-abnormality study (融资融券×解禁窗口).

Research-only.  For every cached lockup event (ann_date >= 2016) of the
mainboard sample universe, measures whether the STOCK's own margin balance
moved abnormally in the 20 sessions before the announcement (stock minus
market aggregate), and whether that abnormality sorts post-announcement
repair returns.  Hypothesis b2 of the margin lane: deleveraging insiders /
leverage flight may precede lockup expiries and explain why weak-market
repairs dominate.

Data sources are all local scratch caches produced by ``event_calendar_fetch``
(share_float, daily, adj_factor) plus ``event_margin_flow_research``
(margin_aggregate) and per-symbol ``margin_detail`` pulls made on demand.
All outputs are research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_margin_lockup_window.py [--max-symbols N]
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_calendar_fetch import CACHE_DIR, FetchError, call_api  # noqa: E402
from Ashare.event_calendar_lockup_strata import (  # noqa: E402
    COST_BPS_ROUNDTRIP_DEFAULT,
    REGIME_BINS,
    load_index_series,
)

STUDY_START = "20160101"
DETAIL_DIRNAME = "margin_detail"
PRE_SESSIONS = 20
POST_HORIZON_SESSIONS = 10


class MarginWindowError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


def load_events(cache: Path) -> tuple[list[dict[str, str]], int]:
    """Lockup events since STUDY_START; same-day holder rows collapse to the
    max float_ratio (same convention as the strata study).  Rows with an
    unparseable ratio are skipped and COUNTED — never silently dropped and
    never fatal while any valid event remains."""
    path = cache / "share_float.csv"
    if not path.exists():
        raise MarginWindowError("cache_missing:share_float.csv")
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fields = next(reader)
        code_i = fields.index("ts_code")
        date_i = fields.index("ann_date")
        ratio_i = fields.index("float_ratio")
        best: dict[tuple[str, str], float] = {}
        order: dict[tuple[str, str], int] = {}
        seq = 0
        skipped_ratio = 0
        for row in reader:
            day = row[date_i]
            if day < STUDY_START:
                continue
            key = (row[code_i], day)
            try:
                ratio = float(row[ratio_i])
            except ValueError:
                skipped_ratio += 1
                continue
            best[key] = max(best.get(key, 0.0), ratio)
            if key not in order:
                order[key] = seq
                seq += 1
    events = [
        {"ts_code": code, "ann_date": day, "float_ratio": best[(code, day)]}
        for (code, day), _ in sorted(order.items(), key=lambda kv: kv[1])
    ]
    if not events:
        raise MarginWindowError("events_empty")
    return events, skipped_ratio


def load_stock_margin(cache: Path, ts_code: str) -> list[tuple[str, float]]:
    """Per-symbol margin balance series (fetch-if-missing, year-sliced)."""
    safe = ts_code.replace(".", "")
    sub = cache / DETAIL_DIRNAME
    path = sub / f"margin_detail_{safe}.csv"
    if not path.exists():
        sub.mkdir(parents=True, exist_ok=True)
        fields_all: list[str] | None = None
        rows_all: list[list] = []
        for year in range(int(STUDY_START[:4]), int(MARGIN_END_YEAR) + 1):
            slice_start = max(STUDY_START, f"{year}0101")
            slice_end = min(MARGIN_END, f"{year}1231")
            fields, rows = call_api(
                "margin_detail",
                {
                    "ts_code": ts_code,
                    "start_date": slice_start,
                    "end_date": slice_end,
                },
            )
            if fields_all is None:
                fields_all = fields
            elif fields != fields_all:
                raise MarginWindowError(f"schema_drift:{ts_code}")
            rows_all.extend(rows)
        if fields_all is None:
            raise MarginWindowError(f"detail_empty:{ts_code}")
        date_i = fields_all.index("trade_date")
        rzye_i = fields_all.index("rzye")
        rows_all.sort(key=lambda r: r[date_i])
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(fields_all)
            writer.writerows(rows_all)
    series: list[tuple[str, float]] = []
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fields = next(reader)
        date_i = fields.index("trade_date")
        rzye_i = fields.index("rzye")
        for row in reader:
            value = row[rzye_i]
            series.append((row[date_i], float(value) if value else 0.0))
    return series


MARGIN_END = "20260821"
MARGIN_END_YEAR = MARGIN_END[:4]


def _session_pos(series_days: list[str], day: str) -> int | None:
    import bisect

    pos = bisect.bisect_right(series_days, day) - 1
    return pos if pos >= 0 else None


def pre_window_change(
    series: list[tuple[str, float]], anchor_day: str, lag: int = PRE_SESSIONS
) -> float | None:
    """Margin-balance change over `lag` sessions ending at the last session
    ON/BEFORE anchor_day; None when history is insufficient."""
    days = [d for d, _ in series]
    pos = _session_pos(days, anchor_day)
    if pos is None or pos < lag:
        return None
    base = series[pos - lag][1]
    level = series[pos][1]
    if base <= 0.0:
        return None
    return level / base - 1.0


def _ensure_price_caches(cache: Path, ts_code: str) -> None:
    """Fetch-if-missing the symbol's daily bars and adjustment factors so the
    study covers sample names outside the original calendar-fetch universe."""
    safe = ts_code.replace(".", "")
    needed = [(f"daily_{safe}.csv", "daily", "close"),
              (f"adjfactor_{safe}.csv", "adj_factor", "adj_factor")]
    missing = [item for item in needed if not (cache / item[0]).exists()]
    if not missing:
        return
    for filename, api, _label in missing:
        fields, rows = call_api(
            api,
            {
                "ts_code": ts_code,
                "start_date": PRICE_HISTORY_START,
                "end_date": MARGIN_END,
            },
        )
        if not rows:
            raise MarginWindowError(f"price_fetch_empty:{api}:{ts_code}")
        _write_cache_csv(cache / filename, fields, rows)


PRICE_HISTORY_START = "20150101"


def _write_cache_csv(path: Path, fields: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)


def forward_return(
    cache: Path, ts_code: str, anchor_day: str, horizon: int = POST_HORIZON_SESSIONS
) -> tuple[float | None, float | None]:
    """Adjusted fwd return after anchor_day plus the SSE-excess version.

    Session 0 is the LAST session on/before ann_date (position opens at that
    close); the return runs close[pos] -> close[pos+horizon]."""
    safe = ts_code.replace(".", "")
    daily_path = cache / f"daily_{safe}.csv"
    adj_path = cache / f"adjfactor_{safe}.csv"
    if not daily_path.exists() or not adj_path.exists():
        return None, None
    closes: list[tuple[str, float]] = []
    with daily_path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fields = next(reader)
        d_i = fields.index("trade_date")
        c_i = fields.index("close")
        closes = [(r[d_i], float(r[c_i])) for r in reader]
    # Tushare daily CSVs arrive newest-first; the bisect below needs ascending.
    closes.sort(key=lambda item: item[0])
    factors: dict[str, float] = {}
    with adj_path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fields = next(reader)
        d_i = fields.index("trade_date")
        f_i = fields.index("adj_factor")
        factors = {r[d_i]: float(r[f_i]) for r in reader}
    days = [d for d, _ in closes]
    import bisect

    pos = bisect.bisect_right(days, anchor_day) - 1
    if pos < 0 or pos + horizon >= len(closes):
        return None, None
    start_day, start_close = closes[pos]
    end_close = closes[pos + horizon][1]
    start_adj = factors.get(start_day)
    end_adj = factors.get(closes[pos + horizon][0])
    if not start_close or not end_close:
        return None, None
    raw = end_close / start_close - 1.0
    if start_adj and end_adj and start_adj > 0:
        raw = (end_close * end_adj) / (start_close * start_adj) - 1.0
    return raw, None  # excess filled by caller against the index map


def excess_against(raw: float | None, index_fwd: float | None) -> float | None:
    if raw is None or index_fwd is None:
        return None
    return raw - index_fwd


def regime_label(index_pairs: list[tuple[object, float]], day: str) -> str:
    from datetime import datetime

    target = datetime.strptime(day, "%Y%m%d").date()
    days = [d for d, _ in index_pairs]
    import bisect

    pos = bisect.bisect_right(days, target) - 1
    if pos < PRE_SESSIONS:
        return "unknown"
    ret = index_pairs[pos][1] / index_pairs[pos - PRE_SESSIONS][1] - 1.0
    for low, high, label in REGIME_BINS:
        if low <= ret < high:
            return label
    return "unknown"


def tercile_table(
    triples: list[tuple[float, float]], buckets: int = 3
) -> list[dict[str, object]]:
    """Mean net fwd-excess and win rate by signal tercile (ascending)."""
    pairs = sorted(triples, key=lambda t: t[0])
    n = len(pairs)
    out: list[dict[str, object]] = []
    cost = COST_BPS_ROUNDTRIP_DEFAULT / 1e4
    if n < buckets:
        return out
    for b in range(buckets):
        start = (n * b) // buckets
        end = (n * (b + 1)) // buckets
        chunk = [fwd for _sig, fwd in pairs[start:end]]
        if not chunk:
            continue
        nets = [f - cost for f in chunk]
        out.append(
            {
                "bucket": b + 1,
                "n": len(chunk),
                "mean_excess_gross": sum(chunk) / len(chunk),
                "mean_excess_net": sum(nets) / len(nets),
                "win_net": sum(1 for v in nets if v > 0.0) / len(nets),
            }
        )
    return out


def run_study(cache: Path = CACHE_DIR, max_symbols: int | None = None) -> dict[str, object]:
    events, skipped_ratio_rows = load_events(cache)
    symbols = sorted({e["ts_code"] for e in events})
    if max_symbols is not None:
        symbols = symbols[:max_symbols]
        events = [e for e in events if e["ts_code"] in set(symbols)]

    # Market-side series once.
    agg_path = cache / "margin_aggregate.csv"
    if not agg_path.exists():
        raise MarginWindowError("cache_missing:margin_aggregate.csv")
    market_series: list[tuple[str, float]] = []
    with agg_path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fields = next(reader)
        d_i = fields.index("trade_date")
        y_i = fields.index("rzye")
        totals: defaultdict[str, float] = defaultdict(float)
        for row in reader:
            totals[row[d_i]] += float(row[y_i]) if row[y_i] else 0.0
        market_series = sorted(totals.items())

    index_pairs = load_index_series(cache)
    ordered_days = [d.strftime("%Y%m%d") for d, _ in index_pairs]
    closes_by_day = {d.strftime("%Y%m%d"): c for d, c in index_pairs}
    fwd_map: dict[str, float] = {}
    for i, day in enumerate(ordered_days):
        j = i + POST_HORIZON_SESSIONS
        if j < len(ordered_days):
            base = closes_by_day[ordered_days[i]]
            fwd_map[day] = closes_by_day[ordered_days[j]] / base - 1.0

    def _fwd_for(day: str) -> float | None:
        """Forward return from the last index session ON/BEFORE ann_date
        (non-trading announcement dates roll back, matching the tracker's
        last-completed-session convention)."""
        import bisect

        pos = bisect.bisect_right(ordered_days, day) - 1
        if pos < 0:
            return None
        return fwd_map.get(ordered_days[pos])

    observations: list[dict[str, object]] = []
    skipped_no_margin = 0
    for idx, code in enumerate(symbols):
        try:
            series = load_stock_margin(cache, code)
            _ensure_price_caches(cache, code)
        except FetchError as exc:
            raise MarginWindowError(f"detail_fetch_failed:{code}:{exc}") from exc
        if not any(v > 0.0 for _, v in series):
            skipped_no_margin += 1
            continue
        for event in events:
            if event["ts_code"] != code:
                continue
            day = event["ann_date"]
            stock_chg = pre_window_change(series, day)
            mkt_chg = pre_window_change(market_series, day)
            if stock_chg is None or mkt_chg is None:
                continue
            raw, _ = forward_return(cache, code, day)
            exc_ret = excess_against(raw, _fwd_for(day))
            if exc_ret is None:
                continue
            observations.append(
                {
                    "ts_code": code,
                    "ann_date": day,
                    "float_ratio": event["float_ratio"],
                    "pre_abnormal": stock_chg - mkt_chg,
                    "fwd_excess": exc_ret,
                    "regime": regime_label(index_pairs, day),
                }
            )
        if (idx + 1) % 25 == 0:
            print(f"progress {idx + 1}/{len(symbols)} observations={len(observations)}")

    usable = [
        (obs["pre_abnormal"], obs["fwd_excess"])  # type: ignore[misc]
        for obs in observations
        if obs["regime"] in ("weak", "sideways", "strong")
    ]
    summary: dict[str, object] = {
        "research_only": True,
        "symbols": len(symbols),
        "events_total": len(events),
        "observations": len(observations),
        "skipped_no_margin_symbols": skipped_no_margin,
        "skipped_bad_ratio_rows": skipped_ratio_rows,
        "tercile_all": tercile_table([(a, b) for a, b in usable]),  # type: ignore[misc]
        "tercile_weak": tercile_table(
            [(o["pre_abnormal"], o["fwd_excess"])  # type: ignore[misc]
             for o in observations if o["regime"] == "weak"]
        ),
    }
    _render(summary)
    return summary


def _fmt_pct(value: object) -> str:
    return f"{float(value) * 100:+.2f}%"


def _render(summary: dict[str, object]) -> None:
    print("## 解禁前个股杠杆异动研究（research_only，报告读数非晋级证据）")
    print(f"- 覆盖：{summary['symbols']} 只样本股 / {summary['events_total']} 个事件；"
          f"可配对观察 {summary['observations']} 条"
          f"（无两融数据跳过 {summary['skipped_no_margin_symbols']} 只；"
          f"比例字段不可解析跳过行 {summary['skipped_bad_ratio_rows']}）")
    for tag in ("tercile_all", "tercile_weak"):
        table = summary[tag]
        assert isinstance(table, list)
        print(f"- 解禁前20日异常杠杆变化三分位 → 后10日超额收益 [{tag}]:")
        for row in table:
            assert isinstance(row, dict)
            print(f"    T{row['bucket']} n={row['n']:<4} "
                  f"net_excess={_fmt_pct(row['mean_excess_net'])} "
                  f"win_net={row['win_net']:.3f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-symbols", type=int, default=None)
    args = parser.parse_args()
    run_study(max_symbols=args.max_symbols)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (MarginWindowError, FetchError) as exc:
        print(f"MARGIN_WINDOW_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
