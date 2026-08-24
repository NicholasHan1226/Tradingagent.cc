"""Daily lockup-event auxiliary evidence for the delayed-paper event sleeve.

Pre-registered shadow trial wiring: A-share mainboard symbols whose
share-float unlock is anchored inside the trailing 30 natural days receive an
auxiliary ``normalized_score=+1.0`` consumed only by the ``event`` sleeve
(``score = raw_rank_score + 0.25 * normalized_score``, already implemented in
the closed loop). The baseline/dynamic_position sleeves stay bit-for-bit
unchanged. Any feed failure degrades to the current production behaviour:
the event sleeve abstains because no matching evidence exists.

Research-tier parity (locked baseline ``load_events`` semantics): events
anchor on ``float_date`` (never ``ann_date``); inverted rows
(``float_date < ann_date``) are skipped; the window is
``[session_date - 30 natural days, session_date)``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from Ashare.minute_loop import (
    MinuteAuxiliaryEvidence,
    _canonical_sha256,
)
from shared.data.sharedsignals_v1 import (
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)
from shared.data.tradingdatas_pagination import collect_query_pages
from shared.universe.policy import is_mainboard_tradable

EVENT_AUX_DATASET_ID = "cn.dataset.share_float"
LOCKUP_LOOKBACK_DAYS = 30
# Server-side pre-filter width over the registry partition column. Lockup
# announcements lead their float dates by weeks-to-months, so the net must
# reach back well beyond the 30-day hit window; the local reducer still owns
# the exact frozen semantics.
LOCKUP_ANN_NET_DAYS = 120
LOCKUP_SCORE = 1.0
EVIDENCE_TYPE = "event"
AUX_EXPIRES_WINDOW = timedelta(minutes=10)
HITS_FILENAME = "event-lockup-hits.json"
_MAX_PAGES = 8
_MAX_ROWS = 20_000
_CST = ZoneInfo("Asia/Shanghai")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MinuteEventAuxError(RuntimeError):
    """Raised when the daily lockup auxiliary feed cannot be produced."""


@dataclass(frozen=True)
class LockupHitBatch:
    """Hits together with their immutable TD query provenance."""

    hits: dict[str, dict[str, Any]]
    source_binding: dict[str, Any]


def _session_date(raw: date | str) -> date:
    if isinstance(raw, datetime):
        raise MinuteEventAuxError("minute_event_aux_session_date_invalid")
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise MinuteEventAuxError("minute_event_aux_session_date_invalid") from exc


def hits_from_rows(
    rows: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
    *,
    session_date: date | str,
) -> dict[str, dict[str, Any]]:
    """Reduce share_float rows to per-symbol lockup hits (pure function).

    Score semantics stay frozen at ``+1.0`` regardless of ratio; the maximum
    observed ratio is retained only as audit metadata.
    """

    session = _session_date(session_date)
    # Row dates are YYYYMMDD; compare against the same compact format so
    # lexicographic order equals chronological order.
    window_start = (session - timedelta(days=LOCKUP_LOOKBACK_DAYS)).strftime("%Y%m%d")
    window_end = session.strftime("%Y%m%d")
    hits: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("ts_code") or "")
        float_day = str(row.get("float_date") or "")
        if len(symbol) == 0 or len(float_day) != 8:
            continue
        if not (window_start <= float_day < window_end):
            continue
        ann_day = str(row.get("ann_date") or "")
        if len(ann_day) == 8 and float_day < ann_day:
            continue
        try:
            ratio = float(row.get("float_ratio"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            ratio = None
        prior = hits.get(symbol)
        candidate = {
            "latest_float_date": float_day,
            "max_ratio": None if ratio is None else round(ratio, 6),
        }
        if prior is None:
            hits[symbol] = candidate
            continue
        if float_day > prior["latest_float_date"]:
            prior["latest_float_date"] = float_day
        prior_max = prior["max_ratio"]
        if ratio is not None and (prior_max is None or ratio > prior_max):
            prior["max_ratio"] = candidate["max_ratio"]
    return hits


def _validated_source_binding(raw: Mapping[str, Any]) -> dict[str, Any]:
    required_text = (
        "receipt_id", "data_through", "observed_at", "catalog_version",
        "pagination_trace_sha256", "semantic_sha256", "ordered_rows_sha256",
        "row_receipt_proofs_sha256", "source_binding_sha256",
    )
    if any(not isinstance(raw.get(key), str) or not raw[key].strip() for key in required_text):
        raise MinuteEventAuxError("minute_event_aux_receipt_binding_invalid")
    if not all(_SHA256.fullmatch(str(raw[key])) for key in required_text[4:]):
        raise MinuteEventAuxError("minute_event_aux_receipt_binding_invalid")
    lineage = raw.get("lineage")
    if not isinstance(lineage, Mapping) or not lineage:
        raise MinuteEventAuxError("minute_event_aux_receipt_binding_invalid")
    try:
        observed_at = datetime.fromisoformat(str(raw["observed_at"]))
    except ValueError as exc:
        raise MinuteEventAuxError("minute_event_aux_receipt_binding_invalid") from exc
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise MinuteEventAuxError("minute_event_aux_receipt_binding_invalid")
    unsigned = {key: raw[key] for key in required_text[:-1]}
    unsigned["lineage"] = dict(lineage)
    if raw["source_binding_sha256"] != _canonical_sha256(unsigned):
        raise MinuteEventAuxError("minute_event_aux_receipt_binding_invalid")
    return {**unsigned, "source_binding_sha256": raw["source_binding_sha256"]}


def _source_binding_from_run(run: Any) -> dict[str, Any]:
    metadata = run.envelope.metadata
    if (
        not isinstance(metadata.receipt_id, str)
        or not metadata.receipt_id
        or not isinstance(metadata.lineage, Mapping)
        or not metadata.lineage
        or not isinstance(metadata.data_through, str)
        or not isinstance(metadata.observed_at, str)
    ):
        raise MinuteEventAuxError("minute_event_aux_receipt_binding_missing")
    unsigned = {
        "receipt_id": metadata.receipt_id,
        "data_through": metadata.data_through,
        "observed_at": metadata.observed_at,
        "catalog_version": run.envelope.catalog_version,
        "lineage": dict(metadata.lineage),
        "pagination_trace_sha256": run.pagination_trace_sha256,
        "semantic_sha256": run.semantic_sha256,
        "ordered_rows_sha256": run.ordered_rows_sha256,
        "row_receipt_proofs_sha256": _canonical_sha256(
            list(metadata.row_receipt_proofs)
        ),
    }
    return _validated_source_binding(
        {**unsigned, "source_binding_sha256": _canonical_sha256(unsigned)}
    )


def fetch_lockup_hits(
    client: SharedSignalsV1Client, *, session_date: date | str
) -> LockupHitBatch:
    """Query the TradingDatas V1 plane once for this session's lockup hits."""

    if not isinstance(client, SharedSignalsV1Client):
        raise MinuteEventAuxError("minute_event_aux_client_invalid")
    session = _session_date(session_date)
    try:
        catalog = client.get_catalog()
    except Exception as exc:  # any feed fault must degrade, never fail the bar
        raise MinuteEventAuxError(f"minute_event_aux_catalog_failed:{exc}") from exc
    entry = next(
        (
            item
            # Wire reality (2026-08-24 rehearsal): envelope ``data`` is a
            # property, not a method.
            for item in catalog.data
            if isinstance(item, Mapping) and item.get("dataset_id") == EVENT_AUX_DATASET_ID
        ),
        None,
    )
    if entry is None:
        raise MinuteEventAuxError("minute_event_aux_dataset_missing")
    try:
        schema_major = int(entry["schema_major"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MinuteEventAuxError("minute_event_aux_schema_unknown") from exc
    # Wire realities rehearsed against production (2026-08-24):
    #
    # * the engine rejects gte/lte with HTTP 400 despite the catalog
    #   advertising them — `between` over compact YYYYMMDD strings is the
    #   supported range shape;
    # * filtering on ``float_date`` is unreachable on this plane: even a
    #   full-year float_date net returns zero rows while ``ann_date`` nets
    #   return data (the registry partitions this dataset by ``ann_date``).
    #   The server-side net therefore rides the partition column and
    #   hits_from_rows applies the exact frozen [T-30, T) float_date
    #   boundary locally; a missed row can only remove evidence
    #   (fail-closed), never fabricate one;
    # * several holders legitimately share one (symbol, ann_date,
    #   float_date), so identity is the dataset-declared five-tuple — the
    #   three-tuple makes collect_query_pages reject legal rows.
    request = QueryRequest(
        dataset_id=EVENT_AUX_DATASET_ID,
        schema_major=schema_major,
        fields=[
            "ts_code",
            "ann_date",
            "float_date",
            "float_ratio",
            "holder_name",
            "share_type",
        ],
        filters={
            "ann_date": {
                "between": [
                    (session - timedelta(days=LOCKUP_ANN_NET_DAYS)).strftime("%Y%m%d"),
                    session.strftime("%Y%m%d"),
                ]
            }
        },
        limit=500,
        include_receipt_proofs=True,
    )
    try:
        run = collect_query_pages(
            client=client,
            request=request,
            identity_fields=(
                "ann_date",
                "float_date",
                "ts_code",
                "holder_name",
                "share_type",
            ),
            max_pages=_MAX_PAGES,
            max_rows=_MAX_ROWS,
        )
    except Exception as exc:  # pagination contract / transport / HTTP failures
        raise MinuteEventAuxError(f"minute_event_aux_query_failed:{exc}") from exc
    return LockupHitBatch(
        hits=hits_from_rows(tuple(run.envelope.data), session_date=session),
        source_binding=_source_binding_from_run(run),
    )


def build_event_evidence(
    hits: Mapping[str, Mapping[str, Any]],
    *,
    decision_time: datetime,
    available_at: datetime,
    source_binding_sha256: str,
) -> tuple[MinuteAuxiliaryEvidence, ...]:
    """Materialize one fresh event evidence per hit symbol for this bar."""

    if not _SHA256.fullmatch(source_binding_sha256):
        raise MinuteEventAuxError("minute_event_aux_receipt_binding_invalid")
    if (
        available_at.tzinfo is None
        or available_at.utcoffset() is None
        or available_at > decision_time
    ):
        raise MinuteEventAuxError("minute_event_aux_availability_invalid")
    expires_at = decision_time + AUX_EXPIRES_WINDOW
    evidence: list[MinuteAuxiliaryEvidence] = []
    for symbol in sorted(hits):
        if not is_mainboard_tradable(symbol):
            continue
        info = hits[symbol]
        float_day = str(info.get("latest_float_date") or "")
        if len(float_day) != 8:
            continue
        event_time = datetime.strptime(float_day, "%Y%m%d").replace(tzinfo=_CST)
        evidence.append(
            MinuteAuxiliaryEvidence(
                symbol=symbol,
                evidence_type=EVIDENCE_TYPE,
                normalized_score=LOCKUP_SCORE,
                event_time=event_time,
                available_at=available_at,
                decision_time=decision_time,
                expires_at=expires_at,
                evidence_sha256=_canonical_sha256(
                    {
                        "dataset_id": EVENT_AUX_DATASET_ID,
                        "symbol": symbol,
                        "float_date": float_day,
                        "evidence_type": EVIDENCE_TYPE,
                        "source_binding_sha256": source_binding_sha256,
                    }
                ),
            )
        )
    return tuple(evidence)


def _write_atomic(path: Path, payload: str) -> None:
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".partial",
        delete=False,
    )
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def cached_hits_document(
    day_dir: Path | str, *, session_date: date | str | None = None
) -> dict[str, Any]:
    """Return the full cached daily document (hits + provenance metadata)."""

    target = Path(day_dir) / HITS_FILENAME
    if not target.exists():
        raise MinuteEventAuxError("minute_event_aux_cache_missing")
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MinuteEventAuxError(f"minute_event_aux_cache_corrupt:{exc}") from exc
    if not isinstance(document, Mapping) or not isinstance(document.get("hits"), Mapping):
        raise MinuteEventAuxError("minute_event_aux_cache_shape_invalid")
    if document.get("schema") != "tradingagent.ashare.minute_event_aux_hits.v2":
        raise MinuteEventAuxError("minute_event_aux_cache_shape_invalid")
    if session_date is not None and document.get("session_date") != _session_date(
        session_date
    ).isoformat():
        raise MinuteEventAuxError("minute_event_aux_cache_session_mismatch")
    if not isinstance(document.get("fetched_at"), str) or not isinstance(document.get("hits_sha256"), str):
        raise MinuteEventAuxError("minute_event_aux_cache_shape_invalid")
    if document["hits_sha256"] != _canonical_sha256(dict(document["hits"])):
        raise MinuteEventAuxError("minute_event_aux_cache_tampered")
    source_binding = document.get("source_binding")
    if not isinstance(source_binding, Mapping):
        raise MinuteEventAuxError("minute_event_aux_cache_shape_invalid")
    return {
        **dict(document),
        "hits": dict(document["hits"]),
        "source_binding": _validated_source_binding(source_binding),
    }


def load_or_refresh_daily_hits(
    day_dir: Path | str,
    *,
    session_date: date | str,
    refresh: Mapping[str, Mapping[str, Any]] | None = None,
    source_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read the cached daily hit set, or persist a freshly fetched one.

    ``refresh`` carries the already-fetched mapping into the atomic cache
    write; callers either pass a cached document through unchanged or fetch
    first. Cache faults surface as :class:`MinuteEventAuxError` so runners
    can degrade to the abstain status quo instead of failing the bar.
    """

    directory = Path(day_dir)
    target = directory / HITS_FILENAME
    if target.exists():
        return cached_hits_document(directory, session_date=session_date)
    if refresh is None or source_binding is None:
        raise MinuteEventAuxError("minute_event_aux_cache_missing")
    validated_binding = _validated_source_binding(source_binding)
    hits = {str(symbol): dict(value) for symbol, value in refresh.items()}
    fetched_at = datetime.now(tz=_CST)
    document = {
        "schema": "tradingagent.ashare.minute_event_aux_hits.v2",
        "session_date": _session_date(session_date).isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "lookback_days": LOCKUP_LOOKBACK_DAYS,
        "hit_count": len(hits),
        "hits": hits,
        "hits_sha256": _canonical_sha256(hits),
        "source_binding": validated_binding,
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _write_atomic(target, json.dumps(document, ensure_ascii=False, sort_keys=True))
    except OSError as exc:
        raise MinuteEventAuxError(f"minute_event_aux_cache_write_failed:{exc}") from exc
    return cached_hits_document(directory, session_date=session_date)


def make_session_client(
    *,
    transport_id: str,
    token_file: Path | str,
    base_url: str,
    transport_factory,
    expected_catalog_version: str,
    access_policy_id: str | None,
    timeout_seconds: float,
) -> SharedSignalsV1Client:
    """Build a dedicated single-dataset client for the lockup feed.

    The runner's minute client pins ``dataset_ids`` to the realtime bar
    dataset, so it cannot serve this query; the dedicated client reuses the
    same transport/token injection chain while whitelisting only
    ``cn.dataset.share_float``.
    """

    config = SharedSignalsV1Config(
        base_url=base_url,
        expected_catalog_version=expected_catalog_version,
        dataset_ids=frozenset({EVENT_AUX_DATASET_ID}),
        access_policy_id=access_policy_id,
        catalog_version_policy="evidence_only",
        timeout_seconds=timeout_seconds,
        max_limit=500,
        cache_ttl_seconds=0,
    )
    transport = transport_factory(
        transport_id,
        token_file=token_file,
        base_url=base_url,
    )
    return SharedSignalsV1Client(config, transport=transport)
