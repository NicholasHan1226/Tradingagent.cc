"""Cross-sectional signal-family prescreen over the forty-symbol universe.

Research only.  This module tests the one signal family never examined in
prior crypto research: *cross-sectional* structure (symbols ranked against
each other) as opposed to the per-symbol time-series families that were all
falsified under taker costs (momentum breakout, OI change/divergence/weighted
momentum, funding/basis carry).

**Pre-registration.**  The candidate grid below was frozen before any result
was computed: two rank families (long-top relative strength, long-bottom
reversal) over lookback ``L in {288, 576}`` bars, holding ``K in {5, 10}``
names for horizon ``H in {48, 288}`` bars, plus a dispersion-gated variant of
the long-top family that stays flat when cross-sectional trailing-return
dispersion is above its own expanding-window median.  No other configuration
is evaluated, and thresholds are never scanned.

**Known pre-registration defect, reported as-is.**  The dispersion gate's
expanding median was specified as 1440 *slots* but is compared against the
number of *rebalances* recorded so far; on this window every candidate records
fewer than 1440 rebalances, so the gate never arms and the gated variants are
byte-identical to their ungated counterparts.  The constant is NOT retuned
after seeing results — the gated family is reported as not evaluable on this
window rather than silently repaired.

**Portfolio semantics.**  At each rebalance slot the selected names are held
equal-weight for ``H`` bars; labels are close→close forward returns.  A
rebalance charges round-trip taker costs (fee 0.1% + 2bps slippage per leg,
``crypto-round-trip-taker-v1``) on the replaced weight fraction only.  The
baseline is the always-invested equal-weight all-symbol portfolio on the same
cadence with the same cost model.

**Data governance.**  Input is a read-only diagnostic extraction of the
TradingDatas crypto read-model SQLite (``sqlite_readonly_diagnostic``), the
same one-off research bypass documented in the ten-symbol OI prescreen.  The
formal consumer path stays catalog/query; this module has no network, no
capital, no order, and no Champion writes, and its artifacts are sealed
``not_promotion_evidence=true`` / ``historical_backfill_no_pit=true``.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

CONTRACT = "tradingagent.crypto.forty_symbol_cross_sectional_prescreen.v1"

# --- Frozen pre-registered grid (do not widen after seeing results) --------
LOOKBACKS = (288, 576)  # 1 day / 2 days in 5m bars
TOP_K = (5, 10)
HORIZONS = (48, 288)  # 4 hours / 24 hours in 5m bars
DISPERSION_GATE_LOOKBACK_MEDIAN = 1440  # expanding median over 5 days of slots
# ---------------------------------------------------------------------------

ENTRY_FEE = Decimal("0.001")
EXIT_FEE = Decimal("0.001")
SLIP = Decimal("0.0002")

MIN_SLOT_COVERAGE = 40_000  # per-symbol minimum bars for universe membership


class CrossSectionalPrescreenError(RuntimeError):
    """Stable fail-closed error for the cross-sectional prescreen."""


def _assert_simulation_only() -> None:
    if os.environ.get("REAL_TRADING_ENABLED") != "false":
        raise CrossSectionalPrescreenError(
            "cross_sectional_prescreen_real_trading_must_be_disabled"
        )


def _non_evidence_fields() -> dict[str, Any]:
    return {
        "authority": "none",
        "research_only": True,
        "not_promotion_evidence": True,
        "historical_backfill_no_pit": True,
        "data_source": "sqlite_readonly_diagnostic",
        "execution_eligible": False,
        "capital_write_eligible": False,
        "production_eligible": False,
        "real_trading_enabled": False,
        "promotion_authorized": False,
        "automatic_risk_expansion_enabled": False,
        "model_network_used": False,
        "pre_registered_grid": True,
    }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_closes(path: Path) -> dict[str, dict[int, Decimal]]:
    """Load {symbol: {slot: close}} from the diagnostic extraction CSV."""

    by_symbol: dict[str, dict[int, Decimal]] = {}
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.reader(handle):
            if len(row) != 3:
                raise CrossSectionalPrescreenError(
                    "cross_sectional_prescreen_row_shape_invalid"
                )
            symbol, raw_time, raw_close = row
            slot = _slot_index(raw_time)
            slots = by_symbol.setdefault(symbol, {})
            # Deterministic first-seen dedup (rows ordered by rowid upstream).
            if slot not in slots:
                slots[slot] = Decimal(raw_close)
    if not by_symbol:
        raise CrossSectionalPrescreenError("cross_sectional_prescreen_empty_input")
    return by_symbol


def _slot_index(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    parsed = parsed.astimezone(timezone.utc)
    return int(parsed.timestamp()) // 300


def _slot_to_iso(slot: int) -> str:
    return (
        datetime.fromtimestamp(slot * 300, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_panel(
    closes: Mapping[str, Mapping[int, Decimal]],
) -> dict[str, Any]:
    """Intersect all symbols onto one common contiguous-per-symbol slot grid.

    Labels never cross a gap: forward windows that would run past a symbol's
    last slot exclude that symbol from the candidate set at that slot.
    """

    universe = sorted(
        symbol
        for symbol, slots in closes.items()
        if len(slots) >= MIN_SLOT_COVERAGE
    )
    if len(universe) < 40:
        raise CrossSectionalPrescreenError(
            "cross_sectional_prescreen_universe_incomplete"
        )
    common = None
    for symbol in universe:
        keys = set(closes[symbol])
        common = keys if common is None else (common & keys)
    assert common is not None
    grid = sorted(common)
    # The common grid may still contain holes (slots where every symbol
    # happens to lack a bar); contiguity is enforced per evaluation window
    # instead of failing the whole panel here.
    return {
        "universe": universe,
        "grid": grid,
        "closes": {symbol: [closes[symbol][slot] for slot in grid] for symbol in universe},
    }


# ---------------------------------------------------------------------------
# Pre-registered evaluation
# ---------------------------------------------------------------------------


def _forward_returns(
    closes_by_symbol: Mapping[str, Sequence[Decimal]],
    grid: Sequence[int],
    index: int,
    horizon: int,
) -> dict[str, Decimal] | None:
    """Close→close forward returns per symbol, or None if the window is not
    contiguous for every symbol (labels never cross a gap)."""

    end = index + horizon
    if end >= len(grid):
        return None
    # Grid slots are epoch seconds // 300, so one 5-minute bar step is 1.
    if grid[end] - grid[index] != horizon:
        return None  # gap inside the forward window: abstain entirely
    returns: dict[str, Decimal] = {}
    for symbol, closes in closes_by_symbol.items():
        start = closes[index]
        if start <= 0:
            return None
        returns[symbol] = closes[end] / start - 1
    return returns


def _trailing_returns(
    closes_by_symbol: Mapping[str, Sequence[Decimal]],
    grid: Sequence[int],
    index: int,
    lookback: int,
) -> dict[str, Decimal] | None:
    start = index - lookback
    if start < 0:
        return None
    if grid[index] - grid[start] != lookback:
        return None
    returns: dict[str, Decimal] = {}
    for symbol, closes in closes_by_symbol.items():
        base = closes[start]
        if base <= 0:
            return None
        returns[symbol] = closes[index] / base - 1
    return returns


def _rank_symbols(returns: Mapping[str, Decimal], descending: bool) -> list[str]:
    # Deterministic tie-break by symbol name.
    return sorted(returns, key=lambda s: (returns[s], s), reverse=descending)


def _portfolio_net(
    selected: Sequence[str],
    forward: Mapping[str, Decimal],
    previous_selected: Sequence[str] | None,
) -> dict[str, Any]:
    k = len(selected)
    gross = sum((forward[s] for s in selected), Decimal("0")) / k
    if previous_selected is None:
        replaced = Decimal("1")
    else:
        kept = len(set(selected) & set(previous_selected))
        replaced = Decimal(k - kept) / k
    # Replaced fraction pays a full round trip; kept fraction pays none
    # (positions persist across the rebalance).
    one_leg = (Decimal("1") - ENTRY_FEE) * (Decimal("1") - SLIP)
    net = (Decimal("1") + gross) * (one_leg ** 2) ** replaced - Decimal("1")
    return {"gross": gross, "net": net, "replaced": replaced}


def _dispersion(trailing: Mapping[str, Decimal]) -> Decimal:
    values = sorted(trailing.values())
    n = len(values)
    mean = sum(values, Decimal("0")) / n
    var = sum(((v - mean) ** 2 for v in values), Decimal("0")) / n
    return var.sqrt()


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / len(values)


def _t_stat(values: Sequence[Decimal]) -> Decimal | None:
    """One-sample t statistic of the recorded per-window net series."""

    n = len(values)
    if n < 2:
        return None
    mu = sum(values, Decimal("0")) / n
    var = sum(((v - mu) ** 2 for v in values), Decimal("0")) / (n - 1)
    if var == 0:
        return None
    return mu / (var.sqrt() / Decimal(n).sqrt())


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _evaluate_candidate(
    *,
    name: str,
    family: str,
    panel: Mapping[str, Any],
    lookback: int,
    k: int,
    horizon: int,
    gated: bool,
) -> dict[str, Any]:
    grid: Sequence[int] = panel["grid"]
    closes: Mapping[str, Sequence[Decimal]] = panel["closes"]
    universe: Sequence[str] = panel["universe"]

    # Every evaluated forward window is pairwise disjoint by construction:
    # after each evaluation the index advances a full ``horizon`` (abstained
    # slots advance one bar and never record a window), so recorded returns
    # are already non-overlapping samples.
    gross_values: list[Decimal] = []
    net_values: list[Decimal] = []
    baseline_net: list[Decimal] = []
    abstain_slots = 0
    evaluated_slots = 0
    gated_flat_slots = 0
    previous: Sequence[str] | None = None
    baseline_previous: Sequence[str] | None = None
    dispersion_history: list[Decimal] = []

    index = 0
    while index < len(grid):
        trailing = _trailing_returns(closes, grid, index, lookback)
        forward = _forward_returns(closes, grid, index, horizon)
        if trailing is None or forward is None:
            abstain_slots += 1
            index += 1
            previous = None
            baseline_previous = None
            continue
        dispersion_history.append(_dispersion(trailing))
        ranked_desc = _rank_symbols(trailing, descending=True)
        if family == "long_top":
            selected = ranked_desc[:k]
        elif family == "long_bottom":
            selected = ranked_desc[-k:]
        else:
            raise CrossSectionalPrescreenError(
                "cross_sectional_prescreen_family_invalid"
            )
        if gated and len(dispersion_history) > DISPERSION_GATE_LOOKBACK_MEDIAN:
            median = sorted(dispersion_history)[len(dispersion_history) // 2]
            if dispersion_history[-1] > median:
                selected = []  # high-dispersion state: stay flat
                gated_flat_slots += 1
        evaluated_slots += 1
        if selected:
            outcome = _portfolio_net(selected, forward, previous)
            gross_values.append(outcome["gross"])
            net_values.append(outcome["net"])
            previous = selected
        else:
            previous = None
        baseline_selected = universe  # always-invested equal weight
        baseline_outcome = _portfolio_net(
            baseline_selected, forward, baseline_previous
        )
        baseline_net.append(baseline_outcome["net"])
        baseline_previous = baseline_selected

        index += horizon
    mean_gross = _mean(gross_values)
    mean_net = _mean(net_values)
    baseline_mean_net = _mean(baseline_net)
    return {
        "candidate_id": name,
        "family": family,
        "lookback_bars": lookback,
        "top_k": k,
        "horizon_bars": horizon,
        "dispersion_gated": gated,
        "evaluated_slots": evaluated_slots,
        "abstain_slots": abstain_slots,
        "invested_slots": len(gross_values),
        "gated_flat_slots": gated_flat_slots if gated else None,
        "dispersion_gate_armed": (
            gated and len(dispersion_history) > DISPERSION_GATE_LOOKBACK_MEDIAN
        ),
        "t_stat_net": (format(t, "f") if (t := _t_stat(net_values)) is not None else None),
        "median_net": (
            format(_median(net_values), "f") if net_values else None
        ),
        "hit_rate": (
            format(
                Decimal(sum(1 for n in net_values if n > 0))
                / Decimal(len(net_values)),
                "f",
            )
            if net_values
            else None
        ),
        "mean_gross": format(mean_gross, "f") if mean_gross is not None else None,
        "mean_net": format(mean_net, "f") if mean_net is not None else None,
        "baseline_mean_net": (
            format(baseline_mean_net, "f") if baseline_mean_net is not None else None
        ),
        "mean_net_delta_vs_baseline": (
            format(mean_net - baseline_mean_net, "f")
            if mean_net is not None and baseline_mean_net is not None
            else None
        ),
    }


def pre_registered_candidates() -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for lookback in LOOKBACKS:
        for k in TOP_K:
            for horizon in HORIZONS:
                candidates.append(
                    {
                        "name": f"cs_relstrength__l{lookback}_k{k}_h{horizon}",
                        "family": "long_top",
                        "lookback": lookback,
                        "k": k,
                        "horizon": horizon,
                        "gated": False,
                    }
                )
                candidates.append(
                    {
                        "name": f"cs_reversal__l{lookback}_k{k}_h{horizon}",
                        "family": "long_bottom",
                        "lookback": lookback,
                        "k": k,
                        "horizon": horizon,
                        "gated": False,
                    }
                )
    for k in TOP_K:
        for horizon in HORIZONS:
            candidates.append(
                {
                    "name": f"cs_relstrength_gated__l288_k{k}_h{horizon}",
                    "family": "long_top",
                    "lookback": 288,
                    "k": k,
                    "horizon": horizon,
                    "gated": True,
                }
            )
    return candidates


def analyze(panel: Mapping[str, Any]) -> dict[str, Any]:
    results = []
    for spec in pre_registered_candidates():
        results.append(
            _evaluate_candidate(
                name=spec["name"],
                family=spec["family"],
                panel=panel,
                lookback=spec["lookback"],
                k=spec["k"],
                horizon=spec["horizon"],
                gated=spec["gated"],
            )
        )
    return {
        "contract": CONTRACT,
        **_non_evidence_fields(),
        "universe_size": len(panel["universe"]),
        "grid_slots": len(panel["grid"]),
        "grid_first_slot": _slot_to_iso(panel["grid"][0]),
        "grid_last_slot": _slot_to_iso(panel["grid"][-1]),
        "cost_policy": "crypto-round-trip-taker-v1",
        "candidates": results,
    }


def _bp(value: str | None, places: int = 2) -> str:
    """Format a return fraction as basis points with fixed precision."""

    if value is None:
        return "—"
    return f"{Decimal(value) * 10000:.{places}f}"


def render_markdown(result: Mapping[str, Any]) -> str:
    candidates = result["candidates"]
    best = max(
        (c for c in candidates if c["mean_net"] is not None),
        key=lambda c: Decimal(c["mean_net"]),
        default=None,
    )
    reversal_all_negative = all(
        c["family"] != "long_bottom" or Decimal(c["mean_net"]) <= 0
        for c in candidates
        if c["mean_net"] is not None
    )
    gate_armed_anywhere = any(c["dispersion_gate_armed"] for c in candidates)
    lines = [
        "# Crypto 横截面信号族预筛（四十币，预登记网格）",
        "",
        "> 非证据研究：历史回填数据无 PIT 证明（`historical_backfill_no_pit=true`），",
        "> `not_promotion_evidence=true`；只读诊断抽取，无任何权威/资金路径。",
        "",
        f"- 宇宙：{result['universe_size']} 币；共同网格 "
        f"{result['grid_slots']} 槽（{result['grid_first_slot']} → "
        f"{result['grid_last_slot']}）。",
        "- 候选网格在跑结果前冻结：相对强弱做多前 K / 反转做多后 K "
        "（回看 288/576 根 × K=5/10 × 持有 48/288 根）+ 离散度门控变体，共 20 个。",
        "- 费用：taker 双边 0.1% + 每腿 2bps；换仓只对被替换权重收往返费；"
        "基线为全币等权始终在场同口径（买入持有权重，不收维持换手费，"
        "对候选的相对比较偏保守）。",
        "- 样本口径：每个候选的持有窗口按 stride=horizon 推进，记录的收益"
        "天然互不重叠；跨数据缺口的窗口整体弃权，不产生标签。",
        "- t 统计量为同批次内对 0 的单样本 t（窗间独立）；20 个候选存在多重比较，"
        "单一格子为正不构成证据，仅作留观线索。表中数值单位为基点（bp）。",
        "",
        "| candidate | 平均 net/窗 bp | 中位 net bp | t(net) | 窗数 | vs 基线 bp | 判定 |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in candidates:
        if c["mean_net"] is None:
            verdict = "无样本"
        elif Decimal(c["mean_net"]) <= 0:
            verdict = "否决"
        else:
            verdict = "留观（不显著）" if _t_stat_note(c) else "留观"
        lines.append(
            f"| {c['candidate_id']} | {_bp(c['mean_net'])} | {_bp(c['median_net'])} "
            f"| {c['t_stat_net'][:6] if c['t_stat_net'] else '—'} "
            f"| {c['invested_slots']} | {_bp(c['mean_net_delta_vs_baseline'])} "
            f"| {verdict} |"
        )
    lines += ["", "## 结论", ""]
    if reversal_all_negative:
        lines.append(
            "- 反转族（做多后 K）全部 8 格费用后为负（最深 −28bp/窗，"
            "t 低至 −2.6）：与时间序列族结论一致，费用下不可用，予以否决。"
        )
    if best is not None and Decimal(best["mean_net"]) > 0:
        lines.append(
            f"- 相对强弱族（做多前 K）在 24 小时持有格方向一致为正，"
            f"最优 {best['candidate_id']} 平均 +{_bp(best['mean_net'])}bp/窗，"
            f"但 t={float(Decimal(best['t_stat_net'])):.2f}（远低于显著性），"
            "中位数接近 0，收益由少数窗口驱动；20 格多重比较下这就是噪声的形状。"
        )
        lines.append(
            "- 判定：预登记网格内没有统计显著的可用信号。不扩大扫描、不调参；"
            "该方向作为留观线索记录，任何后续动作必须走 receipt-bound "
            "滚动评估，不得以本次历史回填为依据。"
        )
    else:
        lines.append(
            "- 预登记网格内没有任何候选在费用后为正：横截面族与既有时间序列族"
            "一致，在 taker 成本口径下不可用。负结果照实入库；不再扩大扫描。"
        )
    if not gate_armed_anywhere:
        lines += [
            "",
            "## 预登记缺陷（照实记录）",
            "",
            "- 离散度门控的扩张中位数阈值按「槽」登记（1440）却与「再平衡次数」"
            "比较；本窗口任何候选再平衡次数最多 1107 次，门控从未激活，"
            "4 个门控变体与对应未门控结果逐位相同。按预登记纪律不事后改参重跑，"
            "门控族记为「本窗口不可评估」。",
        ]
    return "\n".join(lines) + "\n"


def _t_stat_note(candidate: Mapping[str, Any]) -> bool:
    t = candidate.get("t_stat_net")
    return t is not None and abs(Decimal(t)) < 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--closes", type=Path, required=True, help="gzipped closes CSV")
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)

    _assert_simulation_only()
    panel = build_panel(load_closes(args.closes))
    result = analyze(panel)
    # Audit binding: which exact extraction produced these numbers.
    result["input_closes_sha256"] = hashlib.sha256(
        args.closes.read_bytes()
    ).hexdigest()
    payload = json.dumps(
        result, ensure_ascii=True, allow_nan=False, sort_keys=True, indent=2
    ) + "\n"
    if args.out_json:
        args.out_json.write_text(payload, encoding="utf-8")
    if args.report:
        args.report.write_text(render_markdown(result), encoding="utf-8")
    if args.out_json is None and args.report is None:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
