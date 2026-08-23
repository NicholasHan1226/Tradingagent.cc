"""Offline margin-flow (融资融券) regime study for the A-share event program.

Research-only.  Pulls the exchange-aggregate margin series from the Tushare
pro HTTP API into the shared scratch cache and measures one pre-registered
hypothesis pair:

  (a) does leverage contraction coincide with the tracker's ``weak`` market
      phase (mechanism evidence behind the only cost-surviving stratum)?
  (b) does margin momentum carry forward-return information at all
      (candidate timing factor, report-only)?

Interface facts verified 2026-08-24 against the live API: range queries are
supported and return ~3 rows/day (SSE/SZSE/BSE) while ``trade_date`` mode
returns fewer rows, so ingestion MUST use range pagination and dedupe on
(trade_date, exchange_id).  This module is NOT a runtime collector, claims no
TradingDatas authority, and its outputs feed only offline statistics.
All outputs are research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_margin_flow_research.py [--refresh]
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_calendar_fetch import (  # noqa: E402
    CACHE_DIR,
    FetchError,
    fetch_ranged,
)
from Ashare.event_calendar_lockup_strata import (  # noqa: E402
    REGIME_BINS,
    load_index_series,
)

MARGIN_START = "20100401"
MARGIN_END = "20260821"
AGGREGATE_NAME = "margin_aggregate"
FEATURE_LAG_SESSIONS = 20
SHORT_LAG_SESSIONS = 5
FORWARD_HORIZON_SESSIONS = 10
QUANTILE_BUCKETS = 5
# 2010-2015 contains the leverage bubble/bust; structural reads use both the
# full sample and a post-2016 subsample so the pollution is visible, not hidden.
POST_SUBSAMPLE_START = "20160101"


class MarginStudyError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


def dedupe_margin_rows(
    fields: list[str], rows: list[list]
) -> tuple[list[str], list[list], int]:
    """Deduplicate range-paged rows on (trade_date, exchange_id)."""
    date_i = fields.index("trade_date")
    exch_i = fields.index("exchange_id")
    seen: set[tuple[str, str]] = set()
    kept: list[list] = []
    duplicates = 0
    for row in rows:
        key = (row[date_i], row[exch_i])
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        kept.append(row)
    kept.sort(key=lambda r: (r[date_i], r[exch_i]))
    return fields, kept, duplicates


def _as_float(value: object, context: str) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise MarginStudyError(f"numeric_parse_failed:{context}") from exc


def daily_totals(fields: list[str], rows: list[list]) -> list[tuple[str, float, float]]:
    """Collapse exchange rows into per-date (date, rzye_total, rzrqye_total)."""
    date_i = fields.index("trade_date")
    rzye_i = fields.index("rzye")
    rzrqye_i = fields.index("rzrqye")
    totals: dict[str, list[float]] = {}
    for row in rows:
        day = row[date_i]
        bucket = totals.setdefault(day, [0.0, 0.0])
        bucket[0] += _as_float(row[rzye_i], f"rzye:{day}")
        bucket[1] += _as_float(row[rzrqye_i], f"rzrqye:{day}")
    return [(day, values[0], values[1]) for day, values in sorted(totals.items())]


def margin_features(
    totals: list[tuple[str, float, float]],
) -> list[tuple[str, float, float]]:
    """Per date: (date, short-lag change, long-lag change) of aggregate rzye.

    Changes are session-based (the margin series trades on trading days), and
    dates before the longest lag have no feature rather than a truncated one.
    """
    out: list[tuple[str, float, float]] = []
    for pos in range(len(totals)):
        day, rzye, _rzrqye = totals[pos]
        if pos < FEATURE_LAG_SESSIONS:
            continue
        long_base = totals[pos - FEATURE_LAG_SESSIONS][1]
        if long_base <= 0.0:
            raise MarginStudyError(f"rzye_nonpositive_base:{day}")
        long_chg = rzye / long_base - 1.0
        if pos >= SHORT_LAG_SESSIONS:
            short_base = totals[pos - SHORT_LAG_SESSIONS][1]
            short_chg = rzye / short_base - 1.0 if short_base > 0.0 else float("nan")
        else:
            short_chg = float("nan")
        out.append((day, short_chg, long_chg))
    return out


def forward_return_map(
    index_pairs: list[tuple[object, float]], horizon: int = FORWARD_HORIZON_SESSIONS
) -> dict[object, float]:
    """Forward N-session index close-to-close return keyed by session date."""
    out: dict[object, float] = {}
    for pos in range(len(index_pairs) - horizon):
        base = index_pairs[pos][1]
        if base <= 0.0:
            continue
        out[index_pairs[pos][0]] = index_pairs[pos + horizon][1] / base - 1.0
    return out


def regime_by_day(index_pairs: list[tuple[object, float]]) -> dict[object, str]:
    """Same 10-session binning as the tracker's make_regime_lookup."""
    days = [d for d, _ in index_pairs]
    out: dict[object, str] = {}
    for pos, (day, close) in enumerate(index_pairs):
        if pos < FEATURE_LAG_SESSIONS or close <= 0.0:
            continue
        ret = close / index_pairs[pos - FEATURE_LAG_SESSIONS][1] - 1.0
        label = "unknown"
        for low, high, candidate in REGIME_BINS:
            if low <= ret < high:
                label = candidate
                break
        out[day] = label
    del days
    return out


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n != len(ys) or n < 3:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0.0 or var_y <= 0.0:
        return None
    return cov / (var_x**0.5 * var_y**0.5)


