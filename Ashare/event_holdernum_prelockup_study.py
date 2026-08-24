"""Pre-lockup shareholder-count conditioning study (holdernumber).

Realizes the frozen preregistration
(``Ashare/reports/2026-08-24-holdernum-prelockup-preregistration.md``,
merged BEFORE any bucket-return computation).  D1 anchor window is
``[entry-365 natural days, entry)`` on ``ann_date`` (disclosure day =
information-visible day; no look-ahead).  The anchor row is the latest
disclosure in the window; legal multi-period disclosures sharing one
ann_date (measured 14,410 groups, zero same-key conflicts) break ties
by the LATEST end_date, then the larger holder_num (degenerate fallback,
never observed).  The comparison row is the immediately preceding
non-null snapshot under the same tie-break chain and must also lie in
the window.  D2 buckets at fixed +/-5.0 pct change (prior attention
line, not data-mined quantiles): contract / stable / expand /
no_snapshot (<2 qualifying snapshots or non-positive comparison row).
H1 frozen: signal-layer contract double-high vs expand (pre-concentrated
strong hands vs thin retail absorption).  D3 zero imputation — empty
holder_num rows are not snapshots at all; missing cache fails closed.
D4 locked baseline after-caliber engine, 15bps roundtrip.  Panel #12.
research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_holdernum_prelockup_study.py [--cache DIR]
        [--cost-bps N]
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

ANCHOR_WINDOW_DAYS = 365  # frozen lookback cap on disclosure recency
CHANGE_BOUND_PCT = 5.0  # fixed +/- boundary, prior attention line
HOLDERNUM_BUCKETS = ("contract", "stable", "expand", "no_snapshot")
_HOLDERNUM_PREFIX = "holdernum_"


class HolderNumStudyError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


def _parse_holder_num(raw: object) -> float | None:
    try:
        value = float(str(raw or "").strip())
    except ValueError:
        return None
    return value


def _dedupe_by_announcement(
    rows: list[tuple[str, str, float]],
) -> list[tuple[str, str, float]]:
    """Collapse same-ann_date multi-period disclosures into one snapshot.

    Rows must be ascending by ``(ann_date, end_date, holder_num)``;
    keeping the LAST row of each ann_date group realizes the frozen
    tie-break (latest end_date, then larger count).  One announcement =
    one observation, mirroring the pledge panel's one-snapshot logic.
    """
    deduped: list[tuple[str, str, float]] = []
    for row in rows:
        if not deduped or deduped[-1][0] != row[0]:
            deduped.append(row)
        else:
            deduped[-1] = row
    return deduped


def load_holdernum_index(
    cache: Path,
) -> dict[str, list[tuple[str, str, float]]]:
    """Per-symbol ANNOUNCEMENT snapshots in frozen tie-break order.

    Rows are ``(ann_date, end_date, holder_num)`` tuples with NON-EMPTY
    holder_num only (empty disclosures are not snapshots).  Same-day
    multi-period disclosures collapse to one snapshot (latest end_date,
    then larger count); ascending order by ``(ann_date, ...)`` puts the
    window anchor last.
    """
    files = sorted(cache.glob(f"{_HOLDERNUM_PREFIX}*.csv"))
    if not files:
        raise HolderNumStudyError("holdernum_cache_missing")
    rows_by_code: dict[str, list[tuple[str, str, float]]] = {}
    for path in files:
        code = path.stem.removeprefix(_HOLDERNUM_PREFIX)
        # filename stem layout matches daily_<SYMBOL> stems (e.g.
        # 000001SZ); re-dot it into a ts_code-shaped key.
        if len(code) > 6 and "." not in code:
            code = f"{code[:6]}.{code[6:]}"
        rows: list[tuple[str, str, float]] = rows_by_code.setdefault(code, [])
        try:
            handle = path.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for row in csv.DictReader(handle):
                ann = str(row.get("ann_date") or "").strip()
                end = str(row.get("end_date") or "").strip()
                num = _parse_holder_num(row.get("holder_num"))
                if len(ann) != 8 or num is None:
                    continue
                rows.append((ann, end if len(end) == 8 else ann, num))
    for rows in rows_by_code.values():
        rows.sort(key=lambda r: (r[0], r[1], r[2]))
        rows[:] = _dedupe_by_announcement(rows)
    return rows_by_code


def _window_bucket(
    book: list[tuple[str, str, float]] | None, entry_day: str
) -> tuple[str | None, float | None]:
    """(frozen bucket label, anchor pct-change vs preceding announcement).

    Window is ``[entry-365 natural days, entry)`` on ann_date, start
    inclusive, entry day strictly excluded.  Fewer than two qualifying
    snapshots, or a non-positive comparison row, label no_snapshot via
    :func:`attach_holdernum_states`.  The book is defensively re-sorted
    AND announcement-deduped first — an unsorted book would silently
    misplace every bisect (blocktrade #23 family lesson), and raw
    multi-period rows must collapse exactly as in the loader so every
    entry point shares one frozen semantic.
    """
    if not book:
        return None, None
    book = _dedupe_by_announcement(
        sorted(book, key=lambda r: (r[0], r[1], r[2]))
    )
    try:
        entry_date = datetime.strptime(entry_day, "%Y%m%d").date()
    except ValueError:
        return None, None
    window_start = (
        entry_date - timedelta(days=ANCHOR_WINDOW_DAYS)).strftime("%Y%m%d")
    days = [r[0] for r in book]
    lo = bisect.bisect_left(days, window_start)
    hi = bisect.bisect_left(days, entry_day)  # strictly prior to entry
    eligible = book[lo:hi]
    if len(eligible) < 2:
        return None, None
    anchor_ann, _anchor_end, anchor_num = eligible[-1]
    _prev_ann, _prev_end, prev_num = eligible[-2]
    if prev_num <= 0:
        return None, None
    change = 100.0 * (anchor_num - prev_num) / prev_num
    del anchor_ann
    if change <= -CHANGE_BOUND_PCT:
        label: str = "contract"
    elif change >= CHANGE_BOUND_PCT:
        label = "expand"
    else:
        label = "stable"
    return label, change


def attach_holdernum_states(
    signals: list[dict[str, object]],
    index: dict[str, list[tuple[str, str, float]]],
) -> dict[str, int]:
    """Annotate each signal with its pre-window shareholder-count bucket."""
    stats = {bucket: 0 for bucket in HOLDERNUM_BUCKETS}
    stats["attached"] = 0
    for signal in signals:
        code = str(signal["ts_code"])
        entry_day = str(signal["entry_day"])
        label, change = _window_bucket(index.get(code), entry_day)
        bucket = label if label is not None else "no_snapshot"
        signal["holdernum_bucket"] = bucket
        signal["holdernum_change_pct"] = change
        if label is not None:
            book = index[code]
            days = [r[0] for r in book]
            hi = bisect.bisect_left(days, entry_day)
            anchor_ann = book[hi - 1][0]
            lag = (datetime.strptime(entry_day, "%Y%m%d").date()
                   - datetime.strptime(anchor_ann, "%Y%m%d").date()).days
            signal["holdernum_anchor_lag_days"] = lag
        else:
            signal["holdernum_anchor_lag_days"] = None
        stats[bucket] += 1
        stats["attached"] += 1
    return stats


def holdernum_buckets_for_events(
    cache: Path, pairs: list[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    """Side-table helper: (ts_code, entry_day) -> frozen bucket label."""
    index = load_holdernum_index(cache)
    labels: dict[tuple[str, str], str] = {}
    for code, entry_day in pairs:
        label, _change = _window_bucket(index.get(code), entry_day)
        labels[(code, entry_day)] = (
            label if label is not None else "no_snapshot"
        )
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

    index = load_holdernum_index(cache)
    attach_stats = attach_holdernum_states(signals, index)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "universe_uncovered_symbols": uncovered,
    }
    print("## 解禁事前股东户数变化条件层研究（research_only，非晋级证据）")
    print(f"- 信号总数 {len(signals)}；锚窗 [entry−{ANCHOR_WINDOW_DAYS} 自然日,"
          f" entry) 按 ann_date；同日多期披露取 end_date 最新；变化率固定边界 "
          f"±{CHANGE_BOUND_PCT}%；成本 {cost_bps}bps 往返")

    tab = cross_tab(signals, cost_bps=cost_bps, key="holdernum_bucket",
                    labels=HOLDERNUM_BUCKETS)
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
                         key="holdernum_bucket", labels=HOLDERNUM_BUCKETS)
    results["r2_rule_arm_cross_tab"] = rule_tab
    results["h1_primary_contrast"] = {
        "contract_signal_layer": tab.get("contract"),
        "expand_signal_layer": tab.get("expand"),
        "rule_contract": rule_tab.get("contract"),
        "rule_expand": rule_tab.get("expand"),
    }
    print("\n### R2 rule 臂叠加交叉表（弱市×非3–5%带，定义不动）——高覆盖面板，"
          "各桶预期 n≥30 可达，判定按冻结标准执行")
    for label, cell in rule_tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"rule[{label:<11}] n={cell['n']:>4} {mean_txt}bps win={win_txt}")
    print("- H1 主对比（冻结，信号层）：contract 对 expand 均值与胜率是否双高；"
          "观察名单门槛在规则臂：某桶相对未滤 rule 均值+胜率双高且 n≥30")

    lags = sorted(
        int(s["holdernum_anchor_lag_days"])
        for s in signals
        if s.get("holdernum_anchor_lag_days") is not None
    )
    changes = sorted(
        float(s["holdernum_change_pct"])
        for s in signals
        if s.get("holdernum_change_pct") is not None
    )
    def _q(p: float) -> float | None:
        if not changes:
            return None
        return changes[min(int(p * (len(changes) - 1)), len(changes) - 1)]
    results["r3_coverage"] = {
        "with_snapshots": sum(
            1 for b in ("contract", "stable", "expand")
            if b in attach_stats for _ in range(attach_stats[b])
        ),
        "symbols_in_cache": len(index),
        "anchor_lag_mean": (sum(lags) / len(lags)) if lags else None,
        "anchor_lag_max": lags[-1] if lags else None,
        "change_p10": _q(0.10),
        "change_median": _q(0.50),
        "change_p90": _q(0.90),
    }
    print("\n### R3/HV 覆盖与健康检查")
    print(f"- 有双快照标签信号：{results['r3_coverage']['with_snapshots']}/"
          f"{len(signals)}（预注册基准 1035）；缓存符号数 {len(index)}"
          f"（预注册基准 975）；锚行陈旧度均值 "
          f"{results['r3_coverage']['anchor_lag_mean']} 自然日 / 最大 "
          f"{results['r3_coverage']['anchor_lag_max']}（预注册基准 "
          f"51 / 178）；变化率 p10/中位/p90 = {_q(0.10)} / {_q(0.50)} / "
          f"{_q(0.90)}（预注册基准 −13.1 / −0.4 / +25.9）")
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
    except HolderNumStudyError as exc:
        print(f"HOLDERNUM_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
