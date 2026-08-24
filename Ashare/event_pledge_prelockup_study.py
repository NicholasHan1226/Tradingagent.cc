"""Pledge pre-lockup conditioning layer study.

Implements the FROZEN pre-registration
(``2026-08-24-pledge-prelockup-preregistration.md``, PR #464) — this
module only realizes the registered definitions; it never changes them.

For each sell_off signal, pledge_stat snapshots of the same stock whose
``end_date`` falls in ``[entry_day - PRE_WINDOW_DAYS, entry_day)`` are
reduced to the LATEST snapshot (max end_date; ties → max pledge_ratio,
frozen) and classified by fixed boundaries on ``pledge_ratio`` (percent
scale used as-is — the float_ratio lesson):

- ``high``        ratio >= 20.0  (common high-pledge attention line)
- ``mid``         5.0 <= ratio < 20.0
- ``low``         ratio < 5.0 (includes 0 = fully released)
- ``no_snapshot`` empty window or missing file (a label, not an error)

H1 primary contrast (frozen, NEGATIVE hypothesis): within the rule arm,
the ``high`` bucket's mean net bps and win rate are LOWER than
``no_snapshot``'s.  Population = the #423 lockup sell_off stream.
Cache-only.  research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_pledge_prelockup_study.py [--cache DIR]
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

from Ashare.event_calendar_lockup_strata import (  # noqa: E402
    COST_BPS_ROUNDTRIP_DEFAULT,
)
from Ashare.event_margin_crowding_state import (  # noqa: E402
    cross_tab,
)
from Ashare.event_paper_baseline_sim import (  # noqa: E402
    SIM_START,
    build_signals,
    load_events,
    load_index_series,
    load_stock_books,
    rule_arm_filter,
)
from Ashare.event_holdertrade_prelockup_study import (  # noqa: E402
    _parse_day,
)

PRE_WINDOW_DAYS = 30  # frozen lookback, holdertrade family convention
PLEDGE_BUCKETS = ("high", "mid", "low", "no_snapshot")
HIGH_RATIO = 20.0  # frozen boundary, percent scale as-is
MID_RATIO = 5.0  # frozen boundary, percent scale as-is


class PledgeStudyError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


def load_pledge_index(
    cache: Path,
) -> dict[str, tuple[list[str], list[float]]]:
    """Per-symbol ascending (end_dates, pledge_ratios) index.

    Files are one symbol each (``pledgestat_<stem>.csv``, full history);
    rows carry their own ``end_date``.  Rows with no ts_code, an
    unparseable end_date or an unparseable pledge_ratio are skipped —
    there is no filename day to fall back to in the per-symbol layout.
    """
    files = sorted(cache.glob("pledgestat_*.csv"))
    if not files:
        raise PledgeStudyError("pledge_cache_missing")
    days_by_code: dict[str, list[str]] = {}
    ratios_by_code: dict[str, list[float]] = {}
    for path in files:
        try:
            handle = path.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for row in csv.DictReader(handle):
                code = row.get("ts_code") or ""
                day = _parse_day(row.get("end_date"))
                try:
                    ratio = float(row.get("pledge_ratio"))
                except (TypeError, ValueError):
                    continue
                if not code or day is None:
                    continue
                days_by_code.setdefault(code, []).append(day)
                ratios_by_code.setdefault(code, []).append(ratio)
    index: dict[str, tuple[list[str], list[float]]] = {}
    for code, days in days_by_code.items():
        order = sorted(range(len(days)), key=lambda i: days[i])
        index[code] = (
            [days[i] for i in order],
            [ratios_by_code[code][i] for i in order],
        )
    return index


def classify_pledge(ratio: float | None) -> str:
    """Frozen D2 boundaries on the percent scale (ratio used as-is)."""
    if ratio is None:
        return "no_snapshot"
    if ratio >= HIGH_RATIO:
        return "high"
    if ratio >= MID_RATIO:
        return "mid"
    return "low"


def _latest_window_ratio(
    book: tuple[list[str], list[float]] | None, entry_day: str
) -> tuple[str | None, float | None, int]:
    """(latest end_date in [entry-30d, entry), its ratio, snapshot count).

    Frozen D2: classify by the LATEST snapshot only — a higher ratio on
    an older snapshot never leaks into the label.  A tie on the latest
    day resolves to max ratio (frozen conservative rule).  Strictly
    prior to the entry day; window start inclusive.
    """
    if book is None:
        return None, None, 0
    try:
        entry_date = datetime.strptime(entry_day, "%Y%m%d").date()
    except ValueError:
        return None, None, 0
    days, ratios = book
    window_start = (
        entry_date - timedelta(days=PRE_WINDOW_DAYS)).strftime("%Y%m%d")
    lo = bisect.bisect_left(days, window_start)
    hi = bisect.bisect_left(days, entry_day)  # strictly prior to entry
    window_days = days[lo:hi]
    if not window_days:
        return None, None, 0
    latest = window_days[-1]  # ascending by construction
    ratio = max(
        ratios[lo + i]
        for i, day in enumerate(window_days)
        if day == latest
    )
    return latest, ratio, len(window_days)


def attach_pledge_states(
    signals: list[dict[str, object]],
    index: dict[str, tuple[list[str], list[float]]],
) -> dict[str, int]:
    """Annotate each signal with its pre-window pledge bucket."""
    stats = {bucket: 0 for bucket in PLEDGE_BUCKETS}
    stats["attached"] = 0
    for signal in signals:
        code = str(signal["ts_code"])
        entry_day = str(signal["entry_day"])
        latest_day, ratio, n_snap = _latest_window_ratio(
            index.get(code), entry_day
        )
        bucket = classify_pledge(ratio)
        signal["pledge_bucket"] = bucket
        signal["pledge_ratio"] = ratio
        signal["pledge_snapshots"] = n_snap
        if latest_day is not None:
            lag = (datetime.strptime(entry_day, "%Y%m%d").date()
                   - datetime.strptime(latest_day, "%Y%m%d").date()).days
            signal["pledge_lag_days"] = lag
        else:
            signal["pledge_lag_days"] = None
        stats[bucket] += 1
        stats["attached"] += 1
    return stats


def pledge_buckets_for_events(
    cache: Path,
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    """Tracker side-table lookup: {(ts_code, day): pledge bucket}.

    Reuses the study loader on synthetic signals; the frozen window and
    boundaries apply unchanged.  An empty window yields ``no_snapshot``
    rather than an omitted pair — only a cache-level failure (caught by
    the tracker wrapper) leaves events unlabeled.
    """
    index = load_pledge_index(cache)
    synthetic = [
        {"ts_code": str(code), "entry_day": str(day)} for code, day in pairs
    ]
    attach_pledge_states(synthetic, index)
    return {
        (s["ts_code"], s["entry_day"]): s["pledge_bucket"]
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

    index = load_pledge_index(cache)
    attach_stats = attach_pledge_states(signals, index)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "universe_uncovered_symbols": uncovered,
    }
    print("## 解禁事前质押状态条件层研究（research_only，非晋级证据）")
    print(f"- 信号总数 {len(signals)}；窗口 [entry−{PRE_WINDOW_DAYS} 自然日,"
          f" entry)；窗口内最新快照按冻结边界分类 high≥{HIGH_RATIO}/mid "
          f"{MID_RATIO}–{HIGH_RATIO}/low<{MID_RATIO}/no_snapshot；成本 "
          f"{cost_bps}bps 往返")

    tab = cross_tab(signals, cost_bps=cost_bps, key="pledge_bucket",
                    labels=PLEDGE_BUCKETS)
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
                         key="pledge_bucket", labels=PLEDGE_BUCKETS)
    results["r2_rule_arm_cross_tab"] = rule_tab
    results["h1_primary_contrast"] = {
        "high": rule_tab.get("high"),
        "no_snapshot": rule_tab.get("no_snapshot"),
    }
    print("\n### R2 rule 臂叠加交叉表（弱市×非3–5%带，定义不动）")
    for label, cell in rule_tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"rule[{label:<11}] n={cell['n']:>4} {mean_txt}bps win={win_txt}")
    print("- H1 主对比（冻结，负向）：rule[high] 均值与胜率是否双低于 "
          "rule[no_snapshot]——判定按预注册标准执行，此处只呈现数字")

    covered = len(signals) - attach_stats.get("no_snapshot", 0)
    lags = sorted(
        int(s["pledge_lag_days"])
        for s in signals
        if s.get("pledge_lag_days") is not None
    )
    results["r3_coverage"] = {
        "with_snapshot": covered,
        "files_in_cache": len(index),
        "lag_days_mean": (sum(lags) / len(lags)) if lags else None,
        "lag_days_max": lags[-1] if lags else None,
    }
    print("\n### R3/HV 覆盖与健康检查")
    print(f"- 有快照桶合计：{covered}/{len(signals)}；缓存文件数 "
          f"{len(index)}；窗口内最新快照陈旧度均值 "
          f"{results['r3_coverage']['lag_days_mean']} 自然日 / 最大 "
          f"{results['r3_coverage']['lag_days_max']}")
    jumps = _adjacent_ratio_jumps(index)
    print(f"- HV 相邻快照连续性：|Δratio|>10pp 的相邻对占比 "
          f"{jumps:.3f}（新质押事件会造成真实跳变，仅作数据自洽描述）")
    return results


def _adjacent_ratio_jumps(
    index: dict[str, tuple[list[str], list[float]]],
    threshold_pp: float = 10.0,
) -> float:
    """Fraction of adjacent same-stock snapshot pairs with a big jump."""
    pairs = 0
    big = 0
    for _days, ratios in index.values():
        for prev, cur in zip(ratios, ratios[1:]):
            pairs += 1
            if abs(cur - prev) > threshold_pp:
                big += 1
    return (big / pairs) if pairs else 0.0


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
    except PledgeStudyError as exc:
        print(f"PLEDGE_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
