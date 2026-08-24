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
  ``shadow_research`` bridge (excluded from trading-layer KPIs by policy),
* every tracked event is tagged with the market regime (SSE index return
  over the 10 sessions ending at the last session on/before the event
  day, weak/sideways/strong) so the 2026-08-23 stratification finding —
  the lockup repair concentrates in weak markets — can be validated on
  rolling data; lockup events additionally carry their float-ratio bucket
  (same bins as the stratification study),
* the lockup section previews the practice rule distilled from that
  stratification research ("enter in weak markets, avoid the 3-5%
  float-ratio band"): a report-only subset readout with net-of-cost
  columns against the pre-registered evaluation basis — formal keep/fail
  judgements remain bound to the milestone counts in the criteria doc,
* for ``earnings_pos`` the report also shows the *pre-disclosure trade
  window* the study actually claims (entry at the close of the first
  session after the forecast announcement, exit at the disclosure-day
  close).  This is a report-only readout recomputed each pass — it is not
  journaled; the journaled labelled outcome remains the
  hold-past-disclosure control.

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

import bisect
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
from Ashare.event_calendar_lockup_strata import (  # noqa: E402
    COST_BPS_ROUNDTRIP_DEFAULT,
    REGIME_BINS,
    bucket_by_ratio,
    load_index_series,
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


def make_regime_lookup(index_pairs: list[tuple[date, float]]):
    """Regime over the 10 sessions ending at the last session ON/BEFORE day.

    Appointment dates occasionally fall on non-trading days (weekends,
    holidays); the factor resolves their windows to surrounding sessions,
    and the market state actually known when the window opened is the one
    at the last completed session.  Falls back to ``unknown`` before 10
    sessions of index history exist.
    """

    days = [d for d, _ in index_pairs]

    def lookup(day: date) -> str:
        pos = bisect.bisect_right(days, day) - 1
        if pos < 10:
            return "unknown"
        ret = index_pairs[pos][1] / index_pairs[pos - 10][1] - 1.0
        for low, high, label in REGIME_BINS:
            if low <= ret < high:
                return label
        return "unknown"

    return lookup


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
) -> tuple[list, int, dict[str, str]]:
    """Mint lockup-expiry catalyst entries on/after ``since`` via the adapter.

    Returns the validated entries, the count of real rows the adapter
    failed closed on (blank or non-positive share counts), and a mapping of
    entry event id -> float-ratio bucket label (stratification bins) so the
    report can tag each tracked signal with how much of the float unlocks.
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
    ratio_by_event: dict[str, str] = {}
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
        try:
            ratio = float(raw_row.get("float_ratio"))
        except (TypeError, ValueError):
            ratio = None
        ratio_by_event[entry.event_id] = bucket_by_ratio(ratio)
    return entries, skipped, ratio_by_event


def load_disclosure_entries(
    cache: Path, samples: set[str], since: date
) -> tuple[list, int, dict[str, date]]:
    """Mint disclosure entries whose prior forecast has a usable direction.

    The forecast direction (from the study cache's ``forecast.csv``, earliest
    announcement before the disclosure) maps onto ``impact_direction`` so the
    journal carries both axes for every tracked event.  One entry per
    (symbol, appointment date); disclosures with no prior forecast, an
    uncertain one, or none before the disclosure are not tracked — the study
    found no structure there.

    Also returns a mapping of final entry event id -> forecast announcement
    date so the report can measure the pre-disclosure trade window.
    """

    forecasts = load_forecast_directions(cache)
    _fields, rows = _read_csv(cache, "disclosure")
    doc_entries: list[dict] = []
    ann_by_key: dict[tuple[str, str], date] = {}
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
        ann_by_key[(code, f"{appointment[:4]}-{appointment[4:6]}-{appointment[6:8]}")] = _parse_day(
            prior[0]
        )
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
        entries = list(catalyst_entries_from_calendar_document(document))
    except EventCatalystAdapterError as exc:
        raise TrackerError(f"disclosure_adapter_failed:{exc.reason_code}") from exc
    # The calendar path prefixes event ids; re-join via (symbol, date).
    ann_by_event = {
        entry.event_id: ann_by_key[(entry.symbol or "", entry.scheduled_date.isoformat())]
        for entry in entries
        if (entry.symbol or "", entry.scheduled_date.isoformat()) in ann_by_key
    }
    return entries, skipped, ann_by_event


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
    # ISO scheduled_date -> weak/sideways/strong/unknown (SSE 10-session
    # return ending at the last session on/before the event day).
    regime_by_date: dict[str, str] = field(default_factory=dict)
    # entry event_id -> float-ratio bucket label (lockup events only).
    ratio_by_event: dict[str, str] = field(default_factory=dict)
    # entry event_id -> pre-event order-flow absorption bucket (moneyflow
    # side table, report-only like ratio_by_event; empty when the moneyflow
    # cache is unavailable, e.g. CI).
    absorption_by_event: dict[str, str] = field(default_factory=dict)
    # entry event_id -> pre-event block-trade bucket (blocktrade side table,
    # report-only; empty when the blocktrade/daily caches are unavailable).
    blocks_by_event: dict[str, str] = field(default_factory=dict)
    # entry event_id -> pre-event turnover bucket (daily_basic side table,
    # report-only; empty when the dailybasic/daily caches are unavailable).
    turnover_by_event: dict[str, str] = field(default_factory=dict)
    # signal key -> pre-disclosure trade-window stats (report-only readout,
    # computed for earnings_pos; never journaled).
    prewindow_stats: dict[str, dict] = field(default_factory=dict)
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
    ratio_by_event: dict[str, str] = {}
    ann_by_event: dict[str, date] = {}
    skipped = 0
    if LOCKUP_SIGNAL in signals:
        lockup_entries, lockup_skipped, ratio_by_event = load_lockup_entries(
            cache, samples, since
        )
        entries.extend(lockup_entries)
        skipped += lockup_skipped
    if EARNINGS_POS_SIGNAL in signals or EARNINGS_NEG_SIGNAL in signals:
        disc_entries, disc_skipped, ann_by_event = load_disclosure_entries(
            cache, samples, since
        )
        entries.extend(disc_entries)
        skipped += disc_skipped
    bars_by_symbol = load_bars(cache, samples)
    regime_of = make_regime_lookup(load_index_series(cache))
    batch = build_catalyst_shadow_batch(
        entries,
        bars_by_symbol,
        as_of=as_of,
        pre_window_sessions=PRE_WINDOW_SESSIONS,
        post_window_sessions=POST_WINDOW_SESSIONS,
        positioning_profile=POSITIONING_PROFILE_MOMENTUM_EVIDENCE_V1,
    )
    view = build_tracker_view(batch, signals)
    view.regime_by_date = {
        obs.scheduled_date.isoformat(): regime_of(obs.scheduled_date)
        for obs in batch.observations
        if obs.observation_status == "observed"
    }
    view.ratio_by_event = ratio_by_event
    if LOCKUP_SIGNAL in signals:
        view.absorption_by_event = _absorption_labels_for_observations(
            cache, batch.observations
        )
        view.blocks_by_event = _block_labels_for_observations(
            cache, batch.observations
        )
        view.turnover_by_event = _turnover_labels_for_observations(
            cache, batch.observations
        )
    if EARNINGS_POS_SIGNAL in signals:
        view.prewindow_stats[EARNINGS_POS_SIGNAL] = prewindow_breakdown(
            view.buckets[EARNINGS_POS_SIGNAL].labeled, ann_by_event, bars_by_symbol
        )

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


def _post_return_stats(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {"n": 0}
    return {
        "n": n,
        "mean_bps": round(statistics.fmean(values) * 1e4, 1),
        "median_bps": round(statistics.median(values) * 1e4, 1),
        "win_rate": round(sum(1 for v in values if v > 0) / n, 3),
    }


def describe_post_returns(observations: tuple[CatalystShadowObservation, ...]) -> dict:
    return _post_return_stats(
        [float(o.post_return) for o in observations if o.post_return is not None]
    )


def regime_breakdown(
    observations: tuple[CatalystShadowObservation, ...],
    regime_by_date: dict[str, str],
) -> dict[str, dict]:
    """Labelled-outcome stats grouped by pre-observable market regime."""

    groups: dict[str, list[float]] = {}
    for obs in observations:
        regime = regime_by_date.get(obs.scheduled_date.isoformat(), "unknown")
        groups.setdefault(regime, []).append(float(obs.post_return))
    return {regime: _post_return_stats(v) for regime, v in sorted(groups.items())}


RATIO_LABEL_ORDER = ("<1%", "1-3%", "3-5%", ">=5%", "unknown")


def ratio_breakdown(
    observations: tuple[CatalystShadowObservation, ...],
    ratio_by_event: dict[str, str],
) -> dict[str, dict]:
    """Labelled-outcome stats grouped by float-ratio bucket (lockup events).

    Observations without a ratio tag (e.g. disclosure events) are skipped:
    the float-ratio dimension only exists for lockups.
    """

    groups: dict[str, list[float]] = {}
    for obs in observations:
        label = ratio_by_event.get(obs.event_id)
        if label is None:
            continue
        groups.setdefault(label, []).append(float(obs.post_return))
    return {
        label: _post_return_stats(groups[label])
        for label in RATIO_LABEL_ORDER
        if label in groups
    }


ABSORPTION_LABEL_ORDER = ("outflow", "balanced", "inflow")


def absorption_breakdown(
    observations: tuple[CatalystShadowObservation, ...],
    absorption_by_event: dict[str, str],
) -> dict[str, dict]:
    """Labelled-outcome stats grouped by pre-event absorption bucket.

    Mirrors :func:`ratio_breakdown`: observations without a moneyflow-derived
    label (dataset unavailable, insufficient history) are skipped, and an
    empty label table yields an empty breakdown.  Report-only readout for
    rolling validation of the #433 watchlist candidate — never journaled.
    """

    groups: dict[str, list[float]] = {}
    for obs in observations:
        label = absorption_by_event.get(obs.event_id)
        if label is None:
            continue
        groups.setdefault(label, []).append(float(obs.post_return))
    return {
        label: _post_return_stats(groups[label])
        for label in ABSORPTION_LABEL_ORDER
        if label in groups
    }


def _absorption_labels_for_observations(
    cache: Path,
    observations: tuple[CatalystShadowObservation, ...],
) -> dict[str, str]:
    """Map event_id -> absorption bucket via the moneyflow cache.

    Labelling is a report-only decoration on the tracking pass: any failure
    to reach or read the moneyflow dataset (missing cache dir, unreadable
    rows) degrades to "no labels" instead of breaking tracking — CI runs
    without the dataset by design.
    """

    try:
        from Ashare.event_moneyflow_absorption_study import (
            absorption_buckets_for_events,
        )
    except ImportError:
        return {}
    pairs = [
        (obs.symbol, obs.scheduled_date.strftime("%Y%m%d"))
        for obs in observations
        if obs.observation_status == "observed" and obs.symbol
    ]
    if not pairs:
        return {}
    try:
        buckets = absorption_buckets_for_events(cache, pairs)
    except Exception:
        return {}
    labels: dict[str, str] = {}
    for obs in observations:
        if obs.observation_status != "observed" or not obs.symbol:
            continue
        bucket = buckets.get((obs.symbol, obs.scheduled_date.strftime("%Y%m%d")))
        if bucket is not None:
            labels[obs.event_id] = bucket
    return labels


BLOCK_LABEL_ORDER = ("none", "discount_deep", "near_flat")


def block_breakdown(
    observations: tuple[CatalystShadowObservation, ...],
    blocks_by_event: dict[str, str],
) -> dict[str, dict]:
    """Labelled-outcome stats grouped by pre-event block-trade bucket.

    Mirrors :func:`absorption_breakdown`: observations without a label
    (blocktrade/daily cache unavailable, insufficient history) are skipped,
    and an empty label table yields an empty breakdown.  Report-only
    readout for rolling validation of the #436 inverted gradient — never
    journaled.
    """

    groups: dict[str, list[float]] = {}
    for obs in observations:
        label = blocks_by_event.get(obs.event_id)
        if label is None:
            continue
        groups.setdefault(label, []).append(float(obs.post_return))
    return {
        label: _post_return_stats(groups[label])
        for label in BLOCK_LABEL_ORDER
        if label in groups
    }


def _block_labels_for_observations(
    cache: Path,
    observations: tuple[CatalystShadowObservation, ...],
) -> dict[str, str]:
    """Map event_id -> block-trade bucket via the blocktrade + daily caches.

    Report-only decoration on the tracking pass: any failure to reach the
    datasets degrades to "no labels" instead of breaking tracking.
    """

    try:
        from Ashare.event_blocktrade_prelockup_study import (
            block_buckets_for_events,
        )
    except ImportError:
        return {}
    pairs = [
        (obs.symbol, obs.scheduled_date.strftime("%Y%m%d"))
        for obs in observations
        if obs.observation_status == "observed" and obs.symbol
    ]
    if not pairs:
        return {}
    try:
        buckets = block_buckets_for_events(cache, pairs)
    except Exception:
        return {}
    labels: dict[str, str] = {}
    for obs in observations:
        if obs.observation_status != "observed" or not obs.symbol:
            continue
        bucket = buckets.get((obs.symbol, obs.scheduled_date.strftime("%Y%m%d")))
        if bucket is not None:
            labels[obs.event_id] = bucket
    return labels


TURNOVER_LABEL_ORDER = ("shrink", "normal", "surge")


def turnover_breakdown(
    observations: tuple[CatalystShadowObservation, ...],
    turnover_by_event: dict[str, str],
) -> dict[str, dict]:
    """Labelled-outcome stats grouped by pre-event turnover bucket.

    Mirrors :func:`block_breakdown`: observations without a label
    (dailybasic cache unavailable, insufficient strictly-prior history,
    flat baseline) are skipped, and an empty label table yields an empty
    breakdown.  Report-only readout for rolling validation of the #441
    first-read structure — never journaled.
    """

    groups: dict[str, list[float]] = {}
    for obs in observations:
        label = turnover_by_event.get(obs.event_id)
        if label is None:
            continue
        groups.setdefault(label, []).append(float(obs.post_return))
    return {
        label: _post_return_stats(groups[label])
        for label in TURNOVER_LABEL_ORDER
        if label in groups
    }


def _turnover_labels_for_observations(
    cache: Path,
    observations: tuple[CatalystShadowObservation, ...],
) -> dict[str, str]:
    """Map event_id -> turnover bucket via the dailybasic + daily caches.

    Report-only decoration on the tracking pass: any failure to reach the
    datasets degrades to "no labels" instead of breaking tracking.
    """

    try:
        from Ashare.event_turnover_prelockup_study import (
            turnover_buckets_for_events,
        )
    except ImportError:
        return {}
    pairs = [
        (obs.symbol, obs.scheduled_date.strftime("%Y%m%d"))
        for obs in observations
        if obs.observation_status == "observed" and obs.symbol
    ]
    if not pairs:
        return {}
    try:
        buckets = turnover_buckets_for_events(cache, pairs)
    except Exception:
        return {}
    labels: dict[str, str] = {}
    for obs in observations:
        if obs.observation_status != "observed" or not obs.symbol:
            continue
        bucket = buckets.get(
            (obs.symbol, obs.scheduled_date.strftime("%Y%m%d"))
        )
        if bucket is not None:
            labels[obs.event_id] = bucket
    return labels


# Practice rule distilled from the stratification research ("enter in weak
# markets, avoid the 3-5% float-ratio band").  The tracker only previews it
# as a report-only readout against the pre-registered evaluation basis;
# formal keep/fail judgements happen exclusively at the milestone sample
# counts fixed in the criteria document.
RULE_SUBSET_REGIME = "weak"
RULE_SUBSET_EXCLUDED_RATIO_BAND = "3-5%"


def rule_subset_breakdown(
    observations: tuple[CatalystShadowObservation, ...],
    regime_by_date: dict[str, str],
    ratio_by_event: dict[str, str],
) -> dict[str, dict]:
    """Split labelled lockup outcomes into the practice rule vs the rest.

    The subset keeps observations whose regime is weak and whose float
    ratio is tagged outside the avoided band; everything else (other or
    unknown regimes, untagged ratios, the 3-5% band itself) lands in
    ``excluded`` so both sides always sum to the input.  ``*_net`` columns
    deduct one round trip at the default cost model.
    """

    def _stats_with_net(values: list[float]) -> dict:
        stats = _post_return_stats(values)
        if not values:
            return stats
        net = [v - COST_BPS_ROUNDTRIP_DEFAULT / 1e4 for v in values]
        stats.update(
            {
                "mean_net_bps": round(statistics.fmean(net) * 1e4, 1),
                "win_net": round(sum(1 for v in net if v > 0) / len(net), 3),
            }
        )
        return stats

    subset: list[float] = []
    excluded: list[float] = []
    for obs in observations:
        regime = regime_by_date.get(obs.scheduled_date.isoformat(), "unknown")
        label = ratio_by_event.get(obs.event_id)
        ret = float(obs.post_return)
        if (
            regime == RULE_SUBSET_REGIME
            and label is not None
            and label != RULE_SUBSET_EXCLUDED_RATIO_BAND
        ):
            subset.append(ret)
        else:
            excluded.append(ret)
    return {"rule": _stats_with_net(subset), "excluded": _stats_with_net(excluded)}


def prewindow_return(
    bars: list[DailyBar], ann_day: date, target_day: date
) -> float | None:
    """Return over the study's pre-disclosure trade window.

    Entry at the close of the first session strictly AFTER the forecast
    announcement (forecasts are typically published after hours), exit at
    the close of the last session on/before the disclosure appointment.
    ``None`` when either endpoint is not observable in the bar series or
    the window holds no session.
    """

    if not bars:
        return None
    days = [bar.trade_date for bar in bars]
    entry_i = bisect.bisect_right(days, ann_day)
    exit_j = bisect.bisect_right(days, target_day) - 1
    if entry_i >= len(days) or exit_j < entry_i:
        return None
    return bars[exit_j].close / bars[entry_i].close - 1.0


def prewindow_breakdown(
    observations: tuple[CatalystShadowObservation, ...],
    ann_by_event: dict[str, date],
    bars_by_symbol: dict[str, list[DailyBar]],
) -> dict:
    """Aggregate the pre-disclosure trade-window returns for one signal.

    Report-only readout of the studied trade; observations without a known
    forecast announcement date or unobservable endpoints are skipped rather
    than guessed.
    """

    values: list[float] = []
    for obs in observations:
        ann_day = ann_by_event.get(obs.event_id)
        if ann_day is None:
            continue
        ret = prewindow_return(
            bars_by_symbol.get(obs.symbol or "", []), ann_day, obs.scheduled_date
        )
        if ret is not None:
            values.append(ret)
    return _post_return_stats(values)


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
            lines.append(
                "| event_id | symbol | event_date | pre_return | intensity"
                " | regime | float_ratio |"
            )
            lines.append("|---|---|---|---|---|---|---|")
            for obs in sorted(
                bucket.active, key=lambda o: (o.scheduled_date, o.symbol or "")
            ):
                pre_pct = f"{float(obs.pre_return) * 100:.2f}%"
                regime = view.regime_by_date.get(
                    obs.scheduled_date.isoformat(), "unknown"
                )
                ratio = view.ratio_by_event.get(obs.event_id, "-")
                lines.append(
                    f"| {obs.event_id} | {obs.symbol} | {obs.scheduled_date} "
                    f"| {pre_pct} | {obs.anticipation_intensity} | {regime} "
                    f"| {ratio} |"
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
        pre = view.prewindow_stats.get(key)
        if pre and pre.get("n"):
            lines += [
                "Pre-disclosure trade window (entry: first session close after"
                " the forecast announcement; exit: disclosure-day close;"
                " report-only, not journaled):",
                "",
                f"- n={pre['n']}, mean={pre['mean_bps']}bps,"
                f" median={pre['median_bps']}bps, win_rate={pre['win_rate']}",
                "",
            ]
        breakdown = regime_breakdown(bucket.labeled, view.regime_by_date)
        if any(item.get("n") for item in breakdown.values()):
            parts = []
            for regime, item in breakdown.items():
                if not item.get("n"):
                    continue
                parts.append(
                    f"{regime}: n={item['n']}, mean={item['mean_bps']}bps,"
                    f" win_rate={item['win_rate']}"
                )
            lines += ["Labelled outcomes by market regime:", ""] + [
                f"- {part}" for part in parts
            ] + [""]
        ratio_stats = ratio_breakdown(bucket.labeled, view.ratio_by_event)
        if any(item.get("n") for item in ratio_stats.values()):
            parts = []
            for label, item in ratio_stats.items():
                if not item.get("n"):
                    continue
                parts.append(
                    f"{label}: n={item['n']}, mean={item['mean_bps']}bps,"
                    f" win_rate={item['win_rate']}"
                )
            lines += ["Labelled outcomes by float-ratio bucket:", ""] + [
                f"- {part}" for part in parts
            ] + [""]
        absorption_stats = absorption_breakdown(
            bucket.labeled, view.absorption_by_event
        )
        if any(item.get("n") for item in absorption_stats.values()):
            parts = []
            for label, item in absorption_stats.items():
                if not item.get("n"):
                    continue
                parts.append(
                    f"{label}: n={item['n']}, mean={item['mean_bps']}bps,"
                    f" win_rate={item['win_rate']}"
                )
            lines += ["Labelled outcomes by absorption bucket:", ""] + [
                f"- {part}" for part in parts
            ] + [""]
        block_stats = block_breakdown(bucket.labeled, view.blocks_by_event)
        if any(item.get("n") for item in block_stats.values()):
            parts = []
            for label, item in block_stats.items():
                if not item.get("n"):
                    continue
                parts.append(
                    f"{label}: n={item['n']}, mean={item['mean_bps']}bps,"
                    f" win_rate={item['win_rate']}"
                )
            lines += ["Labelled outcomes by pre-event block bucket:", ""] + [
                f"- {part}" for part in parts
            ] + [""]
        turnover_stats = turnover_breakdown(
            bucket.labeled, view.turnover_by_event
        )
        if any(item.get("n") for item in turnover_stats.values()):
            parts = []
            for label, item in turnover_stats.items():
                if not item.get("n"):
                    continue
                parts.append(
                    f"{label}: n={item['n']}, mean={item['mean_bps']}bps,"
                    f" win_rate={item['win_rate']}"
                )
            lines += ["Labelled outcomes by pre-event turnover bucket:",
                      ""] + [
                f"- {part}" for part in parts
            ] + [""]
        if key == LOCKUP_SIGNAL:
            rule = rule_subset_breakdown(
                bucket.labeled, view.regime_by_date, view.ratio_by_event
            )
            subset = rule["rule"]
            if subset.get("n"):
                excluded = rule["excluded"]
                lines += [
                    "Practice-rule subset (weak regime AND float-ratio outside"
                    " the avoided 3-5% band; report-only preview of the"
                    " pre-registered weak-market evaluation - formal judgement"
                    " only at milestone counts, criteria doc):",
                    "",
                    (
                        f"- rule_subset: n={subset['n']}, mean={subset['mean_bps']}bps,"
                        f" mean_net={subset['mean_net_bps']}bps,"
                        f" win_net={subset['win_net']}"
                    ),
                    (
                        "- excluded: "
                        + (
                            f"n={excluded['n']}, mean_net={excluded['mean_net_bps']}bps,"
                            f" win_net={excluded['win_net']}"
                            if excluded.get("n")
                            else "n=0"
                        )
                    ),
                    "",
                ]
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
                "labeled_by_regime": {
                    k: {
                        regime: item["n"]
                        for regime, item in regime_breakdown(
                            b.labeled, view.regime_by_date
                        ).items()
                        if item.get("n")
                    }
                    for k, b in view.buckets.items()
                },
                "labeled_by_ratio": {
                    k: {
                        label: item["n"]
                        for label, item in ratio_breakdown(
                            b.labeled, view.ratio_by_event
                        ).items()
                        if item.get("n")
                    }
                    for k, b in view.buckets.items()
                },
                "labeled_by_absorption": {
                    k: {
                        label: item["n"]
                        for label, item in absorption_breakdown(
                            b.labeled, view.absorption_by_event
                        ).items()
                        if item.get("n")
                    }
                    for k, b in view.buckets.items()
                },
                "labeled_by_block": {
                    k: {
                        label: item["n"]
                        for label, item in block_breakdown(
                            b.labeled, view.blocks_by_event
                        ).items()
                        if item.get("n")
                    }
                    for k, b in view.buckets.items()
                },
                "labeled_by_turnover": {
                    k: {
                        label: item["n"]
                        for label, item in turnover_breakdown(
                            b.labeled, view.turnover_by_event
                        ).items()
                        if item.get("n")
                    }
                    for k, b in view.buckets.items()
                },
                "rule_subset_lockup": (
                    rule_subset_breakdown(
                        view.buckets[LOCKUP_SIGNAL].labeled,
                        view.regime_by_date,
                        view.ratio_by_event,
                    )
                    if LOCKUP_SIGNAL in view.signals
                    else {}
                ),
                "prewindow": {
                    k: v
                    for k, v in view.prewindow_stats.items()
                    if v.get("n")
                },
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
