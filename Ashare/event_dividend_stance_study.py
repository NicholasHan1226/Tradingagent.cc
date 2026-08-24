"""Pre-unlock dividend/split stance conditioning layer (panel #14).

Frozen preregistration: ``Ashare/reports/2026-08-25-dividend-stance-
prelockup-preregistration.md`` (merged before this engine; no bucketed
returns were computed before that merge).  Distribution is an ANNUAL
slow variable, so the anchor window mirrors the holdernumber panel
(#497): [entry-365 natural days, entry), plan unit = (ts_code, end_date)
aggregated across ALL ``div_proc`` stage rows, winning plan = latest
first disclosure in the window.  Buckets are mutually exclusive by
construction:

  split      : winning plan has any stk_div-bearing row (送转 incl. mixed)
  cash_only  : cash bearing and no split
  no_dist    : all rows zero/empty amounts (不分配 declaration)
  no_records : no plan first disclosure inside the window

H1 (frozen, negative): rule[no_dist] mean & win-rate both BELOW the
unfiltered rule arm.  research_only / not_promotion_evidence.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta
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
from Ashare.event_dividend_fetch import DIVIDEND_DIRNAME  # noqa: E402

DIVIDEND_BUCKETS = ("split", "cash_only", "no_dist", "no_records")
STANCE_WINDOW_DAYS = 365
WATCH_LIST_MIN_N = 30  # family-standard gate, frozen in the prereg


class DividendStudyError(RuntimeError):
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
    """Frozen H1 verdict shape (negative mirror of the macro panel's gate)."""
    return bool(
        cell["n"] >= WATCH_LIST_MIN_N
        and cell["mean_net_bps"] is not None
        and baseline["mean_net_bps"] is not None
        and float(cell["mean_net_bps"]) < float(baseline["mean_net_bps"])
        and float(cell["win_rate"]) < float(baseline["win_rate"])
    )


def _parse_day(day: str) -> datetime:
    return datetime.strptime(day, "%Y%m%d")


def load_dividend_plan_index(cache: Path) -> dict[str, dict[str, list]]:
    """One pass over day shards -> plan-level first-disclosure index.

    Returns {ts_code: {end_date: [min_ann_date, any_split, any_cash]}}.
    Stage rows of paying plans can carry empty amounts (live-verified
    2026-08-25), so bearing flags OR across all rows while ann_date
    keeps its minimum.
    """
    folder = cache / DIVIDEND_DIRNAME
    files = sorted(folder.glob("*.csv"))
    if not files:
        raise DividendStudyError("dividend_cache_missing")
    index: dict[str, dict[str, list]] = {}
    for path in files:
        with path.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                code = row.get("ts_code") or ""
                end_date = row.get("end_date") or ""
                ann = row.get("ann_date") or ""
                if not code or not end_date or not ann:
                    continue  # fetch layer already counted these bad
                try:
                    stk = float(row.get("stk_div") or 0.0)
                    cash = float(row.get("cash_div") or 0.0)
                except ValueError:
                    stk, cash = 0.0, 0.0
                plans = index.setdefault(code, {})
                prev = plans.get(end_date)
                if prev is None:
                    plans[end_date] = [ann, stk > 0, cash > 0]
                    continue
                if ann < prev[0]:
                    prev[0] = ann
                if stk > 0:
                    prev[1] = True
                if cash > 0:
                    prev[2] = True
    return index


def _stance_bucket(
    plans: dict[str, list], entry_day: str
) -> tuple[str, int | None]:
    """Frozen D1/D2 semantics for one entry."""
    entry = _parse_day(entry_day)
    wstart = (entry - timedelta(days=STANCE_WINDOW_DAYS)).strftime("%Y%m%d")
    entry_txt = entry.strftime("%Y%m%d")
    in_win = [
        (ann, split, cash)
        for (ann, split, cash) in plans.values()
        if wstart <= ann < entry_txt
    ]
    if not in_win:
        return "no_records", None
    # Defensive re-sort + latest-plan-wins (unsorted-book lesson, 3rd strike).
    ann, split, cash = max(in_win, key=lambda t: t[0])
    staleness = (entry - _parse_day(ann)).days
    if split:
        return "split", staleness
    if cash:
        return "cash_only", staleness
    return "no_dist", staleness


def attach_dividend_stance(
    signals: list[dict[str, object]],
    index: dict[str, dict[str, list]],
) -> dict[str, int]:
    """Annotate each signal with its pre-window dividend stance bucket."""
    stats = {bucket: 0 for bucket in DIVIDEND_BUCKETS}
    stats["attached"] = 0
    for signal in signals:
        code = str(signal["ts_code"])
        entry_day = str(signal["entry_day"])
        bucket, staleness = _stance_bucket(index.get(code, {}), entry_day)
        signal["dividend_bucket"] = bucket
        signal["dividend_stance_lag_days"] = staleness
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
    signals, _sig_stats = build_signals(events, books, index_pairs, global_days[-1])

    index = load_dividend_plan_index(cache)
    attach_stats = attach_dividend_stance(signals, index)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "universe_uncovered_symbols": uncovered,
    }
    print("## 解禁事前分红送转姿态条件层研究（research_only，非晋级证据）")
    print(f"- 信号总数 {len(signals)}；锚窗 [entry−{STANCE_WINDOW_DAYS} 自然日,"
          f" entry) 按 ann_date；方案级聚合跨全部 div_proc 阶段行；最新首披方案"
          f"胜出；成本 {cost_bps}bps 往返")

    tab = cross_tab(signals, cost_bps=cost_bps, key="dividend_bucket",
                    labels=DIVIDEND_BUCKETS)
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
                         key="dividend_bucket", labels=DIVIDEND_BUCKETS)
    baseline = _baseline_cell(rule_signals, cost_bps)
    results["r2_rule_arm_cross_tab"] = rule_tab
    results["r2_rule_unfiltered_baseline"] = baseline
    print("\n### R2 rule 臂叠加交叉表（弱市×非3–5%带，定义不动）与未滤基线")
    for label, cell in {**rule_tab, "UNFILTERED": baseline}.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"rule[{label:<11}] n={cell['n']:>4} {mean_txt}bps win={win_txt}")

    no_dist_cell = rule_tab.get("no_dist", {"n": 0, "mean_net_bps": None,
                                            "win_rate": None})
    eligible = _double_low(no_dist_cell, baseline)
    results["h1_primary_contrast"] = {
        "rule_no_dist": no_dist_cell,
        "rule_unfiltered_baseline": baseline,
        "watch_list_eligible": eligible,
    }
    verdict = "进观察名单" if eligible else "未达标（FAIL 同样是合格产出）"
    print(f"\n### H1 冻结判定：rule 臂 no_dist 对未滤基线双低且 n≥"
          f"{WATCH_LIST_MIN_N} ⇒ {verdict}")

    lags = sorted(
        int(s["dividend_stance_lag_days"])
        for s in signals
        if s.get("dividend_stance_lag_days") is not None
    )
    results["r3_coverage"] = {
        "with_plan_in_window": len(lags),
        "plans_in_index": sum(len(p) for p in index.values()),
        "symbols_in_cache": len(index),
        "lag_days_mean": (sum(lags) / len(lags)) if lags else None,
        "lag_days_median": lags[len(lags) // 2] if lags else None,
        "lag_days_max": lags[-1] if lags else None,
    }
    print("\n### R3/HV 覆盖与健康检查")
    print(f"- 窗内有方案信号：{results['r3_coverage']['with_plan_in_window']}/"
          f"{len(signals)}（预注册计数基准 987）；索引方案数 "
          f"{results['r3_coverage']['plans_in_index']}；缓存股票 "
          f"{results['r3_coverage']['symbols_in_cache']}；锚方案陈旧度均值 "
          f"{results['r3_coverage']['lag_days_mean']} 自然日 / 中位 "
          f"{results['r3_coverage']['lag_days_median']} / 最大 "
          f"{results['r3_coverage']['lag_days_max']}（预注册基准 89.4/83/247）")
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
    except DividendStudyError as exc:
        print(f"DIVIDEND_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(2)
