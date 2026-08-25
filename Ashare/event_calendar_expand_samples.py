"""Expand the event-calendar study sample from top-200 to top-N mainboard.

Research-only.  Reuses the existing research cache and fetch helpers: ranks
mainboard symbols by disclosure count in the cached full-market
``disclosure.csv``, keeps the top ``--limit`` (default 1000), then

* refreshes daily bars + adjustment factors for symbols whose shards are
  missing or staler than ``--max-age-days`` (full-history re-pull,
  idempotent by overwrite);
* merges every ranked symbol's full ``share_float`` batch history into
  ``share_float.csv`` — the tracker's single lockup table — deduplicating
  on batch identity so repeated pulls never duplicate rows;
* rewrites ``sample_symbols_expanded.csv`` with the ranked universe.

The first run after a universe expansion backfills everything; steady-state
runs only pay the per-symbol lockup re-pull plus any stale bar shards.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_calendar_fetch import (  # noqa: E402
    REQUEST_INTERVAL_SECONDS,
    call_api,
    is_mainboard,
)


class ExpandError(RuntimeError):
    """Fail-closed expansion failure with a stable reason code."""


def _read_cache(cache: Path, name: str) -> tuple[list[str], list[list]]:
    path = cache / f"{name}.csv"
    if not path.exists():
        raise ExpandError(f"cache_missing:{path.name}")
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fields = next(reader)
        return fields, [row for row in reader]


def _save_csv(cache: Path, name: str, fields: list[str], rows: list[list]) -> None:
    with (cache / f"{name}.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)


def pick_expansion_symbols(
    cache: Path, limit: int, eligibility: str
) -> list[str]:
    """Top-N mainboard symbols by disclosure appointments in the cache."""

    _fields, rows = _read_cache(cache, "disclosure")
    counts: Counter[str] = Counter()
    for row in rows:
        by_code = dict(zip(_fields, row))
        code = by_code["ts_code"]
        if is_mainboard(code) and "20190101" <= by_code["pre_date"] <= eligibility:
            counts[code] += 1
    ranked = [code for code, _ in counts.most_common(limit)]
    return ranked


def symbol_has_full_data(cache: Path, ts_code: str) -> bool:
    """Existence check kept for callers that only ask "is this covered"."""

    stem = ts_code.replace(".", "")
    return all(
        (cache / f"{prefix}_{stem}.csv").exists()
        for prefix in ("daily", "adjfactor")
    )


def series_max_day(cache: Path, ts_code: str, prefix: str = "daily") -> str | None:
    """Last ``trade_date`` stored in a per-symbol ``<prefix>_<stem>`` shard."""

    path = cache / f"{prefix}_{ts_code.replace('.', '')}.csv"
    if not path.exists():
        return None
    last: str | None = None
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            day = row.get("trade_date") or ""
            if day > (last or ""):
                last = day
    return last


def series_is_fresh(
    cache: Path,
    ts_code: str,
    today: str,
    max_age_days: int,
    prefix: str = "daily",
) -> bool:
    """True when the shard exists and its last session is recent enough.

    Under the tracker's weekly cadence a 6-day ceiling means every
    scheduled run refreshes (gap 7d > 6) while repeated same-week runs
    skip; missing shards are never fresh.
    """

    from Ashare.event_calendar_fetch import _shift_date

    last = series_max_day(cache, ts_code, prefix=prefix)
    if last is None:
        return False
    return last >= _shift_date(today, -max_age_days)


def series_window_end() -> str:
    """December 31 of the current year — never expires mid-stream.

    A hardcoded year-end bound would silently cap every full-history
    re-pull once that year passes (same failure class as the frozen
    fetch windows fixed alongside).
    """

    return f"{time.strftime('%Y')}1231"


def fetch_symbol_series(cache: Path, ts_code: str) -> None:
    """Pull daily + adj_factor for one symbol into per-symbol cache files."""

    stem = ts_code.replace(".", "")
    end = series_window_end()
    for api, name, start in (
        ("daily", f"daily_{stem}", "20180101"),
        ("adj_factor", f"adjfactor_{stem}", "20180101"),
    ):
        fields_out: list[str] | None = None
        rows_out: list[list] = []
        cursor = start
        while cursor <= end:
            fields, rows = call_api(api, {"ts_code": ts_code, "start_date": cursor, "end_date": end})
            if fields_out is None:
                fields_out = fields
            rows_out.extend(rows)
            if len(rows) < 6000:
                break
            last = max(r[fields.index("trade_date")] for r in rows)
            nxt = str(int(last) + 1)
            if nxt <= cursor:
                raise ExpandError(f"pagination_stuck:{ts_code}:{api}")
            cursor = nxt
            time.sleep(REQUEST_INTERVAL_SECONDS)
        if fields_out:
            _save_csv(cache, name, fields_out, rows_out)
        time.sleep(REQUEST_INTERVAL_SECONDS)


def fetch_symbol_lockups(cache: Path, ts_code: str) -> int:
    """Merge one symbol's full lockup history into ``share_float.csv``.

    The tracker's event stream reads ``share_float.csv``, so that file is
    the single lockup table: new unlock announcements must land here or
    they never reach the rolling gate.  Duplicates on
    (ts_code, ann_date, float_date, holder_name, share_type) are dropped,
    making repeated full-history pulls idempotent.  Returns the number of
    API rows seen for this symbol.
    """

    fields, rows = call_api("share_float", {"ts_code": ts_code})
    time.sleep(REQUEST_INTERVAL_SECONDS)
    if not rows:
        return 0
    merged_path = cache / "share_float.csv"
    seen: set[tuple] = set()
    out_fields: list[str] = fields
    out_rows: list[list] = []
    if merged_path.exists():
        old_fields, old_rows = _read_cache(cache, "share_float")
        out_fields = old_fields
        key_idx = [old_fields.index(k) for k in ("ts_code", "ann_date", "float_date", "holder_name", "share_type")]
        for row in old_rows:
            key = tuple(row[i] for i in key_idx)
            if key not in seen:
                seen.add(key)
                out_rows.append(row)
    fi = {name: idx for idx, name in enumerate(fields)}
    for row in rows:
        projected = [
            row[fi.get(name, fi["ts_code"])] if name in fi else ""
            for name in out_fields
        ]
        key = tuple(
            projected[out_fields.index(k)]
            for k in ("ts_code", "ann_date", "float_date", "holder_name", "share_type")
        )
        if key not in seen:
            seen.add(key)
            out_rows.append(projected)
    _save_csv(cache, "share_float", out_fields, out_rows)
    return len(rows)


def write_sample_symbols(cache: Path, symbols: list[str]) -> None:
    _save_csv(cache, "sample_symbols_expanded", ["ts_code"], [[c] for c in symbols])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--cache", type=Path, default=Path("/tmp/ashare_event_research"))
    parser.add_argument("--eligibility", default="20191231")
    parser.add_argument(
        "--max-age-days",
        type=int,
        # Weekly cadence: a 7-day gap trips the refresh; same-week reruns skip.
        default=6,
    )
    args = parser.parse_args()

    ranked = pick_expansion_symbols(args.cache, args.limit, args.eligibility)
    print(f"ranked_symbols={len(ranked)}", flush=True)

    today = time.strftime("%Y%m%d")
    todo = [
        c for c in ranked
        if not series_is_fresh(args.cache, c, today, args.max_age_days)
    ]
    print(
        f"series_to_refresh={len(todo)} "
        f"(max_age_days={args.max_age_days})",
        flush=True,
    )
    for i, code in enumerate(todo, 1):
        fetch_symbol_series(args.cache, code)
        if i % 25 == 0 or i == len(todo):
            print(f"series_progress={i}/{len(todo)} last={code}", flush=True)

    # Unlock announcements arrive continuously and are keyed by symbol, not
    # by bar staleness: re-pull every ranked symbol's batch history each run
    # so new float dates reach the tracker's event stream within one cycle.
    rows_seen = 0
    for i, code in enumerate(ranked, 1):
        rows_seen += fetch_symbol_lockups(args.cache, code)
        if i % 50 == 0 or i == len(ranked):
            print(
                f"lockups_progress={i}/{len(ranked)} rows_seen={rows_seen}",
                flush=True,
            )

    write_sample_symbols(args.cache, ranked)
    print(f"done sample_symbols_expanded={len(ranked)}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ExpandError as exc:
        print(f"EXPAND_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
