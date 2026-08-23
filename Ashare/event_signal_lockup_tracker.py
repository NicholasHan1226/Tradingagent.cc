"""Rolling shadow tracker for the event-calendar study's repeatable signals.

Research-only.  The 2026-08-23 event-calendar study (1000-symbol
robustness-checked) found three repeatable structures; this tracker rolls
them forward on real data inside the shadow channel:

* ``lockup``       — lockup oversold-rebound: lockup expiries the factor
  classifies ``sell_off`` (pre-event 10-session return at or below -3%).
* ``earnings_pos`` — disclosure positive drift: disclosures whose earliest
  prior earnings forecast is positive (预增/扭亏/…).  The study's edge is
  the *pre-disclosure* drift, so a labelled ``post_return`` here is the
  hold-past-disclosure control, not the trade itself.
* ``earnings_neg`` — disclosure negative relief: disclosures whose earliest
  prior forecast is negative (预减/首亏/…); the labelled post window
  measures the relief rally the study predicts.

Mechanics:

* lockup entries are minted through the real ``event_catalyst_adapter``
  row path; disclosure entries go through the calendar-document path with
  the forecast direction mapped onto ``impact_direction`` (disclosures
  without a usable prior forecast carry no studied structure and are not
  tracked),
* classification and labels come from the production
  ``event_catalyst_shadow`` factor under the ``momentum_evidence_v1``
  positioning profile — the default profile's reduce/realize hypotheses
  were falsified by the replay for these families,
* labelled signal outcomes append to the shared SampleJournal via the
  ``shadow_research`` bridge (excluded from trading-layer KPIs by policy).

Deduplication: the observation receipt embeds ``as_of``, so re-running the
same history on a later day produces different journal ids.  The tracker
keeps its own append-only written-ledger and additionally reads back
already-journaled ``shadow_research`` event ids before writing; an event is
journaled once and then frozen.

This grants no capital authority, executes nothing live, and is not
promotion evidence.

Usage::

    python3 Ashare/event_signal_lockup_tracker.py \
        [--cache /tmp/ashare_event_research] \
        [--journal shared/review/ashare/sample_journal.jsonl] \
        [--since 20260601] [--signals lockup,earnings_pos,earnings_neg] \
        [--dry-run]
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
    catalyst_entries_from_calendar_document,
    catalyst_entry_from_lockup_row,
)
from Ashare.event_catalyst_journal import (  # noqa: E402
    journal_records_from_shadow_batch,
)
from Ashare.event_catalyst_shadow import (  # noqa: E402
    POSITIONING_PROFILE_MOMENTUM_EVIDENCE_V1,
    CatalystShadowBatch,
    CatalystShadowObservation,
    DailyBar,
    build_catalyst_shadow_batch,
)
from Ashare.event_calendar_earnings_groups import (  # noqa: E402
    direction_group,
    load_forecast_directions,
)


SIGNAL_EVENT_TYPE = "lockup_expiry"
SIGNAL_ANTICIPATION_CLASS = "sell_off"
EARNINGS_DISCLOSURE_EVENT_TYPE = "earnings_disclosure"
DEFAULT_SINCE = "20260601"
LEDGER_FILENAME = "signal_tracker_ledger.jsonl"

PRE_WINDOW_SESSIONS = 10
POST_WINDOW_SESSIONS = 5

LOCKUP_SIGNAL = "lockup"
EARNINGS_POS_SIGNAL = "earnings_pos"
EARNINGS_NEG_SIGNAL = "earnings_neg"
KNOWN_SIGNALS = (LOCKUP_SIGNAL, EARNINGS_POS_SIGNAL, EARNINGS_NEG_SIGNAL)


class TrackerError(RuntimeError):
    """Fail-closed tracker failure with a stable reason code."""


def parse_signals(raw: str) -> tuple[str, ...]:
    """Parse and validate the --signals list; fail closed on unknown keys."""

    parts = tuple(s.strip() for s in raw.split(",") if s.strip())
    if not parts:
        raise TrackerError("signals_empty")
    unknown = [s for s in parts if s not in KNOWN_SIGNALS]
    if unknown:
        raise TrackerError(f"signal_unknown:{','.join(unknown)}")
    return parts


def observation_signal_keys(obs: CatalystShadowObservation) -> tuple[str, ...]:
    """Signal buckets one observation belongs to (may be none)."""

    keys: list[str] = []
    if (
        obs.event_type == SIGNAL_EVENT_TYPE
        and obs.anticipation_class == SIGNAL_ANTICIPATION_CLASS
    ):
        keys.append(LOCKUP_SIGNAL)
    if obs.event_type == EARNINGS_DISCLOSURE_EVENT_TYPE:
        if obs.impact_direction == "positive":
            keys.append(EARNINGS_POS_SIGNAL)
        elif obs.impact_direction == "negative":
            keys.append(EARNINGS_NEG_SIGNAL)
    return tuple(keys)


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


def load_disclosure_entries(
    cache: Path, samples: set[str], since: date
) -> tuple[list, int]:
    """Mint disclosure entries whose prior forecast has a usable direction.

    The forecast direction (from the study cache's ``forecast.csv``, earliest
    announcement before the disclosure) maps onto ``impact_direction`` so the
    journal carries both axes for every tracked event.  One entry per
    (symbol, appointment date); disclosures with no prior forecast, an
    uncertain one, or none before the disclosure are not tracked — the study
    found no structure there.
    """

    forecasts = load_forecast_directions(cache)
    _fields, rows = _read_csv(cache, "disclosure")
    doc_entries: list[dict] = []
    seen_events: set[tuple[str, str]] = set()
    skipped = 0
    for row in rows:
        code = row["ts_code"]
        if code not in samples or row["pre_date"] < row["ann_date"]:
            continue
        appointment = row["pre_date"]
        if _parse_day(appointment) < since:
            continue
        key = (code, appointment)
        if key in seen_events:
            continue
        prior = forecasts.get((code, row["end_date"]))
        group = (
            direction_group(prior[1])
            if prior and prior[0] <= row["pre_date"]
            else "no_forecast"
        )
        if group == "forecast_positive":
            impact = "positive"
        elif group == "forecast_negative":
            impact = "negative"
        else:
            skipped += 1
            continue
        seen_events.add(key)
        doc_entries.append(
            {
                "event_id": f"disc:{code}:{row['end_date']}:{appointment}",
                "event_type": EARNINGS_DISCLOSURE_EVENT_TYPE,
                "scheduled_date": (
                    f"{appointment[:4]}-{appointment[4:6]}-{appointment[6:8]}"
                ),
                "date_confidence": "hard_date",
                "impact_direction": impact,
                "source_ref": f"tushare:disclosure_date:{row['ann_date']}",
                "symbol": code,
            }
        )
    if not doc_entries:
        raise TrackerError("disclosure_entries_empty")
    document = {"calendar_id": "ashare-event-tracker-v1", "entries": doc_entries}
    try:
        return list(catalyst_entries_from_calendar_document(document)), skipped
    except EventCatalystAdapterError as exc:
        raise TrackerError(f"disclosure_adapter_failed:{exc.reason_code}") from exc


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
class SignalBucket:
    """One signal's tracked observations for a single pass."""

    active: tuple[CatalystShadowObservation, ...] = ()
    labeled: tuple[CatalystShadowObservation, ...] = ()


