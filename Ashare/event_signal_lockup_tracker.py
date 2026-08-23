"""Rolling shadow tracker for the lockup oversold-rebound event signal.

Research-only.  The 2026-08-23 event-calendar study found the strongest
repeatable structure around lockup expiries: symbols that fall into the
factor's ``sell_off`` anticipation class (pre-event 10-session return at or
below -3%) recover with the highest win rate after the event.  This tracker
turns that structure into the first formally tracked event signal:

* it mints lockup-expiry entries from the research cache through the real
  ``event_catalyst_adapter`` row path,
* feeds them with forward-adjusted bars into the real
  ``event_catalyst_shadow`` factor so every classification and label comes
  from production code rather than a reimplementation,
* reports ``sell_off`` observations still inside their post window as the
  currently tracked signal list,
* appends labelled ``sell_off`` outcomes to the shared SampleJournal via the
  ``shadow_research`` bridge (excluded from trading-layer KPIs by policy).

Deduplication: the observation receipt embeds ``as_of``, so re-running the
same history on a later day produces different journal ids.  The tracker
therefore keeps its own append-only written-ledger and additionally reads
back already-journaled ``shadow_research`` event ids before writing; an
event is journaled once and then frozen.

This grants no capital authority, executes nothing live, and is not
promotion evidence.

Usage::

    python3 Ashare/event_signal_lockup_tracker.py \
        [--cache /tmp/ashare_event_research] \
        [--journal shared/review/ashare/sample_journal.jsonl] \
        [--since 20260601] [--dry-run]
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.adapter import DEFAULT_SAMPLE_JOURNAL_PATH  # noqa: E402
from Ashare.event_catalyst_adapter import (  # noqa: E402
    SHARE_FLOAT_DATASET_ID,
    EventCatalystAdapterError,
    catalyst_entry_from_lockup_row,
)
from Ashare.event_catalyst_journal import (  # noqa: E402
    append_shadow_batch_to_journal,
    journal_records_from_shadow_batch,
)
from Ashare.event_catalyst_shadow import (  # noqa: E402
    CatalystShadowBatch,
    CatalystShadowObservation,
    DailyBar,
    build_catalyst_shadow_batch,
)


SIGNAL_EVENT_TYPE = "lockup_expiry"
SIGNAL_ANTICIPATION_CLASS = "sell_off"
DEFAULT_SINCE = "20260601"
LEDGER_FILENAME = "signal_tracker_ledger.jsonl"

PRE_WINDOW_SESSIONS = 10
POST_WINDOW_SESSIONS = 5


class TrackerError(RuntimeError):
    """Fail-closed tracker failure with a stable reason code."""


def _read_csv(cache: Path, name: str) -> tuple[list[str], list[dict[str, str]]]:
    path = cache / f"{name}.csv"
    if not path.exists():
        raise TrackerError(f"cache_missing:{path.name}")
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fields = next(reader)
        rows = [dict(zip(fields, row)) for row in reader]
    return fields, rows


def _parse_day(raw: str) -> date:
    return datetime.strptime(raw.strip(), "%Y%m%d").date()


def load_lockup_entries(
    cache: Path, samples: set[str], since: date
) -> tuple[list, int]:
    """Mint lockup-expiry catalyst entries on/after ``since`` via the adapter.

    Returns the validated entries plus the count of real rows the adapter
    failed closed on (blank or non-positive share counts).
    """

    _fields, rows = _read_csv(cache, "share_float")
    eligible = [
        r
        for r in rows
        if r["ts_code"] in samples
        and r["float_date"] >= r["ann_date"]
        and _parse_day(r["float_date"]) >= since
    ]
    entries = []
    skipped = 0
    for idx, raw_row in enumerate(eligible):
        # CSV cells are strings; the adapter requires numeric share counts.
        row = dict(raw_row)
        for field_name in ("float_share", "float_ratio"):
            try:
                row[field_name] = float(row[field_name])
            except (TypeError, ValueError):
                pass
        try:
            entry = catalyst_entry_from_lockup_row(
                row,
                dataset_id=SHARE_FLOAT_DATASET_ID,
                receipt_id=f"tracker-{idx:06d}",
            )
        except EventCatalystAdapterError:
            skipped += 1
            continue
        entries.append(entry)
    return entries, skipped


def load_bars(cache: Path, samples: set[str]) -> dict[str, list[DailyBar]]:
    """Load forward-adjusted close series for every sample symbol with data."""

    bars_by_symbol: dict[str, list[DailyBar]] = {}
    for code in sorted(samples):
        stem = code.replace(".", "")
        bar_path = cache / f"daily_{stem}.csv"
        adj_path = cache / f"adjfactor_{stem}.csv"
        if not bar_path.exists() or not adj_path.exists():
            continue
        with bar_path.open(encoding="utf-8") as handle:
            reader = csv.reader(handle)
            fields = next(reader)
            close_i = fields.index("close")
            date_i = fields.index("trade_date")
            rows = [(r[date_i], float(r[close_i])) for r in reader]
        with adj_path.open(encoding="utf-8") as handle:
            reader = csv.reader(handle)
            fields = next(reader)
            ai = fields.index("adj_factor")
            di = fields.index("trade_date")
            factors = {r[di]: float(r[ai]) for r in reader}
        latest = max(factors.values())
        series = [
            DailyBar(trade_date=_parse_day(d), close=c * factors[d] / latest)
            for d, c in rows
            if d in factors and c > 0
        ]
        series.sort(key=lambda bar: bar.trade_date)
        if series:
            bars_by_symbol[code] = series
    return bars_by_symbol


@dataclass
class TrackerView:
    """Signal-filtered projection of one shadow batch."""

    batch: CatalystShadowBatch
    status_counts: dict[str, int] = field(default_factory=dict)
    active_signals: tuple[CatalystShadowObservation, ...] = ()
    labeled_signals: tuple[CatalystShadowObservation, ...] = ()
    other_labeled: int = 0
    appended_records: list[dict] = field(default_factory=list)


def build_tracker_view(batch: CatalystShadowBatch) -> TrackerView:
    """Split one lockup-only batch into signal tracking buckets.

    A signal observation is a lockup expiry whose factor classification is
    ``sell_off`` with full pre-window history (``observed``).  Active means
    the post window has not closed yet (``pending``); labelled means both
    windows are observable.
    """

    status_counts: dict[str, int] = {}
    active: list[CatalystShadowObservation] = []
    labeled: list[CatalystShadowObservation] = []
    other_labeled = 0
    for obs in batch.observations:
        status_counts[obs.observation_status] = (
            status_counts.get(obs.observation_status, 0) + 1
        )
        if obs.observation_status != "observed":
            continue
        is_signal = (
            obs.event_type == SIGNAL_EVENT_TYPE
            and obs.anticipation_class == SIGNAL_ANTICIPATION_CLASS
        )
        if obs.post_label_state == "labeled":
            if is_signal:
                labeled.append(obs)
            else:
                other_labeled += 1
        elif is_signal and obs.post_label_state == "pending":
            active.append(obs)
    return TrackerView(
        batch=batch,
        status_counts=status_counts,
        active_signals=tuple(active),
        labeled_signals=tuple(labeled),
        other_labeled=other_labeled,
    )


def load_written_event_ids(journal: Any, ledger_path: Path) -> set[str]:
    """Union of event ids already journaled or ledgered for this signal.

    The journal read-back is the safety net: the observation receipt embeds
    ``as_of``, so a lost or truncated ledger alone would not be enough to
    prevent duplicate shadow_research rows on the next run.
    """

    written: set[str] = set()
    if ledger_path.exists():
        with ledger_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                event_id = str(row.get("event_id") or "")
                if event_id:
                    written.add(event_id)
    read_events = getattr(journal, "read_events", None)
    if callable(read_events):
        for row in read_events():
            if row.get("record_type") != "shadow_research":
                continue
            event_id = str(row.get("event_id") or "")
            if event_id:
                written.add(event_id)
    return written


def signal_journal_records(
    batch: CatalystShadowBatch,
) -> tuple[dict, ...]:
    """Journal records restricted to labelled lockup sell_off observations."""

    return tuple(
        record
        for record in journal_records_from_shadow_batch(batch)
        if record.get("event_type") == SIGNAL_EVENT_TYPE
        and record.get("anticipation_class") == SIGNAL_ANTICIPATION_CLASS
    )


def append_new_outcomes(
    journal: Any,
    ledger_path: Path,
    records: tuple[dict, ...],
) -> list[dict]:
    """Append not-yet-journaled signal outcomes and freeze them in the ledger.

    Order matters: the journal write is authoritative and happens first; the
    ledger row is written only after the journal accepted the record.  A
    crash between the two leaves the journal read-back in
    :func:`load_written_event_ids` to catch the duplicate on the next run.
    """

    if not records:
        return []
    written = load_written_event_ids(journal, ledger_path)
    fresh = [r for r in records if r["event_id"] not in written]
    if not fresh:
        return []
    appended = journal.append_samples(fresh)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        for record in fresh:
            handle.write(
                json.dumps(
                    {
                        "event_id": record["event_id"],
                        "journal_event_id": record["journal_event_id"],
                        "written_as_of": record["as_of"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return appended


def describe_post_returns(observations: tuple[CatalystShadowObservation, ...]) -> dict:
    values = [float(o.post_return) for o in observations if o.post_return is not None]
    n = len(values)
    if n == 0:
        return {"n": 0}
    return {
        "n": n,
        "mean_bps": round(statistics.fmean(values) * 1e4, 1),
        "median_bps": round(statistics.median(values) * 1e4, 1),
        "win_rate": round(sum(1 for v in values if v > 0) / n, 3),
    }


def run_tracker(
    cache: Path,
    journal_path: Path,
    *,
    since: date,
    as_of: datetime,
    dry_run: bool = False,
) -> TrackerView:
    """One full tracker pass; returns the view for reporting."""

    _fields, sym_rows = _read_csv(cache, "sample_symbols")
    samples = {r["ts_code"] for r in sym_rows}
    entries, skipped = load_lockup_entries(cache, samples, since)
    bars_by_symbol = load_bars(cache, samples)
    batch = build_catalyst_shadow_batch(
        entries,
        bars_by_symbol,
        as_of=as_of,
        pre_window_sessions=PRE_WINDOW_SESSIONS,
        post_window_sessions=POST_WINDOW_SESSIONS,
    )
    view = build_tracker_view(batch)

    from shared.review.sample_journal import SampleJournal

    journal = SampleJournal(journal_path) if not dry_run else _NullJournal()
    records = signal_journal_records(batch)
    # The ledger lives beside the journal so both share one lifecycle; the
    # /tmp research cache is rebuildable and must not be the only record.
    appended = [] if dry_run else append_new_outcomes(
        journal, journal_path.parent / LEDGER_FILENAME, records
    )
    view.appended_records.extend(appended)
    return view


class _NullJournal:
    """Dry-run stand-in that reports an empty written set and appends nothing."""

    def read_events(self) -> list[dict]:
        return []

    def append_samples(self, samples):  # pragma: no cover - defensive
        raise TrackerError("dry_run_write_attempted")


def render_report(view: TrackerView, *, since: date, as_of: datetime, dry_run: bool) -> str:
    lines = [
        "# Lockup oversold-rebound signal tracker (research_only)",
        "",
        f"- as_of: {as_of.isoformat()}",
        f"- since: {since.isoformat()} (lockup expiries on/after this date)",
        "- research_only / not_promotion_evidence; no capital authority;",
        "  classifications and labels come from the production shadow factor.",
        "",
        "## Currently tracked signals (post window open)",
        "",
    ]
    if view.active_signals:
        lines.append("| event_id | symbol | float_date | pre_return | intensity |")
        lines.append("|---|---|---|---|---|")
        for obs in sorted(
            view.active_signals, key=lambda o: (o.scheduled_date, o.symbol or "")
        ):
            pre_pct = f"{float(obs.pre_return) * 100:.2f}%"
            lines.append(
                f"| {obs.event_id} | {obs.symbol} | {obs.scheduled_date} "
                f"| {pre_pct} | {obs.anticipation_intensity} |"
            )
    else:
        lines.append("(none)")
    lines += ["", "## Labelled outcomes", ""]
    stats = describe_post_returns(view.labeled_signals)
    lines.append(
        f"- sell_off outcomes observed this pass: n={stats.get('n', 0)}"
        + (
            f", mean={stats['mean_bps']}bps, median={stats['median_bps']}bps,"
            f" win_rate={stats['win_rate']}"
            if stats.get("n")
            else ""
        )
    )
    lines.append(f"- other (non-signal) labelled lockup observations skipped: {view.other_labeled}")
    if view.appended_records:
        lines.append(f"- newly journaled to SampleJournal (shadow_research layer): {len(view.appended_records)}")
    elif dry_run:
        lines.append("- dry run: nothing written to the journal or ledger")
    else:
        lines.append("- no new outcomes to journal (all already frozen by ledger/journal read-back)")
    lines += ["", "## Observation status counts", ""]
    for status, count in sorted(view.status_counts.items()):
        lines.append(f"- {status}: {count}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    def _arg_value(flag: str) -> str:
        return sys.argv[sys.argv.index(flag) + 1]

    cache = Path(_arg_value("--cache")) if "--cache" in sys.argv else Path("/tmp/ashare_event_research")
    journal_path = (
        Path(_arg_value("--journal")) if "--journal" in sys.argv else DEFAULT_SAMPLE_JOURNAL_PATH
    )
    since_raw = _arg_value("--since") if "--since" in sys.argv else DEFAULT_SINCE
    dry_run = "--dry-run" in sys.argv
    as_of = datetime.now(timezone.utc)

    view = run_tracker(
        cache,
        journal_path,
        since=_parse_day(since_raw),
        as_of=as_of,
        dry_run=dry_run,
    )
    print(render_report(view, since=_parse_day(since_raw), as_of=as_of, dry_run=dry_run))
    state_path = cache / "signal_tracker_state.json"
    state_path.write_text(
        json.dumps(
            {
                "research_only": True,
                "batch_receipt_sha256": view.batch.batch_receipt_sha256,
                "status_counts": view.status_counts,
                "active_signals": len(view.active_signals),
                "labeled_signals": len(view.labeled_signals),
                "appended": len(view.appended_records),
                "dry_run": dry_run,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"saved -> {state_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TrackerError as exc:
        print(f"TRACKER_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
