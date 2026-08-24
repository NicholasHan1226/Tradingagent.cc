"""Unlock supply band rebuild: supply_over_float vs announcement float_ratio.

Implements the FROZEN pre-registration
(``2026-08-24-unlock-supply-bands-preregistration.md``, PR #451) — this
module only realizes the registered definitions; it never changes them.

- Variable: ``supply_over_float = float_ratio / (float_share/total_share)``
  — unlock size relative to the tradable float instead of total shares
  (the announcement ratio arrives in percent and is converted to a
  fraction first, so the result is a clean share-of-float fraction).
  The denominator comes from the daily_basic row on the latest session
  STRICTLY BEFORE entry_day (shared anchor convention; rejected as stale
  when that anchor sits more than ``STALENESS_DAYS`` calendar days back).
- Fixed ex-ante edges: < 0.10 → ``small``, < 0.30 → ``mid``, else
  ``large`` (share-of-float boundaries; chosen before touching the joint
  outcome distribution).
- R1: tercile separation power of the old ``float_ratio`` vs the new
  supply variable over the same labelled set (descriptive, in-sample).
- R2: signal-layer + rule-arm overlay cross-tabs under the new buckets
  (rule arm itself is NOT modified — descriptive only).
- R3: overlap matrix old band (<3 / 3–5 / ≥5) × new bucket counts.
- HV: coverage stats plus the Pearson correlation between the old and new
  variables (registered expectation: positive but clearly below 1).

Population = the #423 lockup sell_off stream.  Cache-only.
research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_unlock_supply_study.py [--cache DIR]
        [--cost-bps X]
"""

from __future__ import annotations

import argparse
import bisect
import csv
import sys
from datetime import datetime
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
    INITIAL_CASH_CNY,
    SIM_START,
    build_signals,
    load_events,
    load_index_series,
    load_stock_books,
    max_drawdown,
    monthly_net_returns,
    rule_arm_filter,
    run_portfolio,
)

SUPPLY_SMALL_EDGE = 0.10     # frozen ex-ante share-of-float boundary
SUPPLY_LARGE_EDGE = 0.30     # frozen ex-ante share-of-float boundary
SUPPLY_BUCKETS = ("small", "mid", "large")
STALENESS_DAYS = 21          # calendar staleness cap on the anchored state


class SupplyStudyError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


def classify_supply(value: float) -> str:
    if value < SUPPLY_SMALL_EDGE:
        return "small"
    if value < SUPPLY_LARGE_EDGE:
        return "mid"
    return "large"


def _circ_series(
    cache: Path, symbols: set[str]
) -> dict[str, tuple[list[str], list[float]]]:
    """Per-symbol ascending (days, float_share/total_share) series."""
    wanted_stems = {f"dailybasic_{code[:6]}{code[7:]}" for code in symbols}
    series: dict[str, tuple[list[str], list[float]]] = {}
    for stem in sorted(wanted_stems):
        path = cache / f"{stem}.csv"
        try:
            handle = path.open(encoding="utf-8")
        except FileNotFoundError:
            continue  # no dailybasic history for this symbol: leave absent
        with handle:
            rows_out: list[tuple[str, float]] = []
            for row in csv.DictReader(handle):
                day = row.get("trade_date")
                try:
                    total = float(row["total_share"])  # type: ignore[arg-type]
                    circ = float(row["float_share"])  # type: ignore[arg-type]
                except (KeyError, TypeError, ValueError):
                    continue
                if total <= 0.0 or circ < 0.0:
                    continue
                rows_out.append((day, circ / total))
            # Tushare files arrive NEWEST-FIRST; bisect below needs ascending.
            rows_out.sort(key=lambda item: item[0])
        if rows_out:
            days = [day for day, _value in rows_out]
            values = [value for _day, value in rows_out]
            pure = stem.removeprefix("dailybasic_")
            series[f"{pure[:6]}.{pure[6:]}"] = (days, values)
    if not series:
        raise SupplyStudyError("dailybasic_cache_missing")
    return series


