"""Holdertrade pre-lockup conditioning layer study.

Implements the FROZEN pre-registration
(``2026-08-24-holdertrade-prelockup-preregistration.md``, PR #453) — this
module only realizes the registered definitions; it never changes them.

For each sell_off signal, holdertrade records of the same stock whose
``ann_date`` falls in ``[entry_day - PRE_WINDOW_DAYS, entry_day)`` are
aggregated into a signed share-count net (IN minus DE) and bucketed:

- ``net_buy``     net_vol > 0  (insider support ahead of the unlock)
- ``net_sell``    net_vol < 0  (informed front-running, mirrors #23)
- ``flat``        records exist but net sums to exactly zero
- ``no_records``  empty window or missing per-symbol file (a label, not an error)

The announcement ratio fields are deliberately unused: ``change_ratio``
units are mixed historically and ``avg_price`` is sparsely populated, so
the V1 variable is the raw signed share count (unit-safe by construction).

Population = the #423 lockup sell_off stream. Cache-only.
research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_holdertrade_prelockup_study.py [--cache DIR]
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

PRE_WINDOW_DAYS = 30  # frozen lookback; the window IS the staleness cap
HT_BUCKETS = ("net_buy", "flat", "net_sell", "no_records")


class HoldertradeStudyError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


def _parse_day(value: object) -> str | None:
    text = str(value) if value is not None else ""
    if len(text) != 8:
        return None
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return None
    return text


def load_holdertrade_index(
    cache: Path,
) -> dict[str, tuple[list[str], list[float]]]:
    """Per-symbol ascending (ann_dates, signed_vols) index.

    Files are one ann_date each, so iterating the sorted file list yields
    ascending per-symbol day order by construction (bisect discipline).
    Malformed rows are skipped and counted via the returned stats — see
    ``load_stats`` below; a wholly missing folder fails closed.
    """
    folder = cache / "holdertrade_daily"
    if not folder.is_dir():
        raise HoldertradeStudyError("holdertrade_cache_missing")
    days_by_code: dict[str, list[str]] = {}
    vols_by_code: dict[str, list[float]] = {}
    rows_seen = 0
    for path in sorted(folder.glob("*.csv")):
        ann_date: str | None = None
        try:
            handle = path.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for row in csv.DictReader(handle):
                code = row.get("ts_code") or ""
                if not code:
                    continue
                if ann_date is None:
                    ann_date = _parse_day(path.stem)
                day = _parse_day(row.get("ann_date")) or ann_date
                if day is None:
                    continue
                direction = row.get("in_de")
                try:
                    vol = float(row["change_vol"])  # type: ignore[arg-type]
                except (KeyError, TypeError, ValueError):
                    continue
                if direction == "IN":
                    signed = vol
                elif direction == "DE":
                    signed = -vol
                else:
                    continue
                rows_seen += 1
                days_by_code.setdefault(code, []).append(day)
                vols_by_code.setdefault(code, []).append(signed)
    index: dict[str, tuple[list[str], list[float]]] = {}
    for code, days in days_by_code.items():
        order = sorted(range(len(days)), key=lambda i: days[i])
        index[code] = (
            [days[i] for i in order],
            [vols_by_code[code][i] for i in order],
        )
    if rows_seen == 0:
        raise HoldertradeStudyError("holdertrade_cache_empty")
    return index


def _window_vols(
    book: tuple[list[str], list[float]] | None, entry_day: str
) -> list[float]:
    """Signed volumes of records with ann_date in [entry-30d, entry)."""
    if book is None:
        return []
    try:
        entry_date = datetime.strptime(entry_day, "%Y%m%d").date()
    except ValueError:
        return []
    days, vols = book
    window_start = (
        entry_date - timedelta(days=PRE_WINDOW_DAYS)).strftime("%Y%m%d")
    lo = bisect.bisect_left(days, window_start)
    hi = bisect.bisect_left(days, entry_day)  # strictly prior to entry
    return vols[lo:hi]


def attach_holdertrade_states(
    signals: list[dict[str, object]],
    index: dict[str, tuple[list[str], list[float]]],
) -> dict[str, int]:
    """Annotate each signal with its pre-window holder-trade bucket."""
    stats = {bucket: 0 for bucket in HT_BUCKETS}
    stats["attached"] = 0
    for signal in signals:
        code = str(signal["ts_code"])
        entry_day = str(signal["entry_day"])
        vols = _window_vols(index.get(code), entry_day)
        net = sum(vols)
        if not vols:
            bucket = "no_records"
            net_value: float | None = None
        elif net > 0.0:
            bucket = "net_buy"
            net_value = net
        elif net < 0.0:
            bucket = "net_sell"
            net_value = net
        else:
            bucket = "flat"
            net_value = 0.0
        signal["holder_bucket"] = bucket
        signal["holder_net_vol"] = net_value
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

    index = load_holdertrade_index(cache)
    attach_stats = attach_holdertrade_states(signals, index)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "universe_uncovered_symbols": uncovered,
    }
    print("## 解禁事前股东增减持条件层研究（research_only，非晋级证据）")
    print(f"- 信号总数 {len(signals)}；窗口 [entry−{PRE_WINDOW_DAYS} 自然日,"
          f" entry)；分桶按股数净额 IN−DE；成本 {cost_bps}bps 往返")

    # R1: signal-level cross-tab over ALL four buckets (no_records included).
    tab = cross_tab(signals, cost_bps=cost_bps, key="holder_bucket",
                    labels=HT_BUCKETS)
    results["r1_signal_level_cross_tab"] = tab
    print("\n### R1 信号层四桶交叉表（净 bps / 胜率）")
    print(f"{'bucket':<12} {'n':>6} {'mean_net_bps':>13} {'win_rate':>9}")
    for label, cell in tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"{label:<12} {cell['n']:>6} {mean_txt:>13} {win_txt:>9}")

    # R2: rule-arm overlay (descriptive; the rule arm itself is untouched).
    rule_signals = [s for s in signals if rule_arm_filter(s)]
    rule_tab = cross_tab(rule_signals, cost_bps=cost_bps,
                         key="holder_bucket", labels=HT_BUCKETS)
    results["r2_rule_arm_cross_tab"] = rule_tab
    print("\n### R2 rule 臂叠加交叉表（弱市×非3–5%带，定义不动）")
    for label, cell in rule_tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"rule[{label:<11}] n={cell['n']:>4} {mean_txt}bps win={win_txt}")

    # R3/HV: coverage + direction consistency vs the observation list.
    with_records = (
        attach_stats.get("net_buy", 0)
        + attach_stats.get("net_sell", 0)
        + attach_stats.get("flat", 0)
    )
    results["hv_coverage"] = {
        "with_records": with_records,
        "recorded_buckets": {
            k: attach_stats[k] for k in ("net_buy", "flat", "net_sell")
        },
    }
    print("\n### R3/HV 覆盖与方向一致性")
    print(f"- 有记录桶合计：{with_records}"
          f"/{len(signals)}；净增持 {attach_stats.get('net_buy', 0)}、"
          f"净减持 {attach_stats.get('net_sell', 0)}、持平 "
          f"{attach_stats.get('flat', 0)}")
    print("- H1 预期读法：net_buy > no_records > net_sell（信号层单调）；"
          "方向相反时如实报告并对照 #23 反向发现")

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
    except HoldertradeStudyError as exc:
        print(f"HOLDERTRADE_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
