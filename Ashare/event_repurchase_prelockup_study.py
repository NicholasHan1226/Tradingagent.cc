"""Repurchase pre-lockup conditioning layer study.

Implements the FROZEN pre-registration
(``2026-08-24-repurchase-prelockup-preregistration.md``, PR #458) — this
module only realizes the registered definitions; it never changes them.

For each sell_off signal, repurchase announcements of the same stock whose
``ann_date`` falls in ``[entry_day - PRE_WINDOW_DAYS, entry_day)`` are
classified SOLELY by their ``proc`` state text (vol/amount/price fields
are deliberately unused — mixed units, the float_ratio lesson):

- ``active``      any record in {预案, 股东大会通过, 实施} — buyback in
                  progress: management absorbing upcoming supply
- ``stopped``     no active but a 停止 record — support abandoned
- ``done``        only 完成 records — support already spent
- ``no_records``  empty window or missing file (a label, not an error)

H1 primary contrast (frozen): within the rule arm, ``active`` mean net
bps and win rate exceed ``no_records``.  Population = the #423 lockup
sell_off stream.  Cache-only.  research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_repurchase_prelockup_study.py [--cache DIR]
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
RP_BUCKETS = ("active", "stopped", "done", "no_records")
PROC_ACTIVE_STATES = ("预案", "股东大会通过", "实施")
PROC_STOPPED_STATE = "停止"
PROC_DONE_STATE = "完成"


class RepurchaseStudyError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


def load_repurchase_index(
    cache: Path,
) -> dict[str, tuple[list[str], list[str]]]:
    """Per-symbol ascending (ann_dates, proc_states) index.

    Files are one ann_date each under ``repurchase_ann/``, so iterating
    the sorted file list yields ascending per-symbol day order by
    construction.  Rows with no ts_code or an unparseable ann_date are
    skipped; proc stays raw text (classification matches it exactly).
    """
    folder = cache / "repurchase_ann"
    if not folder.is_dir():
        raise RepurchaseStudyError("repurchase_cache_missing")
    days_by_code: dict[str, list[str]] = {}
    procs_by_code: dict[str, list[str]] = {}
    rows_seen = 0
    for path in sorted(folder.glob("*.csv")):
        fallback_day = _parse_day(path.stem)
        try:
            handle = path.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for row in csv.DictReader(handle):
                code = row.get("ts_code") or ""
                if not code:
                    continue
                day = _parse_day(row.get("ann_date")) or fallback_day
                if day is None:
                    continue
                rows_seen += 1
                days_by_code.setdefault(code, []).append(day)
                procs_by_code.setdefault(code, []).append(
                    str(row.get("proc") or "")
                )
    if rows_seen == 0:
        raise RepurchaseStudyError("repurchase_cache_empty")
    index: dict[str, tuple[list[str], list[str]]] = {}
    for code, days in days_by_code.items():
        order = sorted(range(len(days)), key=lambda i: days[i])
        index[code] = (
            [days[i] for i in order],
            [procs_by_code[code][i] for i in order],
        )
    return index


def classify_repurchase(procs: list[str]) -> str:
    """Frozen D2 priority: active > stopped > done > no_records."""
    if not procs:
        return "no_records"
    if any(p in PROC_ACTIVE_STATES for p in procs):
        return "active"
    if any(p == PROC_STOPPED_STATE for p in procs):
        return "stopped"
    return "done"


def _window_procs(
    book: tuple[list[str], list[str]] | None, entry_day: str
) -> list[str]:
    """Proc states of records with ann_date in [entry-30d, entry)."""
    if book is None:
        return []
    try:
        entry_date = datetime.strptime(entry_day, "%Y%m%d").date()
    except ValueError:
        return []
    days, procs = book
    window_start = (
        entry_date - timedelta(days=PRE_WINDOW_DAYS)).strftime("%Y%m%d")
    lo = bisect.bisect_left(days, window_start)
    hi = bisect.bisect_left(days, entry_day)  # strictly prior to entry
    return procs[lo:hi]


def attach_repurchase_states(
    signals: list[dict[str, object]],
    index: dict[str, tuple[list[str], list[str]]],
) -> dict[str, int]:
    """Annotate each signal with its pre-window repurchase bucket."""
    stats = {bucket: 0 for bucket in RP_BUCKETS}
    stats["attached"] = 0
    for signal in signals:
        code = str(signal["ts_code"])
        entry_day = str(signal["entry_day"])
        procs = _window_procs(index.get(code), entry_day)
        bucket = classify_repurchase(procs)
        signal["repurchase_bucket"] = bucket
        signal["repurchase_procs"] = procs if procs else None
        stats[bucket] += 1
        stats["attached"] += 1
    return stats


def repurchase_buckets_for_events(
    cache: Path,
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    """Tracker side-table lookup: {(ts_code, day): repurchase bucket}.

    Reuses the study loader on synthetic signals; the frozen window and
    priority classification apply unchanged.  An empty window yields
    ``no_records`` rather than an omitted pair — only a cache-level
    failure (caught by the tracker wrapper) leaves events unlabeled.
    """
    index = load_repurchase_index(cache)
    synthetic = [
        {"ts_code": str(code), "entry_day": str(day)} for code, day in pairs
    ]
    attach_repurchase_states(synthetic, index)
    return {
        (s["ts_code"], s["entry_day"]): s["repurchase_bucket"]
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

    index = load_repurchase_index(cache)
    attach_stats = attach_repurchase_states(signals, index)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "universe_uncovered_symbols": uncovered,
    }
    print("## 解禁事前回购公告条件层研究（research_only，非晋级证据）")
    print(f"- 信号总数 {len(signals)}；窗口 [entry−{PRE_WINDOW_DAYS} 自然日,"
          f" entry)；分类仅依据 proc 文本（优先级 active>stopped>done>"
          f"no_records）；成本 {cost_bps}bps 往返")

    tab = cross_tab(signals, cost_bps=cost_bps, key="repurchase_bucket",
                    labels=RP_BUCKETS)
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
                         key="repurchase_bucket", labels=RP_BUCKETS)
    results["r2_rule_arm_cross_tab"] = rule_tab
    results["h1_primary_contrast"] = {
        "active": rule_tab.get("active"),
        "no_records": rule_tab.get("no_records"),
    }
    print("\n### R2 rule 臂叠加交叉表（弱市×非3–5%带，定义不动）")
    for label, cell in rule_tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"rule[{label:<11}] n={cell['n']:>4} {mean_txt}bps win={win_txt}")
    active_cell = rule_tab.get("active") or {}
    base_cell = rule_tab.get("no_records") or {}
    print("- H1 主对比（冻结）：rule[active] vs rule[no_records] "
          "均值与胜率是否双高——判定按预注册标准执行，此处只呈现数字")

    covered = len(signals) - attach_stats.get("no_records", 0)
    results["r3_coverage"] = {
        "with_records": covered,
        "proc_distribution": {
            k: attach_stats[k] for k in RP_BUCKETS if k != "no_records"
        },
    }
    print("\n### R3/HV 覆盖与方向一致性")
    print(f"- 有记录桶合计：{covered}/{len(signals)}；active "
          f"{attach_stats.get('active', 0)}、stopped "
          f"{attach_stats.get('stopped', 0)}、done "
          f"{attach_stats.get('done', 0)}")
    print("- HV：与 holdertrade 净增持桶同为承接意愿代理，方向一致性"
          "对照 #453/#455 读数描述")

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
    except RepurchaseStudyError as exc:
        print(f"REPURCHASE_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