def attach_supply_states(
    signals: list[dict[str, object]],
    series: dict[str, tuple[list[str], list[float]]],
) -> dict[str, int]:
    """Annotate each signal with its supply_over_float value and bucket."""
    stats = {
        "missing_dailybasic": 0,
        "no_prior_session": 0,
        "stale_supply": 0,
        "bad_ratio": 0,
        "attached": 0,
    }
    for signal in signals:
        code = str(signal["ts_code"])
        book = series.get(code)
        if book is None:
            stats["missing_dailybasic"] += 1
            continue
        days, values = book
        pos = bisect.bisect_left(days, str(signal["entry_day"]))
        if pos == 0:
            stats["no_prior_session"] += 1
            continue
        # latest STRICTLY-prior session; distance discipline comes solely
        # from the calendar staleness cap below.
        anchor_pos = pos - 1
        try:
            entry_date = datetime.strptime(
                str(signal["entry_day"]), "%Y%m%d"
            ).date()
            anchor_date = datetime.strptime(days[anchor_pos], "%Y%m%d").date()
        except ValueError:
            stats["no_prior_session"] += 1
            continue
        if (entry_date - anchor_date).days > STALENESS_DAYS:
            stats["stale_supply"] += 1
            continue
        ratio = signal.get("float_ratio")
        try:
            ratio_f = float(ratio)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            stats["bad_ratio"] += 1
            continue
        if ratio_f <= 0.0 or values[anchor_pos] <= 0.0:
            stats["bad_ratio"] += 1
            continue
        # The announcement feed carries float_ratio in PERCENT; convert to a
        # fraction first so supply_over_float is a clean share-of-float
        # fraction matching the frozen 0.10/0.30 edges.
        supply = (ratio_f / 100.0) / values[anchor_pos]
        signal["supply_over_float"] = supply
        signal["supply_bucket"] = classify_supply(supply)
        stats["attached"] += 1
    return stats


def _fmt_pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def _tercile_means(
    values: list[float],
    rets: list[float],
) -> dict[str, dict[str, float | None]]:
    """In-sample tercile means/wins for one variable over labelled signals.

    Bottom and top terciles drop the remainder rows into the middle so the
    extremes stay equal-sized (registered descriptive convention)."""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    k = n // 3
    if k == 0:
        return {"bottom": None, "top": None}
    def _cell(idx: list[int]) -> dict[str, float | None]:
        cell_rets = [rets[i] for i in idx]
        mean_bps = (sum(cell_rets) / len(cell_rets)) * 1e4 if cell_rets else None
        win = (
            sum(1 for r in cell_rets if r > 0.0) / len(cell_rets)
            if cell_rets else None
        )
        return {"n": len(idx), "mean_net_bps": mean_bps, "win_rate": win}
    return {
        "bottom": _cell(order[:k]),
        "top": _cell(order[n - k:]),
    }


