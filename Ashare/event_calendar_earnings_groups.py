"""Group earnings-disclosure events by pre-announcement direction.

Research-only.  The 2026-08-23 event-calendar study found disclosure-day
outcomes highly dispersed (positive mean, negative median): good and bad
earnings cancel out in the aggregate.  This script splits the same
disclosure events by the direction of the earnings *forecast* published
before the disclosure date (Tushare ``forecast``: 预增/预减/扭亏/首亏/...)
and re-runs the event-window statistics per group, answering: which
disclosure groups can actually be traded, if any.

Grouping rules:

* a disclosure event (ts_code, end_date -> pre_date) inherits the direction
  of the symbol's earliest forecast announcement on or before the disclosure
  date for the same report period;
* positive: 预增/略增/续盈/预盈/扭亏; negative: 预减/略减/首亏/续亏/预亏;
  不确定 maps to its own group; disclosures without any prior forecast form
  the ``no_forecast`` group;
* returns are symbol-minus-index excess over identical windows, compared
  against the pooled unconditional excess baseline (same methodology as
  ``event_calendar_stats``).

Nothing here is promotion evidence; figures are descriptive historical
statistics.

Usage::

    python3 Ashare/event_calendar_earnings_groups.py \
        [--cache /tmp/ashare_event_research] [--expanded]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_calendar_stats import (  # noqa: E402
    DailyPanel,
    POST_OFFSETS,
    PRE_OFFSETS,
    build_unconditional_excess_baseline,
    describe,
    load_index_series,
    load_symbol_series,
    paired_excess,
)


POSITIVE_TYPES = {"预增", "略增", "续盈", "预盈", "扭亏"}
NEGATIVE_TYPES = {"预减", "略减", "首亏", "续亏", "预亏"}
UNCERTAIN_TYPES = {"不确定"}
REQUEST_INTERVAL_SECONDS = 0.35


class GroupError(RuntimeError):
    """Fail-closed grouping failure with a stable reason code."""


def _read_csv(cache: Path, name: str) -> tuple[list[str], list[dict[str, str]]]:
    path = cache / f"{name}.csv"
    if not path.exists():
        raise GroupError(f"cache_missing:{path.name}")
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fields = next(reader)
        return fields, [dict(zip(fields, row)) for row in reader]


def _token() -> str:
    token = os.environ.get("TUSHARE_MCP_TOKEN", "").strip().strip('"').strip("'")
    if not token:
        raise GroupError("token_missing")
    return token


def _call_forecast(ts_code: str) -> tuple[list[str], list[list]]:
    request = urllib.request.Request(
        "https://api.tushare.pro",
        data=json.dumps(
            {
                "api_name": "forecast",
                "token": _token(),
                "params": {"ts_code": ts_code},
                "fields": "",
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    last_error: Exception | None = None
    for _ in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())
            if payload.get("code") == 0:
                data = payload.get("data") or {}
                return data.get("fields") or [], data.get("items") or []
            last_error = GroupError(f"api_error:{payload.get('code')}")
        except Exception as exc:  # transient transport failures only
            last_error = exc
        time.sleep(2.0)
    raise GroupError(f"forecast_fetch_failed:{ts_code}:{last_error}")


def _forecast_max_ann_day(
    fields: list[str], rows: list[list]
) -> str | None:
    """Newest announcement date stored in the forecast cache."""

    try:
        idx = fields.index("ann_date")
    except ValueError:
        return None
    last: str | None = None
    for row in rows:
        day = row[idx] if idx < len(row) else ""
        if day > (last or ""):
            last = day
    return last


def ensure_forecast_cache(
    cache: Path, samples: set[str], max_age_days: int = 6
) -> Path:
    """Fetch per-symbol forecast history into ``forecast.csv``.

    Idempotent with two coverage layers.  Symbols absent from the file are
    always fetched.  On top of that, when the file's newest stored
    announcement is staler than ``max_age_days`` every sample symbol is
    re-pulled and merge-deduped on batch identity — otherwise forecasts a
    covered symbol publishes later would never reach the file and their
    disclosure events would silently fall into ``no_forecast``.  Staleness
    is data-driven (newest ``ann_date``, like the bar-shard freshness gate)
    rather than mtime so semantics survive cache restore; quiet stretches
    between forecast seasons simply cost one weekly full-history sweep.
    """

    from Ashare.event_calendar_fetch import _shift_date

    path = cache / "forecast.csv"
    existing_rows: list[list] = []
    fields: list[str] | None = None
    covered: set[str] = set()
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            reader = csv.reader(handle)
            fields = next(reader, None)
            if fields:
                name_i = fields.index("ts_code")
                existing_rows = [row for row in reader]
                covered = {row[name_i] for row in existing_rows if row}
    today = time.strftime("%Y%m%d")
    last_ann = (
        _forecast_max_ann_day(fields, existing_rows)
        if fields is not None else None
    )
    stale = last_ann is None or last_ann < _shift_date(today, -max_age_days)
    if stale:
        todo = sorted(samples)
        mode = f"full_refresh(last_ann={last_ann})"
    else:
        todo = sorted(samples - covered)
        mode = "incremental"
    print(
        f"forecast_cache covered={len(covered)} todo={len(todo)} mode={mode}",
        flush=True,
    )
    if not todo:
        return path
    new_merged: dict[tuple, list] = {}
    fetched_fields: list[str] | None = None
    for i, code in enumerate(todo, 1):
        row_fields, rows = _call_forecast(code)
        if fetched_fields is None and row_fields:
            fetched_fields = row_fields
        for row in rows:
            record = dict(zip(row_fields, row))
            key = (record["ts_code"], record["ann_date"], record["end_date"], record.get("update_flag", ""))
            new_merged[key] = record
        if i % 25 == 0:
            print(f"forecast_progress={i}/{len(todo)}", flush=True)
        time.sleep(REQUEST_INTERVAL_SECONDS)
    if not new_merged:
        return path  # nothing usable fetched; leave the cache untouched
    canonical = fields or fetched_fields
    if not canonical:
        raise GroupError("forecast_empty")

    def _stored_key(row: list) -> tuple:
        def col(name: str) -> str:
            try:
                return row[canonical.index(name)]
            except (ValueError, IndexError):
                return ""
        return (
            col("ts_code"), col("ann_date"), col("end_date"),
            col("update_flag"),
        )

    seen_keys = {_stored_key(row) for row in existing_rows}
    out_rows = existing_rows + [
        _forecast_row(record, canonical)
        for key, record in new_merged.items()
        if key not in seen_keys
    ]
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(canonical)
        writer.writerows(out_rows)
    tmp.replace(path)
    return path


def _forecast_row(record: dict, fields: list[str]) -> list:
    """Project one fetched record onto the cache's canonical columns."""

    return [record.get(name, "") for name in fields]


