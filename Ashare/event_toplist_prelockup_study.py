"""Dragon-tiger list pre-lockup conditioning layer study.

Implements the FROZEN pre-registration
(``2026-08-24-toplist-prelockup-preregistration.md``, PR #477) — this
module only realizes the registered definitions; it never changes them.

For each sell_off signal, top_list rows of the same stock whose
``trade_date`` falls in ``[entry_day - PRE_WINDOW_DAYS, entry_day)`` are
classified by the frozen reason-keyword mapping:

- ``sell_dev``    reason contains 「跌」 (sell-side deviation listing =
                  visible distribution; negative family with #464/#453)
- ``rise_dev``    reason contains 「涨」 (speculative heat, direction open)
- ``other``       any other listing (振幅/换手/empty text)
- ``no_listing``  empty window or no cache file (a label, not an error)

A window hitting several signs resolves by the frozen conservative
precedence ``sell_dev > rise_dev > other`` (negative hypothesis binds
mixed windows into the toxic bucket).

H1 primary contrast (frozen, signal-layer NEGATIVE hypothesis): the
``sell_dev`` bucket's mean net bps and win rate are LOWER than
``no_listing``'s.  Watch-list bar lives at the rule layer (double-low
vs ``rule[no_listing]`` AND n>=30) — coverage facts registered up front
make n>=30 unlikely there, in which case the layer records "signal-layer
mechanism verification + descriptive rule layer" and produces NO
watch-list item.  Population = the #423 lockup sell_off stream.
Cache-only.  research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_toplist_prelockup_study.py [--cache DIR]
        [--cost-bps X]
"""

from __future__ import annotations

import argparse
import bisect
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_calendar_lockup_strata import COST_BPS_ROUNDTRIP_DEFAULT  # noqa: E402
from Ashare.event_margin_crowding_state import cross_tab  # noqa: E402
from Ashare.event_paper_baseline_sim import (  # noqa: E402
    SIM_START,
    build_signals,
    load_events,
    load_index_series,
    load_stock_books,
    rule_arm_filter,
)
from Ashare.event_holdertrade_prelockup_study import _parse_day  # noqa: E402
from Ashare.event_toplist_fetch import TOPLIST_DIRNAME  # noqa: E402

PRE_WINDOW_DAYS = 30  # frozen lookback, holdertrade family convention
TOPLIST_BUCKETS = ("sell_dev", "rise_dev", "other", "no_listing")
# Frozen D2 conservative precedence for mixed-sign windows.
_BUCKET_PRECEDENCE = {"sell_dev": 0, "rise_dev": 1, "other": 2}


class ToplistStudyError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


def load_toplist_index(cache: Path) -> dict[str, tuple[list[str], list[str]]]:
    """Per-symbol ascending (trade_dates, reason_buckets) index.

    Files are one trading day each (``toplist_daily/<YYYYMMDD>.csv``,
    full sweep); the filename day is authoritative and row-level
    ``trade_date`` was already validated by the fetcher.  Rows without a
    ts_code are skipped.
    """
    folder = cache / TOPLIST_DIRNAME
    files = sorted(folder.glob("*.csv")) if folder.is_dir() else []
    if not files:
        raise ToplistStudyError("toplist_cache_missing")
    days_by_code: dict[str, list[str]] = {}
    buckets_by_code: dict[str, list[str]] = {}
    for path in files:
        day = _parse_day(path.stem)
        if day is None:
            continue
        try:
            handle = path.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for row in csv.DictReader(handle):
                code = row.get("ts_code") or ""
                if not code:
                    continue
                days_by_code.setdefault(code, []).append(day)
                buckets_by_code.setdefault(code, []).append(
                    classify_reason(row.get("reason"))
                )
    index: dict[str, tuple[list[str], list[str]]] = {}
    for code, days in days_by_code.items():
        order = sorted(range(len(days)), key=lambda i: days[i])
        index[code] = (
            [days[i] for i in order],
            [buckets_by_code[code][i] for i in order],
        )
    return index


def classify_reason(reason: object) -> str:
    """Frozen D2 keyword mapping on the raw Chinese reason text."""
    text = str(reason or "")
    if "跌" in text:
        return "sell_dev"
    if "涨" in text:
        return "rise_dev"
    return "other"


def _window_bucket(
    book: tuple[list[str], list[str]] | None, entry_day: str
) -> tuple[str | None, int]:
    """(frozen-precedence window label, listing-day count).

    Window is ``[entry-30 natural days, entry)`` — start inclusive,
    entry day strictly excluded.  Mixed signs resolve by the frozen
    precedence sell_dev > rise_dev > other; empty windows label
    ``no_listing`` via :func:`attach_toplist_states`.
    """
    if book is None:
        return None, 0
    try:
        entry_date = datetime.strptime(entry_day, "%Y%m%d").date()
    except ValueError:
        return None, 0
    days, buckets = book
    window_start = (
        entry_date - timedelta(days=PRE_WINDOW_DAYS)).strftime("%Y%m%d")
    lo = bisect.bisect_left(days, window_start)
    hi = bisect.bisect_left(days, entry_day)  # strictly prior to entry
    window_buckets = buckets[lo:hi]
    if not window_buckets:
        return None, 0
    label = min(window_buckets, key=lambda b: _BUCKET_PRECEDENCE[b])
    return label, len(window_buckets)


