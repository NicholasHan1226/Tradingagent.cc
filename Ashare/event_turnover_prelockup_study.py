"""Pre-lockup turnover study: demand-side anomaly around unlock events.

Pre-registered hypotheses (bound to the #440 ``daily_basic`` plane):

- H1: sell_off signals whose PRE-event free-float turnover deviates from
  their own trailing baseline show different post-5d net outcomes.
  Primary direction (ex ante): a turnover SURGE before the unlock reads as
  front-running distribution into the pending supply (negative readout);
  a shrink reads as quiet tape awaiting the overhang (positive readout);
  normal sits between — monotone shrink > normal > surge.
- H2a/H2b: any separation survives conditioning on the margin-state layer
  (#426) and on the supply-side layers already in place (block discount
  #436 / order-flow absorption #432).
- HV (validation, no return math): the announcement-derived ``float_ratio``
  buckets used by every stratification so far are cross-checked against
  ``daily_basic`` float/total share ratios on the entry day — disagreement
  here would re-open the <1% / 1-3% / avoided-3-5% band definitions.

Measure, per signal (entry at the event-day close; strictly-prior windows
only): mean ``turnover_rate_f`` over the prior 10 sessions ÷ trailing-20
session mean of the same field.  Fixed ex-ante edges on that ratio:
≤ 0.7 → ``shrink``, ≥ 1.5 → ``surge``, else → ``normal``.  Rows missing
``turnover_rate_f`` fall back to ``turnover_rate`` for that day only when
the fallback exists (recorded); symbols without a dailybasic file or with
insufficient strictly-prior history stay unlabeled — never guessed.

Population = the #423 lockup sell_off stream.  Cache-only.
research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_turnover_prelockup_study.py [--cache DIR]
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
)
from Ashare.event_margin_crowding_state import (  # noqa: E402
    attach_margin_states,
    cross_tab,
    load_margin_states,
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

PRE_WINDOW_SESSIONS = 10     # pre-event turnover window (strictly prior)
TRAIL_WINDOW_SESSIONS = 20   # own-baseline window (strictly prior)
SHRINK_EDGE = 0.7            # fixed ex-ante quiet-tape boundary
SURGE_EDGE = 1.5             # fixed ex-ante distribution boundary
TURNOVER_BUCKETS = ("shrink", "normal", "surge")
FLOAT_BANDS = ("<1%", "1-3%", "3-5%", ">5%")


class TurnoverStudyError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


def classify_turnover_state(ratio: float) -> str:
    if ratio <= SHRINK_EDGE:
        return "shrink"
    if ratio >= SURGE_EDGE:
        return "surge"
    return "normal"


def _turnover_series(cache: Path, symbols: set[str]) -> dict[str, tuple[list[str], list[float]]]:
    """Per-symbol ascending (days, turnover_rate_f|turnover_rate) series."""
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
                raw = row.get("turnover_rate_f") or row.get("turnover_rate")
                try:
                    value = float(raw)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
                rows_out.append((day, value))
            # Tushare files arrive NEWEST-FIRST; bisect below needs ascending.
            rows_out.sort(key=lambda item: item[0])
        if rows_out:
            days = [day for day, _value in rows_out]
            values = [value for _day, value in rows_out]
            pure = stem.removeprefix("dailybasic_")
            series[f"{pure[:6]}.{pure[6:]}"] = (days, values)
    if not series:
        raise TurnoverStudyError("dailybasic_cache_missing")
    return series


def attach_turnover_states(
    signals: list[dict[str, object]],
    series: dict[str, tuple[list[str], list[float]]],
    pre_window: int = PRE_WINDOW_SESSIONS,
    trail_window: int = TRAIL_WINDOW_SESSIONS,
) -> dict[str, int]:
    """Annotate each signal with its pre-unlock turnover bucket, in place."""
    stats = {
        "missing_dailybasic": 0,
        "insufficient_history": 0,
        "flat_baseline": 0,
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
        if pos < max(pre_window, trail_window):
            stats["insufficient_history"] += 1
            continue
        baseline = sum(values[pos - trail_window:pos]) / trail_window
        if baseline <= 0.0:
            stats["flat_baseline"] += 1
            continue
        window_mean = sum(values[pos - pre_window:pos]) / pre_window
        signal["turnover_ratio"] = window_mean / baseline
        signal["turnover_bucket"] = classify_turnover_state(
            window_mean / baseline
        )
        stats["attached"] += 1
    return stats


def turnover_buckets_for_events(
    cache: Path,
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    """Tracker side-table lookup: {(ts_code, day): bucket}.

    Reuses the study loaders on synthetic signals; ``day`` may be any
    calendar date (bisect rolls forward to the symbol's first session on/
    after it).  Pairs without a dailybasic file or enough strictly-prior
    history are omitted — absence means "unlabeled", never guessed.
    """

    symbols = {str(code) for code, _day in pairs}
    series = _turnover_series(cache, symbols)
    synthetic = [
        {"ts_code": str(code), "entry_day": str(day)} for code, day in pairs
    ]
    attach_turnover_states(synthetic, series)
    return {
        (s["ts_code"], s["entry_day"]): s["turnover_bucket"]
        for s in synthetic
        if "turnover_bucket" in s
    }


def float_band(ratio: float) -> str:
    if ratio < 0.01:
        return "<1%"
    if ratio <= 0.03:
        return "1-3%"
    if ratio <= 0.05:
        return "3-5%"
    return ">5%"


def _fmt_pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def joint_cross(
    signals: list[dict[str, object]],
    key_a: str,
    key_b: str,
    cost_bps: float,
) -> dict[tuple[str, str], dict[str, object]]:
    from Ashare.event_margin_crowding_state import net_trade_return

    cells: dict[tuple[str, str], list[float]] = {}
    for signal in signals:
        a = signal.get(key_a)
        b = signal.get(key_b)
        if a is None or b is None:
            continue
        cells.setdefault((str(a), str(b)), []).append(
            net_trade_return(signal, cost_bps)
        )
    return {
        pair: {
            "n": len(rets),
            "mean_net_bps": (sum(rets) / len(rets)) * 1e4 if rets else None,
            "win_rate": (
                sum(1 for r in rets if r > 0.0) / len(rets) if rets else None
            ),
        }
        for pair, rets in sorted(cells.items())
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

    symbols = {str(s["ts_code"]) for s in signals}
    series = _turnover_series(cache, symbols)
    attach_stats = attach_turnover_states(signals, series)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "universe_uncovered_symbols": uncovered,
    }
    print("## 解禁前换手率研究（research_only，非晋级证据）")
    print(f"- 信号总数 {len(signals)}；dailybasic 序列 {len(series)} 只；"
          f"附加统计 {attach_stats}；成本 {cost_bps}bps 往返；事前 "
          f"{PRE_WINDOW_SESSIONS} 会话 / 基线 {TRAIL_WINDOW_SESSIONS} 会话；"
          f"固定边界 shrink ≤{SHRINK_EDGE}, surge ≥{SURGE_EDGE}")

    labeled = [s for s in signals
               if s.get("turnover_bucket") in TURNOVER_BUCKETS]
    tab = cross_tab(labeled, cost_bps=cost_bps, key="turnover_bucket",
                    labels=TURNOVER_BUCKETS)
    results["signal_level_cross_tab"] = tab
    print("\n### 信号层交叉表（净 bps / 胜率）")
    print(f"{'bucket':<8} {'n':>6} {'mean_net_bps':>13} {'win_rate':>9}")
    for label, cell in tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        print(
            f"{label:<8} {cell['n']:>6} "
            f"{'—' if mean_bps is None else f'{float(mean_bps):+.1f}':>13} "
            f"{'—' if win is None else f'{float(win):.3f}':>9}"
        )

    def _portfolio_row(name: str, arm: list[dict[str, object]]) -> None:
        arm_sorted = sorted(arm, key=lambda s: (str(s["entry_day"]), str(s["ts_code"])))
        if len(arm_sorted) < 10:
            results["portfolio"][name] = {"signals": len(arm_sorted)}  # type: ignore[index]
            print(f"{name:<22} {len(arm_sorted):>5}   （样本不足，不跑组合）")
            return
        run = run_portfolio(arm_sorted, global_days, books, cost_bps=cost_bps)
        nav = run["nav"]
        months = [r for _, r in monthly_net_returns(nav, base=INITIAL_CASH_CNY)]
        row = {
            "closed_positions": run["closed_positions"],
            "total_net_return": nav[-1][1] / INITIAL_CASH_CNY - 1.0,
            "monthly_mean": sum(months) / len(months),
            "monthly_worst": min(months),
            "max_drawdown": max_drawdown(nav),
            "win_rate": run["win_rate"],
        }
        results["portfolio"][name] = row  # type: ignore[index]
        print(
            f"{name:<22} {row['closed_positions']:>5} "
            f"{_fmt_pct(float(row['total_net_return'])):>9} "
            f"{_fmt_pct(float(row['monthly_mean'])):>8} "
            f"{_fmt_pct(float(row['monthly_worst'])):>8} "
            f"{_fmt_pct(float(row['max_drawdown'])):>8} "
            f"{row['win_rate']:>6.3f}"
        )

    results["portfolio"] = {}  # type: ignore[assignment]
    print("\n### 组合层（同槽位口径对照）")
    header = (f"{'arm':<22} {'closed':>5} {'总净':>9} {'月均净':>8} "
              f"{'最差月':>8} {'回撤':>8} {'胜率':>6}")
    print(header)
    _portfolio_row("pooled_labeled", labeled)
    for label in TURNOVER_BUCKETS:
        _portfolio_row(
            f"turnover_{label}",
            [s for s in signals if s.get("turnover_bucket") == label],
        )

    rule = [s for s in signals if rule_arm_filter(s)]
    rule_labeled = [s for s in rule
                    if s.get("turnover_bucket") in TURNOVER_BUCKETS]
    rule_tab = cross_tab(rule_labeled, cost_bps=cost_bps,
                         key="turnover_bucket", labels=TURNOVER_BUCKETS)
    results["rule_arm_cross_tab"] = rule_tab
    print("\n### rule 臂叠加交叉表（判定层）")
    for label, cell in rule_tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        print(
            f"rule[{label:<7}] n={cell['n']:>4} "
            f"{'—' if mean_bps is None else f'{float(mean_bps):+.1f}'}bps "
            f"{'—' if win is None else f'win={float(win):.3f}'}"
        )

    # H2a: independence from the market margin-state layer.
    try:
        margin_days, states = load_margin_states(cache)
        attach_margin_states(labeled, margin_days, states)
        joint_a = joint_cross(labeled, "margin_state", "turnover_bucket",
                              cost_bps=cost_bps)
        results["h2a_margin_joint"] = {
            f"{a}|{b}": cell for (a, b), cell in joint_a.items()
        }
        print("\n### H2a 联合交叉：margin_state × turnover_bucket")
        for (a, b), cell in sorted(joint_a.items()):
            mean_bps = cell["mean_net_bps"]
            win = cell["win_rate"]
            print(
                f"{a:<11} x {b:<7} n={cell['n']:>4} "
                f"{'—' if mean_bps is None else f'{float(mean_bps):+.1f}'}bps "
                f"{'—' if win is None else f'win={float(win):.3f}'}"
            )
    except Exception as exc:  # noqa: BLE001 - readout must not kill the study
        results["h2a_margin_joint_error"] = str(exc)
        print(f"\nH2a 联合不可用：{exc}")

    # H2b/H2c: independence from the supply-side layers (block / absorption).
    for layer, module_name, func_name, bucket_key in (
        ("block", "event_blocktrade_prelockup_study",
         "block_buckets_for_events", "block_bucket"),
        ("absorption", "event_moneyflow_absorption_study",
         "absorption_buckets_for_events", "absorption_bucket"),
    ):
        try:
            module = __import__(f"Ashare.{module_name}", fromlist=[func_name])
            lookup = getattr(module, func_name)
            pairs = [(str(s["ts_code"]), str(s["entry_day"])) for s in labeled]
            buckets = lookup(cache, pairs)
            for s in labeled:
                s[bucket_key] = buckets.get(
                    (str(s["ts_code"]), str(s["entry_day"])),
                    "insufficient_history",
                )
            layer_labeled = [s for s in labeled
                             if s[bucket_key] != "insufficient_history"]
            joint = joint_cross(layer_labeled, bucket_key,
                                "turnover_bucket", cost_bps=cost_bps)
            results[f"h2_{layer}_joint"] = {
                f"{a}|{b}": cell for (a, b), cell in joint.items()
            }
            print(f"\n### H2 联合交叉：{layer} × turnover_bucket")
            for (a, b), cell in sorted(joint.items()):
                mean_bps = cell["mean_net_bps"]
                win = cell["win_rate"]
                print(
                    f"{a:<13} x {b:<7} n={cell['n']:>4} "
                    f"{'—' if mean_bps is None else f'{float(mean_bps):+.1f}'}bps "
                    f"{'—' if win is None else f'win={float(win):.3f}'}"
                )
        except Exception as exc:  # noqa: BLE001 - dataset optional (e.g. CI)
            results[f"h2_{layer}_joint_error"] = str(exc)
            print(f"\nH2 {layer} 联合不可用（数据缺失时为预期降级）：{exc}")

    # HV: announcement float_ratio vs daily_basic share ratio (no returns).
    try:
        share_meta: dict[str, tuple[list[str], list[float]]] = {}
        for path in sorted(cache.glob("dailybasic_*.csv")):
            stem = path.stem.removeprefix("dailybasic_")
            code = f"{stem[:6]}.{stem[6:]}"
            rows_out: list[tuple[str, float]] = []
            with path.open(encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    try:
                        fs = float(row["float_share"])
                        ts = float(row["total_share"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if ts > 0.0:
                        rows_out.append((row["trade_date"], fs / ts))
            rows_out.sort(key=lambda item: item[0])
            if rows_out:
                share_meta[code] = (
                    [d for d, _r in rows_out], [r for _d, r in rows_out],
                )
        matched = disagree = skipped = 0
        ann_ratios: list[float] = []
        db_ratios: list[float] = []
        examples: list[str] = []
        for s in labeled:
            meta = share_meta.get(str(s["ts_code"]))
            if meta is None or "float_ratio" not in s:
                skipped += 1
                continue
            pos = bisect.bisect_left(meta[0], str(s["entry_day"]))
            if pos >= len(meta[0]):
                skipped += 1
                continue
            dd_ratio = meta[1][pos]
            ann_ratio = float(s["float_ratio"])
            ann_ratios.append(ann_ratio)
            db_ratios.append(dd_ratio)
            if float_band(dd_ratio) == float_band(ann_ratio):
                matched += 1
            else:
                disagree += 1
                if len(examples) < 5:
                    examples.append(
                        f"{s['ts_code']}@{s['entry_day']} "
                        f"ann={ann_ratio:.4f}({float_band(ann_ratio)}) "
                        f"db={dd_ratio:.4f}({float_band(dd_ratio)})"
                    )
        results["hv_float_crosscheck"] = {
            "agree": matched,
            "disagree": disagree,
            "skipped": skipped,
            "ann_mean_ratio": (sum(ann_ratios) / len(ann_ratios))
            if ann_ratios else None,
            "db_mean_ratio": (sum(db_ratios) / len(db_ratios))
            if db_ratios else None,
        }
        print("\n### HV 流通盘口径交叉验证（公告比例 vs daily_basic）")
        ann_mean = results["hv_float_crosscheck"]["ann_mean_ratio"]
        db_mean = results["hv_float_crosscheck"]["db_mean_ratio"]
        print(f"- agree={matched} disagree={disagree} skipped={skipped}; "
              f"mean ann="
              f"{'—' if ann_mean is None else f'{float(ann_mean):.4f}'} "
              f"vs db="
              f"{'—' if db_mean is None else f'{float(db_mean):.4f}'}"
              f"（口径差异属定义问题，非数据错误）")
        for line in examples:
            print(f"  ! {line}")
    except Exception as exc:  # noqa: BLE001 - validation is auxiliary
        results["hv_float_crosscheck_error"] = str(exc)
        print(f"\nHV 交叉验证不可用：{exc}")

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
    except TurnoverStudyError as exc:
        print(f"TURNOVER_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
