"""Pre-lockup chips study: underwater-float pressure around unlock events.

Pre-registered hypotheses (bound to the #443 ``cyq_perf`` plane):

- H1: sell_off signals grouped by the LAST strictly-prior session's
  ``winner_rate`` separate in post-5d net outcomes.  Primary direction
  (ex ante): an UNDERWATER float (<0.3 winners) amplifies unlock supply —
  trapped holders exit into the new shares (negative readout); a PROFIT
  float (>0.7) lets the market absorb supply from strength (positive);
  mid sits between — monotone profit > mid > underwater.
- H2a/H2b: any separation survives conditioning on the margin-state layer
  (#426) and on the demand/supply layers already in place (turnover #441,
  block discount #436, absorption #432).
- HV (data-quality sanity, no return math): winner_rate must cohere with
  price-vs-chip-cost position — sessions where close > weight_avg should
  carry systematically higher winner_rate than sessions where close <
  weight_avg.  A violation would question the cyq_perf feed itself.

Measure, per signal: ``winner_rate`` on the most recent session STRICTLY
BEFORE entry_day (bisect roll-back over suspension gaps; rejected as stale
when that anchor sits more than ``STALENESS_DAYS`` calendar days back — a
pre-suspension chip state is not current information).  Fixed ex-ante
edges: < 0.3 → ``underwater``, < 0.7 → ``mid``, else → ``profit``.

Population = the #423 lockup sell_off stream.  Cache-only.
research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_chips_prelockup_study.py [--cache DIR]
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

UNDERWATER_EDGE = 0.3        # fixed ex-ante trapped-float boundary
PROFIT_EDGE = 0.7            # fixed ex-ante strong-float boundary
CHIPS_BUCKETS = ("underwater", "mid", "profit")
STALENESS_DAYS = 21          # calendar staleness cap on the anchored state


class ChipsStudyError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


def classify_winner_rate(value: float) -> str:
    if value < UNDERWATER_EDGE:
        return "underwater"
    if value < PROFIT_EDGE:
        return "mid"
    return "profit"


def _winner_series(cache: Path, symbols: set[str]) -> dict[str, tuple[list[str], list[float]]]:
    """Per-symbol ascending (days, winner_rate) series from cyqperf CSVs."""
    wanted_stems = {f"cyqperf_{code[:6]}{code[7:]}" for code in symbols}
    series: dict[str, tuple[list[str], list[float]]] = {}
    for stem in sorted(wanted_stems):
        path = cache / f"{stem}.csv"
        try:
            handle = path.open(encoding="utf-8")
        except FileNotFoundError:
            continue  # no chip history for this symbol: leave absent
        with handle:
            rows_out: list[tuple[str, float]] = []
            for row in csv.DictReader(handle):
                day = row.get("trade_date")
                try:
                    value = float(row.get("winner_rate"))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
                rows_out.append((day, value / 100.0))  # feed is percent
            # Tushare files arrive NEWEST-FIRST; bisect below needs ascending.
            rows_out.sort(key=lambda item: item[0])
        if rows_out:
            days = [day for day, _value in rows_out]
            values = [value for _day, value in rows_out]
            pure = stem.removeprefix("cyqperf_")
            series[f"{pure[:6]}.{pure[6:]}"] = (days, values)
    if not series:
        raise ChipsStudyError("cyq_cache_missing")
    return series


def attach_chips_states(
    signals: list[dict[str, object]],
    series: dict[str, tuple[list[str], list[float]]],
) -> dict[str, int]:
    """Annotate each signal with its pre-unlock winner-rate bucket."""
    stats = {
        "missing_cyq": 0,
        "no_prior_session": 0,
        "stale_chips": 0,
        "attached": 0,
    }
    for signal in signals:
        code = str(signal["ts_code"])
        book = series.get(code)
        if book is None:
            stats["missing_cyq"] += 1
            continue
        days, values = book
        pos = bisect.bisect_left(days, str(signal["entry_day"]))
        if pos == 0:
            stats["no_prior_session"] += 1
            continue
        # latest STRICTLY-prior session; distance discipline comes solely
        # from the calendar staleness cap below.
        anchor_pos = pos - 1
        # calendar staleness cap: a chip state from before a long suspension
        # is NOT current information — leave the signal unlabeled instead.
        try:
            entry_date = datetime.strptime(
                str(signal["entry_day"]), "%Y%m%d"
            ).date()
            anchor_date = datetime.strptime(days[anchor_pos], "%Y%m%d").date()
        except ValueError:
            stats["no_prior_session"] += 1
            continue
        if (entry_date - anchor_date).days > STALENESS_DAYS:
            stats["stale_chips"] += 1
            continue
        # most recent strictly-prior session (roll back over short gaps)
        signal["winner_rate"] = values[anchor_pos]
        signal["chips_bucket"] = classify_winner_rate(values[anchor_pos])
        stats["attached"] += 1
    return stats


def chips_buckets_for_events(
    cache: Path,
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    """Tracker side-table lookup: {(ts_code, day): bucket}.

    Reuses the study loaders on synthetic signals; ``day`` may be any
    calendar date (bisect rolls BACK to the latest session before it).
    Pairs without a cyqperf file or a prior session are omitted — absence
    means "unlabeled", never guessed.
    """

    symbols = {str(code) for code, _day in pairs}
    series = _winner_series(cache, symbols)
    synthetic = [
        {"ts_code": str(code), "entry_day": str(day)} for code, day in pairs
    ]
    attach_chips_states(synthetic, series)
    return {
        (s["ts_code"], s["entry_day"]): s["chips_bucket"]
        for s in synthetic
        if "chips_bucket" in s
    }


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
    series = _winner_series(cache, symbols)
    attach_stats = attach_chips_states(signals, series)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "universe_uncovered_symbols": uncovered,
    }
    print("## 解禁前筹码研究（research_only，非晋级证据）")
    print(f"- 信号总数 {len(signals)}；cyqperf 序列 {len(series)} 只；"
          f"附加统计 {attach_stats}；成本 {cost_bps}bps 往返；"
          f"固定边界 underwater <{UNDERWATER_EDGE}, profit ≥{PROFIT_EDGE}"
          f"（事前最近会话；锚点早于 {STALENESS_DAYS} 自然日视为陈旧拒标）")

    labeled = [s for s in signals if s.get("chips_bucket") in CHIPS_BUCKETS]
    tab = cross_tab(labeled, cost_bps=cost_bps, key="chips_bucket",
                    labels=CHIPS_BUCKETS)
    results["signal_level_cross_tab"] = tab
    print("\n### 信号层交叉表（净 bps / 胜率）")
    print(f"{'bucket':<11} {'n':>6} {'mean_net_bps':>13} {'win_rate':>9}")
    for label, cell in tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        print(
            f"{label:<11} {cell['n']:>6} "
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
    for label in CHIPS_BUCKETS:
        _portfolio_row(
            f"chips_{label}",
            [s for s in signals if s.get("chips_bucket") == label],
        )

    rule = [s for s in signals if rule_arm_filter(s)]
    rule_labeled = [s for s in rule
                    if s.get("chips_bucket") in CHIPS_BUCKETS]
    rule_tab = cross_tab(rule_labeled, cost_bps=cost_bps,
                         key="chips_bucket", labels=CHIPS_BUCKETS)
    results["rule_arm_cross_tab"] = rule_tab
    print("\n### rule 臂叠加交叉表（判定层）")
    for label, cell in rule_tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        print(
            f"rule[{label:<10}] n={cell['n']:>4} "
            f"{'—' if mean_bps is None else f'{float(mean_bps):+.1f}'}bps "
            f"{'—' if win is None else f'win={float(win):.3f}'}"
        )

    # H2a: independence from the market margin-state layer.
    try:
        margin_days, states = load_margin_states(cache)
        attach_margin_states(labeled, margin_days, states)
        joint_a = joint_cross(labeled, "margin_state", "chips_bucket",
                              cost_bps=cost_bps)
        results["h2a_margin_joint"] = {
            f"{a}|{b}": cell for (a, b), cell in joint_a.items()
        }
        print("\n### H2a 联合交叉：margin_state × chips_bucket")
        for (a, b), cell in sorted(joint_a.items()):
            mean_bps = cell["mean_net_bps"]
            win = cell["win_rate"]
            print(
                f"{a:<11} x {b:<10} n={cell['n']:>4} "
                f"{'—' if mean_bps is None else f'{float(mean_bps):+.1f}'}bps "
                f"{'—' if win is None else f'win={float(win):.3f}'}"
            )
    except Exception as exc:  # noqa: BLE001 - readout must not kill the study
        results["h2a_margin_joint_error"] = str(exc)
        print(f"\nH2a 联合不可用：{exc}")

    # H2b/H2c/H2d: independence from turnover / block / absorption layers.
    for layer, module_name, func_name, bucket_key in (
        ("turnover", "event_turnover_prelockup_study",
         "turnover_buckets_for_events", "turnover_bucket"),
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
                                "chips_bucket", cost_bps=cost_bps)
            results[f"h2_{layer}_joint"] = {
                f"{a}|{b}": cell for (a, b), cell in joint.items()
            }
            print(f"\n### H2 联合交叉：{layer} × chips_bucket")
            for (a, b), cell in sorted(joint.items()):
                mean_bps = cell["mean_net_bps"]
                win = cell["win_rate"]
                print(
                    f"{a:<13} x {b:<10} n={cell['n']:>4} "
                    f"{'—' if mean_bps is None else f'{float(mean_bps):+.1f}'}bps "
                    f"{'—' if win is None else f'win={float(win):.3f}'}"
                )
        except Exception as exc:  # noqa: BLE001 - dataset optional (e.g. CI)
            results[f"h2_{layer}_joint_error"] = str(exc)
            print(f"\nH2 {layer} 联合不可用（数据缺失时为预期降级）：{exc}")

    # HV: winner_rate vs price-position sanity (no returns).  Sessions where
    # the daily close sits ABOVE the chip weight-average cost should carry
    # systematically higher winner_rate than sessions below it — a violation
    # would question the cyq_perf feed itself, not the hypothesis.
    try:
        wr_above: list[float] = []
        wr_below: list[float] = []
        skipped = 0
        for s in labeled:
            code = str(s["ts_code"])
            day = str(s["entry_day"])
            book = series.get(code)
            if book is None:
                skipped += 1
                continue
            days, values = book
            pos = bisect.bisect_left(days, day)
            if pos == 0:
                skipped += 1
                continue
            wr = values[pos - 1]
            anchor_day = days[pos - 1]
            close = _close_on(cache, code, anchor_day)
            wavg = _weight_avg_on(cache, code, anchor_day)
            if close is None or wavg is None or wavg <= 0.0:
                skipped += 1
                continue
            (wr_above if close > wavg else wr_below).append(wr)
        mean_above = sum(wr_above) / len(wr_above) if wr_above else None
        mean_below = sum(wr_below) / len(wr_below) if wr_below else None
        results["hv_price_position_sanity"] = {
            "n_close_above_cost": len(wr_above),
            "n_close_below_cost": len(wr_below),
            "mean_winner_rate_above": mean_above,
            "mean_winner_rate_below": mean_below,
            "skipped": skipped,
        }
        print("\n### HV 数据质量验证：winner_rate vs 价格-成本位置")
        print(
            f"- close>weight_avg：n={len(wr_above)} "
            f"mean_winner_rate="
            f"{'—' if mean_above is None else f'{float(mean_above):.3f}'}；"
            f"close<weight_avg：n={len(wr_below)} "
            f"mean_winner_rate="
            f"{'—' if mean_below is None else f'{float(mean_below):.3f}'}；"
            f"skipped={skipped}"
            + (
                ""
                if mean_above is None or mean_below is None
                else (
                    "（方向正确，数据可信）"
                    if mean_above > mean_below
                    else "（方向异常——需核查 cyq_perf 口径！）"
                )
            )
        )
    except Exception as exc:  # noqa: BLE001 - validation is auxiliary
        results["hv_price_position_sanity_error"] = str(exc)
        print(f"\nHV 验证不可用：{exc}")

    return results


def _load_column_on(
    cache: Path,
    code: str,
    prefix: str,
    column: str,
    day: str,
) -> float | None:
    """Value of ``column`` on exactly ``day`` from a per-symbol CSV."""
    path = cache / f"{prefix}_{code[:6]}{code[7:]}.csv"
    try:
        handle = path.open(encoding="utf-8")
    except FileNotFoundError:
        return None
    with handle:
        for row in csv.DictReader(handle):
            if row.get("trade_date") == day:
                try:
                    value = float(row[column])  # type: ignore[arg-type]
                except (KeyError, TypeError, ValueError):
                    return None
                return value
    return None


def _close_on(cache: Path, code: str, day: str) -> float | None:
    return _load_column_on(cache, code, "daily", "close", day)


def _weight_avg_on(cache: Path, code: str, day: str) -> float | None:
    return _load_column_on(cache, code, "cyqperf", "weight_avg", day)


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
    except ChipsStudyError as exc:
        print(f"CHIPS_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
