"""All-market dragon-tiger branch-seat (``top_inst``) daily fetcher.

Data expansion bound to the #35-extension binding (ladder doc, PR #487):
pre-event listing-day INSTITUTIONAL seat net-buy structure as a
short-horizon confirmation layer — horizon pre-bounded by the external
evidence triangulation (Hong et al 2025: seat trading relates to
SHORT-term returns only).  The endpoint shares the ``top_list``
contract family (live-probed 2026-08-24): one call per TRADING DAY,
rows landed one CSV per day under ``<cache>/topinst_daily/``.  Trading
days come from the on-disk SSE index series via ``load_index_series``
(fail-closed when absent).  Idempotency mirrors the toplist fetcher:
existing files are never re-fetched (safe resume); per-day failures are
recorded without aborting the sweep; an EMPTY response on a real
trading day writes no file and is counted in ``empty_days`` so re-runs
retry it.  Fail-closed guards: aborts below ``MIN_FREE_BYTES`` free;
missing token aborts before any call.  Seat-identity classification
(机构专用 vs hot-money branches) is deliberately NOT done here — it is
a frozen-preregistration concern; this fetcher lands raw rows only.
research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_topinst_fetch.py [--cache DIR]
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
DEFAULT_END = "20260824"
TOPINST_DIRNAME = "topinst_daily"
MIN_FREE_BYTES = 2 * 1024**3  # abort below 2 GiB free on the cache volume
FIELDS = [
    "trade_date",
    "ts_code",
    "exalter",
    "buy",
    "buy_rate",
    "sell",
    "sell_rate",
    "net_buy",
    "side",
    "reason",
]


class TopinstFetchError(RuntimeError):
    """Fail-closed fetch failure with a stable reason code."""


def resolve_trading_days(cache: Path, start: str, end: str) -> list[str]:
    """SSE trading sessions inside [start, end] from the index series.

    Fail-closed: a missing index CSV raises rather than guessing a
    calendar.  Dates are returned ascending as YYYYMMDD strings.
    """
    if len(start) != 8 or len(end) != 8 or start > end:
        raise TopinstFetchError("bad_range")
    from Ashare.event_calendar_lockup_strata import StrataError

    try:
        from Ashare.event_calendar_lockup_strata import load_index_series

        pairs = load_index_series(cache)
    except StrataError as exc:
        raise TopinstFetchError(f"calendar_unavailable:{exc}") from exc
    days = [
        d.strftime("%Y%m%d")
        for d, _ in pairs
        if start <= d.strftime("%Y%m%d") <= end
    ]
    if not days:
        raise TopinstFetchError("calendar_empty_for_range")
    return days


def _token() -> str:
    import os

    token = os.environ.get("TUSHARE_MCP_TOKEN", "").strip().strip('"').strip("'")
    if not token:
        raise TopinstFetchError("token_missing")
    return token


def fetch_topinst(
    cache: Path,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    delay: float = 0.3,
    call=None,
    days: list[str] | None = None,
) -> dict[str, object]:
    """Fetch branch seats day by day into per-trade_date CSV files.

    ``call(trade_date)`` returns the raw day payload as a list of row
    dicts (tests inject fakes; production wraps the raw HTTP endpoint).
    ``days`` overrides the resolved trading calendar (tests); production
    resolves it fail-closed from the index series.
    """
    folder = cache / TOPINST_DIRNAME
    if shutil.disk_usage(cache).free < MIN_FREE_BYTES:
        raise TopinstFetchError("disk_full_guard")
    folder.mkdir(parents=True, exist_ok=True)

    sessions = list(days) if days is not None else None
    if sessions is None:
        sessions = resolve_trading_days(cache, start, end)

    stats: dict[str, object] = {
        "days": len(sessions),
        "files_written": 0,
        "files_skipped": 0,
        "rows_seen": 0,
        "bad_rows": 0,
        "empty_days": 0,
        "errors": [],
    }
    if call is None:

        def call(trade_date: str):  # noqa: E306
            from Ashare.event_calendar_fetch import call_api

            fields, rows = call_api("top_inst", {"trade_date": trade_date})
            return [dict(zip(fields, row)) for row in rows]

    for day in sessions:
        try:
            rows = call(day)
        except Exception as exc:  # noqa: BLE001 - recorded, sweep continues
            stats["errors"].append(f"{day}: {exc}")
            time.sleep(max(delay, 0.1))
            continue
        time.sleep(max(delay, 0.0))
        kept: list[dict] = []
        for row in rows:
            stats["rows_seen"] = int(stats["rows_seen"]) + 1
            ts_code = str(row.get("ts_code") or "")
            trade_date = str(row.get("trade_date") or "")
            exalter = str(row.get("exalter") or "")
            # A seat row without its branch name cannot be classified at
            # the study layer — count and drop.
            if not ts_code or not exalter or trade_date != day:
                stats["bad_rows"] = int(stats["bad_rows"]) + 1
                continue
            kept.append(row)
        if not kept:
            # Real trading day with nothing returned: source depth/gap —
            # write nothing so re-runs retry the day (limit-list rule).
            stats["empty_days"] = int(stats["empty_days"]) + 1
            continue
        path = folder / f"{day}.csv"
        if path.exists():
            stats["files_skipped"] += len(kept)
            continue
        _write_csv(path, kept)
        stats["files_written"] = int(stats["files_written"]) + 1
    return stats


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--delay", type=float, default=0.3)
    args = parser.parse_args()
    from Ashare.event_calendar_fetch import CACHE_DIR

    cache = args.cache if args.cache is not None else CACHE_DIR
    stats = fetch_topinst(cache, start=args.start, end=args.end, delay=args.delay)
    print(
        f"topinst fetch: days={stats['days']} "
        f"written={stats['files_written']} skipped={stats['files_skipped']} "
        f"rows={stats['rows_seen']} bad={stats['bad_rows']} "
        f"empty_days={stats['empty_days']} "
        f"errors={len(stats['errors'])}"  # type: ignore[arg-type]
    )
    for err in stats["errors"]:  # type: ignore[union-attr]
        print(f"  ERROR {err}", file=sys.stderr)
    return 1 if stats["errors"] else 0  # type: ignore[arg-type]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TopinstFetchError as exc:
        print(f"TOPINST_FETCH_FAILED {exc}", file=sys.stderr)
        sys.exit(2)