def load_forecast_directions(cache: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """Earliest pre-disclosure forecast per (ts_code, end_date).

    Returns {(ts_code, end_date): (ann_date, raw_type)} keeping the first
    announcement (smallest ann_date, then update_flag order) per report
    period.
    """

    _fields, rows = _read_csv(cache, "forecast")
    first: dict[tuple[str, str], tuple[str, str]] = {}
    for row in rows:
        key = (row["ts_code"], row["end_date"])
        candidate = (row["ann_date"], row.get("type", ""))
        if key not in first or candidate[0] < first[key][0]:
            first[key] = candidate
    return first


def direction_group(raw_type: str) -> str:
    if raw_type in POSITIVE_TYPES:
        return "forecast_positive"
    if raw_type in NEGATIVE_TYPES:
        return "forecast_negative"
    if raw_type in UNCERTAIN_TYPES:
        return "forecast_uncertain"
    return "forecast_unknown"


def load_disclosure_events(
    cache: Path,
    samples: set[str],
    forecasts: dict[tuple[str, str], tuple[str, str]],
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, int]]:
    """Bucket disclosure events (symbol, pre_date) by forecast direction."""

    _fields, rows = _read_csv(cache, "disclosure")
    buckets: dict[str, list[tuple[str, str]]] = {}
    counts = {"unknown_type": 0, "no_forecast": 0, "grouped": 0}
    for row in rows:
        code = row["ts_code"]
        if code not in samples or row["pre_date"] < row["ann_date"]:
            continue
        prior = forecasts.get((code, row["end_date"]))
        if prior is None or prior[0] > row["pre_date"]:
            group = "no_forecast"
            counts["no_forecast"] += 1
        else:
            group = direction_group(prior[1])
            if group == "forecast_unknown":
                counts["unknown_type"] += 1
            else:
                counts["grouped"] += 1
        buckets.setdefault(group, []).append((code, row["pre_date"]))
    for group in buckets:
        buckets[group] = sorted(set(buckets[group]))
    return buckets, counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=Path("/tmp/ashare_event_research"))
    parser.add_argument(
        "--expanded",
        action="store_true",
        help="use sample_symbols_expanded / share_float_expanded panels",
    )
    args = parser.parse_args()
    cache = args.cache

    symbol_file = "sample_symbols_expanded" if args.expanded else "sample_symbols"
    _fields, sym_rows = _read_csv(cache, symbol_file)
    samples = {r["ts_code"] for r in sym_rows}

    idx_panel = DailyPanel(load_index_series(cache))
    panels: dict[str, DailyPanel] = {}
    for code in sorted(samples):
        series = load_symbol_series(cache, code)
        if len(series) >= 250:
            panels[code] = DailyPanel(series)
    print(f"panels={len(panels)}", flush=True)

    forecast_path = ensure_forecast_cache(cache, samples)
    print(f"forecast_cache={forecast_path.name}", flush=True)
    forecasts = load_forecast_directions(cache)
    buckets, counts = load_disclosure_events(cache, samples, forecasts)
    print(f"event_counts={counts}", flush=True)

    results: dict = {
        "research_only": True,
        "symbol_file": symbol_file,
        "event_counts": counts,
        "groups": {},
    }
    baseline_cache: dict[tuple[int, int], list[float]] = {}

    def baseline_for(start_shift: int, end_shift: int) -> list[float]:
        key = (start_shift, end_shift)
        if key not in baseline_cache:
            baseline_cache[key] = build_unconditional_excess_baseline(
                panels, idx_panel, start_shift, end_shift
            )
        return baseline_cache[key]

    def _shifts(window_name: str) -> tuple[int, int]:
        if window_name.endswith("day0"):
            return -1, 0
        parts = window_name.replace("excess_", "").split("_")
        span = int(parts[1].rstrip("d"))
        return (-span, -1) if parts[0] == "pre" else (0, span)

    for group in sorted(buckets):
        events = buckets[group]
        windows: dict[str, list[float]] = {
            **{f"excess_pre_{p}d": [] for p in PRE_OFFSETS},
            **{f"excess_post_{p}d": [] for p in POST_OFFSETS},
            "excess_day0": [],
        }
        matched = 0
        for symbol, event_date in events:
            panel = panels.get(symbol)
            if panel is None:
                continue
            matched += 1
            window_shifts: dict[str, tuple[int, int]] = {
                **{f"excess_pre_{p}d": (-p, -1) for p in PRE_OFFSETS},
                **{f"excess_post_{p}d": (0, p) for p in POST_OFFSETS},
                "excess_day0": (-1, 0),
            }
            for name, (start_shift, end_shift) in window_shifts.items():
                value = paired_excess(panel, idx_panel, event_date, start_shift, end_shift)
                if value is not None:
                    windows[name].append(value)
        results["groups"][group] = {
            "events": len(events),
            "events_on_panels": matched,
            "windows": {
                name: describe(values, baseline_for(*_shifts(name)))
                for name, values in windows.items()
            },
        }

    out_path = cache / "earnings_groups_summary.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print("# Earnings disclosure by forecast direction (research_only)\n")
    print(f"- symbol_file: {symbol_file}; event counts: {counts}")
    print("\n| group | window | n | mean_bps | median | win_rate | excess | t |")
    print("|---|---|---|---|---|---|---|---|")
    for group, block in sorted(results["groups"].items()):
        for name, stat in block["windows"].items():
            if stat.get("insufficient"):
                continue
            print(
                f"| {group} | {name} | {stat['n']} | {stat['mean_bps']} "
                f"| {stat['median_bps']} | {stat['win_rate']} "
                f"| {stat['excess_vs_base_bps']} | {stat['tstat_vs_base']} |"
            )
    print(f"\nsaved -> {out_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GroupError as exc:
        print(f"GROUPS_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