def quantile_spread(
    pairs: list[tuple[float, float]], buckets: int = QUANTILE_BUCKETS
) -> dict[str, object]:
    """Mean forward return by signal quintile plus the top-minus-bottom spread.

    Pairs must already be sorted ascending by the signal value; ties keep
    deterministic order so bucket sizes differ by at most one.
    """
    n = len(pairs)
    if n < buckets:
        return {"n": n, "buckets": [], "spread": None}
    rows: list[dict[str, object]] = []
    for b in range(buckets):
        start = (n * b) // buckets
        end = (n * (b + 1)) // buckets
        chunk = pairs[start:end]
        fwds = [fwd for _sig, fwd in chunk]
        rows.append(
            {
                "bucket": b + 1,
                "n": len(chunk),
                "mean_fwd": sum(fwds) / len(fwds),
                "win": sum(1 for f in fwds if f > 0.0) / len(fwds),
            }
        )
    spread = float(rows[-1]["mean_fwd"]) - float(rows[0]["mean_fwd"])
    return {"n": n, "buckets": rows, "spread": spread}


def _write_aggregate(cache: Path, fields: list[str], rows: list[list]) -> Path:
    """Write inside the caller-supplied cache (the shared save_csv targets
    the fixed module-level directory and would bypass ``cache``)."""
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{AGGREGATE_NAME}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)
    return path


def load_cached_aggregate(cache: Path) -> tuple[list[str], list[list]]:
    path = cache / f"{AGGREGATE_NAME}.csv"
    if not path.exists():
        raise MarginStudyError(f"cache_missing:{path.name}")
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fields = next(reader)
        return fields, [row for row in reader]


