"""Pre-lockup branch-seat (``top_inst``) short-window confirmation study.

Realizes the frozen preregistration
(``Ashare/reports/2026-08-24-topinst-prelockup-preregistration.md``,
merged BEFORE any bucket-return computation).  D1 window is
``[entry-30 natural days, entry)`` (holdertrade family convention).
D2 classifies seat identity by exact exalter rules — ``机构专用`` =
inst, names containing ``股通专用`` = connect, everything else =
branch — and reads direction ONLY as the window sum of institutional
net_buy; connect/branch rows never enter v1 direction reading (they
still mark listing presence).  Four mutually exclusive buckets per
signal: inst_netbuy / inst_netsell / listed_no_inst (includes the
degenerate exact-zero sum) / no_listing.  H1 frozen: signal-layer
inst_netbuy double-high vs inst_netsell.  The rule layer is
pre-declared unreachable (24/357 inst-window coverage) so it stays
descriptive — the pre-registered baseline case.  D3 zero imputation;
missing cache fails closed.  D4 locked baseline after-caliber engine,
15bps roundtrip.  research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_topinst_prelockup_study.py [--cache DIR]
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
from Ashare.event_holdertrade_prelockup_study import _parse_day  # noqa: E402
from Ashare.event_topinst_fetch import TOPINST_DIRNAME  # noqa: E402

PRE_WINDOW_DAYS = 30  # frozen lookback, holdertrade family convention
TOPINST_BUCKETS = ("inst_netbuy", "inst_netsell", "listed_no_inst", "no_listing")


class TopinstStudyError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


def classify_seat(exalter: object) -> str:
    """Frozen D2 seat-identity mapping on the raw Chinese branch name."""
    name = str(exalter or "")
    if name == "机构专用":
        return "inst"
    if "股通专用" in name:
        return "connect"
    return "branch"


def _inst_net_buy(row: dict[str, str]) -> float:
    """net_buy parsed conservatively: unparseable contributes 0.0."""
    try:
        return float(str(row.get("net_buy") or "").strip())
    except ValueError:
        return 0.0


def load_topinst_index(
    cache: Path,
) -> dict[str, tuple[list[str], list[float]]]:
    """Per-symbol ascending (listing_days, inst net_buy day-sums) index.

    Files are one trading day each (``topinst_daily/<YYYYMMDD>.csv``,
    full sweep); the filename day is authoritative — the fetcher already
    validated row-level trade_date and exalter.  Every kept row marks a
    listing day for its symbol; only INST rows contribute to the
    per-day net-buy sum.
    """
    folder = cache / TOPINST_DIRNAME
    files = sorted(folder.glob("*.csv")) if folder.is_dir() else []
    if not files:
        raise TopinstStudyError("topinst_cache_missing")
    days_by_code: dict[str, list[str]] = {}
    sums_by_code: dict[str, list[float]] = {}
    for path in files:
        day = _parse_day(path.stem)
        if day is None:
            continue
        try:
            handle = path.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            slot: dict[str, int] = {}
            for row in csv.DictReader(handle):
                code = row.get("ts_code") or ""
                if not code:
                    continue
                if code not in slot:
                    slot[code] = len(days_by_code.setdefault(code, []))
                    days_by_code[code].append(day)
                    sums_by_code.setdefault(code, []).append(0.0)
                if classify_seat(row.get("exalter")) == "inst":
                    sums_by_code[code][slot[code]] += _inst_net_buy(row)
    index: dict[str, tuple[list[str], list[float]]] = {}
    for code, days in days_by_code.items():
        order = sorted(range(len(days)), key=lambda i: days[i])
        index[code] = (
            [days[i] for i in order],
            [sums_by_code[code][i] for i in order],
        )
    return index


def _window_bucket(
    book: tuple[list[str], list[float]] | None, entry_day: str
) -> tuple[str | None, int, float]:
    """(frozen bucket label, listed-day count, Σ inst net_buy).

    Window is ``[entry-30 natural days, entry)`` — start inclusive,
    entry day strictly excluded.  A positive/negative window sum labels
    inst_netbuy / inst_netsell; an exact-zero sum falls to
    listed_no_inst together with inst-free listings.  Empty windows
    label ``no_listing`` via :func:`attach_topinst_states`.
    """
    if book is None:
        return None, 0, 0.0
    try:
        entry_date = datetime.strptime(entry_day, "%Y%m%d").date()
    except ValueError:
        return None, 0, 0.0
    days, sums = book
    window_start = (
        entry_date - timedelta(days=PRE_WINDOW_DAYS)).strftime("%Y%m%d")
    lo = bisect.bisect_left(days, window_start)
    hi = bisect.bisect_left(days, entry_day)  # strictly prior to entry
    window_sums = sums[lo:hi]
    if not window_sums:
        return None, 0, 0.0
    total = sum(window_sums)
    if total > 0:
        label: str = "inst_netbuy"
    elif total < 0:
        label = "inst_netsell"
    else:
        label = "listed_no_inst"
    return label, hi - lo, total


def attach_topinst_states(
    signals: list[dict[str, object]],
    index: dict[str, tuple[list[str], list[float]]],
) -> dict[str, int]:
    """Annotate each signal with its pre-window branch-seat bucket."""
    stats = {bucket: 0 for bucket in TOPINST_BUCKETS}
    stats["attached"] = 0
    for signal in signals:
        code = str(signal["ts_code"])
        entry_day = str(signal["entry_day"])
        label, hits, total = _window_bucket(index.get(code), entry_day)
        bucket = label if label is not None else "no_listing"
        signal["topinst_bucket"] = bucket
        signal["topinst_hits"] = hits
        signal["topinst_inst_netbuy_sum"] = total if label is not None else None
        if label is not None:
            days, _sums = index[code]
            hi = bisect.bisect_left(days, entry_day)
            latest = days[hi - 1]
            lag = (datetime.strptime(entry_day, "%Y%m%d").date()
                   - datetime.strptime(latest, "%Y%m%d").date()).days
            signal["topinst_lag_days"] = lag
        else:
            signal["topinst_lag_days"] = None
        stats[bucket] += 1
        stats["attached"] += 1
    return stats


def topinst_buckets_for_events(
    cache: Path, pairs: list[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    """Side-table helper: (ts_code, entry_day) -> frozen bucket label."""
    index = load_topinst_index(cache)
    labels: dict[tuple[str, str], str] = {}
    for code, entry_day in pairs:
        label, _hits, _total = _window_bucket(index.get(code), entry_day)
        labels[(code, entry_day)] = (
            label if label is not None else "no_listing"
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

    index = load_topinst_index(cache)
    attach_stats = attach_topinst_states(signals, index)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "universe_uncovered_symbols": uncovered,
    }
    print("## 解禁事前营业部席位短窗确认层研究（research_only，非晋级证据）")
    print(f"- 信号总数 {len(signals)}；窗口 [entry−{PRE_WINDOW_DAYS} 自然日,"
          f" entry)；席位身份冻结规则 机构专用→inst / 含股通专用→connect / "
          f"其余→branch；方向只取窗口内 inst 行 net_buy Σ；成本 "
          f"{cost_bps}bps 往返")

    tab = cross_tab(signals, cost_bps=cost_bps, key="topinst_bucket",
                    labels=TOPINST_BUCKETS)
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
                         key="topinst_bucket", labels=TOPINST_BUCKETS)
    results["r2_rule_arm_cross_tab"] = rule_tab
    results["h1_primary_contrast"] = {
        "inst_netbuy_signal_layer": tab.get("inst_netbuy"),
        "inst_netsell_signal_layer": tab.get("inst_netsell"),
    }
    print("\n### R2 rule 臂叠加交叉表（弱市×非3–5%带，定义不动）"
          "——各桶已知大概率 n<30（预注册预告基准情形），按预注册降为描述性")
    for label, cell in rule_tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"rule[{label:<13}] n={cell['n']:>4} {mean_txt}bps win={win_txt}")
    print("- H1 主对比（冻结，信号层）：inst_netbuy 对 inst_netsell 均值与胜率"
          "是否双高——判定按预注册标准执行，此处只呈现数字")

    lags = sorted(
        int(s["topinst_lag_days"])
        for s in signals
        if s.get("topinst_lag_days") is not None
    )
    multi = sum(1 for s in signals if int(s.get("topinst_hits") or 0) > 1)
    with_seats = (
        attach_stats.get("inst_netbuy", 0)
        + attach_stats.get("inst_netsell", 0)
        + attach_stats.get("listed_no_inst", 0)
    )
    with_direction = (
        attach_stats.get("inst_netbuy", 0) + attach_stats.get("inst_netsell", 0)
    )
    results["r3_coverage"] = {
        "with_seat_rows": with_seats,
        "with_inst_directional": with_direction,
        "files_in_cache": len(index),
        "lag_days_mean": (sum(lags) / len(lags)) if lags else None,
        "lag_days_max": lags[-1] if lags else None,
        "multi_hit_signals": multi,
    }
    print("\n### R3/HV 覆盖与健康检查")
    print(f"- 有席位行信号合计：{with_seats}/{len(signals)}"
          f"（预注册计数基准 130）；其中机构行方向分桶计数 "
          f"{with_direction}（预注册「含 inst 行」基准 80；差值＝窗口 Σ 恰为 "
          f"0 的退化并入 listed_no_inst）；缓存符号数 {len(index)}；"
          f"窗口内最近上榜陈旧度均值 "
          f"{results['r3_coverage']['lag_days_mean']} 自然日 / 最大 "
          f"{results['r3_coverage']['lag_days_max']}；多日命中信号 {multi}")
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
    except TopinstStudyError as exc:
        print(f"TOPINST_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
