"""Pre-lockup own-history valuation-percentile conditioning layer (panel #17).

Frozen preregistration: ``Ashare/reports/2026-08-25-unlock-valuation-
prelockup-preregistration.md`` (merged before this engine; no bucketed
returns were computed before that merge).  Zero new data dependency: labels
come from the family's per-symbol ``dailybasic_*.csv`` shards (2,096-row
full history each).  For every entry signal the current value is the last
``pe_ttm`` STRICTLY BEFORE ``entry_day``; the history window is up to 250
valid positive values before that row; the label percentile is the share of
history <= current.  Fixed ex-ante edges on a statistic whose null
distribution is uniform:

  low_le25      : percentile <= 0.25
  mid           : between the edges
  high_ge75     : percentile >= 0.75   (H1 primary, negative direction)
  short_history : fewer than 200 valid history values
  loss_or_missing : current value null or <= 0 (Tushare nulls loss-makers)

Shards store newest-first; every read defensively re-sorts (#23 lesson).
H1 (frozen, negative): rule[high_ge75] mean & win-rate both BELOW the
unfiltered rule arm.  research_only / not_promotion_evidence.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_calendar_lockup_strata import (  # noqa: E402
    COST_BPS_ROUNDTRIP_DEFAULT,
    load_index_series,
)
from Ashare.event_margin_crowding_state import (  # noqa: E402
    cross_tab,
    net_trade_return,
)
from Ashare.event_paper_baseline_sim import (  # noqa: E402
    SIM_START,
    build_signals,
    load_events,
    load_stock_books,
    rule_arm_filter,
)

VALUATION_BUCKETS = ("low_le25", "mid", "high_ge75", "short_history",
                     "loss_or_missing")
LOW_EDGE = 0.25               # frozen ex-ante edges (uniform null statistic)
HIGH_EDGE = 0.75
HIST_WIN = 250                # rows of history before the current row
PCT_MIN_HIST = 200            # valid values required else short_history
WATCH_LIST_MIN_N = 30         # family-standard gate, frozen in the prereg


class ValuationStudyError(RuntimeError):
    """Fail-closed study error with a stable reason code."""


def _baseline_cell(signals: list[dict[str, object]], cost_bps: float) -> dict:
    rets = [net_trade_return(s, cost_bps) for s in signals]
    return {
        "n": len(rets),
        "mean_net_bps": (sum(rets) / len(rets)) * 1e4 if rets else None,
        "win_rate": (sum(1 for r in rets if r > 0.0) / len(rets))
        if rets
        else None,
    }


def _double_low(cell: dict, baseline: dict) -> bool:
    """Frozen H1 verdict shape (same gate family as panels #13-#16)."""
    return bool(
        cell["n"] >= WATCH_LIST_MIN_N
        and cell["mean_net_bps"] is not None
        and baseline["mean_net_bps"] is not None
        and float(cell["mean_net_bps"]) < float(baseline["mean_net_bps"])
        and float(cell["win_rate"]) < float(baseline["win_rate"])
    )


def load_pe_book(cache: Path, ts_code: str) -> tuple[list[str], list[float]]:
    """One pass over one dailybasic shard -> ascending (days, pe_ttm).

    Shards store newest-first; defensive re-sort before any bisect
    (unsorted-book silent misalignment, #23 lesson fourth appearance).
    """
    path = cache / f"dailybasic_{ts_code.replace('.', '')}.csv"
    if not path.exists():
        raise ValuationStudyError(f"cache_missing:dailybasic_{ts_code}.csv")
    days: list[str] = []
    pes: list[float] = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            day = row.get("trade_date") or ""
            if not day or day < SIM_START:
                continue
            try:
                value = float(row.get("pe_ttm"))
            except (TypeError, ValueError):
                value = None  # type: ignore[assignment]
            days.append(day)
            pes.append(value)
    if days and days[0] > days[-1]:
        days.reverse()
        pes.reverse()
    return days, pes


def valuation_label(
    days: list[str], pes: list[float | None], entry_day: str
) -> str:
    """Frozen D1 label for one signal (strictly-before entry semantics)."""
    pos = bisect.bisect_left(days, str(entry_day))
    if pos == 0:
        return "short_history"  # no strictly-prior row = empty history
    current = pes[pos - 1]
    if current is None or current <= 0.0:
        return "loss_or_missing"
    history = [
        v
        for v in pes[max(0, pos - 1 - HIST_WIN):pos - 1]
        if v is not None and v > 0.0
    ]
    if len(history) < PCT_MIN_HIST:
        return "short_history"
    pct = sum(1 for v in history if v <= current) / len(history)
    if pct <= LOW_EDGE:
        return "low_le25"
    if pct >= HIGH_EDGE:
        return "high_ge75"
    return "mid"


def attach_valuation_bucket(
    signals: list[dict[str, object]], cache: Path
) -> dict[str, int]:
    """Annotate each signal with its pre-entry valuation-percentile bucket."""
    stats: dict[str, int] = {bucket: 0 for bucket in VALUATION_BUCKETS}
    stats["attached"] = 0
    books: dict[str, tuple[list[str], list[float]]] = {}
    for signal in signals:
        code = str(signal["ts_code"])
        if code not in books:
            books[code] = load_pe_book(cache, code)
        days, pes = books[code]
        bucket = valuation_label(days, pes, str(signal["entry_day"]))
        signal["valuation_bucket"] = bucket
        stats[bucket] += 1
        stats["attached"] += 1
    return stats


def valuation_buckets_for_entries(
    cache: Path, entries: list[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    """Side-table helper: (ts_code, event_day) -> frozen bucket label.

    ``day`` is the unlock day (float_date); it is snapped to the shard's
    own last session on or before that day so the label reproduces the
    study engine's entry_day semantics exactly (``build_signals`` snaps
    on the price-book grid; the dailybasic grid carries the same session
    set).  Entries whose shard is absent stay unlabeled instead of
    failing the batch — the tracker universe grows beyond the study's
    1,000-symbol shard set, and one unknown symbol must not silence the
    whole rolling table (unlabeled is honest, mislabeled is not).
    """
    labels: dict[tuple[str, str], str] = {}
    books: dict[str, tuple[list[str], list[float]] | None] = {}
    for code, day in dict.fromkeys(entries):
        if code not in books:
            try:
                books[code] = load_pe_book(cache, code)
            except ValuationStudyError:
                books[code] = None
        book = books[code]
        if book is None:
            continue
        days, pes = book
        pos = bisect.bisect_right(days, str(day))
        if pos == 0:
            labels[(code, day)] = "short_history"
            continue
        labels[(code, day)] = valuation_label(days, pes, days[pos - 1])
    return labels


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

    attach_stats = attach_valuation_bucket(signals, cache)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "universe_uncovered_symbols": uncovered,
    }
    print("## 解禁事前估值分位条件层研究（research_only，非晋级证据）")
    print(f"- 标签=严格早于入场日的 pe_ttm 自身 {HIST_WIN} 行分位（有效历史 "
          f">={PCT_MIN_HIST}），固定边界 ≤{LOW_EDGE}/≥{HIGH_EDGE}；成本 "
          f"{cost_bps}bps 往返")

    tab = cross_tab(signals, cost_bps=cost_bps, key="valuation_bucket",
                    labels=VALUATION_BUCKETS)
    results["r1_signal_level_cross_tab"] = tab
    print("\n### R1 信号层五桶交叉表（净 bps / 胜率）")
    print(f"{'bucket':<16} {'n':>6} {'mean_net_bps':>13} {'win_rate':>9}")
    for label, cell in tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"{label:<16} {cell['n']:>6} {mean_txt:>13} {win_txt:>9}")

    rule_signals = [s for s in signals if rule_arm_filter(s)]
    rule_tab = cross_tab(rule_signals, cost_bps=cost_bps,
                         key="valuation_bucket", labels=VALUATION_BUCKETS)
    baseline = _baseline_cell(rule_signals, cost_bps)
    results["r2_rule_arm_cross_tab"] = rule_tab
    results["r2_rule_unfiltered_baseline"] = baseline
    print("\n### R2 rule 臂叠加交叉表（弱市×非3–5%带，定义不动）与未滤基线")
    for label, cell in {**rule_tab, "UNFILTERED": baseline}.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"rule[{label:<15}] n={cell['n']:>4} {mean_txt}bps win={win_txt}")

    high_cell = rule_tab.get("high_ge75",
                             {"n": 0, "mean_net_bps": None, "win_rate": None})
    eligible = _double_low(high_cell, baseline)
    results["h1_primary_contrast"] = {
        "rule_high_ge75": high_cell,
        "rule_unfiltered_baseline": baseline,
        "watch_list_eligible": eligible,
    }
    verdict = "进观察名单" if eligible else "未达标（FAIL 同样是合格产出）"
    print(f"\n### H1 冻结判定：rule 臂 high_ge75 对未滤基线双低且 n≥"
          f"{WATCH_LIST_MIN_N} ⇒ {verdict}")

    results["r3_coverage"] = {
        "attached": attach_stats["attached"],
        "bucket_counts": {b: attach_stats[b] for b in VALUATION_BUCKETS},
    }
    print("\n### R3/HV 覆盖与健康检查")
    cov = results["r3_coverage"]
    counts = "/".join(str(cov["bucket_counts"][b]) for b in VALUATION_BUCKETS)
    print(f"- 已贴标 {cov['attached']}/{len(signals)}（预注册基准 all 桶计数 "
          f"415/219/152/159/90）；rule 臂基准 149/69/45/69/25")
    print(f"- all 桶计数：{counts}")
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
    except ValuationStudyError as exc:
        print(f"VALUATION_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(2)