def run_study(cache: Path = CACHE_DIR, refresh: bool = False) -> dict[str, object]:
    """Fetch-if-needed then compute; returns the summary dict and prints it."""
    if refresh or not (cache / f"{AGGREGATE_NAME}.csv").exists():
        fields, rows = fetch_ranged("margin", MARGIN_START, MARGIN_END)
        fields, rows, _dups = dedupe_margin_rows(fields, rows)
        _write_aggregate(cache, fields, rows)
    else:
        fields, rows = load_cached_aggregate(cache)

    totals = daily_totals(fields, rows)
    if len(totals) < FEATURE_LAG_SESSIONS + FORWARD_HORIZON_SESSIONS:
        raise MarginStudyError("sample_too_short")

    features = margin_features(totals)
    index_pairs = load_index_series(cache)
    fwd = forward_return_map(index_pairs)
    regimes = regime_by_day(index_pairs)

    joined: list[tuple[str, float, float, str | None]] = []
    for day, _short_chg, long_chg in features:
        day_key = _to_date_key(day)
        forward = fwd.get(day_key)
        regime = regimes.get(day_key)
        if forward is None:
            continue
        joined.append((day, long_chg, forward, regime))

    full = [row for row in joined]
    post = [row for row in joined if row[0] >= POST_SUBSAMPLE_START]

    summary: dict[str, object] = {
        "research_only": True,
        "rows_raw": len(rows),
        "days_total": len(totals),
        "days_joined": len(joined),
        "joined_first": joined[0][0] if joined else None,
        "joined_last": joined[-1][0] if joined else None,
        "regime_means_full": _regime_means(full),
        "regime_means_post2016": _regime_means(post),
        "quantile_full": quantile_spread([(chg, f) for _d, chg, f, _r in sorted(full, key=lambda r: r[1])]),
        "quantile_post2016": quantile_spread([(chg, f) for _d, chg, f, _r in sorted(post, key=lambda r: r[1])]),
        "corr_full": pearson([r[1] for r in full], [r[2] for r in full]),
        "corr_post2016": pearson([r[1] for r in post], [r[2] for r in post]),
    }
    _render(summary)
    return summary


def _to_date_key(raw: str):
    from datetime import datetime

    return datetime.strptime(raw, "%Y%m%d").date()


def _regime_means(rows: list[tuple[str, float, float, str | None]]) -> dict[str, dict[str, float]]:
    groups: dict[str, list[float]] = {}
    for _day, chg, _fwd, regime in rows:
        if regime is None or regime == "unknown":
            continue
        groups.setdefault(regime, []).append(chg)
    out: dict[str, dict[str, float]] = {}
    for label in ("weak", "sideways", "strong"):
        values = groups.get(label, [])
        if not values:
            continue
        ordered = sorted(values)
        out[label] = {
            "n": len(values),
            "mean_chg20": sum(values) / len(values),
            "median_chg20": ordered[len(ordered) // 2],
        }
    return out


def _fmt_pct(value: object) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:+.1f}%"


def _render(summary: dict[str, object]) -> None:
    print("## 融资融券×市场阶段研究（research_only，报告读数非晋级证据）")
    print(f"- 样本：交易日 {summary['days_total']} 天（{summary['joined_first']}.."
          f"{summary['joined_last']} 可配对），特征+前向收益配对 "
          f"{summary['days_joined']} 天（原始行 {summary['rows_raw']}）；"
          f"配对窗受指数缓存起点限制")
    for tag in ("regime_means_full", "regime_means_post2016"):
        print(f"- 融资余额20日变化 × 市场阶段 [{tag}]:")
        means = summary[tag]
        assert isinstance(means, dict)
        for label, stats in means.items():
            print(f"    {label:>8}: n={stats['n']:<5} mean={_fmt_pct(stats['mean_chg20'])} "
                  f"median={_fmt_pct(stats['median_chg20'])}")
    for tag in ("quantile_full", "quantile_post2016"):
        quant = summary[tag]
        assert isinstance(quant, dict)
        print(f"- 融资动量五分位 → 后10日指数收益 [{tag}] spread="
              f"{_fmt_pct(quant['spread'])}")
        for row in quant["buckets"]:
            assert isinstance(row, dict)
            print(f"    Q{row['bucket']} n={row['n']:<5} mean_fwd={_fmt_pct(row['mean_fwd'])} "
                  f"win={row['win']:.3f}")
    print(f"- Pearson corr(chg20, fwd10): full={summary['corr_full']} "
          f"post2016={summary['corr_post2016']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="refetch even when cached")
    args = parser.parse_args()
    run_study(refresh=args.refresh)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (MarginStudyError, FetchError) as exc:
        print(f"MARGIN_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
