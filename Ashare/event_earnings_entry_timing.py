"""Entry-timing refinement for the earnings positive-drift signal.

Research-only one-off analysis.  The rolling tracker's first pre-window
reading for ``earnings_pos`` (enter at the first session after the forecast
announcement, exit at the disclosure-day close) came out strongly negative
(n=20, mean -365 bps) against a historical pre-10d excess drift of
+150 bps.  This script answers the descriptive follow-up on the full
2018-2026 expanded sample: **where inside the announcement->disclosure
window does the drift actually accrue**, i.e. how much of the move is left
when entry is delayed to k sessions before the disclosure.

Definitions (mirroring the tracker so numbers stay comparable):

* event = positive-group disclosure (earliest prior forecast type 预增/
  略增/续盈/预盈/扭亏 per (ts_code, end_date));
* earliest executable entry = close of the first session strictly AFTER
  the forecast announcement date;
* exit = disclosure appointment day close (hard date);
* ``k-entry`` return = close(exit) / close(max(j-k, first_entry)) - 1 with
  j the exit index — entering k sessions before disclosure but never
  before the forecast existed;
* both absolute and SSE-index excess versions are reported; net columns
  deduct one round trip (same 15 bps default as the strata module).

Nothing here is promotion evidence; results feed the signal-definition
discussion only.
"""

from __future__ import annotations

import argparse
import bisect
import json
import statistics
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_calendar_earnings_groups import (  # noqa: E402
    load_disclosure_events,
    load_forecast_directions,
)
from Ashare.event_calendar_lockup_strata import COST_BPS_ROUNDTRIP_DEFAULT  # noqa: E402
from Ashare.event_calendar_stats import (  # noqa: E402
    DailyPanel,
    _read_csv,
    load_index_series,
    load_symbol_series,
)

K_OFFSETS = (1, 2, 3, 5, 10, 15, 20)
LEAD_BUCKETS = ((0, 10, "lead<10"), (10, 30, "lead10-30"), (30, 10**9, "lead>30"))