def _old_band(ratio: float) -> str:
    if ratio < 3.0:
        return "<3"
    if ratio < 5.0:
        return "3-5"
    return ">=5"


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

    symbols = {str(s["ts_code"]) for s in signals}
    series = _circ_series(cache, symbols)
    attach_stats = attach_supply_states(signals, series)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "universe_uncovered_symbols": uncovered,
    }
    print("## 解禁供给带重建研究（research_only，非晋级证据）")
    print(f"- 信号总数 {len(signals)}；dailybasic 序列 {len(series)} 只；"
          f"附加统计 {attach_stats}；成本 {cost_bps}bps 往返；"
          f"固定边界 small <{SUPPLY_SMALL_EDGE}, large ≥{SUPPLY_LARGE_EDGE}"
          f"（占流通盘比例；事前最近会话锚点，陈旧上限 "
          f"{STALENESS_DAYS} 自然日）")

    labeled = [s for s in signals if s.get("supply_bucket") in SUPPLY_BUCKETS]

    # R1: separation power, old variable vs new variable (same labelled set).
    from Ashare.event_margin_crowding_state import net_trade_return as _ntr

    rets = [_ntr(s, cost_bps) for s in labeled]
    old_vals = [float(s["float_ratio"]) for s in labeled]  # type: ignore[arg-type]
    new_vals = [float(s["supply_over_float"]) for s in labeled]  # type: ignore[arg-type]
    r1_old = _tercile_means(old_vals, rets)
    r1_new = _tercile_means(new_vals, rets)
    results["r1_terciles"] = {"float_ratio": r1_old, "supply_over_float": r1_new}
    print("\n### R1 分离力对比（样本内三分位，顶/底组净 bps 与胜率）")
    for name, cell in (("float_ratio(旧)", r1_old),
                       ("supply_over_float(新)", r1_new)):
        if cell["bottom"] is None or cell["top"] is None:
            print(f"- {name}: 样本不足")
            continue
        bot, top = cell["bottom"], cell["top"]
        spread = (
            float(top["mean_net_bps"]) - float(bot["mean_net_bps"])
            if top["mean_net_bps"] is not None
            and bot["mean_net_bps"] is not None else None
        )
        # Precompute cells: nested same-quote f-strings break py3.11 CI.
        bot_bps = (
            "—" if bot["mean_net_bps"] is None
            else f"{float(bot['mean_net_bps']):+.1f}"
        )
        top_bps = (
            "—" if top["mean_net_bps"] is None
            else f"{float(top['mean_net_bps']):+.1f}"
        )
        spread_txt = "—" if spread is None else f"{spread:+.1f}"
        print(
            f"- {name}: bottom(n={bot['n']}, {bot_bps}bps)"
            f" top(n={top['n']}, {top_bps}bps)"
            f" 价差={spread_txt}bps"
        )

    # R2: fixed-edge cross-tabs under the NEW buckets (descriptive only).
    tab = cross_tab(labeled, cost_bps=cost_bps, key="supply_bucket",
                    labels=SUPPLY_BUCKETS)
    results["r2_signal_level_cross_tab"] = tab
    print("\n### R2 新固定边界交叉表（净 bps / 胜率；rule 臂定义不动）")
    print(f"{'bucket':<7} {'n':>6} {'mean_net_bps':>13} {'win_rate':>9}")
    for label, cell in tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        print(
            f"{label:<7} {cell['n']:>6} "
            f"{'—' if mean_bps is None else f'{float(mean_bps):+.1f}':>13} "
            f"{'—' if win is None else f'{float(win):.3f}':>9}"
        )
    rule_labeled = [s for s in signals
                    if rule_arm_filter(s)
                    and s.get("supply_bucket") in SUPPLY_BUCKETS]
    rule_tab = cross_tab(rule_labeled, cost_bps=cost_bps,
                         key="supply_bucket", labels=SUPPLY_BUCKETS)
    results["r2_rule_arm_cross_tab"] = rule_tab
    for label, cell in rule_tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        print(
            f"rule[{label:<6}] n={cell['n']:>4} "
            f"{'—' if mean_bps is None else f'{float(mean_bps):+.1f}'}bps "
            f"{'—' if win is None else f'win={float(win):.3f}'}"
        )

    # R3: old-band × new-bucket overlap matrix.
    overlap: dict[str, dict[str, int]] = {}
    for s in labeled:
        ob = _old_band(float(s["float_ratio"]))  # type: ignore[arg-type]
        nb = str(s["supply_bucket"])
        overlap.setdefault(ob, {}).setdefault(nb, 0)
        overlap[ob][nb] += 1
    results["r3_overlap_matrix"] = overlap
    print("\n### R3 重叠矩阵（旧行 × 新列 计数）")
    for ob in ("<3", "3-5", ">=5"):
        row = overlap.get(ob, {})
        cells = " ".join(f"{nb}:{row.get(nb, 0)}" for nb in SUPPLY_BUCKETS)
        print(f"- {ob:>4} -> {cells}")

    # HV: coverage + old/new correlation (registered: positive, clearly <1).
    try:
        n = len(new_vals)
        mean_o = sum(old_vals) / n
        mean_n = sum(new_vals) / n
        cov = sum((o - mean_o) * (nv - mean_n)
                  for o, nv in zip(old_vals, new_vals))
        var_o = sum((o - mean_o) ** 2 for o in old_vals)
        var_n = sum((nv - mean_n) ** 2 for nv in new_vals)
        corr = cov / ((var_o * var_n) ** 0.5) if var_o > 0 and var_n > 0 else None
        results["hv_correlation"] = {
            "n": n,
            "pearson_old_vs_new": corr,
            "verdict": (
                "方向正确（正相关且明显<1，口径确实不同）"
                if corr is not None and 0.0 < corr < 0.9
                else "需核查口径！"
            ),
        }
        print("\n### HV 数据质量：新旧变量相关系数")
        print(f"- n={n} pearson="
              f"{'—' if corr is None else f'{corr:.3f}'} "
              f"(预注册预期：正相关且 <0.9)")
    except Exception as exc:  # noqa: BLE001 - validation is auxiliary
        results["hv_correlation_error"] = str(exc)
        print(f"\nHV 相关性不可用：{exc}")

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
    except SupplyStudyError as exc:
        print(f"SUPPLY_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
