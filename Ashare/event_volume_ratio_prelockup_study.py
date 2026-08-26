"""Pre-lockup volume-ratio conditioning layer (panel #18).

Frozen preregistration: ``Ashare/reports/2026-08-25-volume-ratio-
prelockup-preregistration.md`` (merged before this engine; no bucketed
returns were computed before that merge).  Labels come from the family's
per-symbol ``dailybasic_*.csv`` shards (``volume_ratio`` field, Tushare's
same-day volume over trailing-5-day mean volume).

For every entry signal the current value is the ``volume_ratio`` of the
last shard row STRICTLY BEFORE ``entry_day`` (single-session snapshot;
the field is already a self-normalised ratio so no extra windowing).
Fixed ex-ante horizontal edges (frozen from p25/p75=0.72/1.11 rounded to
attention lines, before any bucketed return math):

  low     : volume_ratio < 0.70   (descriptive contrast only, no
                                   direction promised)
  normal  : 0.70 <= v < 1.20      (baseline band)
  high    : v >= 1.20             (H1 primary, negative direction:
                                   pre-unlock distribution visibility)

D3 zero-imputation: missing shard file, no strictly-prior row, and
null/unparseable value all fall to ``no_data`` reference rows counted
separately, never entering the primary contrast (probe measured all
three at 0 on the study cache; the semantics stay defensive against
cache evolution).

H1 (frozen, negative): rule[high] mean & win-rate both BELOW the
unfiltered rule arm, n >= 30 -> watch list; panel #18 multiple-comparison
discipline forbids deployment candidacy regardless of outcome.
research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_volume_ratio_prelockup_study.py [--cache DIR]
        [--cost-bps X]
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

VOLUME_RATIO_BUCKETS = ("low", "normal", "high", "no_data")
LOW_EDGE = 0.70               # frozen ex-ante edges (p25/p75 attention lines)
HIGH_EDGE = 1.20
WATCH_LIST_MIN_N = 30         # family-standard gate, frozen in the prereg


class VolumeRatioStudyError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


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
    """Frozen H1 verdict shape (same gate family as panels #13-#17)."""
    return bool(
        cell["n"] >= WATCH_LIST_MIN_N
        and cell["mean_net_bps"] is not None
        and baseline["mean_net_bps"] is not None
        and float(cell["mean_net_bps"]) < float(baseline["mean_net_bps"])
        and float(cell["win_rate"]) < float(baseline["win_rate"])
    )


def load_volume_ratio_book(
    cache: Path, ts_code: str
) -> tuple[list[str], list[float | None]] | None:
    """One pass over one dailybasic shard -> ascending (days, volume_ratio).

    Returns ``None`` when the shard file is absent (prereg D3: missing
    shard is a ``no_data`` reference case, not a crash — the probe
    measured zero such cases and the label must stay honest if the
    cache evolves).  Shards store newest-first; every read defensively
    re-sorts (#23 lesson).
    """
    path = cache / f"dailybasic_{ts_code.replace('.', '')}.csv"
    if not path.exists():
        return None
    days: list[str] = []
    ratios: list[float | None] = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            day = row.get("trade_date") or ""
            if not day or day < SIM_START:
                continue
            try:
                value = float(row.get("volume_ratio"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                value = None
            days.append(day)
            ratios.append(value)
    if days and days[0] > days[-1]:
        days.reverse()
        ratios.reverse()
    return days, ratios


def volume_ratio_label(
    days: list[str],
    ratios: list[float | None],
    entry_day: str,
) -> str:
    """Frozen D1/D2/D3 label for one signal."""
    pos = bisect.bisect_left(days, str(entry_day))
    if pos == 0:
        return "no_data"  # no strictly-prior session row
    current = ratios[pos - 1]
    if current is None:
        return "no_data"
    if current < LOW_EDGE:
        return "low"
    if current >= HIGH_EDGE:
        return "high"
    return "normal"


def attach_volume_ratio_bucket(
    signals: list[dict[str, object]], cache: Path
) -> dict[str, int]:
    """Annotate each signal with its pre-entry volume-ratio bucket."""
    stats: dict[str, int] = {bucket: 0 for bucket in VOLUME_RATIO_BUCKETS}
    stats["attached"] = 0
    books: dict[str, tuple[list[str], list[float | None]] | None] = {}
    for signal in signals:
        code = str(signal["ts_code"])
        if code not in books:
            books[code] = load_volume_ratio_book(cache, code)
        book = books[code]
        if book is None:
            bucket = "no_data"  # missing shard file (prereg D3)
        else:
            days, ratios = book
            bucket = volume_ratio_label(days, ratios,
                                        str(signal["entry_day"]))
        signal["volume_ratio_bucket"] = bucket
        stats[bucket] += 1
        stats["attached"] += 1
    return stats


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
    signals, _sig_stats = build_signals(events, books, index_pairs,
                                        global_days[-1])

    attach_stats = attach_volume_ratio_bucket(signals, cache)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "universe_uncovered_symbols": uncovered,
    }
    print("## 解禁事前量比条件层研究（research_only，非晋级证据）")
    print(f"- 标签=严格早于入场日最后会话的 volume_ratio 快照，固定边界 "
          f"<{LOW_EDGE}/≥{HIGH_EDGE}；成本 {cost_bps}bps 往返")

    tab = cross_tab(signals, cost_bps=cost_bps, key="volume_ratio_bucket",
                    labels=VOLUME_RATIO_BUCKETS)
    results["r1_signal_level_cross_tab"] = tab
    print("\n### R1 信号层四桶交叉表（净 bps / 胜率）")
    print(f"{'bucket':<10} {'n':>6} {'mean_net_bps':>13} {'win_rate':>9}")
    for label, cell in tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"{label:<10} {cell['n']:>6} {mean_txt:>13} {win_txt:>9}")

    rule_signals = [s for s in signals if rule_arm_filter(s)]
    rule_tab = cross_tab(rule_signals, cost_bps=cost_bps,
                         key="volume_ratio_bucket",
                         labels=VOLUME_RATIO_BUCKETS)
    baseline = _baseline_cell(rule_signals, cost_bps)
    results["r2_rule_arm_cross_tab"] = rule_tab
    results["r2_rule_unfiltered_baseline"] = baseline
    print("\n### R2 rule 臂叠加交叉表（弱市×非3–5%带，定义不动）与未滤基线")
    for label, cell in {**rule_tab, "UNFILTERED": baseline}.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"rule[{label:<10}] n={cell['n']:>4} {mean_txt}bps win={win_txt}")

    high_cell = rule_tab.get("high",
                             {"n": 0, "mean_net_bps": None,
                              "win_rate": None})
    eligible = _double_low(high_cell, baseline)
    results["h1_primary_contrast"] = {
        "rule_high": high_cell,
        "rule_unfiltered_baseline": baseline,
        "watch_list_eligible": eligible,
    }
    verdict = "进观察名单" if eligible else "未达标（FAIL 同样是合格产出）"
    print(f"\n### H1 冻结判定：rule 臂 high 对未滤基线双低且 n≥"
          f"{WATCH_LIST_MIN_N} ⇒ {verdict}")

    results["r3_coverage"] = {
        "attached": attach_stats["attached"],
        "bucket_counts": {b: attach_stats[b] for b in VOLUME_RATIO_BUCKETS},
    }
    print("\n### R3 覆盖与健康检查")
    cov = results["r3_coverage"]
    counts = "/".join(str(cov["bucket_counts"][b])
                      for b in VOLUME_RATIO_BUCKETS)
    print(f"- 已贴标 {cov['attached']}/{len(signals)}；覆盖率证据以预注册"
          f"探针收据为准，本引擎不内嵌基准数")
    print(f"- all 桶计数：{counts}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--cost-bps",
                        type=float, default=COST_BPS_ROUNDTRIP_DEFAULT)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    run_study(cache, cost_bps=args.cost_bps)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except VolumeRatioStudyError as exc:
        print(f"VOLUME_RATIO_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(2)