@dataclass
class TrackerView:
    """Signal-filtered projection of one shadow batch."""

    batch: CatalystShadowBatch
    signals: tuple[str, ...] = ()
    status_counts: dict[str, int] = field(default_factory=dict)
    buckets: dict[str, SignalBucket] = field(default_factory=dict)
    unattributed_labeled: int = 0
    appended_records: list[dict] = field(default_factory=list)


def build_tracker_view(
    batch: CatalystShadowBatch, signals: tuple[str, ...]
) -> TrackerView:
    """Split one batch into per-signal tracking buckets.

    An observation joins a bucket when it is ``observed`` and matches one of
    the requested signal predicates; ``active`` means the post window is
    still open (``pending``), ``labeled`` means both windows are observable.
    Labelled observations matching no requested signal are counted but never
    journaled.
    """

    status_counts: dict[str, int] = {}
    buckets = {key: SignalBucket() for key in signals}
    active_acc: dict[str, list] = {key: [] for key in signals}
    labeled_acc: dict[str, list] = {key: [] for key in signals}
    unattributed = 0
    requested = set(signals)
    for obs in batch.observations:
        status_counts[obs.observation_status] = (
            status_counts.get(obs.observation_status, 0) + 1
        )
        if obs.observation_status != "observed":
            continue
        keys = [k for k in observation_signal_keys(obs) if k in requested]
        if obs.post_label_state == "labeled":
            if keys:
                for key in keys:
                    labeled_acc[key].append(obs)
            else:
                unattributed += 1
        elif obs.post_label_state == "pending" and keys:
            for key in keys:
                active_acc[key].append(obs)
    for key in signals:
        buckets[key] = SignalBucket(
            active=tuple(active_acc[key]), labeled=tuple(labeled_acc[key])
        )
    return TrackerView(
        batch=batch,
        signals=signals,
        status_counts=status_counts,
        buckets=buckets,
        unattributed_labeled=unattributed,
    )


