"""Expand the event-calendar study sample from top-200 to top-N mainboard.

Research-only.  Reuses the existing research cache and fetch helpers: ranks
mainboard symbols by disclosure count in the cached full-market
``disclosure.csv``, keeps the top ``--limit`` (default 1000), then

* refreshes daily bars + adjustment factors for symbols whose shards are
  missing or staler than ``--max-age-days`` — recently stale shards are
  topped up by whole-market per-session pulls (two calls per missing
  session regardless of universe size), while missing shards or gaps
  beyond ``DATE_TOPUP_MAX_DAYS`` fall back to the full-history re-pull
  (idempotent by overwrite);
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
    PAGE_LIMIT,
    REQUEST_INTERVAL_SECONDS,
    _shift_date,
    call_api,
    is_mainboard,
)


class ExpandError(RuntimeError):
    """Fail-closed expansion failure with a stable reason code."""


# Staleness beyond this many days no longer pays off as per-session
# top-ups: fall back to the full-history re-pull, which also heals the
# frozen shard so the fallback cannot recur every week.
DATE_TOPUP_MAX_DAYS = 30


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


def trading_days(start: str, end: str) -> list[str]:
    """SSE open sessions in ``[start, end]``, ascending, via trade_cal."""

    fields, rows = call_api(
        "trade_cal",
        {"exchange": "SSE", "start_date": start, "end_date": end},
    )
    time.sleep(REQUEST_INTERVAL_SECONDS)
    col = {name: idx for idx, name in enumerate(fields)}
    return sorted(
        row[col["cal_date"]]
        for row in rows
        if str(row[col["is_open"]]) == "1"
    )


def fetch_series_topup(cache: Path, codes: list[str], today: str) -> int:
    """Date-driven incremental top-up for stale-but-present bar shards.

    Whole-market ``trade_date`` pulls (~5.5k rows each, under the 6000-row
    cap) replace one-full-history-pull-per-symbol: a weekly gap costs two
    calls per missing session instead of two per symbol — the difference
    between a timeout-bound run and a bounded one at top-1000 scale.
    Rows are projected into the existing per-symbol shards, newest first
    like every other write; symbols absent from a session (suspension)
    simply gain no rows and stay stale for a later cycle or the 30-day
    full-history fallback.  Each shard family filters against its OWN last
    stored session — the daily and adjfactor shards can drift apart when
    the shared research cache was touched between runs, and filtering one
    family with the other's watermark would append duplicates.  A response
    at the page cap fails closed rather than truncate silently.  Returns
    the number of shards extended.
    """

    cutoff = _shift_date(today, -DATE_TOPUP_MAX_DAYS)
    lasts_by_prefix: dict[str, dict[str, str]] = {}
    for prefix in ("daily", "adjfactor"):
        lasts: dict[str, str] = {}
        for code in codes:
            last = series_max_day(cache, code, prefix=prefix)
            if last is not None and last >= cutoff:
                lasts[code] = last
        if lasts:
            lasts_by_prefix[prefix] = lasts
    if not lasts_by_prefix:
        return 0
    oldest = min(min(m.values()) for m in lasts_by_prefix.values())
    start = _shift_date(oldest, 1)
    if start > today:
        return 0  # shards already current through today: nothing to pull
    dates = trading_days(start, today)
    print(
        f"series_topup symbols={len(codes)} sessions={len(dates)} "
        f"range={start}-{today}",
        flush=True,
    )
    api_of = {"daily": "daily", "adjfactor": "adj_factor"}
    pulled: dict[str, dict[str, list[list]]] = {"daily": {}, "adjfactor": {}}
    fields_seen: dict[str, list[str]] = {}
    for day in dates:
        for prefix in ("daily", "adjfactor"):
            lasts = lasts_by_prefix.get(prefix)
            if lasts is None:
                continue
            fields, rows = call_api(api_of[prefix], {"trade_date": day})
            if len(rows) >= PAGE_LIMIT:
                raise ExpandError(f"{api_of[prefix]}_date_capped:{day}")
            known = fields_seen.setdefault(prefix, list(fields))
            if fields != known:
                raise ExpandError(f"{prefix}_schema_drift:{day}")
            ti = fields.index("ts_code")
            di = fields.index("trade_date")
            bucket = pulled[prefix]
            for row in rows:
                last = lasts.get(row[ti])
                if last is not None and row[di] > last:
                    bucket.setdefault(row[ti], []).append(row)
            time.sleep(REQUEST_INTERVAL_SECONDS)
    extended = 0
    for prefix, bucket in pulled.items():
        for code, rows in bucket.items():
            shard_fields, old_rows = _read_cache(cache, f"{prefix}_{code.replace('.', '')}")
            di = shard_fields.index("trade_date")
            merged = sorted(rows, key=lambda r: r[di], reverse=True) + old_rows
            _save_csv(cache, f"{prefix}_{code.replace('.', '')}", shard_fields, merged)
            extended += 1
    print(f"series_topup done shards_extended={extended}", flush=True)
    return extended


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


LOCKUP_KEY_FIELDS = ("ts_code", "ann_date", "float_date", "holder_name", "share_type")


class ShareFloatMerger:
    """In-memory accumulator merging share_float batches into one table.

    ``share_float.csv`` is the tracker's single lockup table — new unlock
    announcements must land here or they never reach the rolling gate.
    Duplicates on :data:`LOCKUP_KEY_FIELDS` are dropped, so repeated
    full-history pulls stay idempotent.  Loading reads the table once and
    :meth:`save` writes it back once; absorbing N responses in between used
    to rewrite the whole file per symbol (~10GB of churn across a top-1000
    sweep).
    """

    def __init__(self, cache: Path, fields: list[str], rows: list[list]):
        self._cache = cache
        self.fields = fields
        self.rows = rows
        # Key positions wait for the first response when the table does not
        # exist yet (its schema comes from that response's fields).
        self._key_idx: list[int] = []
        self._seen: set[tuple] = set()
        if fields:
            self._key_idx = [fields.index(k) for k in LOCKUP_KEY_FIELDS]
            self._seen = {tuple(row[i] for i in self._key_idx) for row in rows}

    @classmethod
    def load(cls, cache: Path) -> ShareFloatMerger:
        if (cache / "share_float.csv").exists():
            fields, rows = _read_cache(cache, "share_float")
        else:
            fields, rows = [], []
        return cls(cache, fields, rows)

    def absorb(self, fields: list[str], rows: list[list]) -> int:
        """Merge one API response; returns the number of API rows seen."""

        if not rows:
            return 0
        if not self.fields:
            self.fields = list(fields)
        if not self._key_idx:
            self._key_idx = [self.fields.index(k) for k in LOCKUP_KEY_FIELDS]
        fi = {name: idx for idx, name in enumerate(fields)}
        absorbed = 0
        for row in rows:
            projected = [
                row[fi.get(name, fi["ts_code"])] if name in fi else ""
                for name in self.fields
            ]
            key = tuple(projected[i] for i in self._key_idx)
            if key not in self._seen:
                self._seen.add(key)
                self.rows.append(projected)
            absorbed += 1
        return absorbed

    def save(self) -> None:
        if self.fields:
            _save_csv(self._cache, "share_float", self.fields, self.rows)


def fetch_symbol_lockups(cache: Path, ts_code: str) -> int:
    """Merge one symbol's full lockup history into ``share_float.csv``.

    Single-symbol convenience path over :class:`ShareFloatMerger`; the
    weekly universe sweep batches through one merger instead.  Returns the
    number of API rows seen for this symbol.
    """

    fields, rows = call_api("share_float", {"ts_code": ts_code})
    time.sleep(REQUEST_INTERVAL_SECONDS)
    merger = ShareFloatMerger.load(cache)
    seen = merger.absorb(fields, rows)
    merger.save()
    return seen


def partition_refresh(
    cache: Path, todo: list[str], cutoff: str
) -> tuple[list[str], list[str], list[str]]:
    """Split stale symbols into (missing, fallback, topup) work lists.

    ``missing``: no daily shard at all — full-history pull required.
    ``fallback``: gap beyond the top-up window, or a broken shard pair
    (daily present, adjfactor absent — e.g. a crash between the two
    writes); the full re-pull restores the pair instead of leaving a
    half-fed symbol behind.
    ``topup``: recently stale complete pairs — date-driven whole-market
    pulls apply.
    """

    missing: list[str] = []
    fallback: list[str] = []
    topup: list[str] = []
    for code in todo:
        last = series_max_day(cache, code)
        if last is None:
            missing.append(code)
        elif last < cutoff:
            fallback.append(code)
        elif not symbol_has_full_data(cache, code):
            fallback.append(code)
        else:
            topup.append(code)
    return missing, fallback, topup


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
    missing, fallback, topup = partition_refresh(
        args.cache, todo, _shift_date(today, -DATE_TOPUP_MAX_DAYS)
    )
    print(
        f"series_to_refresh={len(todo)} "
        f"(max_age_days={args.max_age_days} missing={len(missing)} "
        f"fallback={len(fallback)} topup={len(topup)})",
        flush=True,
    )
    fetch_series_topup(args.cache, topup, today)
    for i, code in enumerate(missing + fallback, 1):
        fetch_symbol_series(args.cache, code)
        if i % 25 == 0 or i == len(missing + fallback):
            print(
                f"series_progress={i}/{len(missing + fallback)} last={code}",
                flush=True,
            )

    # Unlock announcements arrive continuously and are keyed by symbol, not
    # by bar staleness: re-pull every ranked symbol's batch history each run
    # so new float dates reach the tracker's event stream within one cycle.
    # Whole-market date slicing is unsound here (single days can exceed the
    # row cap), so the sweep stays per-symbol; only the table merge is
    # batched — read once, dedupe in memory, write once.
    merger = ShareFloatMerger.load(args.cache)
    rows_seen = 0
    for i, code in enumerate(ranked, 1):
        fields, rows = call_api("share_float", {"ts_code": code})
        time.sleep(REQUEST_INTERVAL_SECONDS)
        rows_seen += merger.absorb(fields, rows)
        if i % 50 == 0 or i == len(ranked):
            print(
                f"lockups_progress={i}/{len(ranked)} rows_seen={rows_seen}",
                flush=True,
            )
        if i % 250 == 0 or i == len(ranked):
            # Checkpoint: the old per-symbol path persisted every merge, so
            # a crashed sweep resumed with most pulls already absorbed.
            merger.save()
    merger.save()

    write_sample_symbols(args.cache, ranked)
    print(f"done sample_symbols_expanded={len(ranked)}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ExpandError as exc:
        print(f"EXPAND_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
