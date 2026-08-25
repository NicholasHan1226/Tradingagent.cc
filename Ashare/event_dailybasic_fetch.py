"""Per-symbol daily ``daily_basic`` fetcher (valuation & turnover panel).

Data expansion bound to two mechanism hypotheses:

- H1 (turnover normalization): pre-event turnover-rate anomalies carry
  demand-side information complementary to the three supply-side views
  already in place (margin support #430, order-flow outflow #432, block
  discount #436) — intensity measures so far use raw amounts only.
- H2 (float cross-validation): the stratification variable ``float_ratio``
  currently derives from share_float announcements; ``daily_basic`` float
  shares / circ_mv give an independent measure to validate those buckets.

Fetched per SYMBOL (one call returns the symbol's full daily_basic history
≈ 2.1k rows, far below any row cap — no truncation guard needed), so files
line up with the existing per-symbol layout: ``dailybasic_<stem>.csv``
beside ``daily_<stem>.csv`` / ``adjfactor_<stem>.csv``.  The sweep is driven
by symbols ALREADY in the local cache (stems of ``daily_*.csv``) and by the
local SSE index calendar for the date window; shards whose last session is
staler than ``--max-age-days`` are refreshed two ways: recently stale ones
via whole-market per-session pulls (one call covers every cached symbol for
a session — a weekly gap costs one call instead of one per symbol) projected
into their shards newest-first, while missing shards or gaps beyond
``TOPUP_MAX_DAYS`` fall back to the full-history re-pull and atomic
overwrite (valuation labels snap entry days to the shard's own last
session, so a frozen shard would silently age every label).  Fresh shards
are skipped, empty responses leave NO file, per-symbol failures and failed
sessions are recorded without aborting the sweep, atomic ``.partial``
writes, fail-closed disk guard.  Rows stay raw — bucketing and factor math
are study-layer concerns.

research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_dailybasic_fetch.py [--cache DIR]
        [--start YYYYMMDD] [--end YYYYMMDD] [--max-age-days N]
        [--delay SECONDS]
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
MIN_FREE_BYTES = 2 * 1024**3  # abort below 2 GiB free on the cache volume
# Staleness beyond this many days no longer pays off as per-session top-ups;
# those shards fall back to the full-history re-pull (which heals them).
TOPUP_MAX_DAYS = 30


def _default_end() -> str:
    """Run-time today: a hardcoded end date silently freezes the window."""

    return time.strftime("%Y%m%d")


class DailybasicFetchError(RuntimeError):
    """Fail-closed fetch failure with a stable reason code."""


def _cached_stems(cache: Path) -> list[str]:
    """Stems of symbols already present as ``daily_*.csv``."""
    return sorted(path.stem.removeprefix("daily_")
                  for path in cache.glob("daily_*.csv"))


def _write_day(path: Path, fields: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".partial")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)
    tmp_path.replace(path)


def _read_shard(cache: Path, stem: str) -> tuple[list[str], list[list]]:
    with (cache / f"dailybasic_{stem}.csv").open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        return next(reader), [row for row in reader]


def fetch_dailybasic_topup(
    cache: Path,
    stems_last: dict[str, str],
    end: str,
    delay_seconds: float,
) -> dict[str, object]:
    """Date-driven whole-market top-up for stale-but-present label shards.

    One ``daily_basic`` ``trade_date`` pull (~5.5k rows) covers every cached
    symbol for one session, so a weekly gap costs one call per missing
    session instead of one per symbol.  Rows are projected into the
    existing shards newest-first; symbols absent from a session simply gain
    no rows and stay stale for a later cycle.  Failed sessions are recorded
    (the caller exits non-zero) instead of aborting the sweep; a response
    at the page cap fails closed rather than truncate silently.
    """

    from Ashare.event_calendar_expand_samples import trading_days
    from Ashare.event_calendar_fetch import (
        PAGE_LIMIT,
        _shift_date,
        call_api,
    )

    free = shutil.disk_usage(cache).free
    if free < MIN_FREE_BYTES:
        raise DailybasicFetchError(f"disk_low:{free}")
    start = _shift_date(min(stems_last.values()), 1)
    dates = trading_days(start, end)
    print(
        f"dailybasic_topup symbols={len(stems_last)} sessions={len(dates)} "
        f"range={start}-{end}",
        flush=True,
    )
    failed_dates: list[str] = []
    collected: dict[str, list[list]] = {}
    fields_seen: list[str] | None = None
    for day in dates:
        try:
            fields, rows = call_api("daily_basic", {"trade_date": day})
        except Exception:  # noqa: BLE001 - record the date, keep going
            failed_dates.append(day)
            time.sleep(max(delay_seconds, 0.05))
            continue
        if len(rows) >= PAGE_LIMIT:
            raise DailybasicFetchError(f"daily_basic_date_capped:{day}")
        if fields_seen is None:
            fields_seen = list(fields)
        ti = fields.index("ts_code")
        di = fields.index("trade_date")
        for row in rows:
            stem = row[ti].replace(".", "")
            last = stems_last.get(stem)
            if last is not None and row[di] > last:
                collected.setdefault(stem, []).append(row)
        time.sleep(max(delay_seconds, 0.05))
    projected = 0
    for stem, rows in collected.items():
        shard_fields, old_rows = _read_shard(cache, stem)
        di = shard_fields.index("trade_date")
        _write_day(
            cache / f"dailybasic_{stem}.csv",
            shard_fields,
            sorted(rows, key=lambda r: r[di], reverse=True) + old_rows,
        )
        projected += 1
    print(
        f"dailybasic_topup done projected={projected} "
        f"failed_dates={failed_dates}",
        flush=True,
    )
    return {
        "topup_symbols": len(stems_last),
        "topup_sessions": len(dates),
        "topup_projected": projected,
        "failed_dates": failed_dates,
    }


def fetch_dailybasic(
    cache: Path,
    start: str = DEFAULT_START,
    end: str | None = None,
    delay_seconds: float = 0.12,
    max_age_days: int = 6,
) -> dict[str, object]:
    """Fetch daily_basic per cached symbol; freshness-aware and resumable.

    Symbols whose shard is missing or staler than ``max_age_days`` (its
    last ``trade_date`` older than that many days before today) are
    refreshed — recently stale ones via :func:`fetch_dailybasic_topup`
    whole-market session pulls, missing shards or gaps beyond
    ``TOPUP_MAX_DAYS`` via a full re-pull that overwrites; fresh shards
    are skipped without touching the network, keeping weekly scheduled
    runs idempotent.
    """

    from Ashare.event_calendar_expand_samples import (
        series_is_fresh,
        series_max_day,
    )
    from Ashare.event_calendar_fetch import _shift_date, call_api

    end = end or _default_end()
    stems = _cached_stems(cache)
    if not stems:
        raise DailybasicFetchError("daily_cache_missing")
    today = time.strftime("%Y%m%d")
    todo = [
        stem for stem in stems
        if not series_is_fresh(
            cache, stem, today, max_age_days, prefix="dailybasic"
        )
    ]
    skipped_existing = len(stems) - len(todo)

    # Partition the refresh work: date-driven top-ups only pay off when the
    # shard exists and its gap is recent; everything else keeps the
    # full-history path (which also heals long-frozen shards).
    absent_set = {
        stem for stem in todo
        if not (cache / f"dailybasic_{stem}.csv").exists()
    }
    cutoff = _shift_date(today, -TOPUP_MAX_DAYS)
    ancient: list[str] = []
    recent_stale: dict[str, str] = {}
    for stem in todo:
        if stem in absent_set:
            continue
        last = series_max_day(cache, stem, prefix="dailybasic") or ""
        if last < cutoff:
            ancient.append(stem)
        else:
            recent_stale[stem] = last
    topup_summary: dict[str, object] = {
        "topup_symbols": 0,
        "topup_sessions": 0,
        "topup_projected": 0,
        "failed_dates": [],
    }
    if recent_stale:
        topup_summary.update(
            fetch_dailybasic_topup(cache, recent_stale, end, delay_seconds)
        )

    sweep_set = absent_set | set(ancient)
    sweep = [stem for stem in todo if stem in sweep_set]
    fetched = 0
    empty_symbols: list[str] = []
    failed_symbols: list[str] = []
    fields_out: list[str] | None = None
    for idx, stem in enumerate(sweep):
        target = cache / f"dailybasic_{stem}.csv"
        free = shutil.disk_usage(cache).free
        if free < MIN_FREE_BYTES:
            raise DailybasicFetchError(f"disk_low:{free}")
        try:
            fields, rows = call_api(
                "daily_basic",
                {"ts_code": stem_to_code(stem),
                 "start_date": start,
                 "end_date": end},
            )
        except Exception:  # noqa: BLE001 - record the symbol, keep going
            failed_symbols.append(stem)
            time.sleep(max(delay_seconds, 0.05))
            continue
        if fields_out is None and fields:
            fields_out = list(fields)
        if not rows:
            empty_symbols.append(stem)
        else:
            _write_day(target, list(fields), rows)
            fetched += 1
        if (idx + 1) % 200 == 0:
            print(
                f"dailybasic {idx + 1}/{len(sweep)} fetched={fetched} "
                f"empty={len(empty_symbols)} failed={len(failed_symbols)}",
                flush=True,
            )
        time.sleep(delay_seconds)
    summary: dict[str, object] = {
        "symbols_in_cache": len(stems),
        "refresh_todo": len(todo),
        "max_age_days": max_age_days,
        "fetched": fetched,
        "skipped_existing": skipped_existing,
        "empty_symbols": empty_symbols,
        "failed_symbols": failed_symbols,
        "fields": fields_out,
        **topup_summary,
    }
    print(
        f"dailybasic done: range={start}-{end} todo={len(todo)} "
        f"(sweep={len(sweep)} topup={topup_summary['topup_symbols']}) "
        f"fetched={fetched} skipped_existing={skipped_existing} "
        f"empty={len(empty_symbols)} failed={len(failed_symbols)}"
    )
    if failed_symbols:
        print("failed symbols (rerun resumes past existing files): "
              + ",".join(failed_symbols))
    return summary


def stem_to_code(stem: str) -> str:
    """``000001SZ`` -> ``000001.SZ`` (same mapping as the study loaders)."""
    return f"{stem[:6]}.{stem[6:]}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--start", type=str, default=DEFAULT_START)
    parser.add_argument("--end", type=str, default=None,
                        help="defaults to run-time today")
    parser.add_argument(
        "--max-age-days",
        type=int,
        # Weekly cadence: a 7-day gap trips the refresh; same-week reruns skip.
        default=6,
    )
    parser.add_argument("--delay", type=float, default=0.12)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    summary = fetch_dailybasic(
        cache,
        start=args.start,
        end=args.end,
        delay_seconds=args.delay,
        max_age_days=args.max_age_days,
    )
    failed = (
        bool(summary["failed_symbols"]) or bool(summary["failed_dates"])
    )
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DailybasicFetchError as exc:
        print(f"DAILYBASIC_FETCH_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
