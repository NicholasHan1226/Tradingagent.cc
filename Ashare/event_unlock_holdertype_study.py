"""Unlock-batch holder-type conditioning layer (panel #16).

Frozen preregistration: ``Ashare/reports/2026-08-25-unlock-holdertype-
preregistration.md`` (merged before this engine; no bucketed returns were
computed before that merge).  Zero new data dependency: labels come from
the family's ``share_float.csv`` ``share_type`` field keyed by the event
identity (ts_code, float_date).  Mixed-type batches (~9.5% of keys) are
collapsed by frozen presence-precedence ordered by hypothesized mechanical
supply pressure:

  placement    : 定增股份 or 公开增发一般股份  (hard-exit financial investors)
  insider      : 首发原始股 or 首发战略配售股份 (control-motivated, weak supply)
  incentive    : 股权激励限售流通              (tax-driven small sales)
  other_legacy : everything else (股权分置/其他类型), reference-only

H1 (frozen, negative): rule[placement] mean & win-rate both BELOW the
unfiltered rule arm.  research_only / not_promotion_evidence.
"""

from __future__ import annotations

import argparse
import csv
import sys
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

HOLDERTYPE_BUCKETS = ("placement", "insider", "incentive", "other_legacy",
                      "no_match")
PLACEMENT_TYPES = {"定增股份", "公开增发一般股份"}
INSIDER_TYPES = {"首发原始股", "首发战略配售股份"}
INCENTIVE_TYPES = {"股权激励限售流通"}
WATCH_LIST_MIN_N = 30  # family-standard gate, frozen in the prereg


class HolderTypeStudyError(RuntimeError):
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
    """Frozen H1 verdict shape (same gate family as panels #13/#14/#15)."""
    return bool(
        cell["n"] >= WATCH_LIST_MIN_N
        and cell["mean_net_bps"] is not None
        and baseline["mean_net_bps"] is not None
        and float(cell["mean_net_bps"]) < float(baseline["mean_net_bps"])
        and float(cell["win_rate"]) < float(baseline["win_rate"])
    )


def holdertype_bucket(types: set[str] | frozenset[str]) -> str:
    """Frozen D1 presence-precedence for one batch's share_type set."""
    if types & PLACEMENT_TYPES:
        return "placement"
    if types & INSIDER_TYPES:
        return "insider"
    if types & INCENTIVE_TYPES:
        return "incentive"
    if types:
        return "other_legacy"
    return "no_match"


def load_holdertype_index(cache: Path) -> dict[tuple[str, str], set[str]]:
    """One pass over share_float.csv -> {(ts_code, float_date): type set}.

    Validity filter mirrors ``load_events`` (float_date >= SIM_START,
    float_date >= ann_date, parseable ratio) so labels describe exactly
    the batches the signal stream is built from.
    """
    path = cache / "share_float.csv"
    if not path.exists():
        raise HolderTypeStudyError("cache_missing:share_float.csv")
    index: dict[tuple[str, str], set[str]] = {}
    with path.open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            code = row.get("ts_code") or ""
            ann = row.get("ann_date") or ""
            float_day = row.get("float_date") or ""
            try:
                float(row.get("float_ratio"))
            except (TypeError, ValueError):
                continue
            if not code or not float_day or float_day < SIM_START \
                    or float_day < ann:
                continue
            stype = row.get("share_type") or ""
            if stype:
                index.setdefault((code, float_day), set()).add(stype)
    return index


def attach_holdertype_bucket(
    signals: list[dict[str, object]],
    index: dict[tuple[str, str], set[str]],
) -> dict[str, int]:
    """Annotate each signal with its unlock-batch holder-type bucket.

    Labels key on float_date (event identity), never entry_day."""
    stats: dict[str, int] = {bucket: 0 for bucket in HOLDERTYPE_BUCKETS}
    stats["attached"] = 0
    for signal in signals:
        key = (str(signal["ts_code"]), str(signal["float_date"]))
        bucket = holdertype_bucket(index.get(key, set()))
        signal["holdertype_bucket"] = bucket
        stats[bucket] += 1
        stats["attached"] += 1
    return stats


