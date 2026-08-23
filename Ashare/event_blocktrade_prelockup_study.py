"""Pre-lockup block-trade study: does supply exit via the block channel?

Pre-registered hypotheses (bound to the block_trade fetcher): unlocked
lockup shares most often exit through BLOCK transactions, which bypass the
visible order book entirely — the order-flow absorption lane (#431/#432)
cannot see them.  Anticipatory positioning may show up before the event.

- H1: sell_off signals with pre-event block prints in the symbol's own
  name show different post-5d net outcomes than signals without any print,
  and the premium dimension separates motivated exits (deep discount) from
  neutral transfers.
- H2: that separation is independent of the market margin-state layer and
  of the order-flow absorption layer.

Measure, per signal (entry at the event-day close; strictly-prior windows
only — sessions on/after the entry day never enter any window):

- pre-window = 10 sessions (wider than moneyflow's 5 ex ante: block prints
  are sparse — tens of rows per day across the whole market);
- ``none``: no print for the symbol in the window;
- otherwise volume-weighted premium vs the SAME-day unadjusted close
  (block price and daily close share the raw-price convention); fixed edge
  at −3% chosen ex ante as the customary A-share deep-discount boundary:
  vw_premium <= −3% → ``discount_deep``, else → ``near_flat``;
- descriptive intensity only (not a bucket): Σ block amount over the
  window ÷ trailing-20-session average daily amount (both converted to
  thousand-yuan; block amount is price×vol in 万元, ×10 → 千元).

Population = the #423 lockup sell_off stream.  Cache-only.
research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_blocktrade_prelockup_study.py [--cache DIR]
        [--cost-bps X]
"""

from __future__ import annotations

import argparse
import bisect
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

BLOCKTRADE_DIRNAME = "blocktrade_daily"
PRE_WINDOW_SESSIONS = 10     # pre-event block-print window (strictly prior)
TRAIL_WINDOW_SESSIONS = 20   # daily-turnover normalizer window (strictly prior)
DISCOUNT_EDGE = -0.03        # fixed ex-ante deep-discount boundary
BLOCK_BUCKETS = ("none", "discount_deep", "near_flat")
WAN_TO_QIAN = 10.0           # block amount 万元 -> 千元


class BlocktradeStudyError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


def classify_block_state(vw_premium: float) -> str:
    if vw_premium <= DISCOUNT_EDGE:
        return "discount_deep"
    return "near_flat"


def load_symbol_blocks(
    cache: Path, symbols: set[str]
) -> dict[str, tuple[list[str], list[float], list[float]]]:
    """Per-symbol (days, amounts_wan, prices) of raw block prints."""
    flow_dir = cache / BLOCKTRADE_DIRNAME
    if not flow_dir.is_dir():
        raise BlocktradeStudyError(f"blocktrade_dir_missing:{flow_dir}")
    import csv

    series: dict[str, tuple[list[str], list[float], list[float]]] = {}
    for path in sorted(flow_dir.glob("*.csv")):
        day = path.stem
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                code = row.get("ts_code")
                if code not in symbols:
                    continue
                try:
                    amount = float(row["amount"])
                    price = float(row["price"])
                except (KeyError, TypeError, ValueError):
                    continue  # malformed row: skip, do not guess
                days, amounts, prices = series.setdefault(code, ([], [], []))
                days.append(day)
                amounts.append(amount)
                prices.append(price)
    return series


def load_symbol_daily_meta(
    cache: Path, symbols: set[str]
) -> dict[str, tuple[list[str], dict[str, float], list[float]]]:
    """Per-symbol (days, {day: close}, amounts_qian) from the daily cache.

    Provides the session calendar, same-day closes for premium math and
    the turnover normalizer.  Symbols without a daily file are omitted.
    """

    import csv

    wanted_stems = {f"daily_{code[:6]}{code[7:]}" for code in symbols}
    series: dict[str, tuple[list[str], dict[str, float], list[float]]] = {}
    for stem in sorted(wanted_stems):
        path = cache / f"{stem}.csv"
        try:
            handle = path.open(encoding="utf-8")
        except FileNotFoundError:
            continue  # no daily history for this symbol: leave it absent
        with handle:
            reader = csv.DictReader(handle)
            # Tushare daily files arrive NEWEST-FIRST: normalize to ascending
            # session order, otherwise every bisect below silently misreads.
            rows_out: list[tuple[str, float, float]] = []
            for row in reader:
                day = row.get("trade_date")
                try:
                    close = float(row["close"])
                    amount = float(row["amount"])
                except (KeyError, TypeError, ValueError):
                    continue
                rows_out.append((day, close, amount))
            rows_out.sort(key=lambda item: item[0])
            days = [day for day, _close, _amount in rows_out]
            closes = {day: close for day, close, _amount in rows_out}
            amounts = [amount for _day, _close, amount in rows_out]
        code = f"{stem[6:12]}.{stem[12:]}"
        if days:
            series[code] = (days, closes, amounts)
    if not series:
        raise BlocktradeStudyError("daily_cache_missing")
    return series


