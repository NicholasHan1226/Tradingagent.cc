"""Top-ten register conditioning layer (panel #21).

Frozen preregistration: ``Ashare/reports/2026-08-25-top10-register-
preregistration.md`` (merged as PR #557 before this engine; no bucketed
returns were computed before that merge).  Labels come from the
per-symbol ``top10_<stem>.csv`` disclosure history fetched by PR #539:
for each event key the register snapshot is the latest fully-disclosed
top-ten list before the lockup expiry (max ``(end_date, ann_date)``
among rows with both dates <= float_date, full scan, no early break).

holder_type is collapsed into three macro classes by frozen explicit
mapping (closed under vocabulary drift — unknown values default to
``corp_or_unknown``):

  natural        : 自然人
  fin_inst       : 26-value explicit institutional list
  corp_or_unknown: everything else incl. blank values

Seat-share buckets of the snapshot: ``natural_heavy`` (natural share
>= 0.7), else ``fin_inst_heavy`` (institutional share >= 0.7), else
``mixed_other``; missing file / no qualifying snapshot -> ``no_match``
with the reason split recorded.

H1 (frozen, negative): rule[natural_heavy] mean & win-rate both BELOW
the unfiltered rule arm; n >= 30 gate or the bucket is pre-downgraded
to descriptive-only.  fin_inst_heavy / mixed_other are designated
secondary descriptive contrasts by the prereg itself (no gate verdict).
research_only / not_promotion_evidence.
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

REGISTER_BUCKETS = ("natural_heavy", "fin_inst_heavy", "mixed_other",
                    "no_match")
NO_MATCH_REASONS = ("no_file", "no_early_snapshot")
NATURAL_TYPES = frozenset({"自然人"})
FIN_INST_TYPES = frozenset({
    "开放式投资基金", "封闭式投资基金", "基金管理公司", "基金专户理财",
    "其他金融产品", "资产管理公司", "券商集合资产管理计划", "保险资管产品",
    "社保基金、社保机构", "基本养老保险基金", "企业年金", "保险投资组合",
    "金融机构—证券公司", "金融机构—银行", "金融机构—信托公司",
    "金融机构—保险公司", "金融机构—期货公司", "金融机构—金融租赁公司",
    "证券公司", "银行", "保险公司", "信托公司集合信托计划",
    "信托公司单一证券信托", "信托公司", "财务公司", "公益基金",
})
HEAVY_SHARE = 0.7
WATCH_LIST_MIN_N = 30  # family-standard gate, frozen in the prereg


class RegisterStudyError(RuntimeError):
    """Fail-closed study error with a stable reason code."""


def macro_class(holder_type: str) -> str:
    """Frozen three-way holder_type mapping (unknown -> corp_or_unknown)."""
    if holder_type in NATURAL_TYPES:
        return "natural"
    if holder_type in FIN_INST_TYPES:
        return "fin_inst"
    return "corp_or_unknown"


def _bucket_from_counts(counts: list[int]) -> str:
    total = counts[0] + counts[1] + counts[2]
    if total <= 0:
        return "no_match"
    if counts[0] / total >= HEAVY_SHARE:
        return "natural_heavy"
    if counts[1] / total >= HEAVY_SHARE:
        return "fin_inst_heavy"
    return "mixed_other"


def load_register_index(
    cache: Path,
) -> dict[str, list[tuple[tuple[str, str], list[int]]]]:
    """One pass over top10_*.csv -> {ts_code: sorted [((ed, ad), [nat,fin,oth])]}.

    Rows with non-digit dates are skipped; snapshots aggregate seat
    counts by macro class per ``(end_date, ann_date)`` pair and are
    sorted ascending so lookups can keep the LAST qualifying entry
    (full-scan semantics, no early break).
    """
    paths = sorted(cache.glob("top10_*.csv"))
    if not paths:
        raise RegisterStudyError("cache_missing:top10_*.csv")
    index: dict[str, dict[tuple[str, str], list[int]]] = {}
    for path in paths:
        stem = path.stem.removeprefix("top10_")
        code = f"{stem[:6]}.{stem[6:]}"
        snaps = index.setdefault(code, {})
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                end_day = row.get("end_date") or ""
                ann_day = row.get("ann_date") or ""
                if not end_day.isdigit() or not ann_day.isdigit():
                    continue
                counts = snaps.setdefault((end_day, ann_day), [0, 0, 0])
                counts[{"natural": 0, "fin_inst": 1}.get(
                    macro_class((row.get("holder_type") or "").strip()), 2
                )] += 1
    return {
        code: sorted(snaps.items()) for code, snaps in index.items()
    }


def register_bucket(
    index: dict[str, list[tuple[tuple[str, str], list[int]]]],
    ts_code: str,
    float_date: str,
) -> str:
    """Latest fully-disclosed register before ``float_date`` -> bucket."""
    candidates = index.get(ts_code)
    if candidates is None:
        return "no_match"
    best: list[int] | None = None
    for (end_day, ann_day), counts in candidates:  # ascending order scan
        if end_day <= float_date and ann_day <= float_date:
            best = counts  # keep last qualifying; NO early break
    if best is None:
        return "no_match"
    return _bucket_from_counts(best)


def attach_register_bucket(
    signals: list[dict[str, object]],
    index: dict[str, list[tuple[tuple[str, str], list[int]]]],
) -> dict[str, int]:
    """Annotate each signal with its pre-expiry register bucket.

    Labels key on float_date (event identity), never entry_day.  The
    no_match reason split is recorded on the signal as
    ``reg_no_match_reason`` ('' when matched)."""
    stats: dict[str, int] = {bucket: 0 for bucket in REGISTER_BUCKETS}
    stats["attached"] = 0
    for signal in signals:
        code = str(signal["ts_code"])
        float_day = str(signal["float_date"])
        bucket = register_bucket(index, code, float_day)
        reason = ""
        if bucket == "no_match":
            # no qualifying snapshot: distinguish a missing per-symbol
            # file from a file with only post-expiry disclosures
            reason = "no_file" if index.get(code) is None \
                else "no_early_snapshot"
        signal["reg_bucket"] = bucket
        signal["reg_no_match_reason"] = reason
        stats[bucket] += 1
        stats["attached"] += 1
    return stats


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
    """Frozen H1 verdict shape (same gate family as panels #13–#16)."""
    return bool(
        cell["n"] >= WATCH_LIST_MIN_N
        and cell["mean_net_bps"] is not None
        and baseline["mean_net_bps"] is not None
        and float(cell["mean_net_bps"]) < float(baseline["mean_net_bps"])
        and float(cell["win_rate"]) < float(baseline["win_rate"])
    )


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
    signals, _sig_stats = build_signals(events, books, index_pairs,
                                        global_days[-1])

    index = load_register_index(cache)
    attach_stats = attach_register_bucket(signals, index)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "universe_uncovered_symbols": uncovered,
    }
    print("## 前十大股东承接结构条件层研究（research_only，非晋级证据）")
    print(f"- 快照=解禁前最近一次完整披露登记（(end_date, ann_date) 最大且 "
          f"双<=float_date，全量扫描）；宏类映射冻结显式清单；席位占比 "
          f">=0.7 判 heavy；成本 {cost_bps}bps 往返")

    tab = cross_tab(signals, cost_bps=cost_bps, key="reg_bucket",
                    labels=REGISTER_BUCKETS)
    results["r1_signal_level_cross_tab"] = tab
    print("\n### R1 信号层四桶交叉表（净 bps / 胜率）")
    print(f"{'bucket':<16} {'n':>6} {'mean_net_bps':>13} {'win_rate':>9}")
    for label, cell in tab.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"{label:<16} {cell['n']:>6} {mean_txt:>13} {win_txt:>9}")

    rule_signals = [s for s in signals if rule_arm_filter(s)]
    rule_tab = cross_tab(rule_signals, cost_bps=cost_bps,
                         key="reg_bucket", labels=REGISTER_BUCKETS)
    baseline = _baseline_cell(rule_signals, cost_bps)
    results["r2_rule_arm_cross_tab"] = rule_tab
    results["r2_rule_unfiltered_baseline"] = baseline
    print("\n### R2 rule 臂叠加交叉表（弱市×非3–5%带，定义不动）与未滤基线")
    for label, cell in {**rule_tab, "UNFILTERED": baseline}.items():
        mean_bps = cell["mean_net_bps"]
        win = cell["win_rate"]
        mean_txt = "—" if mean_bps is None else f"{float(mean_bps):+.1f}"
        win_txt = "—" if win is None else f"{float(win):.3f}"
        print(f"rule[{label:<14}] n={cell['n']:>4} {mean_txt}bps "
              f"win={win_txt}")

    nat_cell = rule_tab.get("natural_heavy",
                            {"n": 0, "mean_net_bps": None, "win_rate": None})
    eligible = _double_low(nat_cell, baseline)
    results["h1_primary_contrast"] = {
        "rule_natural_heavy": nat_cell,
        "rule_unfiltered_baseline": baseline,
        "watch_list_eligible": eligible,
    }
    verdict = "进观察名单" if eligible else "未达标（FAIL 同样是合格产出）"
    print(f"\n### H1 冻结判定：rule 臂 natural_heavy 对未滤基线双低且 n≥"
          f"{WATCH_LIST_MIN_N} ⇒ {verdict}")
    print("（fin_inst_heavy 已按预注册预先降级为描述性对照，不追认）")

    reasons: dict[str, int] = {}
    for signal in signals:
        reason = str(signal["reg_no_match_reason"])
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
    mismatch = sum(
        1 for s in signals if str(s["entry_day"]) != str(s["float_date"])
    )
    results["r3_coverage"] = {
        "attached": attach_stats["attached"],
        "no_match": attach_stats["no_match"],
        "no_match_reasons": reasons,
        "entry_vs_float_mismatch": mismatch,
        "symbols_in_register_index": len(index),
    }
    print("\n### R3/HV 覆盖与健康检查")
    cov = results["r3_coverage"]
    print(f"- 已贴标 {cov['attached']}/{len(signals)}（no_match="
          f"{cov['no_match']}；覆盖率证据由独立标签探针留存，本引擎不内嵌"
          f"基准数）；no_match 原因分布 {reasons}；entry≠float_date "
          f"{cov['entry_vs_float_mismatch']} 条；登记索引股票数 "
          f"{cov['symbols_in_register_index']}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--cost-bps", type=float,
                        default=COST_BPS_ROUNDTRIP_DEFAULT)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    run_study(cache, cost_bps=args.cost_bps)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RegisterStudyError as exc:
        print(f"REGISTER_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(2)
