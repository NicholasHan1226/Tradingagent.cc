"""Detached read-only realized-spread projection over the observation store.

This is deliberately a research sink, not a runtime participant.  It reads
the append-only ten-symbol observation event chain plus the immutable
spreads sidecars (book-ticker snapshots) and aggregates realized top-of-book
spread statistics per symbol per bounded UTC calendar-day window into one
immutable, checksum-bound research artifact under its own
``evolution/ten_symbol_spread_projection`` namespace.  The artifact is
sample-level cost evidence for a later cost-after evaluation to substitute
realized spreads for assumed costs; this projection never touches any
strategy, factor record, evaluation, core, capital, order, Champion, or
network path.  It mirrors the bars-sidecar consumption precedent: every
terminal slot's event ``spread`` block is compared value-for-value against
the independently re-derived sidecar content before the slot is aggregated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping
import uuid

from Crypto.fixture_sim.contracts import _assert_simulation_only
from Crypto.market_observation import (
    OBSERVATION_SYMBOLS,
    TEN_SYMBOL_SPREAD_CONTRACT,
    TEN_SYMBOL_SPREADS_SIDECAR_CONTRACT,
    CryptoMarketObservationError,
    build_spread_event_block,
    validate_ten_symbol_spreads_sidecar,
)
import Crypto.ten_symbol_factor_research as projection
from Crypto.ten_symbol_observation_store import (
    TEN_SYMBOL_CONTRACTS,
    TERMINAL_SLOT_TYPES,
    CryptoTenSymbolObservationContracts,
    CryptoTenSymbolObservationStore,
    CryptoTenSymbolObservationStoreError,
)


TEN_SYMBOL_SPREAD_PROJECTION_CONTRACT = (
    "tradingagent.crypto.ten_symbol_spread_projection.v1"
)
TEN_SYMBOL_SPREAD_PROJECTION_CHECKPOINT_CONTRACT = (
    "tradingagent.crypto.ten_symbol_spread_projection_checkpoint.v1"
)
CHECKPOINT_FILENAME = "spread_projection_checkpoint.json"
_SYMBOLS = OBSERVATION_SYMBOLS
_SPREAD_BLOCK_KEYS = frozenset(
    {
        "contract",
        "status",
        "reason_code",
        "sampled_symbol_count",
        "rejected_symbol_count",
        "rejected_reasons",
        "spread_sha256",
        "catalog_version",
    }
)
_SPREAD_STATUSES = frozenset({"completed", "degraded", "unavailable"})
_FIVE_MINUTES = timedelta(minutes=5)


class CryptoTenSymbolSpreadProjectionError(RuntimeError):
    """Stable fail-closed error for the detached spread projection."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CryptoTenSymbolSpreadProjectionError(
            "ten_symbol_spread_projection_payload_invalid"
        ) from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _utc(value: Any, *, reason: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CryptoTenSymbolSpreadProjectionError(reason)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CryptoTenSymbolSpreadProjectionError(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise CryptoTenSymbolSpreadProjectionError(reason)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal(value: Any, *, reason: str) -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise CryptoTenSymbolSpreadProjectionError(reason)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CryptoTenSymbolSpreadProjectionError(reason) from exc
    if not result.is_finite():
        raise CryptoTenSymbolSpreadProjectionError(reason)
    return result


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _result(*, status: str, **fields: Any) -> dict[str, Any]:
    return {
        "contract": TEN_SYMBOL_SPREAD_PROJECTION_CONTRACT,
        "status": status,
        "learning_mode": "detached_offline_worker",
        "manual_review_required": True,
        **fields,
        **projection._non_authority_fields(),
    }


# ---------------------------------------------------------------------------
# Store access and per-slot evidence binding
# ---------------------------------------------------------------------------


def _open_store(
    root: Path,
    contracts: CryptoTenSymbolObservationContracts = TEN_SYMBOL_CONTRACTS,
) -> CryptoTenSymbolObservationStore:
    required = (root, root / "slot_index")
    if any(not path.exists() or path.is_symlink() for path in required):
        raise CryptoTenSymbolSpreadProjectionError(
            "ten_symbol_spread_projection_root_incomplete"
        )
    try:
        return CryptoTenSymbolObservationStore(root, contracts=contracts)
    except (CryptoTenSymbolObservationStoreError, OSError, ValueError) as exc:
        raise CryptoTenSymbolSpreadProjectionError(
            "ten_symbol_spread_projection_core_invalid"
        ) from exc


def _validate_spread_block(
    event: Mapping[str, Any],
    *,
    symbols: tuple[str, ...] = OBSERVATION_SYMBOLS,
    spread_contract: str = TEN_SYMBOL_SPREAD_CONTRACT,
) -> dict[str, Any]:
    """Shape-check one terminal event's spread status block.

    The store verifies chain integrity and the fixed authority fields but
    deliberately does not lock the spread block's exact key set, so the
    projection shape-checks it before trusting it.  A malformed block in an
    otherwise verified event is tamper evidence and fails closed.
    """

    reason = "ten_symbol_spread_projection_event_block_invalid"
    spread = event.get("spread")
    if not isinstance(spread, Mapping) or set(spread) != _SPREAD_BLOCK_KEYS:
        raise CryptoTenSymbolSpreadProjectionError(reason)
    block = dict(spread)
    status = block.get("status")
    sampled = block.get("sampled_symbol_count")
    rejected = block.get("rejected_symbol_count")
    reasons = block.get("rejected_reasons")
    reason_code = block.get("reason_code")
    spread_sha256 = block.get("spread_sha256")
    catalog_version = block.get("catalog_version")
    if (
        block.get("contract") != spread_contract
        or status not in _SPREAD_STATUSES
        or isinstance(sampled, bool)
        or not isinstance(sampled, int)
        or isinstance(rejected, bool)
        or not isinstance(rejected, int)
        or not (0 <= sampled <= len(symbols))
        or not (0 <= rejected <= len(symbols))
        # Per-symbol evidence binds exactly one entry per symbol; a leg-wide
        # degradation has no per-symbol evidence and reports zero counts.
        or (spread_sha256 is not None and sampled + rejected != len(symbols))
        or (spread_sha256 is None and (sampled != 0 or rejected != 0))
        or not isinstance(reasons, dict)
        or len(reasons) != rejected
        or any(
            not isinstance(symbol, str)
            or symbol not in symbols
            or not isinstance(code, str)
            or not code
            for symbol, code in reasons.items()
        )
        or (reason_code is not None and not isinstance(reason_code, str))
        or (isinstance(reason_code, str) and not reason_code)
        or (spread_sha256 is not None and not _is_sha256(spread_sha256))
        or (catalog_version is not None and not isinstance(catalog_version, str))
        or (isinstance(catalog_version, str) and not catalog_version)
    ):
        raise CryptoTenSymbolSpreadProjectionError(reason)
    if (
        (status == "completed" and (rejected != 0 or spread_sha256 is None))
        or (status == "unavailable" and sampled != 0)
        or (spread_sha256 is None and status != "unavailable")
    ):
        raise CryptoTenSymbolSpreadProjectionError(reason)
    return block


def _bind_slot_evidence(
    store: CryptoTenSymbolObservationStore,
    event: Mapping[str, Any],
    block: Mapping[str, Any],
    *,
    window_end_iso: str,
    symbols: tuple[str, ...] = OBSERVATION_SYMBOLS,
    spread_contract: str = TEN_SYMBOL_SPREAD_CONTRACT,
    spreads_sidecar_contract: str = TEN_SYMBOL_SPREADS_SIDECAR_CONTRACT,
) -> list[dict[str, Any]] | None:
    """Return the slot's verified spread entries, or None when the sidecar
    is missing.

    Every claimed digest is re-derived from the persisted sidecar and
    compared value-for-value against the store event, mirroring the bars
    sidecar consumption precedent.  A missing sidecar only excludes the
    slot (recorded as ``sidecar_missing``); a corrupt sidecar or any digest
    drift between two verified artifacts is tamper evidence and fails
    closed, matching the runtime's ``runtime_spreads_sidecar_invalid``
    discipline.
    """

    reason = "ten_symbol_spread_projection_sidecar_invalid"
    try:
        sidecar = store.read_spreads_sidecar(window_end_iso)
    except CryptoTenSymbolObservationStoreError as exc:
        raise CryptoTenSymbolSpreadProjectionError(reason) from exc
    if sidecar is None:
        return None
    try:
        entries = validate_ten_symbol_spreads_sidecar(
            sidecar,
            symbols=symbols,
            spreads_sidecar_contract=spreads_sidecar_contract,
        )
    except CryptoMarketObservationError as exc:
        raise CryptoTenSymbolSpreadProjectionError(reason) from exc
    rebuilt = build_spread_event_block(
        entries=entries,
        catalog_version=sidecar["catalog_version"],
        spread_sha256=str(sidecar["spread_sha256"]),
        spread_contract=spread_contract,
    )
    if (
        _canonical_json(rebuilt) != _canonical_json(dict(block))
        or sidecar.get("window_end") != window_end_iso
        or sidecar.get("profile_sha256") != event.get("profile_sha256")
    ):
        raise CryptoTenSymbolSpreadProjectionError(
            "ten_symbol_spread_projection_spread_digest_mismatch"
        )
    return entries


# ---------------------------------------------------------------------------
# Aggregation: per symbol per UTC calendar day
# ---------------------------------------------------------------------------


_BPS_QUANTUM = Decimal("0.00000001")


def _spread_bps(row: Mapping[str, Any]) -> Decimal:
    reason = "ten_symbol_spread_projection_sample_invalid"
    bid = _decimal(row.get("bid_price"), reason=reason)
    ask = _decimal(row.get("ask_price"), reason=reason)
    if bid <= 0 or ask < bid:
        raise CryptoTenSymbolSpreadProjectionError(reason)
    mid = (ask + bid) / Decimal(2)
    # Quantize once at ingestion so identical inputs always aggregate to
    # identical statistics, free of Decimal context rounding noise.
    return ((ask - bid) / mid * Decimal(10000)).quantize(_BPS_QUANTUM)


def _quantile(sorted_values: list[Decimal], fraction: Decimal) -> Decimal:
    """Type-7 (linear interpolation between closest ranks) quantile."""

    if not sorted_values:
        raise CryptoTenSymbolSpreadProjectionError(
            "ten_symbol_spread_projection_sample_invalid"
        )
    count = len(sorted_values)
    if count == 1:
        return sorted_values[0]
    rank = Decimal(count - 1) * fraction
    lower = int(rank)
    if lower + 1 >= count:
        return sorted_values[-1]
    return sorted_values[lower] + (sorted_values[lower + 1] - sorted_values[lower]) * (
        rank - lower
    )


class _Bucket:
    def __init__(self) -> None:
        self.samples: list[Decimal] = []
        self.rejected = 0
        self.rejected_reasons: dict[str, int] = {}
        self.slots: set[str] = set()
        self.first_slot: str | None = None
        self.last_slot: str | None = None
        self.first_observed_at: str | None = None
        self.last_observed_at: str | None = None

    def add_sample(self, *, slot: str, observed_at: str, spread_bps: Decimal) -> None:
        self.samples.append(spread_bps)
        self.slots.add(slot)
        self.first_slot = slot if self.first_slot is None else min(self.first_slot, slot)
        self.last_slot = slot if self.last_slot is None else max(self.last_slot, slot)
        if self.first_observed_at is None or observed_at < self.first_observed_at:
            self.first_observed_at = observed_at
        if self.last_observed_at is None or observed_at > self.last_observed_at:
            self.last_observed_at = observed_at

    def add_rejection(self, *, slot: str, reason_code: str) -> None:
        self.rejected += 1
        self.rejected_reasons[reason_code] = self.rejected_reasons.get(reason_code, 0) + 1
        self.slots.add(slot)
        self.first_slot = slot if self.first_slot is None else min(self.first_slot, slot)
        self.last_slot = slot if self.last_slot is None else max(self.last_slot, slot)

    def stats(self) -> dict[str, Any]:
        total = len(self.samples) + self.rejected
        ordered = sorted(self.samples)
        spread_stats: dict[str, Any]
        if ordered:
            mean = (sum(ordered, Decimal(0)) / Decimal(len(ordered))).quantize(
                _BPS_QUANTUM
            )
            spread_stats = {
                "mean_bps": _text(mean),
                "median_bps": _text(
                    _quantile(ordered, Decimal("0.5")).quantize(_BPS_QUANTUM)
                ),
                "p25_bps": _text(
                    _quantile(ordered, Decimal("0.25")).quantize(_BPS_QUANTUM)
                ),
                "p75_bps": _text(
                    _quantile(ordered, Decimal("0.75")).quantize(_BPS_QUANTUM)
                ),
                "min_bps": _text(ordered[0]),
                "max_bps": _text(ordered[-1]),
            }
        else:
            spread_stats = {
                "mean_bps": None,
                "median_bps": None,
                "p25_bps": None,
                "p75_bps": None,
                "min_bps": None,
                "max_bps": None,
            }
        return {
            "sample_count": len(self.samples),
            "rejected_count": self.rejected,
            "rejection_rate": _text(Decimal(self.rejected) / Decimal(total)),
            "rejected_reason_counts": dict(sorted(self.rejected_reasons.items())),
            "slot_count": len(self.slots),
            "first_slot": self.first_slot,
            "last_slot": self.last_slot,
            "first_observed_at": self.first_observed_at,
            "last_observed_at": self.last_observed_at,
            **spread_stats,
        }


def _spread_metric_contract() -> dict[str, Any]:
    return {
        "metric": "realized_top_of_book_relative_spread",
        "spread_bps_formula": "(ask_price - bid_price) / mid * 10000",
        "mid_formula": "(ask_price + bid_price) / 2",
        "quantile_method": "type7_linear_interpolation",
        "bps_quantization": "0.00000001_round_half_even",
        "aggregation_window": "utc_calendar_day_per_symbol",
        "sample_unit": "one book_ticker snapshot per symbol per terminal 5m slot",
        "rejected_entries": "counted_in_rejection_rate_only_never_in_spread_stats",
        "intended_consumer": (
            "detached cost-after evaluation substituting realized spreads for"
            " assumed costs; research evidence only, no strategy/evaluation"
            " logic is attached here"
        ),
    }


# ---------------------------------------------------------------------------
# Immutable artifact plus compact checkpoint
# ---------------------------------------------------------------------------


def _projection_root(root: Path) -> Path:
    return root / "evolution" / "ten_symbol_spread_projection"


def _ensure_root(root: Path) -> Path:
    parent = root / "evolution"
    if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
        raise CryptoTenSymbolSpreadProjectionError(
            "ten_symbol_spread_projection_directory_invalid"
        )
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True)
    evolution = _projection_root(root)
    for directory in (evolution, evolution / "artifacts"):
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise CryptoTenSymbolSpreadProjectionError(
                    "ten_symbol_spread_projection_directory_invalid"
                )
        else:
            directory.mkdir(mode=0o700)
    return evolution


def _atomic_checkpoint(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (_canonical_json(payload) + "\n").encode()
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise CryptoTenSymbolSpreadProjectionError(
            "ten_symbol_spread_projection_checkpoint_write_failed"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _validated_current(evolution: Path) -> dict[str, Any] | None:
    """Validate only the compact checkpoint and its one bound artifact."""

    checkpoint_path = evolution / CHECKPOINT_FILENAME
    if not checkpoint_path.exists() and not checkpoint_path.is_symlink():
        return None
    try:
        current = projection._parse_canonical(
            checkpoint_path, reason="ten_symbol_spread_projection_checkpoint_invalid"
        )
    except projection.CryptoTenSymbolFactorProjectionError as exc:
        raise CryptoTenSymbolSpreadProjectionError(
            "ten_symbol_spread_projection_checkpoint_invalid"
        ) from exc
    material = dict(current)
    claimed = material.pop("checkpoint_sha256", None)
    if (
        current.get("contract") != TEN_SYMBOL_SPREAD_PROJECTION_CHECKPOINT_CONTRACT
        or claimed != _sha256(material)
        or any(
            current.get(key) != value
            for key, value in projection._non_authority_fields().items()
        )
    ):
        raise CryptoTenSymbolSpreadProjectionError(
            "ten_symbol_spread_projection_checkpoint_invalid"
        )
    outcome = current.get("last_projected_outcome_sha256")
    artifact_sha256 = current.get("artifact_sha256")
    if not _is_sha256(outcome) or not _is_sha256(artifact_sha256):
        raise CryptoTenSymbolSpreadProjectionError(
            "ten_symbol_spread_projection_checkpoint_invalid"
        )
    artifact_path = evolution / "artifacts" / f"{outcome}.json"
    try:
        artifact = projection._parse_canonical(
            artifact_path, reason="ten_symbol_spread_projection_artifact_invalid"
        )
    except projection.CryptoTenSymbolFactorProjectionError as exc:
        raise CryptoTenSymbolSpreadProjectionError(
            "ten_symbol_spread_projection_artifact_invalid"
        ) from exc
    artifact_material = dict(artifact)
    claimed_artifact = artifact_material.pop("artifact_sha256", None)
    if (
        claimed_artifact != _sha256(artifact_material)
        or claimed_artifact != artifact_sha256
        or artifact.get("outcome_sha256") != outcome
        or artifact.get("contract") != TEN_SYMBOL_SPREAD_PROJECTION_CONTRACT
    ):
        raise CryptoTenSymbolSpreadProjectionError(
            "ten_symbol_spread_projection_artifact_invalid"
        )
    return current


# ---------------------------------------------------------------------------
# Projection run
# ---------------------------------------------------------------------------


def run_crypto_ten_symbol_spread_projection(
    *,
    output_root: Path | str,
    symbols: tuple[str, ...] = OBSERVATION_SYMBOLS,
    contracts: CryptoTenSymbolObservationContracts = TEN_SYMBOL_CONTRACTS,
    spread_contract: str = TEN_SYMBOL_SPREAD_CONTRACT,
    spreads_sidecar_contract: str = TEN_SYMBOL_SPREADS_SIDECAR_CONTRACT,
) -> dict[str, Any]:
    """Aggregate verified spreads sidecars into one immutable artifact.

    The run deterministically rebuilds the full inventory from the verified
    event chain on every invocation: the same store state always yields the
    same outcome, so reruns are idempotent (``no_new_outcome``) and the
    emitted artifact bytes never change for the same input.  Slots whose
    spread leg was leg-wide unavailable, whose slot predates the feature, or
    whose sidecar is missing are excluded and explicitly recorded; corrupt
    sidecars or digest drift fail closed.
    """

    _assert_simulation_only()
    root = Path(output_root)
    store = _open_store(root, contracts)
    if store.pending_record_read_only() is not None:
        return _result(status="deferred_core_pending")
    try:
        events = store.events_read_only()
    except (CryptoTenSymbolObservationStoreError, OSError, ValueError) as exc:
        raise CryptoTenSymbolSpreadProjectionError(
            "ten_symbol_spread_projection_core_invalid"
        ) from exc

    buckets: dict[str, dict[str, _Bucket]] = {symbol: {} for symbol in symbols}
    symbol_totals: dict[str, _Bucket] = {symbol: _Bucket() for symbol in symbols}
    totals = _Bucket()
    spread_sources: list[dict[str, Any]] = []
    skipped_slots: list[dict[str, Any]] = []
    terminal_slot_count = 0
    sampled_entry_count = 0
    rejected_entry_count = 0
    first_slot: str | None = None
    last_slot: str | None = None

    for event in events:
        if event.get("event_type") not in TERMINAL_SLOT_TYPES:
            continue
        terminal_slot_count += 1
        window_end_iso = str(event.get("window_end"))
        window_end = _utc(
            window_end_iso, reason="ten_symbol_spread_projection_source_invalid"
        )
        slot_iso = _iso(window_end - _FIVE_MINUTES)
        first_slot = slot_iso if first_slot is None else min(first_slot, slot_iso)
        last_slot = slot_iso if last_slot is None else max(last_slot, slot_iso)
        if "spread" not in event:
            skipped_slots.append(
                {"window_end": window_end_iso, "reason": "feature_ineligible"}
            )
            continue
        block = _validate_spread_block(
            event,
            symbols=symbols,
            spread_contract=spread_contract,
        )
        if block["spread_sha256"] is None:
            skipped_slots.append(
                {
                    "window_end": window_end_iso,
                    "reason": "spread_unavailable",
                    "reason_code": block["reason_code"],
                }
            )
            continue
        entries = _bind_slot_evidence(
            store,
            event,
            block,
            window_end_iso=window_end_iso,
            symbols=symbols,
            spread_contract=spread_contract,
            spreads_sidecar_contract=spreads_sidecar_contract,
        )
        if entries is None:
            skipped_slots.append(
                {"window_end": window_end_iso, "reason": "sidecar_missing"}
            )
            continue
        spread_sources.append(
            {
                "window_end": window_end_iso,
                "source_event_checksum": event.get("checksum"),
                "spread_sha256": block["spread_sha256"],
            }
        )
        day = window_end.date().isoformat()
        for entry in entries:
            symbol = str(entry["symbol"])
            bucket = buckets[symbol].setdefault(day, _Bucket())
            if entry["status"] == "sampled":
                sampled_entry_count += 1
                value = _spread_bps(entry["row"])
                observed_at = str(entry["observed_at"])
                bucket.add_sample(
                    slot=slot_iso, observed_at=observed_at, spread_bps=value
                )
                symbol_totals[symbol].add_sample(
                    slot=slot_iso, observed_at=observed_at, spread_bps=value
                )
                totals.add_sample(
                    slot=slot_iso, observed_at=observed_at, spread_bps=value
                )
            else:
                rejected_entry_count += 1
                reason_code = str(entry["reason_code"])
                bucket.add_rejection(slot=slot_iso, reason_code=reason_code)
                symbol_totals[symbol].add_rejection(
                    slot=slot_iso, reason_code=reason_code
                )
                totals.add_rejection(slot=slot_iso, reason_code=reason_code)

    counts = {
        "terminal_slot_count": terminal_slot_count,
        "slots_with_spread_evidence": len(spread_sources),
        "slots_feature_ineligible": sum(
            item["reason"] == "feature_ineligible" for item in skipped_slots
        ),
        "slots_spread_unavailable": sum(
            item["reason"] == "spread_unavailable" for item in skipped_slots
        ),
        "slots_sidecar_missing": sum(
            item["reason"] == "sidecar_missing" for item in skipped_slots
        ),
        "sampled_entry_count": sampled_entry_count,
        "rejected_entry_count": rejected_entry_count,
    }
    if sampled_entry_count == 0:
        return _result(
            status="insufficient_spread_samples",
            **counts,
            outcome_sha256=None,
            artifact_sha256=None,
            projected_through_slot=None,
        )

    outcome = _sha256(
        {"skipped": skipped_slots, "sources": spread_sources}
    )
    evolution = _ensure_root(root)
    with projection._lock(evolution):
        current = _validated_current(evolution)
        if current is not None and current.get(
            "last_projected_outcome_sha256"
        ) == outcome:
            return _result(
                status="no_new_outcome",
                **counts,
                outcome_sha256=outcome,
                artifact_sha256=current["artifact_sha256"],
                projected_through_slot=current.get("projected_through_slot"),
            )
        artifact = {
            "contract": TEN_SYMBOL_SPREAD_PROJECTION_CONTRACT,
            "artifact_type": "ten_symbol_realized_spread_projection",
            "outcome_sha256": outcome,
            "spread_metric": _spread_metric_contract(),
            "source": {
                "store_head_checksum": (
                    str(events[-1]["checksum"]) if events else None
                ),
                "first_slot": first_slot,
                "last_slot": last_slot,
                **counts,
                "spread_sources": spread_sources,
                "skipped_slots": skipped_slots,
            },
            "symbols": list(symbols),
            "buckets": {
                symbol: {
                    day: bucket.stats()
                    for day, bucket in sorted(days.items())
                }
                for symbol, days in buckets.items()
                if days
            },
            "symbol_totals": {
                symbol: bucket.stats()
                for symbol, bucket in symbol_totals.items()
                if bucket.samples or bucket.rejected
            },
            "totals": totals.stats(),
            **projection._non_authority_fields(),
        }
        artifact["artifact_sha256"] = _sha256(artifact)
        projection._write_immutable(
            evolution / "artifacts" / f"{outcome}.json", artifact
        )
        checkpoint = {
            "contract": TEN_SYMBOL_SPREAD_PROJECTION_CHECKPOINT_CONTRACT,
            "last_projected_outcome_sha256": outcome,
            "artifact_sha256": artifact["artifact_sha256"],
            "projected_through_slot": last_slot,
            **counts,
            **projection._non_authority_fields(),
        }
        checkpoint["checkpoint_sha256"] = _sha256(checkpoint)
        _atomic_checkpoint(evolution / CHECKPOINT_FILENAME, checkpoint)
    return _result(
        status="projected",
        **counts,
        outcome_sha256=outcome,
        artifact_sha256=artifact["artifact_sha256"],
        projected_through_slot=last_slot,
    )


def ten_symbol_spread_projection_exit_code(result: Mapping[str, Any]) -> int:
    if not isinstance(result, Mapping) or result.get("status") not in {
        "projected",
        "no_new_outcome",
        "insufficient_spread_samples",
        "deferred_core_pending",
    }:
        return 2
    return (
        0
        if all(
            result.get(key) == value
            for key, value in projection._non_authority_fields().items()
        )
        else 2
    )


__all__ = [
    "CHECKPOINT_FILENAME",
    "CryptoTenSymbolSpreadProjectionError",
    "TEN_SYMBOL_SPREAD_PROJECTION_CHECKPOINT_CONTRACT",
    "TEN_SYMBOL_SPREAD_PROJECTION_CONTRACT",
    "run_crypto_ten_symbol_spread_projection",
    "ten_symbol_spread_projection_exit_code",
]