def stats_line(values: list[float], cost_bps: float) -> dict:
    n = len(values)
    if not n:
        return {"n": 0}
    ordered = sorted(values)
    net = [v - cost_bps / 1e4 for v in values]
    return {
        "n": n,
        "mean_bps": round(statistics.fmean(values) * 1e4, 1),
        "median_bps": round(statistics.median(values) * 1e4, 1),
        "win_rate": round(sum(1 for v in values if v > 0) / n, 3),
        "mean_net_bps": round(statistics.fmean(net) * 1e4, 1),
        "win_net": round(sum(1 for v in net if v > 0) / len(net), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=Path("/tmp/ashare_event_research"))
    parser.add_argument("--cost-bps", type=float, default=COST_BPS_ROUNDTRIP_DEFAULT)
    args = parser.parse_args()
    cache = args.cache
    cost = args.cost_bps
    symbol_file = "sample_symbols_expanded"

    _fields, sym_rows = _read_csv(cache, symbol_file)
    samples = {r["ts_code"] for r in sym_rows}

    index_panel = DailyPanel(load_index_series(cache))
    panels: dict[str, DailyPanel] = {}
    for code in sorted(samples):
        series = load_symbol_series(cache, code)
        if len(series) >= 250:
            panels[code] = DailyPanel(series)
    print(f"panels={len(panels)}", flush=True)

    forecasts = load_forecast_directions(cache)
    buckets, counts = load_disclosure_events(cache, samples, forecasts)
    positives = sorted(set(buckets.get("forecast_positive", [])))
    print(f"positive_events={len(positives)} counts={counts}", flush=True)

    _f, disc_rows = _read_csv(cache, "disclosure")
    period_by_key = {(r["ts_code"], r["pre_date"]): r["end_date"] for r in disc_rows}

    abs_full_all: list[float] = []
    ex_full_all: list[float] = []
    abs_by_k: dict[int, list[float]] = {k: [] for k in K_OFFSETS}
    ex_by_k: dict[int, list[float]] = {k: [] for k in K_OFFSETS}
    lead_records: dict[str, dict[str, list[float]]] = {
        label: {"full": [], "k10": []} for _, _, label in LEAD_BUCKETS
    }
    excess_by_year: dict[str, list[float]] = {}
    skipped_no_prewindow = 0

    for code, pre_date in positives:
        panel = panels.get(code)
        j = panel.pos.get(pre_date) if panel else None
        prior = forecasts.get((code, period_by_key.get((code, pre_date), "")))
        if panel is None or j is None or prior is None:
            continue
        first_entry = bisect.bisect_right(panel.dates, prior[0])
        if first_entry >= j:
            skipped_no_prewindow += 1
            continue
        i_exit = index_panel.pos.get(pre_date)
        ret_full = panel.closes[j] / panel.closes[first_entry] - 1.0
        abs_full_all.append(ret_full)
        i_entry = index_panel.pos.get(panel.dates[first_entry])
        ex_full = (
            ret_full - (index_panel.closes[i_exit] / index_panel.closes[i_entry] - 1.0)
            if (
                i_entry is not None
                and i_exit is not None
                and index_panel.closes[i_entry] > 0
            )
            else None
        )
        if ex_full is not None:
            ex_full_all.append(ex_full)
            excess_by_year.setdefault(prior[0][:4], []).append(ex_full)
        lead = j - first_entry
        for k in K_OFFSETS:
            entry_idx = max(j - k, first_entry)
            if entry_idx >= j:
                continue
            ret_k = panel.closes[j] / panel.closes[entry_idx] - 1.0
            abs_by_k[k].append(ret_k)
            e_idx = index_panel.pos.get(panel.dates[entry_idx])
            if e_idx is not None and i_exit is not None and index_panel.closes[e_idx] > 0:
                ex_by_k[k].append(
                    ret_k - (index_panel.closes[i_exit] / index_panel.closes[e_idx] - 1.0)
                )
            if k == 10:
                bucket = next(lab for lo, hi, lab in LEAD_BUCKETS if lo <= lead < hi)
                lead_records[bucket]["full"].append(ret_full)
                lead_records[bucket]["k10"].append(ret_k)

    summary = {
        "research_only": True,
        "universe": symbol_file,
        "cost_bps_roundtrip": cost,
        "positive_events_grouped": len(positives),
        "skipped_no_prewindow": skipped_no_prewindow,
        "absolute": {
            "first_session_after_announcement": stats_line(abs_full_all, cost),
            **{
                f"enter_{k}_sessions_before_disclosure": stats_line(abs_by_k[k], cost)
                for k in K_OFFSETS
            },
        },
        "excess_vs_sse": {
            "first_session_after_announcement": stats_line(ex_full_all, cost),
            **{
                f"enter_{k}_sessions_before_disclosure": stats_line(ex_by_k[k], cost)
                for k in K_OFFSETS
            },
        },
        "by_lead_bucket_enter10": {
            bucket: {
                "full_window": stats_line(v["full"], cost),
                "enter_10": stats_line(v["k10"], cost),
            }
            for bucket, v in lead_records.items()
        },
        "excess_by_year": {
            year: stats_line(values, cost)
            for year, values in sorted(excess_by_year.items())
        },
    }

    out_path = cache / "earnings_entry_timing.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n# Earnings positive-drift entry timing (research_only)\n")
    print(
        f"- universe={symbol_file}, grouped_positive={len(positives)}, "
        f"no_prewindow_skipped={skipped_no_prewindow}"
    )
    print(f"- cost model: {cost}bps round trip deducted once in *_net columns\n")
    for section, title in (
        ("absolute", "绝对收益口径（与跟踪器入账一致）"),
        ("excess_vs_sse", "超额口径（减上证同窗，与历史 +150bps 读数可比）"),
    ):
        print(f"## {title}\n")
        print("| 入场时点 | n | mean | median | win | mean_net | win_net |")
        print("|---|---|---|---|---|---|---|")
        for key, stat in summary[section].items():
            if not stat.get("n"):
                print(f"| {key} | 0 | | | | | |")
                continue
            print(
                f"| {key} | {stat['n']} | {stat['mean_bps']} | {stat['median_bps']} "
                f"| {stat['win_rate']} | {stat['mean_net_bps']} | {stat['win_net']} |"
            )
        print()
    print("## 按预告→披露窗口长度分桶（T-10 入场捕获多少）\n")
    print("| 窗口长度 | 全程持有 mean | 全程 win | T-10 入场 mean | T-10 win |")
    print("|---|---|---|---|---|")
    for bucket, v in summary["by_lead_bucket_enter10"].items():
        full_s, k10_s = v["full_window"], v["enter_10"]
        if not full_s.get("n"):
            print(f"| {bucket} | 0 | | | |")
            continue
        print(
            f"| {bucket} | {full_s['mean_bps']} | {full_s['win_rate']} "
            f"| {k10_s.get('mean_bps', '')} | {k10_s.get('win_rate', '')} |"
        )
    print("\n## 超额口径分年度（全程持有，公告次日入场）\n")
    print("| 年份(按预告公告日) | n | mean_net | median | win_net |")
    print("|---|---|---|---|---|")
    for year, stat in summary["excess_by_year"].items():
        if not stat.get("n"):
            continue
        print(
            f"| {year} | {stat['n']} | {stat['mean_net_bps']} "
            f"| {stat['median_bps']} | {stat['win_net']} |"
        )
    print(f"\nsaved -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