def attach_block_states(
    signals: list[dict[str, object]],
    blocks: dict[str, tuple[list[str], list[float], list[float]]],
    meta: dict[str, tuple[list[str], dict[str, float], list[float]]],
    pre_window: int = PRE_WINDOW_SESSIONS,
    trail_window: int = TRAIL_WINDOW_SESSIONS,
) -> dict[str, int]:
    """Annotate each signal with its pre-event block bucket, in place."""
    stats = {
        "missing_daily": 0,
        "insufficient_history": 0,
        "insufficient_premium": 0,
        "attached": 0,
    }
    for signal in signals:
        code = str(signal["ts_code"])
        book_meta = meta.get(code)
        if book_meta is None:
            stats["missing_daily"] += 1
            continue
        m_days, m_closes, m_amounts = book_meta
        pos = bisect.bisect_left(m_days, str(signal["entry_day"]))
        if pos < max(pre_window, trail_window):
            stats["insufficient_history"] += 1
            continue
        avg_amount = sum(m_amounts[pos - trail_window:pos]) / trail_window
        if avg_amount <= 0.0:
            stats["insufficient_history"] += 1
            continue
        win_start = m_days[pos - pre_window]
        book_blocks = blocks.get(code)
        lo = hi = 0
        w_amounts: list[float] = []
        w_premia: list[float] = []
        if book_blocks is not None:
            b_days = book_blocks[0]
            lo = bisect.bisect_left(b_days, win_start)
            hi = bisect.bisect_left(b_days, str(signal["entry_day"]))
            b_amounts = book_blocks[1]
            b_prices = book_blocks[2]
            for i in range(lo, hi):
                close = m_closes.get(b_days[i])
                if close is None or close <= 0.0:
                    continue  # print without a resolvable same-day close
                w_amounts.append(b_amounts[i])
                w_premia.append(b_prices[i] / close - 1.0)
        if not w_amounts:
            signal["block_bucket"] = "none"
            signal["block_intensity"] = 0.0
            stats["attached"] += 1
            continue
        total_wan = sum(w_amounts)
        vw_premium = (
            sum(a * p for a, p in zip(w_amounts, w_premia)) / total_wan
        )
        signal["block_bucket"] = classify_block_state(vw_premium)
        signal["block_vw_premium"] = vw_premium
        signal["block_intensity"] = total_wan * WAN_TO_QIAN / avg_amount
        stats["attached"] += 1
    return stats


def _fmt_pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def joint_cross(
    signals: list[dict[str, object]],
    key_a: str,
    key_b: str,
    cost_bps: float,
) -> dict[tuple[str, str], dict[str, object]]:
    """Cell-level n / mean net bps / win rate over two annotation keys."""

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


