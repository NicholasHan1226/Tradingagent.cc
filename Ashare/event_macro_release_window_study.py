"""Macro release-window conditioning study (market-level timing panel).

Realizes the frozen preregistration
(``Ashare/reports/2026-08-24-macro-release-window-preregistration.md``,
merged BEFORE any bucket-return computation).  D1 release-day set =
union of presumed publication days from the static cadence rule table
(CPI/PPI -> 9th of the month AFTER the data month, M2 -> 11th, GDP ->
17th of the month after quarter end), each shifted FORWARD to the next
trading day when it lands off-calendar.  The rule table uses ONLY
calendar information knowable before the release day (period keys +
fixed cadence — no market data, no actual-value backfill), frozen as
the no-look-ahead clause; presumed-vs-actual schedule error (+/-0-3
natural days) attenuates separation toward the conservative direction.
Window is ``[entry-1td, entry+1td]`` on the locked index calendar.

D2 buckets with unique-label precedence ante > same_day > post >
outside (multi-release windows exist: 57 all-arm / 17 rule-arm):
ante holds through uncertainty, same_day has an ambiguous intraday
information state (official times span pre-open to post-close across
indicators), post enters after resolution.  H1 frozen: rule-arm
``ante`` double-high vs the UNFILTERED rule-arm baseline (mean AND
win rate, n >= 30 gate); same_day/post never enter promotion judgment
under this preregistration (post n=16 fails the gate).

D3 zero imputation; placeholder rows (period key present, ALL value
columns empty — measured transient near publication boundaries) keep
contributing their period key but are counted as QA; consumers
discard them defensively at read time (blocktrade #23 / holdernum
#497 double-guard lesson).  Missing cache file fails closed.

D4 locked baseline after-caliber engine (limit-lock realism fix),
15bps roundtrip, loaders identical to the family; one-shot readout —
no bucketed returns before this engine PR merges after the prereg.

research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_macro_release_window_study.py [--cache DIR]
        [--cost-bps N]
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_calendar_lockup_strata import COST_BPS_ROUNDTRIP_DEFAULT  # noqa: E402
from Ashare.event_margin_crowding_state import cross_tab, net_trade_return  # noqa: E402
from Ashare.event_paper_baseline_sim import (  # noqa: E402
    SIM_START,
    build_signals,
    load_events,
    load_index_series,
    load_stock_books,
    rule_arm_filter,
)

#: file stem -> csv filename beside ``daily_*.csv`` in the shared cache.
MACRO_FILES: dict[str, str] = {
    "gdp": "macro_gdp.csv",
    "cpi": "macro_cpi.csv",
    "ppi": "macro_ppi.csv",
    "money": "macro_money.csv",
}

#: period-key column per endpoint (labels depend ONLY on these keys).
KEY_COLUMN: dict[str, str] = {
    "gdp": "quarter",
    "cpi": "month",
    "ppi": "month",
    "money": "month",
}

#: monthly cadence rule: publication day-of-month AFTER the data month.
MONTHLY_CADENCE_DAY: dict[str, int] = {"cpi": 9, "ppi": 9, "money": 11}

#: quarterly cadence rule: GDP publishes day 17 of the month after quarter end.
GDP_CADENCE_DAY = 17
QUARTER_END_MONTH: dict[str, int] = {"Q1": 3, "Q2": 6, "Q3": 9, "Q4": 12}

MACRO_BUCKETS: tuple[str, ...] = ("ante", "same_day", "post", "outside")

WATCH_LIST_MIN_N = 30  # family-standard gate, frozen in the prereg


class MacroStudyError(RuntimeError):
    """Fail-closed study failure with a stable reason code."""


def _add_month(ym: str, k: int) -> str:
    """``"202612"`` + 1 month -> ``"202701"``."""
    year, month = int(ym[:4]), int(ym[4:])
    total = year * 12 + (month - 1) + k
    return f"{total // 12:04d}{total % 12 + 1:02d}"


def _all_values_empty(fieldnames: list[str], row: dict[str, str]) -> bool:
    """True when every non-key column is empty — D3 placeholder shape."""
    key_col = KEY_COLUMN["gdp"] if "quarter" in fieldnames else "month"
    return all(
        row.get(name) in (None, "")
        for name in fieldnames
        if name != key_col
    )


def presumed_release_days(
    cache: Path, trading_days: set[str]
) -> tuple[dict[str, set[str]], dict[str, int]]:
    """Union release-day table plus per-endpoint placeholder QA counts.

    Reads ONLY period keys (frozen D1 no-look-ahead clause); values are
    irrelevant to labels.  Each presumed day shifts forward to the next
    trading day while off-calendar.
    """
    release: dict[str, set[str]] = {}
    placeholders: dict[str, int] = {}
    for stem, filename in MACRO_FILES.items():
        path = cache / filename
        if not path.exists():
            raise MacroStudyError(f"macro_cache_missing:{stem}")
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
        placeholders[stem] = sum(
            1 for row in rows if _all_values_empty(fieldnames, row)
        )
        for row in rows:
            raw_key = (row.get(KEY_COLUMN[stem]) or "").strip()
            if not raw_key:
                continue
            if stem == "gdp":
                quarter = raw_key[4:]
                base = _add_month(raw_key[:4] + f"{QUARTER_END_MONTH[quarter]:02d}", 1)
                presumed = f"{base}{GDP_CADENCE_DAY}"
            else:
                presumed = (
                    f"{_add_month(raw_key, 1)}{MONTHLY_CADENCE_DAY[stem]:02d}"
                )
            day = presumed
            probe = date(int(day[:4]), int(day[4:6]), int(day[6:]))
            while day not in trading_days:
                probe += timedelta(days=1)
                if probe > date.fromordinal(
                    date(int(presumed[:4]), int(presumed[4:6]),
                         int(presumed[6:])).toordinal() + 15
                ):
                    # calendar must cover the presumed day's neighbourhood;
                    # an endless shift means a broken/partial calendar
                    raise MacroStudyError(f"release_day_unresolvable:{presumed}")
                day = probe.strftime("%Y%m%d")
            release.setdefault(day, set()).add(stem)
    return release, placeholders


def label_for_entry(
    entry_day: str,
    release_days: set[str],
    days: list[str],
    pos_of: dict[str, int],
) -> str:
    """Frozen D2 bucket with precedence ante > same_day > post > outside."""
    idx = pos_of[entry_day]
    upper = days[idx + 1] if idx + 1 < len(days) else entry_day
    lower = days[idx - 1] if idx > 0 else entry_day
    if any(entry_day < d <= upper for d in release_days):
        return "ante"
    if entry_day in release_days:
        return "same_day"
    if any(lower <= d < entry_day for d in release_days):
        return "post"
    return "outside"


def attach_macro_states(
    signals: list[dict[str, object]],
    release_days: dict[str, set[str]],
    days: list[str],
    pos_of: dict[str, int],
) -> dict[str, int]:
    """Annotate each signal with its release-window bucket (unique label)."""
    stats: dict[str, int] = {bucket: 0 for bucket in MACRO_BUCKETS}
    stats["attached"] = 0
    keys = set(release_days)
    for signal in signals:
        entry_day = str(signal["entry_day"])
        bucket = label_for_entry(entry_day, keys, days, pos_of)
        signal["macro_bucket"] = bucket
        signal["macro_release_indicators"] = sorted(
            release_days.get(entry_day, set())
        ) if bucket == "same_day" else []
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


def _double_high(cell: dict, baseline: dict) -> bool:
    return bool(
        cell["n"] >= WATCH_LIST_MIN_N
        and cell["mean_net_bps"] is not None
        and baseline["mean_net_bps"] is not None
        and float(cell["mean_net_bps"]) > float(baseline["mean_net_bps"])
        and float(cell["win_rate"]) > float(baseline["win_rate"])
    )


def run_study(
    cache: Path, cost_bps: float = COST_BPS_ROUNDTRIP_DEFAULT
) -> dict[str, object]:
    """One-shot readout of the frozen panel #13 preregistration."""
    index_pairs = load_index_series(cache)
    days = [d.strftime("%Y%m%d") for d, _ in index_pairs]
    pos_of = {d: i for i, d in enumerate(days)}
    global_days = [d for d in days if d >= SIM_START]

    events, _stats = load_events(cache)
    books, uncovered = load_stock_books(cache)
    signals, _sig_stats = build_signals(events, books, index_pairs, global_days[-1])

    release, placeholders = presumed_release_days(cache, set(days))
    attach_stats = attach_macro_states(signals, release, days, pos_of)

    results: dict[str, object] = {
        "research_only": True,
        "not_promotion_evidence": True,
        "cost_bps_roundtrip": cost_bps,
        "signals_total": len(signals),
        "attach_stats": attach_stats,
        "placeholder_rows_by_endpoint": placeholders,
        "release_days_in_calendar_span": len(release),
        "universe_uncovered_symbols": uncovered,
    }
    print("## 宏观发布窗条件层研究（research_only，非晋级证据，面板 #13）")
    print(f"- 推定发布日并集 {len(release)} 天；窗口 [entry−1td, entry+1td]；"
          f"标签优先级 ante > same_day > post > outside；成本 {cost_bps}bps 往返")
    print(f"- 占位行 QA 计数（键仍参与定日）：{placeholders}")

    tab = cross_tab(signals, cost_bps=cost_bps, key="macro_bucket",
                    labels=MACRO_BUCKETS)
    results["r1_signal_level_cross_tab"] = tab
    print("\n### R1 信号层四桶交叉表（净 bps / 胜率）")
    print(f"{'bucket':<10} {'n':>6} {'mean_net_bps':>13} {'win_rate':>9}")
    for label, cell in tab.items():
        mean_txt = ("—" if cell["mean_net_bps"] is None
                    else f"{float(cell['mean_net_bps']):+.1f}")
        win_txt = ("—" if cell["win_rate"] is None
                   else f"{float(cell['win_rate']):.3f}")
        print(f"{label:<10} {cell['n']:>6} {mean_txt:>13} {win_txt:>9}")

    rule_signals = [s for s in signals if rule_arm_filter(s)]
    rule_tab = cross_tab(rule_signals, cost_bps=cost_bps,
                         key="macro_bucket", labels=MACRO_BUCKETS)
    baseline = _baseline_cell(rule_signals, cost_bps)
    results["r2_rule_arm_cross_tab"] = rule_tab
    results["r2_rule_unfiltered_baseline"] = baseline
    print("\n### R2 rule 臂叠加交叉表（弱市×非3–5%带，定义不动）与未滤基线")
    print(f"{'bucket':<10} {'n':>6} {'mean_net_bps':>13} {'win_rate':>9}")
    for label, cell in {**rule_tab, "UNFILTERED": baseline}.items():
        mean_txt = ("—" if cell["mean_net_bps"] is None
                    else f"{float(cell['mean_net_bps']):+.1f}")
        win_txt = ("—" if cell["win_rate"] is None
                   else f"{float(cell['win_rate']):.3f}")
        print(f"{label:<10} {cell['n']:>6} {mean_txt:>13} {win_txt:>9}")

    ante_cell = rule_tab.get("ante", {"n": 0, "mean_net_bps": None,
                                      "win_rate": None})
    eligible = _double_high(ante_cell, baseline)
    results["h1_primary_contrast"] = {
        "rule_ante": ante_cell,
        "rule_unfiltered_baseline": baseline,
        "watch_list_eligible": eligible,
    }
    verdict = "进观察名单" if eligible else "未达标（FAIL 同样是合格产出）"
    print(f"\n### H1 冻结判定：rule 臂 ante 对未滤基线双高且 n≥{WATCH_LIST_MIN_N}"
          f" ⇒ {verdict}")
    print("- same_day/post 为描述性桶，本预注册下永不进入升降级判定。")
    print("- 无论结果均不作部署候选；反向使用需独立重新预注册。")
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--cost-bps", type=float,
                        default=COST_BPS_ROUNDTRIP_DEFAULT)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    import json

    print(json.dumps(run_study(cache, cost_bps=args.cost_bps),
                     ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MacroStudyError as exc:
        print(f"MACRO_STUDY_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
