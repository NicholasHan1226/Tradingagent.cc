"""Pre-unlock price-structure (trailing 63-session return) layer (#15).

Frozen preregistration: ``Ashare/reports/2026-08-25-pricestruct-runup-
prelockup-preregistration.md`` (merged before this engine; no bucketed
returns were computed before that merge).  Zero new data dependency:
labels are computed directly on the family's adjusted-close series
(``load_stock_books``), strictly before each entry:

  pos  = bisect_left(book.days, entry_day)      # entry day excluded
  r63  = closes[pos-1] / closes[pos-64] - 1     # needs pos >= 64

Fixed-boundary buckets (mutually exclusive by construction):

  runup_pos     : r63 > 0        (gain-realization window)
  drift_down    : -20% < r63 <= 0
  deep_dd       : r63 <= -20%
  short_history : fewer than 64 sessions before entry (2018 cache-origin
                  artifact; reference-only row)

H1 (frozen, negative): rule[runup_pos] mean & win-rate both BELOW the
unfiltered rule arm.  research_only / not_promotion_evidence.
"""

from __future__ import annotations

import argparse
import sys
from bisect import bisect_left
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
    BaselineSimError,
    build_signals,
    load_events,
    load_stock_books,
    rule_arm_filter,
)

RUNUP_BUCKETS = ("runup_pos", "drift_down", "deep_dd", "short_history")
WINDOW_SESSIONS = 64  # positions back; spans 63 return intervals
DEEP_DD_LINE = -0.20  # fixed boundary, not sample quantile
WATCH_LIST_MIN_N = 30  # family-standard gate, frozen in the prereg


class PriceStructureStudyError(RuntimeError):
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
    """Frozen H1 verdict shape (same gate family as panels #13/#14)."""
    return bool(
        cell["n"] >= WATCH_LIST_MIN_N
        and cell["mean_net_bps"] is not None
        and baseline["mean_net_bps"] is not None
        and float(cell["mean_net_bps"]) < float(baseline["mean_net_bps"])
        and float(cell["win_rate"]) < float(baseline["win_rate"])
    )


def _load_books(cache: Path) -> tuple[dict, int]:
    """load_stock_books with the failure surfaced under this module's type."""
    try:
        return load_stock_books(cache)
    except BaselineSimError as exc:
        raise PriceStructureStudyError(str(exc)) from exc


def _r63_label(
    book, entry_day: str
) -> tuple[str, float | None]:
    """Frozen D1/D2 arithmetic for one entry against one symbol book."""
    pos = bisect_left(book.days, str(entry_day))
    if pos < WINDOW_SESSIONS:
        return "short_history", None
    value = book.closes[pos - 1] / book.closes[pos - WINDOW_SESSIONS] - 1.0
    if value > 0.0:
        return "runup_pos", value
    if value > DEEP_DD_LINE:
        return "drift_down", value
    return "deep_dd", value


def attach_runup_bucket(
    signals: list[dict[str, object]],
    books: dict,
) -> dict[str, int]:
    """Annotate each signal with its pre-entry trailing-return bucket."""
    stats: dict[str, int] = {bucket: 0 for bucket in RUNUP_BUCKETS}
    stats["attached"] = 0
    for signal in signals:
        code = str(signal["ts_code"])
        book = books.get(code)
        if book is None:
            # Signals whose symbol never loaded a bar file count as
            # short_history (probe-consistent), not an error.
            bucket, value = "short_history", None
        else:
            bucket, value = _r63_label(book, str(signal["entry_day"]))
        signal["runup_bucket"] = bucket
        signal["r63_value"] = value
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
    books, uncovered = _load_books(cache)
    signals, _sig_stats = build_signals(events, books, index_pairs, global_days[-1])

    attach_stats = attach_runup_bucket(signals, books)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "universe_uncovered_symbols": uncovered,
    }
    print("## 解禁事前价格结构条件层研究（research_only，非晋级证据）")
    print(f"- r63 = 入场前最后会话 ÷ 前 64 个位置收盘 − 1（复权口径，entry 日"
          f"严格排除）；固定边界 >0 / (−20%,0] / ≤−20%；成本 {cost_bps}bps 往返")

    tab = cross_tab(signals, cost_bps=cost_bps, key="runup_bucket",
                    labels=RUNUP_BUCKETS)
    results["r1_signal_level_cross_tab"] = tab
    print("\n### R1 信号层四桶交叉表（净 bps / 胜率）")
    print(f"{'bucket':<14} {'n':>6} {'mean_net_bps':>13} {'win_rate':>9}")
    for label, cell in tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"{label:<14} {cell['n']:>6} {mean_txt:>13} {win_txt:>9}")

    rule_signals = [s for s in signals if rule_arm_filter(s)]
    rule_tab = cross_tab(rule_signals, cost_bps=cost_bps,
                         key="runup_bucket", labels=RUNUP_BUCKETS)
    baseline = _baseline_cell(rule_signals, cost_bps)
    results["r2_rule_arm_cross_tab"] = rule_tab
    results["r2_rule_unfiltered_baseline"] = baseline
    print("\n### R2 rule 臂叠加交叉表（弱市×非3–5%带，定义不动）与未滤基线")
    for label, cell in {**rule_tab, "UNFILTERED": baseline}.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"rule[{label:<13}] n={cell['n']:>4} {mean_txt}bps win={win_txt}")

    runup_cell = rule_tab.get("runup_pos",
                              {"n": 0, "mean_net_bps": None, "win_rate": None})
    eligible = _double_low(runup_cell, baseline)
    results["h1_primary_contrast"] = {
        "rule_runup_pos": runup_cell,
        "rule_unfiltered_baseline": baseline,
        "watch_list_eligible": eligible,
    }
    verdict = "进观察名单" if eligible else "未达标（FAIL 同样是合格产出）"
    print(f"\n### H1 冻结判定：rule 臂 runup_pos 对未滤基线双低且 n≥"
          f"{WATCH_LIST_MIN_N} ⇒ {verdict}")

    values = sorted(
        float(s["r63_value"]) for s in signals if s.get("r63_value") is not None
    )
    results["r3_coverage"] = {
        "labeled": len(values),
        "short_history": attach_stats["short_history"],
        "q10": values[int(0.10 * (len(values) - 1))] if values else None,
        "q50": values[len(values) // 2] if values else None,
        "q90": values[int(0.90 * (len(values) - 1))] if values else None,
    }
    print("\n### R3/HV 覆盖与健康检查")
    cov = results["r3_coverage"]
    print(f"- 可标注信号：{cov['labeled']}/{len(signals)}"
          f"（short_history={cov['short_history']}，预注册基准 all "
          f"995/40）；r63 分位 p10/p50/p90 = "
          f"{cov['q10']:+.3f}/{cov['q50']:+.3f}/{cov['q90']:+.3f}"
          f"（预注册基准 −0.256/−0.075/+0.165）")
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
    except PriceStructureStudyError as exc:
        print(f"PRICESTRUCT_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(2)