def run_study(cache: Path, cost_bps: float = COST_BPS_ROUNDTRIP_DEFAULT) -> dict[str, object]:
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
    blocks = load_symbol_blocks(cache, symbols)
    meta = load_symbol_daily_meta(cache, symbols)
    attach_stats = attach_block_states(signals, blocks, meta)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "universe_uncovered_symbols": uncovered,
    }
    print("## 解禁前大宗交易研究（research_only，非晋级证据）")
    print(f"- 信号总数 {len(signals)}；大宗序列 {len(blocks)} 只；"
          f"日线元数据 {len(meta)} 只；附加统计 {attach_stats}；成本 "
          f"{cost_bps}bps 往返；事前 {PRE_WINDOW_SESSIONS} 会话窗口、"
          f"折价固定边界 {DISCOUNT_EDGE:+.0%}")

    labeled = [
        s for s in signals
        if s.get("block_bucket") in ("none", "discount_deep", "near_flat")
    ]
    tab = cross_tab(labeled, cost_bps=cost_bps, key="block_bucket",
                    labels=BLOCK_BUCKETS)
    results["signal_level_cross_tab"] = tab
    print("\n### 信号层交叉表（净 bps / 胜率）")
    print(f"{'bucket':<14} {'n':>6} {'mean_net_bps':>13} {'win_rate':>9}")
    for label, cell in tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        print(
            f"{label:<14} {cell['n']:>6} "
            f"{'—' if mean_bps is None else f'{float(mean_bps):+.1f}':>13} "
            f"{'—' if win is None else f'{float(win):.3f}':>9}"
        )

    def _portfolio_row(name: str, arm: list[dict[str, object]]) -> None:
        arm_sorted = sorted(arm, key=lambda s: (str(s["entry_day"]), str(s["ts_code"])))
        if len(arm_sorted) < 10:
            results["portfolio"][name] = {"signals": len(arm_sorted)}  # type: ignore[index]
            print(f"{name:<26} {len(arm_sorted):>5}   （样本不足，不跑组合）")
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
            f"{name:<26} {row['closed_positions']:>5} "
            f"{_fmt_pct(float(row['total_net_return'])):>9} "
            f"{_fmt_pct(float(row['monthly_mean'])):>8} "
            f"{_fmt_pct(float(row['monthly_worst'])):>8} "
            f"{_fmt_pct(float(row['max_drawdown'])):>8} "
            f"{row['win_rate']:>6.3f}"
        )

    results["portfolio"] = {}  # type: ignore[assignment]
    print("\n### 组合层（同槽位口径对照）")
    header = (f"{'arm':<26} {'closed':>5} {'总净':>9} {'月均净':>8} "
              f"{'最差月':>8} {'回撤':>8} {'胜率':>6}")
    print(header)
    _portfolio_row("pooled_labeled", labeled)
    for label in BLOCK_BUCKETS:
        _portfolio_row(
            f"block_{label}",
            [s for s in signals if s.get("block_bucket") == label],
        )

    rule = [s for s in signals if rule_arm_filter(s)]
    rule_labeled = [
        s for s in rule
        if s.get("block_bucket") in ("none", "discount_deep", "near_flat")
    ]
    rule_tab = cross_tab(rule_labeled, cost_bps=cost_bps, key="block_bucket",
                         labels=BLOCK_BUCKETS)
    results["rule_arm_cross_tab"] = rule_tab
    print("\n### rule 臂叠加交叉表（判定层）")
    for label, cell in rule_tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        print(
            f"rule[{label:<13}] n={cell['n']:>4} "
            f"{'—' if mean_bps is None else f'{float(mean_bps):+.1f}'}bps "
            f"{'—' if win is None else f'win={float(win):.3f}'}"
        )

    # H2a: independence from the market margin-state layer.
    try:
        margin_days, states = load_margin_states(cache)
        attach_margin_states(labeled, margin_days, states)
        joint_a = joint_cross(labeled, "margin_state", "block_bucket",
                              cost_bps=cost_bps)
        results["h2a_margin_joint"] = {
            f"{a}|{b}": cell for (a, b), cell in joint_a.items()
        }
        print("\n### H2a 联合交叉：margin_state × block_bucket（n/净bps/胜率）")
        for (a, b), cell in sorted(joint_a.items()):
            mean_bps = cell["mean_net_bps"]
            win = cell["win_rate"]
            print(
                f"{a:<11} x {b:<13} n={cell['n']:>4} "
                f"{'—' if mean_bps is None else f'{float(mean_bps):+.1f}'}bps "
                f"{'—' if win is None else f'win={float(win):.3f}'}"
            )
    except Exception as exc:  # noqa: BLE001 - readout must not kill the study
        results["h2a_margin_joint_error"] = str(exc)
        print(f"\nH2a 联合不可用：{exc}")

    # H2b: independence from the order-flow absorption layer (#432).
    try:
        from Ashare.event_moneyflow_absorption_study import (
            absorption_buckets_for_events,
        )

        pairs = [(str(s["ts_code"]), str(s["entry_day"])) for s in labeled]
        buckets = absorption_buckets_for_events(cache, pairs)
        for s in labeled:
            s["absorption_bucket"] = buckets.get(
                (str(s["ts_code"]), str(s["entry_day"])), "insufficient_history"
            )
        abs_labeled = [s for s in labeled
                       if s["absorption_bucket"] != "insufficient_history"]
        joint_b = joint_cross(abs_labeled, "absorption_bucket",
                              "block_bucket", cost_bps=cost_bps)
        results["h2b_absorption_joint"] = {
            f"{a}|{b}": cell for (a, b), cell in joint_b.items()
        }
        print("\n### H2b 联合交叉：absorption × block（n/净bps/胜率）")
        for (a, b), cell in sorted(joint_b.items()):
            mean_bps = cell["mean_net_bps"]
            win = cell["win_rate"]
            print(
                f"{a:<9} x {b:<13} n={cell['n']:>4} "
                f"{'—' if mean_bps is None else f'{float(mean_bps):+.1f}'}bps "
                f"{'—' if win is None else f'win={float(win):.3f}'}"
            )
    except Exception as exc:  # noqa: BLE001 - dataset optional (e.g. CI)
        results["h2b_absorption_joint_error"] = str(exc)
        print(f"\nH2b 联合不可用（moneyflow 数据缺失时为预期降级）：{exc}")

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
    except BlocktradeStudyError as exc:
        print(f"BLOCKTRADE_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