def attach_toplist_states(
    signals: list[dict[str, object]],
    index: dict[str, tuple[list[str], list[str]]],
) -> dict[str, int]:
    """Annotate each signal with its pre-window dragon-tiger bucket."""
    stats = {bucket: 0 for bucket in TOPLIST_BUCKETS}
    stats["attached"] = 0
    for signal in signals:
        code = str(signal["ts_code"])
        entry_day = str(signal["entry_day"])
        label, hits = _window_bucket(index.get(code), entry_day)
        bucket = label if label is not None else "no_listing"
        signal["toplist_bucket"] = bucket
        signal["toplist_hits"] = hits
        if label is not None:
            days, _buckets = index[code]
            hi = bisect.bisect_left(days, entry_day)
            latest = days[hi - 1]
            lag = (datetime.strptime(entry_day, "%Y%m%d").date()
                   - datetime.strptime(latest, "%Y%m%d").date()).days
            signal["toplist_lag_days"] = lag
        else:
            signal["toplist_lag_days"] = None
        stats[bucket] += 1
        stats["attached"] += 1
    return stats


def toplist_buckets_for_events(
    cache: Path,
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    """Tracker side-table lookup: {(ts_code, day): toplist bucket}.

    Reuses the study loader on synthetic signals; the frozen window and
    keyword mapping apply unchanged.  An empty window yields
    ``no_listing`` rather than an omitted pair — only a cache-level
    failure (caught by the tracker wrapper) leaves events unlabeled.
    """
    index = load_toplist_index(cache)
    synthetic = [
        {"ts_code": str(code), "entry_day": str(day)} for code, day in pairs
    ]
    attach_toplist_states(synthetic, index)
    return {
        (s["ts_code"], s["entry_day"]): s["toplist_bucket"]
        for s in synthetic
    }


def run_study(
    cache: Path, cost_bps: float = COST_BPS_ROUNDTRIP_DEFAULT
) -> dict[str, object]:
    index_pairs = load_index_series(cache)
    global_days = [
        d.strftime("%Y%m%d")
        for d, _ in index_pairs
        if d.strftime("%Y%m%d") >= SIM_START
    ]
    events, _stats = load_events(cache)
    books, uncovered = load_stock_books(cache)
    signals, _sig_stats = build_signals(events, books, index_pairs, global_days[-1])

    index = load_toplist_index(cache)
    attach_stats = attach_toplist_states(signals, index)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "universe_uncovered_symbols": uncovered,
    }
    print("## 解禁事前龙虎榜上榜条件层研究（research_only，非晋级证据）")
    print(f"- 信号总数 {len(signals)}；窗口 [entry−{PRE_WINDOW_DAYS} 自然日,"
          f" entry)；冻结关键词映射 跌→sell_dev / 涨→rise_dev / 其它→other，"
          f"混合窗口按 sell_dev>rise_dev>other 保守归桶；成本 "
          f"{cost_bps}bps 往返")

    tab = cross_tab(signals, cost_bps=cost_bps, key="toplist_bucket",
                    labels=TOPLIST_BUCKETS)
    results["r1_signal_level_cross_tab"] = tab
    print("\n### R1 信号层四桶交叉表（净 bps / 胜率）")
    print(f"{'bucket':<12} {'n':>6} {'mean_net_bps':>13} {'win_rate':>9}")
    for label, cell in tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"{label:<12} {cell['n']:>6} {mean_txt:>13} {win_txt:>9}")

    rule_signals = [s for s in signals if rule_arm_filter(s)]
    rule_tab = cross_tab(rule_signals, cost_bps=cost_bps,
                         key="toplist_bucket", labels=TOPLIST_BUCKETS)
    results["r2_rule_arm_cross_tab"] = rule_tab
    results["h1_primary_contrast"] = {
        "sell_dev": rule_tab.get("sell_dev"),
        "signal_layer_no_listing": tab.get("no_listing"),
    }
    print("\n### R2 rule 臂叠加交叉表（弱市×非3–5%带，定义不动）"
          "——各桶已知大概率 n<30，按预注册降为描述性")
    for label, cell in rule_tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"rule[{label:<11}] n={cell['n']:>4} {mean_txt}bps win={win_txt}")
    print("- H1 主对比（冻结，信号层负向）：sell_dev 均值与胜率是否双低于 "
          "no_listing——判定按预注册标准执行，此处只呈现数字")

    lags = sorted(
        int(s["toplist_lag_days"])
        for s in signals
        if s.get("toplist_lag_days") is not None
    )
    multi = sum(
        1 for s in signals if int(s.get("toplist_hits") or 0) > 1
    )
    results["r3_coverage"] = {
        "with_listing": attach_stats.get("sell_dev", 0)
        + attach_stats.get("rise_dev", 0)
        + attach_stats.get("other", 0),
        "files_in_cache": len(index),
        "lag_days_mean": (sum(lags) / len(lags)) if lags else None,
        "lag_days_max": lags[-1] if lags else None,
        "multi_hit_signals": multi,
    }
    print("\n### R3/HV 覆盖与健康检查")
    print(f"- 有上榜信号合计：{results['r3_coverage']['with_listing']}/"
          f"{len(signals)}（预注册计数基准 131）；缓存文件数 "
          f"{len(index)}；窗口内最近上榜陈旧度均值 "
          f"{results['r3_coverage']['lag_days_mean']} 自然日 / 最大 "
          f"{results['r3_coverage']['lag_days_max']}；多日命中信号 "
          f"{multi}")
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
    except ToplistStudyError as exc:
        print(f"TOPLIST_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
