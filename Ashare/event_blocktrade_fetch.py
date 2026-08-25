"""All-market daily ``block_trade`` fetcher (off-exchange block transactions).

Data expansion bound to a mechanism hypothesis: unlocked lockup shares most
often exit through the BLOCK channel, so pre-event block activity and its
pricing (amount, counterparty mix) may carry supply-clearing information
that order-flow absorption (#431) cannot see — block trades bypass the
visible order book entirely.  One Tushare call per trading day returns that
day's block prints (tens of rows observed — sparse by nature; includes
ETF/fund blocks which stay in the raw rows for study-layer filtering).

Layout & idempotency mirror the moneyflow fetcher (#431): one raw CSV per
day under ``<cache>/blocktrade_daily/``; existing files are never re-fetched
(safe resume).  Empty responses leave NO file so a later rerun retries them.
Rows stay raw — universe filtering, per-symbol aggregation and premium
math are study-layer concerns.

Fail-closed guards: aborts when the cache volume drops below
``MIN_FREE_BYTES`` free; per-day failures are recorded (printed at the end,
exit code 1 if any) without aborting the sweep.  Block volume is far below
any row limit, so no truncation marker is needed.  research_only /
not_promotion_evidence.

Usage::

    python3 Ashare/event_blocktrade_fetch.py [--cache DIR]
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
BLOCKTRADE_DIRNAME = "blocktrade_daily"
MIN_FREE_BYTES = 2 * 1024**3  # abort below 2 GiB free on the cache volume


class BlocktradeFetchError(RuntimeError):
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
        raise BlocktradeFetchError(f"index_cache_missing:{exc}") from exc
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


def fetch_blocktrade_daily(
    cache: Path,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    delay_seconds: float = 0.05,
) -> dict[str, object]:
    """Fetch all-market block trades per session; idempotent and resumable."""
    from Ashare.event_calendar_fetch import call_api

    out_dir = cache / BLOCKTRADE_DIRNAME
    days = _session_days(cache, start, end)
    if not days:
        raise BlocktradeFetchError("no_sessions_in_range")
    fetched = 0
    skipped_existing = 0
    empty_days: list[str] = []
    failed_days: list[str] = []
    fields_out: list[str] | None = None
    for idx, day in enumerate(days):
        target = out_dir / f"{day}.csv"
        if target.exists():
            skipped_existing += 1
            continue
        free = shutil.disk_usage(cache).free
        if free < MIN_FREE_BYTES:
            raise BlocktradeFetchError(f"disk_low:{free}")
        try:
            fields, rows = call_api("block_trade", {"trade_date": day})
        except Exception:  # noqa: BLE001 - record the day, keep the sweep going
            failed_days.append(day)
            time.sleep(max(delay_seconds, 0.05))
            continue
        if fields_out is None and fields:
            fields_out = list(fields)
        if not rows:
            empty_days.append(day)
        else:
            _write_day(target, list(fields), rows)
            fetched += 1
        if (idx + 1) % 200 == 0:
            print(
                f"blocktrade {idx + 1}/{len(days)} fetched={fetched} "
                f"empty={len(empty_days)} failed={len(failed_days)}",
                flush=True,
            )
        time.sleep(delay_seconds)
    summary: dict[str, object] = {
        "sessions_in_range": len(days),
        "fetched": fetched,
        "skipped_existing": skipped_existing,
        "empty_days": empty_days,
        "failed_days": failed_days,
        "fields": fields_out,
        "out_dir": str(out_dir),
    }
    print(
        f"blocktrade done: range={start}-{end} fetched={fetched} "
        f"skipped_existing={skipped_existing} empty={len(empty_days)} "
        f"failed={len(failed_days)}"
    )
    if failed_days:
        print("failed days (rerun resumes past existing files): "
              + ",".join(failed_days))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--start", type=str, default=DEFAULT_START)
    parser.add_argument("--end", type=str, default=DEFAULT_END)
    parser.add_argument("--delay", type=float, default=0.05)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    summary = fetch_blocktrade_daily(
        cache, start=args.start, end=args.end, delay_seconds=args.delay
    )
    return 1 if summary["failed_days"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BlocktradeFetchError as exc:
        print(f"BLOCKTRADE_FETCH_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