def holdertype_buckets_for_entries(
    cache: Path,
    entries: list[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    """Side-table helper: (ts_code, float_date) -> frozen bucket label.

    Keys on float_date identity exactly like ``attach_holdertype_bucket``
    (event identity, never entry_day).  Unknown batches map to
    ``no_match`` rather than erroring, mirroring engine semantics;
    missing share_float.csv stays fail-closed and the tracker wrapper
    degrades to "no labels" instead of breaking tracking.
    """
    index = load_holdertype_index(cache)
    return {
        (code, day): holdertype_bucket(index.get((code, day), set()))
        for code, day in dict.fromkeys(entries)
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

    index = load_holdertype_index(cache)
    attach_stats = attach_holdertype_bucket(signals, index)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "universe_uncovered_symbols": uncovered,
    }
    print("## 解禁批次持有人类型条件层研究（research_only，非晋级证据）")
    print(f"- 批次=(ts_code, float_date) 全行 share_type 集合；冻结优先级 "
          f"placement>insider>incentive>other_legacy（混型按最强机械供给排序）；"
          f"成本 {cost_bps}bps 往返")

    tab = cross_tab(signals, cost_bps=cost_bps, key="holdertype_bucket",
                    labels=HOLDERTYPE_BUCKETS)
    results["r1_signal_level_cross_tab"] = tab
    print("\n### R1 信号层五桶交叉表（净 bps / 胜率）")
    print(f"{'bucket':<13} {'n':>6} {'mean_net_bps':>13} {'win_rate':>9}")
    for label, cell in tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"{label:<13} {cell['n']:>6} {mean_txt:>13} {win_txt:>9}")

    rule_signals = [s for s in signals if rule_arm_filter(s)]
    rule_tab = cross_tab(rule_signals, cost_bps=cost_bps,
                         key="holdertype_bucket", labels=HOLDERTYPE_BUCKETS)
    baseline = _baseline_cell(rule_signals, cost_bps)
    results["r2_rule_arm_cross_tab"] = rule_tab
    results["r2_rule_unfiltered_baseline"] = baseline
    print("\n### R2 rule 臂叠加交叉表（弱市×非3–5%带，定义不动）与未滤基线")
    for label, cell in {**rule_tab, "UNFILTERED": baseline}.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"rule[{label:<12}] n={cell['n']:>4} {mean_txt}bps win={win_txt}")

    place_cell = rule_tab.get("placement",
                              {"n": 0, "mean_net_bps": None, "win_rate": None})
    eligible = _double_low(place_cell, baseline)
    results["h1_primary_contrast"] = {
        "rule_placement": place_cell,
        "rule_unfiltered_baseline": baseline,
        "watch_list_eligible": eligible,
    }
    verdict = "进观察名单" if eligible else "未达标（FAIL 同样是合格产出）"
    print(f"\n### H1 冻结判定：rule 臂 placement 对未滤基线双低且 n≥"
          f"{WATCH_LIST_MIN_N} ⇒ {verdict}")

    mismatch = sum(
        1 for s in signals if str(s["entry_day"]) != str(s["float_date"])
    )
    results["r3_coverage"] = {
        "attached": attach_stats["attached"],
        "no_match": attach_stats["no_match"],
        "entry_vs_float_mismatch": mismatch,
        "batch_keys_in_index": len(index),
    }
    print("\n### R3/HV 覆盖与健康检查")
    cov = results["r3_coverage"]
    print(f"- 已贴标 {cov['attached']}/{len(signals)}（no_match="
          f"{cov['no_match']}，预注册基准 all 桶计数 441/178/385/31/0）；"
          f"entry≠float_date {cov['entry_vs_float_mismatch']} 条"
          f"（预注册基准 5）；批次键 {cov['batch_keys_in_index']}")
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
    except HolderTypeStudyError as exc:
        print(f"HOLDERTYPE_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(2)