def load_written_event_ids(journal: Any, ledger_path: Path) -> set[str]:
    """Union of event ids already journaled or ledgered for tracked signals.

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
    batch: CatalystShadowBatch, signals: tuple[str, ...]
) -> tuple[dict, ...]:
    """Journal records restricted to observations matching requested signals.

    Filtering joins back through the observation receipts because the
    journal bridge does not carry ``impact_direction``.
    """

    requested = set(signals)
    keep_ids = {
        f"catalyst:{obs.observation_sha256[:32]}"
        for obs in batch.observations
        if any(k in requested for k in observation_signal_keys(obs))
    }
    return tuple(
        record
        for record in journal_records_from_shadow_batch(batch)
        if record.get("journal_event_id") in keep_ids
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


class _NullJournal:
    """Dry-run stand-in that reports an empty written set and appends nothing."""

    def read_events(self) -> list[dict]:
        return []

    def append_samples(self, samples):  # pragma: no cover - defensive
        raise TrackerError("dry_run_write_attempted")


def run_tracker(
    cache: Path,
    journal_path: Path,
    *,
    since: date,
    as_of: datetime,
    signals: tuple[str, ...] = (LOCKUP_SIGNAL,),
    dry_run: bool = False,
) -> TrackerView:
    """One full tracker pass; returns the view for reporting."""

    _fields, sym_rows = _read_csv(cache, "sample_symbols")
    samples = {r["ts_code"] for r in sym_rows}
    entries: list = []
    skipped = 0
    if LOCKUP_SIGNAL in signals:
        lockup_entries, lockup_skipped = load_lockup_entries(cache, samples, since)
        entries.extend(lockup_entries)
        skipped += lockup_skipped
    if EARNINGS_POS_SIGNAL in signals or EARNINGS_NEG_SIGNAL in signals:
        disc_entries, disc_skipped = load_disclosure_entries(cache, samples, since)
        entries.extend(disc_entries)
        skipped += disc_skipped
    bars_by_symbol = load_bars(cache, samples)
    batch = build_catalyst_shadow_batch(
        entries,
        bars_by_symbol,
        as_of=as_of,
        pre_window_sessions=PRE_WINDOW_SESSIONS,
        post_window_sessions=POST_WINDOW_SESSIONS,
        positioning_profile=POSITIONING_PROFILE_MOMENTUM_EVIDENCE_V1,
    )
    view = build_tracker_view(batch, signals)

    from shared.review.sample_journal import SampleJournal

    journal = SampleJournal(journal_path) if not dry_run else _NullJournal()
    records = signal_journal_records(batch, signals)
    # The ledger lives beside the journal so both share one lifecycle; the
    # /tmp research cache is rebuildable and must not be the only record.
    appended = [] if dry_run else append_new_outcomes(
        journal, journal_path.parent / LEDGER_FILENAME, records
    )
    view.appended_records.extend(appended)
    return view


def render_report(
    view: TrackerView,
    *,
    since: date,
    as_of: datetime,
    dry_run: bool,
) -> str:
    titles = {
        LOCKUP_SIGNAL: "Lockup oversold-rebound (sell_off into expiry)",
        EARNINGS_POS_SIGNAL: (
            "Disclosure positive drift (prior 预增-class forecast; "
            "post window is the hold-past-disclosure control)"
        ),
        EARNINGS_NEG_SIGNAL: (
            "Disclosure negative relief (prior 预减-class forecast; "
            "post window measures the relief window)"
        ),
    }
    lines = [
        "# Event signal tracker (research_only)",
        "",
        f"- as_of: {as_of.isoformat()}",
        f"- since: {since.isoformat()} (events on/after this date)",
        f"- signals: {', '.join(view.signals)}",
        "- research_only / not_promotion_evidence; no capital authority;",
        "  classifications and labels come from the production shadow factor",
        "  under the momentum_evidence_v1 positioning profile.",
        "",
    ]
    for key in view.signals:
        bucket = view.buckets.get(key, SignalBucket())
        lines += [f"## {titles.get(key, key)}", ""]
        if bucket.active:
            lines.append("| event_id | symbol | event_date | pre_return | intensity |")
            lines.append("|---|---|---|---|---|")
            for obs in sorted(
                bucket.active, key=lambda o: (o.scheduled_date, o.symbol or "")
            ):
                pre_pct = f"{float(obs.pre_return) * 100:.2f}%"
                lines.append(
                    f"| {obs.event_id} | {obs.symbol} | {obs.scheduled_date} "
                    f"| {pre_pct} | {obs.anticipation_intensity} |"
                )
        else:
            lines.append("Currently tracked (post window open): (none)")
        stats = describe_post_returns(bucket.labeled)
        summary = f"Labelled outcomes this pass: n={stats.get('n', 0)}"
        if stats.get("n"):
            summary += (
                f", mean={stats['mean_bps']}bps, median={stats['median_bps']}bps"
                f", win_rate={stats['win_rate']}"
            )
        lines += ["", summary, ""]
    lines += ["## Journal writes", ""]
    if view.appended_records:
        lines.append(
            "- newly journaled to SampleJournal (shadow_research layer): "
            f"{len(view.appended_records)}"
        )
    elif dry_run:
        lines.append("- dry run: nothing written to the journal or ledger")
    else:
        lines.append(
            "- no new outcomes to journal (all already frozen by "
            "ledger/journal read-back)"
        )
    lines += [
        f"- labelled non-signal observations skipped: {view.unattributed_labeled}",
        "",
        "## Observation status counts",
        "",
    ]
    for status, count in sorted(view.status_counts.items()):
        lines.append(f"- {status}: {count}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    def _arg_value(flag: str) -> str:
        return sys.argv[sys.argv.index(flag) + 1]

    cache = (
        Path(_arg_value("--cache"))
        if "--cache" in sys.argv
        else Path("/tmp/ashare_event_research")
    )
    journal_path = (
        Path(_arg_value("--journal"))
        if "--journal" in sys.argv
        else DEFAULT_SAMPLE_JOURNAL_PATH
    )
    since_raw = _arg_value("--since") if "--since" in sys.argv else DEFAULT_SINCE
    signals_raw = _arg_value("--signals") if "--signals" in sys.argv else LOCKUP_SIGNAL
    dry_run = "--dry-run" in sys.argv
    as_of = datetime.now(timezone.utc)
    signals = parse_signals(signals_raw)

    view = run_tracker(
        cache,
        journal_path,
        since=_parse_day(since_raw),
        as_of=as_of,
        signals=signals,
        dry_run=dry_run,
    )
    print(render_report(view, since=_parse_day(since_raw), as_of=as_of, dry_run=dry_run))
    state_path = cache / "signal_tracker_state.json"
    state_path.write_text(
        json.dumps(
            {
                "research_only": True,
                "batch_receipt_sha256": view.batch.batch_receipt_sha256,
                "signals": list(view.signals),
                "status_counts": view.status_counts,
                "active": {k: len(b.active) for k, b in view.buckets.items()},
                "labeled": {k: len(b.labeled) for k, b in view.buckets.items()},
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
