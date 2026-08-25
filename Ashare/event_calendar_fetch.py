"""One-shot research data fetch for the A-share event-calendar study.

Research-only.  Pulls historical event calendars (LPR release dates, earnings
disclosure appointments, lockup expiries) plus index and mainboard sample
daily bars from the Tushare pro HTTP API into a local scratch cache under
/tmp/ashare_event_research/.  This module is NOT a runtime collector, claims
no TradingDatas authority, and its outputs feed only the offline event-window
statistics in ``event_calendar_stats.py``.

The token is read from the ``TUSHARE_MCP_TOKEN`` environment variable and is
never written to disk, logs, or the cache.

Usage::

    python3 Ashare/event_calendar_fetch.py [--max-samples 200] [--refresh-disclosure]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

API_URL = "http://api.tushare.pro"
CACHE_DIR = Path("/tmp/ashare_event_research")
PAGE_LIMIT = 6000
REQUEST_INTERVAL_SECONDS = 0.35
MAX_RETRIES = 6

INDEX_CODES = ("000001.SH", "000300.SH")
STUDY_START = "20180101"
# Run-time today: a frozen end silently stops the disclosure calendar (and
# every window bounded by it) from ever seeing appointments past the day it
# was written — the rolling tracker's forward windows would starve.
STUDY_END = time.strftime("%Y%m%d")
# Sample eligibility: the endpoint's date window filters report periods, so
# the earliest observable pre_date is 2019 (2018 annual reports).  Requiring
# a pre_date within 2019 therefore selects companies listed before 2019.
SAMPLE_ELIGIBILITY_ANN_DATE = "20191231"


class FetchError(RuntimeError):
    """Fail-closed fetch failure; never returns partial silent results."""


def _token() -> str:
    token = os.environ.get("TUSHARE_MCP_TOKEN", "").strip().strip('"')
    if not token:
        raise FetchError("tushare_token_missing")
    return token


def call_api(api_name: str, params: dict | None = None) -> tuple[list[str], list[list]]:
    """Call one Tushare pro endpoint and return (fields, rows)."""
    body = json.dumps(
        {
            "api_name": api_name,
            "token": _token(),
            "fields": "",
            "params": params or {},
        }
    ).encode("utf-8")
    last_error: str | None = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(
                API_URL,
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"transport:{exc}"
            time.sleep(1.5 * (attempt + 1))
            continue
        if payload.get("code") != 0:
            last_error = f"api:{payload.get('msg')}"
            time.sleep(1.5 * (attempt + 1))
            continue
        data = payload.get("data") or {}
        return list(data.get("fields") or []), list(data.get("items") or [])
    raise FetchError(f"{api_name}_failed:{last_error}")


def _shift_date(raw: str, days: int) -> str:
    from datetime import datetime, timedelta

    parsed = datetime.strptime(raw, "%Y%m%d")
    return (parsed + timedelta(days=days)).strftime("%Y%m%d")


def fetch_ranged(
    api_name: str, start: str, end: str, depth: int = 0
) -> tuple[list[str], list[list]]:
    """Fetch all rows of one endpoint by bounded date-range slicing.

    Two server-side behaviours force this shape: several endpoints reject
    ``offset`` pagination outright, and some return EMPTY (not truncated)
    rows for multi-year windows.  So the outer pass slices by calendar year,
    and within one year a count-based bisection handles pages that hit the
    row cap.
    """
    if depth == 0:
        fields_all: list[str] | None = None
        rows_all: list[list] = []
        for slice_start, slice_end in _year_slices(start, end):
            fields, rows = fetch_ranged(api_name, slice_start, slice_end, depth=1)
            if fields_all is None:
                fields_all = fields
            elif fields != fields_all:
                raise FetchError(f"{api_name}_schema_drift")
            rows_all.extend(rows)
            time.sleep(REQUEST_INTERVAL_SECONDS)
        if fields_all is None:
            raise FetchError(f"{api_name}_empty_range:{start}:{end}")
        return fields_all, rows_all

    if depth > 24:
        raise FetchError(f"{api_name}_range_too_deep:{start}:{end}")
    fields, rows = call_api(api_name, {"start_date": start, "end_date": end})
    time.sleep(REQUEST_INTERVAL_SECONDS)
    if len(rows) < PAGE_LIMIT:
        return fields, rows
    mid = _shift_date(start, (_days_between(start, end)) // 2)
    if mid <= start or mid > end:
        raise FetchError(f"{api_name}_cannot_split:{start}:{end}")
    left_fields, left_rows = fetch_ranged(api_name, start, _shift_date(mid, -1), depth + 1)
    right_fields, right_rows = fetch_ranged(api_name, mid, end, depth + 1)
    if left_fields != right_fields:
        raise FetchError(f"{api_name}_schema_drift")
    return left_fields, left_rows + right_rows


def _year_slices(start: str, end: str) -> list[tuple[str, str]]:
    slices = []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        slice_start = max(start, f"{year}0101")
        slice_end = min(end, f"{year}1231")
        if slice_start <= slice_end:
            slices.append((slice_start, slice_end))
    return slices


def _days_between(start: str, end: str) -> int:
    from datetime import datetime

    return (datetime.strptime(end, "%Y%m%d") - datetime.strptime(start, "%Y%m%d")).days


def save_csv(name: str, fields: list[str], rows: list[list]) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{name}.csv"
    import csv

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)
    return path


def is_mainboard(ts_code: str) -> bool:
    return ts_code.endswith(".SH") and ts_code.startswith("60") or (
        ts_code.endswith(".SZ") and ts_code.startswith("00")
    )


def _disclosure_period_ends(start: str, end: str) -> list[str]:
    """Report-period end dates (YYYYMMDD quarter/year closes) in [start, end]."""

    out: list[str] = []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        for tail in ("0331", "0630", "0930", "1231"):
            day = f"{year}{tail}"
            if start <= day <= end:
                out.append(day)
    return out


# Appointments are published weeks ahead of each period's disclosure season;
# a ~13-month horizon keeps next year's Q1/Q2 schedules reachable.
DISCLOSURE_HORIZON_DAYS = 400
# Season gaps can legitimately quiet announcements for weeks (between a
# quarter's disclosure season and the next season's schedule publication),
# so the staleness sentinel warns far above that instead of failing closed.
DISCLOSURE_STALE_WARN_DAYS = 60


def refresh_disclosure(force: bool = False) -> tuple[list[str], list[list]]:
    """Load — or rebuild — the full-market disclosure calendar, overwriting.

    Two silent starvation modes make naive paths unusable here:

    * Reuse-if-present under weekly CI freezes the calendar at whatever week
      actions/cache last saved it — new appointments and reschedules stop
      landing while the tracker keeps reading stale windows.
    * ``fetch_ranged``'s year slicing silently captures ONLY annual-report
      periods on this endpoint: the API matches the report period itself,
      and a [Y0101..Y1231] slice never asks for Q1/H1/Q3 closes.  Months of
      "successful" pulls produced an annual-only calendar whose next inflow
      was a year away.

    So ``force=True`` sweeps EVERY quarter/year period end from STUDY_START
    through a ~13-month future horizon with exact ``end_date`` queries
    (empirically the only shape this endpoint honours), then upserts on
    (ts_code, end_date): a newer announcement — including a rescheduled
    appointment — replaces the stored row; cached rows outside the sweep
    survive untouched.
    """

    if not force:
        cached = _load_cached_csv("disclosure")
        if cached is not None:
            print(f"disclosure_date reused rows={len(cached[1])}")
            return cached

    today = time.strftime("%Y%m%d")
    horizon = _shift_date(today, DISCLOSURE_HORIZON_DAYS)
    periods = _disclosure_period_ends(STUDY_START, horizon)
    canonical: list[str] | None = None
    merged: dict[tuple[str, str], list] = {}
    seeded = _load_cached_csv("disclosure")
    if seeded is not None:
        canonical, rows = seeded
        t_i, e_i = canonical.index("ts_code"), canonical.index("end_date")
        for row in rows:
            merged[(row[t_i], row[e_i])] = row
    for period in periods:
        fields, rows = call_api("disclosure_date", {"end_date": period})
        time.sleep(REQUEST_INTERVAL_SECONDS)
        if canonical is None:
            canonical = fields
        elif fields != canonical:
            raise FetchError("disclosure_schema_drift")
        t_i = canonical.index("ts_code")
        e_i = canonical.index("end_date")
        a_i = canonical.index("ann_date")
        for row in rows:
            key = (row[t_i], row[e_i])
            old = merged.get(key)
            if old is None or row[a_i] >= old[a_i]:
                merged[key] = row
    if canonical is None:
        raise FetchError("disclosure_empty_sweep")
    out_rows = list(merged.values())
    a_i = canonical.index("ann_date")
    p_i = canonical.index("pre_date")
    ann_max = max((r[a_i] for r in out_rows if r[a_i]), default="")
    pre_max = max((r[p_i] for r in out_rows if r[p_i]), default="")
    print(
        f"disclosure_freshness rows={len(out_rows)} periods={len(periods)} "
        f"ann_max={ann_max} pre_max={pre_max}",
        flush=True,
    )
    # The endpoint's parameter semantics already drifted once and silently
    # emptied refetches; a wall-clock sentinel on the freshest announcement
    # is the cheapest tripwire.  Warning only: a season-boundary lull must
    # not abort the whole tracker run.
    if ann_max and ann_max < _shift_date(today, -DISCLOSURE_STALE_WARN_DAYS):
        print(
            f"DISCLOSURE_STALE_WARNING newest announcement {ann_max} is older "
            f"than {DISCLOSURE_STALE_WARN_DAYS} days — the calendar feed may "
            f"have stalled or its contract drifted again; earnings forward "
            f"windows will starve silently.",
            flush=True,
        )
    print(
        f"disclosure_date repulled periods={len(periods)} rows={len(out_rows)} "
        f"-> {save_csv('disclosure', canonical, out_rows)}"
    )
    return canonical, out_rows


def _load_cached_csv(name: str) -> tuple[list[str], list[list]] | None:
    path = CACHE_DIR / f"{name}.csv"
    if not path.exists():
        return None
    import csv

    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fields = next(reader)
        return fields, [row for row in reader]


SHARE_FLOAT_KEY = ("ts_code", "ann_date", "float_date", "holder_name",
                   "share_type")


def merge_share_float_rows(
    fields: list[str], rows: list[list]
) -> tuple[list[str], list[list]]:
    """Merge this run's per-symbol share_float rows into the cached table.

    A blind overwrite truncates every symbol outside this run's sample list
    (the top-1000 expansion universe lives in the same file, so each top-200
    refetch would silently erase ~765 symbols' unlock history); dedupe on
    the batch identity key keeps repeated full-history pulls idempotent.
    """
    import csv

    path = CACHE_DIR / "share_float.csv"
    out_fields: list[str] = fields
    existing: list[list] = []
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            reader = csv.reader(handle)
            old_fields = next(reader, None)
            if old_fields:
                out_fields = old_fields
                existing = [row for row in reader]
    key_names = [k for k in SHARE_FLOAT_KEY if k in out_fields]
    key_idx = [out_fields.index(k) for k in key_names]

    def _key(row: list[str]) -> tuple:
        return tuple(row[i] if i < len(row) else "" for i in key_idx)

    seen: set[tuple] = set()
    merged: list[list] = []
    for row in existing:
        key = _key(row)
        if key not in seen:
            seen.add(key)
            merged.append(row)
    incoming_idx = {name: i for i, name in enumerate(fields)}
    added = 0
    for row in rows:
        projected = [
            row[incoming_idx[name]] if name in incoming_idx else ""
            for name in out_fields
        ]
        key = _key(projected)
        if key not in seen:
            seen.add(key)
            merged.append(projected)
            added += 1
    print(
        f"share_float merge: {len(existing)} cached + {added} new "
        f"= {len(merged)} rows",
        flush=True,
    )
    return out_fields, merged


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-samples", type=int, default=200)
    parser.add_argument(
        "--refresh-disclosure",
        action="store_true",
        help="Re-pull the disclosure calendar even when a cached copy exists.",
    )
    args = parser.parse_args()

    started = time.time()

    # 1. LPR release dates (the quote date IS the release date, holiday-rolled).
    fields, rows = call_api(
        "shibor_lpr", {"start_date": "20190101", "end_date": STUDY_END}
    )
    print(f"shibor_lpr rows={len(rows)} -> {save_csv('lpr', fields, rows)}")
    time.sleep(REQUEST_INTERVAL_SECONDS)

    # 2. Index daily bars.
    for code in INDEX_CODES:
        fields, rows = call_api(
            "index_daily",
            {
                "ts_code": code,
                "start_date": STUDY_START,
                "end_date": STUDY_END,
            },
        )
        safe = code.replace(".", "")
        print(
            f"index_daily {code} rows={len(rows)} -> "
            f"{save_csv('index_' + safe, fields, rows)}"
        )
        time.sleep(REQUEST_INTERVAL_SECONDS)

    # 3. Earnings disclosure appointments (hard dates, full history).
    #    The endpoint's date window filters the report period (end_date),
    #    so ann_date values sit near each period's disclosure season, not
    #    at the window start.  Reuse a previous fetch unless --refresh-disclosure.
    fields, rows = refresh_disclosure(force=args.refresh_disclosure)
    time.sleep(REQUEST_INTERVAL_SECONDS)

    # 4. Pick mainboard sample symbols from disclosure history.
    disc_symbol_counts = Counter(
        row[fields.index("ts_code")]
        for row in rows
        if is_mainboard(row[fields.index("ts_code")])
        and row[fields.index("pre_date")] >= STUDY_START
        and row[fields.index("pre_date")] <= SAMPLE_ELIGIBILITY_ANN_DATE
    )
    samples = [
        code
        for code, _count in disc_symbol_counts.most_common(args.max_samples)
    ]
    print(f"sample_symbols={len(samples)} (mainboard, listed before {SAMPLE_ELIGIBILITY_ANN_DATE})")
    save_csv("sample_symbols", ["ts_code"], [[code] for code in samples])

    # 5. Per-symbol pulls: daily bars, adjustment factors, lockup expiries.
    #    share_float single days can exceed the 6000-row cap (e.g. late
    #    Feb 2021), so full-market date slicing is unsound; the study only
    #    needs sample symbols anyway.
    lockup_fields: list[str] | None = None
    lockup_rows: list[list] = []
    for idx, code in enumerate(samples):
        safe = code.replace(".", "")
        d_fields, d_rows = call_api(
            "daily",
            {"ts_code": code, "start_date": STUDY_START, "end_date": STUDY_END},
        )
        save_csv(f"daily_{safe}", d_fields, d_rows)
        time.sleep(REQUEST_INTERVAL_SECONDS)
        a_fields, a_rows = call_api(
            "adj_factor",
            {"ts_code": code, "start_date": STUDY_START, "end_date": STUDY_END},
        )
        save_csv(f"adjfactor_{safe}", a_fields, a_rows)
        time.sleep(REQUEST_INTERVAL_SECONDS)
        s_fields, s_rows = call_api("share_float", {"ts_code": code})
        if lockup_fields is None:
            lockup_fields = s_fields
        elif s_fields != lockup_fields:
            raise FetchError("share_float_schema_drift")
        lockup_rows.extend(s_rows)
        time.sleep(REQUEST_INTERVAL_SECONDS)
        if (idx + 1) % 25 == 0:
            print(
                f"progress {idx + 1}/{len(samples)} lockups={len(lockup_rows)} "
                f"elapsed={int(time.time() - started)}s"
            )

    if lockup_fields is None:
        raise FetchError("share_float_empty")
    merged_fields, merged_rows = merge_share_float_rows(
        lockup_fields, lockup_rows
    )
    print(
        f"share_float rows={len(merged_rows)} -> "
        f"{save_csv('share_float', merged_fields, merged_rows)}"
    )

    print(f"DONE symbols={len(samples)} elapsed={int(time.time() - started)}s")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FetchError as exc:
        print(f"FETCH_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
