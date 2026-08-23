"""Offline event-window probability statistics for the A-share calendar study.

Research-only.  Reads the scratch cache produced by the companion fetch
script and computes, for each event family, the conditional forward-return
distribution around the event versus the unconditional same-horizon
baseline:

* ``lpr``        -- LPR release dates vs index daily bars,
* ``disclosure`` -- earnings-disclosure appointments vs sample-symbol
  excess returns (symbol minus index over the identical window),
* ``lockup``     -- lockup expiries vs sample-symbol excess returns.

The unconditional baseline for excess returns is built from the same
symbol-day universe over all observable days, not only event days, so the
t-statistics compare like with like.  Nothing here is promotion evidence;
all figures are descriptive historical statistics subject to event
clustering and multiple-comparison caveats that the report must state.

Usage::

    python3 Ashare/event_calendar_stats.py [--cache /tmp/ashare_event_research]
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from pathlib import Path

INDEX_CODE = "000001.SH"
INDEX_CACHE_STEM = "index_000001SH"
PRE_OFFSETS = (5, 10)
POST_OFFSETS = (1, 3, 5)
DRIFT_CURVE_RANGE = range(-10, 6)


class StatsError(RuntimeError):
    """Fail-closed statistics failure with a stable reason code."""


def _read_csv(cache: Path, name: str) -> tuple[list[str], list[dict[str, str]]]:
    path = cache / f"{name}.csv"
    if not path.exists():
        raise StatsError(f"cache_missing:{path.name}")
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fields = next(reader)
        rows = [dict(zip(fields, row)) for row in reader]
    return fields, rows


def load_index_series(cache: Path) -> list[tuple[str, float]]:
    """Load the ordered index (YYYYMMDD, close) series."""
    _fields, rows = _read_csv(cache, INDEX_CACHE_STEM)
    return _sorted_unique_pairs(
        [(r["trade_date"], float(r["close"])) for r in rows], "index"
    )


def load_symbol_series(cache: Path, ts_code: str) -> list[tuple[str, float]]:
    """Load one symbol's forward-adjusted close series.

    Daily bars and adjustment factors arrive as separate cache files; join
    them by trade_date and normalise to the latest factor (qfq).
    """
    stem = ts_code.replace(".", "")
    _fields, bar_rows = _read_csv(cache, f"daily_{stem}")
    _fields, adj_rows = _read_csv(cache, f"adjfactor_{stem}")
    if not bar_rows or not adj_rows:
        return []
    factors = {r["trade_date"]: float(r["adj_factor"]) for r in adj_rows}
    latest = max(factors.values())
    pairs = [
        (r["trade_date"], float(r["close"]) * factors[r["trade_date"]] / latest)
        for r in bar_rows
        if r["trade_date"] in factors and float(r["close"]) > 0
    ]
    return _sorted_unique_pairs(pairs, ts_code)


def _sorted_unique_pairs(
    pairs: list[tuple[str, float]], name: str
) -> list[tuple[str, float]]:
    pairs.sort()
    if len({d for d, _ in pairs}) != len(pairs):
        raise StatsError(f"duplicate_dates:{name}")
    return pairs


def load_events_lpr(cache: Path) -> list[str]:
    _fields, rows = _read_csv(cache, "lpr")
    return sorted({r["date"] for r in rows})


def load_events_disclosure(
    cache: Path, samples: set[str]
) -> list[tuple[str, str]]:
    _fields, rows = _read_csv(cache, "disclosure")
    events = {
        (r["ts_code"], r["pre_date"])
        for r in rows
        if r["ts_code"] in samples and r["pre_date"] >= r["ann_date"]
    }
    return sorted(events)


def load_events_lockup(
    cache: Path, samples: set[str]
) -> list[tuple[str, str]]:
    _fields, rows = _read_csv(cache, "share_float")
    events = {
        (r["ts_code"], r["float_date"])
        for r in rows
        if r["ts_code"] in samples and r["float_date"] >= r["ann_date"]
    }
    return sorted(events)


class DailyPanel:
    """Date-indexed lookup over one ordered close series."""

    def __init__(self, pairs: list[tuple[str, float]]) -> None:
        self.dates = [d for d, _ in pairs]
        self.closes = [c for _, c in pairs]
        self.pos = {d: i for i, d in enumerate(self.dates)}

    def window_return(self, event_idx: int, start_shift: int, end_shift: int) -> float | None:
        """Return close(event_idx+start_shift) -> close(event_idx+end_shift)."""
        a = event_idx + start_shift
        b = event_idx + end_shift
        if a < 0 or b < 0 or b >= len(self.closes):
            return None
        if self.closes[a] <= 0:
            return None
        return self.closes[b] / self.closes[a] - 1.0


def describe(sample: list[float], baseline: list[float]) -> dict:
    n = len(sample)
    if n < 30 or len(baseline) < 100:
        return {"n": n, "insufficient": True}
    mean = statistics.fmean(sample)
    base_mean = statistics.fmean(baseline)
    base_std = statistics.stdev(baseline)
    se = base_std / math.sqrt(n)
    ordered = sorted(sample)
    return {
        "n": n,
        "mean_bps": round(mean * 1e4, 1),
        "median_bps": round(statistics.median(sample) * 1e4, 1),
        "win_rate": round(sum(1 for v in sample if v > 0) / n, 3),
        "p25_bps": round(ordered[n // 4] * 1e4, 1),
        "p75_bps": round(ordered[(3 * n) // 4] * 1e4, 1),
        "base_mean_bps": round(base_mean * 1e4, 1),
        "excess_vs_base_bps": round((mean - base_mean) * 1e4, 1),
        "tstat_vs_base": round((mean - base_mean) / se, 2) if se > 0 else None,
    }


def paired_excess(
    sym_panel: DailyPanel,
    idx_panel: DailyPanel,
    event_date: str,
    start_shift: int,
    end_shift: int,
) -> float | None:
    """Symbol minus index return over the identical window, else None."""
    sym_pos = sym_panel.pos.get(event_date)
    idx_pos = idx_panel.pos.get(event_date)
    if sym_pos is None or idx_pos is None:
        return None
    sym_ret = sym_panel.window_return(sym_pos, start_shift, end_shift)
    idx_ret = idx_panel.window_return(idx_pos, start_shift, end_shift)
    if sym_ret is None or idx_ret is None:
        return None
    return sym_ret - idx_ret


def build_unconditional_excess_baseline(
    panels: dict[str, DailyPanel],
    idx_panel: DailyPanel,
    start_shift: int,
    end_shift: int,
) -> list[float]:
    """Pooled symbol-minus-index returns over every observable symbol-day."""
    out: list[float] = []
    idx_dates = set(idx_panel.pos)
    for panel in panels.values():
        for i, day in enumerate(panel.dates):
            if day not in idx_dates:
                continue
            sym_ret = panel.window_return(i, start_shift, end_shift)
            idx_ret = idx_panel.window_return(idx_panel.pos[day], start_shift, end_shift)
            if sym_ret is not None and idx_ret is not None:
                out.append(sym_ret - idx_ret)
    return out


def main() -> int:
    cache = (
        Path(sys.argv[sys.argv.index("--cache") + 1])
        if "--cache" in sys.argv
        else Path("/tmp/ashare_event_research")
    )

    idx_panel = DailyPanel(load_index_series(cache))
    _fields, sym_rows = _read_csv(cache, "sample_symbols")
    samples = {r["ts_code"] for r in sym_rows}
    panels: dict[str, DailyPanel] = {}
    for code in sorted(samples):
        series = load_symbol_series(cache, code)
        if len(series) >= 250:
            panels[code] = DailyPanel(series)

    print(f"panels={len(panels)} index_days={len(idx_panel.dates)}", flush=True)

    results: dict[str, dict] = {"research_only": True, "index": INDEX_CODE}

    # --- 1. LPR release dates vs index -------------------------------------
    lpr_events = load_events_lpr(cache)
    lpr_block: dict = {"events": len(lpr_events), "windows": {}}
    for pre in PRE_OFFSETS:
        vals = []
        for e in lpr_events:
            pos = idx_panel.pos.get(e)
            if pos is None:
                continue
            v = idx_panel.window_return(pos, -pre, -1)
            if v is not None:
                vals.append(v)
        lpr_block["windows"][f"pre_{pre}d"] = describe(
            vals, build_index_baseline(idx_panel, -pre, -1)
        )
    for post in POST_OFFSETS:
        vals = []
        for e in lpr_events:
            pos = idx_panel.pos.get(e)
            if pos is None:
                continue
            v = idx_panel.window_return(pos, 0, post)
            if v is not None:
                vals.append(v)
        lpr_block["windows"][f"post_{post}d"] = describe(
            vals, build_index_baseline(idx_panel, 0, post)
        )
    day0_vals = []
    drift_curve: list[list] = []
    for offset in DRIFT_CURVE_RANGE:
        curve_vals = []
        for e in lpr_events:
            pos = idx_panel.pos.get(e)
            if pos is None:
                continue
            v = idx_panel.window_return(pos, offset - 1, offset)
            if v is not None:
                curve_vals.append(v)
        if offset == 0:
            day0_vals = curve_vals
        if curve_vals:
            drift_curve.append([offset, round(statistics.fmean(curve_vals) * 1e4, 1)])
    lpr_block["windows"]["day0"] = describe(day0_vals, build_index_baseline(idx_panel, -1, 0))
    lpr_block["drift_curve_mean_daily_bps"] = drift_curve
    results["lpr"] = lpr_block

    # --- 2 & 3. Symbol-level families vs pooled unconditional excess ------
    disc_events = load_events_disclosure(cache, samples)
    lock_events = load_events_lockup(cache, samples)

    accumulators = {
        family: {
            f"excess_pre_{pre}d": [] for pre in PRE_OFFSETS
        }
        | {f"excess_post_{post}d": [] for post in POST_OFFSETS}
        | {"excess_day0": []}
        for family in ("disclosure", "lockup")
    }

    def accumulate(family: str, events: list[tuple[str, str]]) -> None:
        matched = 0
        for symbol, event_date in events:
            panel = panels.get(symbol)
            if panel is None:
                continue
            matched += 1
            buckets = accumulators[family]
            for pre in PRE_OFFSETS:
                v = paired_excess(panel, idx_panel, event_date, -pre, -1)
                if v is not None:
                    buckets[f"excess_pre_{pre}d"].append(v)
            for post in POST_OFFSETS:
                v = paired_excess(panel, idx_panel, event_date, 0, post)
                if v is not None:
                    buckets[f"excess_post_{post}d"].append(v)
            v0 = paired_excess(panel, idx_panel, event_date, -1, 0)
            if v0 is not None:
                buckets["excess_day0"].append(v0)
        return matched

    disc_matched = accumulate("disclosure", disc_events)
    lock_matched = accumulate("lockup", lock_events)

    print("baselines: building pooled unconditional excess ...", flush=True)
    baseline_cache: dict[tuple[int, int], list[float]] = {}

    def baseline_for(start_shift: int, end_shift: int) -> list[float]:
        key = (start_shift, end_shift)
        if key not in baseline_cache:
            baseline_cache[key] = build_unconditional_excess_baseline(
                panels, idx_panel, start_shift, end_shift
            )
        return baseline_cache[key]

    results["disclosure"] = {
        "events": len(disc_events),
        "events_on_sample_panels": disc_matched,
        "symbols_observed": len(panels),
        "windows": {
            name: describe(vals, baseline_for(_shifts(name)[0], _shifts(name)[1]))
            for name, vals in accumulators["disclosure"].items()
        },
    }
    results["lockup"] = {
        "events": len(lock_events),
        "events_on_sample_panels": lock_matched,
        "symbols_observed": len(panels),
        "windows": {
            name: describe(vals, baseline_for(_shifts(name)[0], _shifts(name)[1]))
            for name, vals in accumulators["lockup"].items()
        },
    }

    out_path = cache / "stats_summary.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    # Console Markdown summary.
    print("# Event-window study summary (research_only)\n")
    print(f"- index: {INDEX_CODE}; sample symbols observed: {len(panels)}")
    for family in ("lpr", "disclosure", "lockup"):
        block = results[family]
        print(f"\n## {family} — events={block['events']}\n")
        print("| window | n | mean_bps | median | win_rate | excess_vs_base | t |")
        print("|---|---|---|---|---|---|---|")
        for win_name, stat in block["windows"].items():
            if stat.get("insufficient"):
                print(f"| {win_name} | {stat['n']} | insufficient | | | | |")
                continue
            print(
                f"| {win_name} | {stat['n']} | {stat['mean_bps']} | {stat['median_bps']} "
                f"| {stat['win_rate']} | {stat['excess_vs_base_bps']} | {stat['tstat_vs_base']} |"
            )
    curve = results["lpr"]["drift_curve_mean_daily_bps"]
    print("\nLPR mean daily drift (bps): " + ", ".join(f"{o}:{v}" for o, v in curve))
    print(f"\nsaved -> {out_path}")
    return 0


def _shifts(window_name: str) -> tuple[int, int]:
    if window_name.endswith("day0"):
        # Event-day return: previous close into the event-day close.
        return -1, 0
    parts = window_name.replace("excess_", "").split("_")
    span = int(parts[1].rstrip("d"))
    return (-span, -1) if parts[0] == "pre" else (0, span)


def build_index_baseline(
    idx_panel: DailyPanel, start_shift: int, end_shift: int
) -> list[float]:
    out = []
    for idx in range(len(idx_panel.dates)):
        v = idx_panel.window_return(idx, start_shift, end_shift)
        if v is not None:
            out.append(v)
    return out


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StatsError as exc:
        print(f"STATS_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
