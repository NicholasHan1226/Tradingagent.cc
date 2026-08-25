"""All-market daily list fetchers: suspensions & limit-hit rosters.

Data expansion bound to two mechanism hypotheses:

- H1 (suspension masking): unlock-window suspensions hide trading days from
  every signal so far (session spans silently stretch); a per-day roster
  lets studies VERIFY suspension safety instead of assuming it, and may
  itself label events (suspension right before an unlock = information).
- H2 (limit-lock realism): the fill-realism audit found locked entries
  (+1.26% of signals) enter at an unreachable price and locked exits roll
  optimistically (-9.85bps bias); a per-day limit roster gives the exact
  {symbol, day, limit_type} set needed to pre-register the skip/roll-forward
  polish (#441 follow-up) instead of approximating from price moves alone.
- H3 (holder behavior): insider increase/decrease filings around unlock
  windows are a direct supply-intent signal; ``stk_holdertrade`` rows carry
  in_de direction, change_ratio vs float and avg_price for study-layer use.

All three Tushare endpoints are per-DAY all-market feeds (``suspend_d``,
``limit_list_d`` — the latter includes U/D/Z rows when ``limit_type`` is
omitted — and ``stk_holdertrade``, keyed by announcement date), so ONE
sweep covers them: per session, one call per endpoint with that endpoint's
day-filter key, one raw CSV each under ``<cache>/suspend_daily/<day>.csv``
/ ``<cache>/limitlist_daily/<day>.csv`` / ``<cache>/holdertrade_daily/
<day>.csv``.  Layout & idempotency mirror the
blocktrade fetcher (#423): existing files are never re-fetched (safe
resume); empty responses leave NO file so a rerun retries that day;
per-endpoint failures are recorded without aborting the sweep.  Roster
volume is far below any row limit — no truncation marker needed.
Fail-closed guards: local SSE index calendar drives the day list, abort
when the cache volume drops below ``MIN_FREE_BYTES`` free.  Rows stay raw.

research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_market_lists_fetch.py [--cache DIR]
        [--start YYYYMMDD] [--end YYYYMMDD] [--delay SECONDS]
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DEFAULT_START = "20180101"
DEFAULT_END = time.strftime("%Y%m%d")
# Run-time today: a frozen end date silently stops the
# window from ingesting new sessions.
# (tushare endpoint, output subdirectory, day-filter param).  Verified live
# 2026-08-24: ``stk_holdertrade`` filtered by ``trade_date`` returns a
# 3000-row CAPPED page whose rows carry foreign ann_dates — only
# ``ann_date`` is a clean single-announcement-day filter there; the two
# roster endpoints key on ``trade_date``.
LIST_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("suspend_d", "suspend_daily", "trade_date"),
    ("limit_list_d", "limitlist_daily", "trade_date"),
    ("stk_holdertrade", "holdertrade_daily", "ann_date"),
)
MIN_FREE_BYTES = 2 * 1024**3  # abort below 2 GiB free on the cache volume


class MarketListsFetchError(RuntimeError):
    """Fail-closed fetch failure with a stable reason code."""


def _session_days(cache: Path, start: str, end: str) -> list[str]:
    """SSE sessions from the local index cache inside [start, end]."""
    from Ashare.event_calendar_lockup_strata import (
        StrataError,
        load_index_series,
    )

    try:
        pairs = load_index_series(cache)
    except StrataError as exc:
        raise MarketListsFetchError(f"index_cache_missing:{exc}") from exc
    return [
        d.strftime("%Y%m%d")
        for d, _ in pairs
        if start <= d.strftime("%Y%m%d") <= end
    ]


def _write_day(path: Path, fields: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".partial")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)
    tmp_path.replace(path)


def fetch_market_lists(
    cache: Path,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    delay_seconds: float = 0.12,
) -> dict[str, object]:
    """Fetch both daily rosters per session; idempotent and resumable."""
    from Ashare.event_calendar_fetch import call_api

    out_dirs = {api: cache / dirname for api, dirname, _p in LIST_SOURCES}
    day_keys = {api: key for api, _d, key in LIST_SOURCES}
    days = _session_days(cache, start, end)
    if not days:
        raise MarketListsFetchError("no_sessions_in_range")
    fetched = {api: 0 for api, _d, _k in LIST_SOURCES}
    skipped_existing = {api: 0 for api, _d, _k in LIST_SOURCES}
    empty_days: dict[str, list[str]] = {
        api: [] for api, _d, _k in LIST_SOURCES
    }
    failed_days: dict[str, list[str]] = {
        api: [] for api, _d, _k in LIST_SOURCES
    }
    fields_out: dict[str, list[str] | None] = {
        api: None for api, _d, _k in LIST_SOURCES
    }
    steps = len(days) * len(LIST_SOURCES)
    step = 0
    for day in days:
        for api, _dirname, param_key in LIST_SOURCES:
            target = out_dirs[api] / f"{day}.csv"
            step += 1
            if target.exists():
                skipped_existing[api] += 1
                continue
            free = shutil.disk_usage(cache).free
            if free < MIN_FREE_BYTES:
                raise MarketListsFetchError(f"disk_low:{free}")
            try:
                fields, rows = call_api(api, {param_key: day})
            except Exception:  # noqa: BLE001 - record it, keep the sweep going
                failed_days[api].append(day)
                time.sleep(max(delay_seconds, 0.05))
                continue
            if fields_out[api] is None and fields:
                fields_out[api] = list(fields)
            if not rows:
                empty_days[api].append(day)
            else:
                _write_day(target, list(fields), rows)
                fetched[api] += 1
            if step % 400 == 0:
                progress = " ".join(
                    f"{api}={fetched[api]}" for api, _d, _k in LIST_SOURCES
                )
                print(f"marketlists {step}/{steps} fetched {progress}",
                      flush=True)
            time.sleep(delay_seconds)
    summary: dict[str, object] = {
        "sessions_in_range": len(days),
        "sources": {
            api: {
                "fetched": fetched[api],
                "skipped_existing": skipped_existing[api],
                "empty_days": empty_days[api],
                "failed_days": failed_days[api],
                "fields": fields_out[api],
                "out_dir": str(out_dirs[api]),
            }
            for api, _d, _k in LIST_SOURCES
        },
    }
    parts = " ".join(
        f"{api}: fetched={fetched[api]} skipped={skipped_existing[api]} "
        f"empty={len(empty_days[api])} failed={len(failed_days[api])}"
        for api, _d, _k in LIST_SOURCES
    )
    print(f"marketlists done: range={start}-{end} {parts}")
    for api, _d, _k in LIST_SOURCES:
        if failed_days[api]:
            print(f"{api} failed days (rerun resumes past existing files): "
                  + ",".join(failed_days[api]))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--start", type=str, default=DEFAULT_START)
    parser.add_argument("--end", type=str, default=DEFAULT_END)
    parser.add_argument("--delay", type=float, default=0.12)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    summary = fetch_market_lists(
        cache, start=args.start, end=args.end, delay_seconds=args.delay
    )
    failed = any(s["failed_days"] for s in summary["sources"].values())  # type: ignore[union-attr]
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MarketListsFetchError as exc:
        print(f"MARKET_LISTS_FETCH_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
