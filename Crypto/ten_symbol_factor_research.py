"""Detached ten-symbol factor projections over the observation store.

This is deliberately a research sink, not a runtime participant.  It reads
the append-only ten-symbol observation event chain plus the immutable bars
sidecars, and writes only its own append-only
``evolution/ten_symbol_factor_research`` namespace.  It mirrors the
delayed-paper factor projection mechanics (records/receipts/labels/
checkpoints, immutable writes, checkpoint hash chain, segment cutting,
retriable time-budget debt) with the input swapped to the ten-symbol store.
It has no core, capital, order, Champion, or network access.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from time import monotonic
from typing import Any, Iterator, Mapping
import uuid

from Crypto.factor_research import (
    TEN_SYMBOL_FACTOR_SET_ID,
    TEN_SYMBOL_FACTOR_SET_VERSION,
    WINDOW_BARS,
    CryptoFactorResearchError,
    build_factor_snapshot,
    build_forward_label,
    evaluate_factor_hypotheses,
)
from Crypto.fixture_sim.contracts import _assert_simulation_only
from Crypto.market_observation import (
    OBSERVATION_SYMBOLS,
    CryptoMarketObservation,
    CryptoMarketObservationError,
    observation_from_ten_symbol_bars_sidecar,
)
from Crypto.ten_symbol_observation_store import (
    TERMINAL_SLOT_TYPES,
    CryptoTenSymbolObservationStore,
    CryptoTenSymbolObservationStoreError,
)
from shared.governance.evidence_readiness import load_evidence_readiness_contract


TEN_SYMBOL_FACTOR_PROJECTION_CONTRACT = (
    "tradingagent.crypto.ten_symbol_factor_projection.v1"
)
TEN_SYMBOL_FACTOR_PROJECTION_RECEIPT_CONTRACT = (
    "tradingagent.crypto.ten_symbol_factor_projection_receipt.v1"
)
TEN_SYMBOL_FACTOR_PROJECTION_CHECKPOINT_CONTRACT = (
    "tradingagent.crypto.ten_symbol_factor_projection_checkpoint.v1"
)
OPERATIONAL_MATURITY_CONTINUOUS_COMPLETIONS = 288
MAX_CATCHUP_UNITS = 12
_SYMBOLS = OBSERVATION_SYMBOLS
_HORIZONS = (60, 240, 720, 1440)
_MAX_FILE_BYTES = 2 * 1024 * 1024
_FULL_SCRUB_MAX_SECONDS = 110.0
SEGMENTED_LEARNING_CONSUMER_PROFILE_ID = "crypto-5m-ohlcv-13bar-forward-labels-v2"
_FIVE_MINUTES = timedelta(minutes=5)


class CryptoTenSymbolFactorProjectionError(RuntimeError):
    """Stable fail-closed error for detached ten-symbol factor projections."""


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
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_payload_invalid"
        ) from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _non_authority_fields() -> dict[str, Any]:
    return {
        "authority": "none",
        "research_only": True,
        "learning_authority": False,
        "execution_eligible": False,
        "execution_authority": False,
        "durable_execution_receipt": False,
        "production_eligible": False,
        "real_trading_enabled": False,
        "network_used": False,
        "model_network_used": False,
        "testnet_used": False,
        "live_broker_used": False,
        "promotion_authorized": False,
        "automatic_champion_replacement": False,
        "automatic_risk_expansion_enabled": False,
        "outbox_id": None,
        "capital_commit_id": None,
    }


def _result(*, status: str, **fields: Any) -> dict[str, Any]:
    return {
        "contract": TEN_SYMBOL_FACTOR_PROJECTION_CONTRACT,
        "status": status,
        "learning_mode": "detached_offline_worker",
        "manual_review_required": True,
        **fields,
        **_non_authority_fields(),
    }


def _segmented_learning_policy() -> dict[str, Any]:
    """Load only the Crypto-owned interpretation of the shared readiness rule."""

    try:
        contract = load_evidence_readiness_contract()
        policy = contract.market_policies["crypto"]
        maturity = policy["operational_maturity"]
        segmented = policy["segmented_learning"]
    except (KeyError, ValueError, TypeError) as exc:
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_readiness_contract_invalid"
        ) from exc
    if (
        contract.safety["simulation_only"] is not True
        or contract.safety["real_trading_enabled"] is not False
        or contract.safety["automatic_promotion_enabled"] is not False
        or maturity.get("minimum_continuous_slots")
        != OPERATIONAL_MATURITY_CONTINUOUS_COMPLETIONS
        or maturity.get("purpose") != "automatic_runtime_maturity"
        or segmented.get("allowed") is not True
        or segmented.get("minimum_slots_source")
        != "preregistered_feature_and_label_profile"
        or segmented.get("gap_crossing_allowed") is not False
        or segmented.get("automatic_promotion_allowed") is not False
    ):
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_readiness_contract_invalid"
        )
    return {
        "contract_id": contract.contract_id,
        "operational_maturity_minimum_continuous_slots": maturity[
            "minimum_continuous_slots"
        ],
        "segmented_learning_allowed": segmented["allowed"],
        "minimum_slots_source": segmented["minimum_slots_source"],
        "gap_crossing_allowed": segmented["gap_crossing_allowed"],
        "automatic_promotion_allowed": segmented["automatic_promotion_allowed"],
    }


def _segmented_learning_consumer_profile() -> dict[str, Any]:
    """Return the frozen ten-symbol feature/label definition for research only."""

    return {
        "consumer_profile_id": SEGMENTED_LEARNING_CONSUMER_PROFILE_ID,
        "feature_set_id": TEN_SYMBOL_FACTOR_SET_ID,
        "feature_set_version": TEN_SYMBOL_FACTOR_SET_VERSION,
        "window_bars": WINDOW_BARS,
        "bar_interval_minutes": 5,
        "symbols": list(_SYMBOLS),
        "required_label_horizon_minutes": 60,
        "auxiliary_attribution_horizons": list(_HORIZONS[1:]),
    }


def _utc(value: Any, *, reason: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CryptoTenSymbolFactorProjectionError(reason)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CryptoTenSymbolFactorProjectionError(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise CryptoTenSymbolFactorProjectionError(reason)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _assert_regular(path: Path, *, reason: str) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise CryptoTenSymbolFactorProjectionError(reason) from exc
    try:
        before = os.fstat(descriptor)
        node = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > _MAX_FILE_BYTES
            or node.st_dev != before.st_dev
            or node.st_ino != before.st_ino
        ):
            raise CryptoTenSymbolFactorProjectionError(reason)
        encoded = os.read(descriptor, _MAX_FILE_BYTES + 1)
        after = os.fstat(descriptor)
        if len(encoded) != before.st_size or after.st_size != before.st_size:
            raise CryptoTenSymbolFactorProjectionError(reason)
    except CryptoTenSymbolFactorProjectionError:
        raise
    except OSError as exc:
        raise CryptoTenSymbolFactorProjectionError(reason) from exc
    finally:
        os.close(descriptor)
    if not encoded.endswith(b"\n") or b"\x00" in encoded:
        raise CryptoTenSymbolFactorProjectionError(reason)
    return encoded


def _parse_canonical(path: Path, *, reason: str) -> dict[str, Any]:
    try:
        payload = json.loads(_assert_regular(path, reason=reason).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoTenSymbolFactorProjectionError(reason) from exc
    if not isinstance(payload, dict) or (
        _canonical_json(payload) + "\n"
    ).encode() != _assert_regular(path, reason=reason):
        raise CryptoTenSymbolFactorProjectionError(reason)
    return payload


def _write_immutable(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    if path.exists() or path.is_symlink():
        if (
            _assert_regular(path, reason="ten_symbol_factor_projection_immutable_conflict")
            != encoded
        ):
            raise CryptoTenSymbolFactorProjectionError(
                "ten_symbol_factor_projection_immutable_conflict"
            )
        return
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
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError:
        if (
            _assert_regular(path, reason="ten_symbol_factor_projection_immutable_conflict")
            != encoded
        ):
            raise CryptoTenSymbolFactorProjectionError(
                "ten_symbol_factor_projection_immutable_conflict"
            )
    except OSError as exc:
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_write_failed"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _root(root: Path) -> Path:
    return root / "evolution" / "ten_symbol_factor_research"


def _paths(root: Path, observation_id: str) -> dict[str, Path]:
    evolution = _root(root)
    return {
        "record": evolution / "records" / f"{observation_id}.json",
        "receipt": evolution / "receipts" / f"{observation_id}.json",
    }


def _label_path(root: Path, observation_id: str, symbol: str, horizon: int) -> Path:
    return _root(root) / "labels" / f"{observation_id}-{symbol.lower()}-{horizon}.json"


def _ensure_root(root: Path) -> Path:
    parent = root / "evolution"
    if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_directory_invalid"
        )
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True)
    evolution = _root(root)
    for directory in (
        evolution,
        evolution / "records",
        evolution / "receipts",
        evolution / "labels",
        evolution / "checkpoints",
    ):
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise CryptoTenSymbolFactorProjectionError(
                    "ten_symbol_factor_projection_directory_invalid"
                )
        else:
            directory.mkdir(mode=0o700)
    return evolution


@contextmanager
def _lock(evolution: Path) -> Iterator[None]:
    path = evolution / ".lock"
    try:
        descriptor = os.open(
            path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        metadata = os.fstat(descriptor)
        node = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or node.st_ino != metadata.st_ino
        ):
            raise CryptoTenSymbolFactorProjectionError(
                "ten_symbol_factor_projection_lock_invalid"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except CryptoTenSymbolFactorProjectionError:
        raise
    except OSError as exc:
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_lock_invalid"
        ) from exc
    finally:
        if "descriptor" in locals():
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


# ---------------------------------------------------------------------------
# Source units: terminal store events plus their bars sidecars
# ---------------------------------------------------------------------------


def _open_store(root: Path) -> CryptoTenSymbolObservationStore:
    required = (
        root,
        root / "slot_index",
    )
    if any(not path.exists() or path.is_symlink() for path in required):
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_root_incomplete"
        )
    try:
        return CryptoTenSymbolObservationStore(root)
    except (CryptoTenSymbolObservationStoreError, OSError, ValueError) as exc:
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_core_invalid"
        ) from exc


def _terminal_events(store: CryptoTenSymbolObservationStore) -> list[dict[str, Any]]:
    try:
        events = store.events_read_only()
    except (CryptoTenSymbolObservationStoreError, OSError, ValueError) as exc:
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_core_invalid"
        ) from exc
    units: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") not in TERMINAL_SLOT_TYPES:
            continue
        event_id = event.get("event_id")
        checksum = event.get("checksum")
        if (
            not isinstance(event_id, str)
            or not event_id
            or not isinstance(checksum, str)
            or len(checksum) != 64
        ):
            raise CryptoTenSymbolFactorProjectionError(
                "ten_symbol_factor_projection_source_invalid"
            )
        window_end = _utc(
            event.get("window_end"),
            reason="ten_symbol_factor_projection_source_invalid",
        )
        units.append(
            {
                "event": event,
                "observation_id": event_id,
                "source_event_checksum": checksum,
                "window_end": window_end,
                "slot": window_end - _FIVE_MINUTES,
            }
        )
    return units


def _event_observation_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = (
        event.get("recovery_observation")
        if event.get("event_type") == "data_gap"
        else event.get("observation")
    )
    if not isinstance(payload, Mapping):
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_source_invalid"
        )
    return payload


def _attach_eligibility(
    store: CryptoTenSymbolObservationStore, unit: dict[str, Any]
) -> None:
    """Bind the unit's bars sidecar or mark the slot feature-ineligible.

    A slot whose sidecar is missing, unreadable, internally inconsistent, or
    whose recomputed digests drift from the store event is never projected:
    it cuts the segment exactly like a data gap.  The event chain itself was
    already verified by the store, so this is a feature-eligibility gate,
    not a chain-integrity gate.
    """

    event = unit["event"]
    unit["eligible"] = False
    unit["ineligible_reason"] = "sidecar_missing"
    unit["observation"] = None
    unit["rows_by_symbol"] = None
    unit["sidecar_sha256"] = None
    try:
        sidecar = store.read_bars_sidecar(str(event["window_end"]))
    except CryptoTenSymbolObservationStoreError:
        unit["ineligible_reason"] = "sidecar_digest_mismatch"
        return
    if sidecar is None:
        return
    try:
        observation, rows_by_symbol = observation_from_ten_symbol_bars_sidecar(
            sidecar
        )
    except CryptoMarketObservationError:
        unit["ineligible_reason"] = "sidecar_digest_mismatch"
        return
    event_observation = _event_observation_payload(event)
    if (
        sidecar.get("profile_sha256") != event.get("profile_sha256")
        or observation.catalog_version != event.get("catalog_version")
        or event_observation.get("observation_sha256")
        != observation.observation_sha256
        or event_observation.get("market_data_sha256")
        != observation.market_data_sha256
    ):
        unit["ineligible_reason"] = "sidecar_digest_mismatch"
        return
    unit["eligible"] = True
    unit["ineligible_reason"] = None
    unit["observation"] = observation
    unit["rows_by_symbol"] = rows_by_symbol
    unit["sidecar_sha256"] = _sha256(sidecar)


def _build_units(
    store: CryptoTenSymbolObservationStore, *, deadline: float | None = None
) -> list[dict[str, Any]] | None:
    units = _terminal_events(store)
    for unit in units:
        if deadline is not None and monotonic() >= deadline:
            return None
        _attach_eligibility(store, unit)
    return units


def _latest_continuous(units: list[dict[str, Any]]) -> int:
    count = 0
    expected: datetime | None = None
    for unit in reversed(units):
        if not unit["eligible"]:
            break
        slot = unit["slot"]
        if expected is not None and slot != expected:
            break
        count += 1
        expected = slot - _FIVE_MINUTES
    return count


def _segment_ids(units: list[dict[str, Any]]) -> dict[str, str]:
    """Assign a stable segment identifier to every terminal unit.

    A new segment starts at the first unit, after any feature-ineligible
    slot (missing/mismatched sidecar), and across any non-contiguous slot
    step (data gap).  Labels never cross a segment boundary.
    """

    result: dict[str, str] = {}
    previous_slot: datetime | None = None
    previous_eligible = False
    segment_start: datetime | None = None
    for unit in units:
        slot = unit["slot"]
        if (
            previous_slot is None
            or not previous_eligible
            or slot - previous_slot != _FIVE_MINUTES
        ):
            segment_start = slot
        if segment_start is None:
            raise CryptoTenSymbolFactorProjectionError(
                "ten_symbol_factor_projection_segment_invalid"
            )
        result[unit["observation_id"]] = "crypto-5m-segment-" + (
            segment_start.strftime("%Y%m%dT%H%M%SZ")
        )
        previous_slot = slot
        previous_eligible = bool(unit["eligible"])
    return result


# ---------------------------------------------------------------------------
# Records, receipts, checkpoints
# ---------------------------------------------------------------------------


def _factor_input(
    unit: Mapping[str, Any], symbol: str
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    observation = unit["observation"]
    if not isinstance(observation, CryptoMarketObservation):
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_source_invalid"
        )
    sources = {source.symbol: source for source in observation.sources}
    source = sources.get(symbol)
    raw_bars = unit["rows_by_symbol"].get(symbol)
    if source is None or not isinstance(raw_bars, list) or len(raw_bars) != 13:
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_window_invalid"
        )
    converted: list[dict[str, Any]] = []
    for row in raw_bars:
        if not isinstance(row, Mapping):
            raise CryptoTenSymbolFactorProjectionError(
                "ten_symbol_factor_projection_window_invalid"
            )
        converted.append(
            {
                "open_time": row.get("open_time"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "base_volume": row.get("volume"),
                "quote_volume": row.get("quote_volume"),
            }
        )
    evidence = {
        # The ten-symbol receipt lives at source level; the per-source
        # identity digest binds the lineage material for this window.
        "receipt_id": source.receipt_id,
        "lineage_sha256": source.identity_sha256,
        "data_through": _iso(source.data_through),
        "observed_at": _iso(source.observed_at),
    }
    close = raw_bars[-1].get("close")
    if not isinstance(close, str) or not close:
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_anchor_price_invalid"
        )
    return converted, evidence, close


def _cross_section_context(
    snapshots: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Descriptive cross-sectional ranks; context only, never a hypothesis."""

    def returns(field: str) -> dict[str, Decimal]:
        values: dict[str, Decimal] = {}
        for symbol in _SYMBOLS:
            features = snapshots[symbol].get("features")
            if not isinstance(features, Mapping):
                raise CryptoTenSymbolFactorProjectionError(
                    "ten_symbol_factor_projection_record_invalid"
                )
            try:
                values[symbol] = Decimal(str(features.get(field)))
            except Exception as exc:
                raise CryptoTenSymbolFactorProjectionError(
                    "ten_symbol_factor_projection_record_invalid"
                ) from exc
        return values

    def ranks(values: Mapping[str, Decimal]) -> dict[str, int]:
        return {
            symbol: 1
            + sum(1 for other in _SYMBOLS if values[other] > values[symbol])
            for symbol in _SYMBOLS
        }

    return_1h = returns("return_1h")
    return_15m = returns("return_15m")
    return {
        "context_role": "descriptive_cross_section_context_only",
        "is_research_hypothesis": False,
        "adds_new_hypothesis": False,
        "rank_order": "1_is_highest_return_ties_share_rank",
        "symbol_order": list(_SYMBOLS),
        "return_1h_rank": ranks(return_1h),
        "return_15m_rank": ranks(return_15m),
        "return_1h_spread": format(max(return_1h.values()) - min(return_1h.values()), "f"),
        "return_15m_spread": format(
            max(return_15m.values()) - min(return_15m.values()), "f"
        ),
    }


def _record(unit: Mapping[str, Any], *, segment_id: str) -> dict[str, Any]:
    observation = unit["observation"]
    observation_id = str(unit["observation_id"])
    snapshots: dict[str, dict[str, Any]] = {}
    prices: dict[str, str] = {}
    for symbol in _SYMBOLS:
        bars, evidence, price = _factor_input(unit, symbol)
        snapshots[symbol] = build_factor_snapshot(
            observation_id=observation_id,
            symbol=symbol,
            bars=bars,
            evidence=evidence,
            supported_symbols=_SYMBOLS,
            feature_set_id=TEN_SYMBOL_FACTOR_SET_ID,
            feature_set_version=TEN_SYMBOL_FACTOR_SET_VERSION,
        )
        prices[symbol] = price
    if not isinstance(segment_id, str) or not segment_id.startswith(
        "crypto-5m-segment-"
    ):
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_segment_invalid"
        )
    record = {
        "contract": TEN_SYMBOL_FACTOR_PROJECTION_CONTRACT,
        "event_type": "factor_projection",
        "observation_id": observation_id,
        "source_event_checksum": unit["source_event_checksum"],
        "source_observation_sha256": observation.observation_sha256,
        "source_bars_sidecar_sha256": unit["sidecar_sha256"],
        "market": "crypto",
        "market_slot": _iso(unit["slot"]),
        "window_end": _iso(unit["window_end"]),
        "catalog_version": observation.catalog_version,
        "profile_sha256": unit["event"].get("profile_sha256"),
        "segment_id": segment_id,
        "segmented_learning_consumer_profile_id": (
            SEGMENTED_LEARNING_CONSUMER_PROFILE_ID
        ),
        "snapshots": snapshots,
        "cross_section_context": _cross_section_context(snapshots),
        "label_anchor_prices": prices,
        **_non_authority_fields(),
    }
    record["factor_projection_sha256"] = _sha256(record)
    return record


def _receipt(record: Mapping[str, Any]) -> dict[str, Any]:
    row = {
        "contract": TEN_SYMBOL_FACTOR_PROJECTION_RECEIPT_CONTRACT,
        "observation_id": record["observation_id"],
        "source_event_checksum": record["source_event_checksum"],
        "source_bars_sidecar_sha256": record["source_bars_sidecar_sha256"],
        "factor_projection_sha256": record["factor_projection_sha256"],
        **_non_authority_fields(),
    }
    row["projection_receipt_sha256"] = _sha256(row)
    return row


def _checkpoint(
    sequence: int,
    previous: str | None,
    unit: Mapping[str, Any],
    *,
    segment_id: str,
    receipt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    eligible = bool(unit["eligible"])
    if eligible and receipt is None:
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_checkpoint_invalid"
        )
    row = {
        "contract": TEN_SYMBOL_FACTOR_PROJECTION_CHECKPOINT_CONTRACT,
        "sequence": sequence,
        "previous_checkpoint_sha256": previous,
        "observation_id": unit["observation_id"],
        "source_event_checksum": unit["source_event_checksum"],
        "market_slot": _iso(unit["slot"]),
        "segment_id": segment_id,
        "projection_outcome": "projected" if eligible else "sidecar_ineligible",
        "ineligible_reason": None if eligible else unit["ineligible_reason"],
        "projection_receipt_sha256": (
            receipt["projection_receipt_sha256"] if receipt is not None else None
        ),
        **_non_authority_fields(),
    }
    row["checkpoint_sha256"] = _sha256(row)
    return row


def _read_checkpoints(evolution: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((evolution / "checkpoints").glob("*.json")):
        row = _parse_canonical(
            path, reason="ten_symbol_factor_projection_checkpoint_invalid"
        )
        material = dict(row)
        claimed = material.pop("checkpoint_sha256", None)
        expected = len(rows) + 1
        previous = rows[-1]["checkpoint_sha256"] if rows else None
        if (
            path.name != f"{expected:012d}.json"
            or row.get("contract") != TEN_SYMBOL_FACTOR_PROJECTION_CHECKPOINT_CONTRACT
            or row.get("sequence") != expected
            or row.get("previous_checkpoint_sha256") != previous
            or row.get("projection_outcome")
            not in {"projected", "sidecar_ineligible"}
            or claimed != _sha256(material)
            or any(
                row.get(key) != value for key, value in _non_authority_fields().items()
            )
        ):
            raise CryptoTenSymbolFactorProjectionError(
                "ten_symbol_factor_projection_checkpoint_invalid"
            )
        rows.append(row)
    return rows


def _latest_checkpoint(evolution: Path) -> tuple[int, dict[str, Any] | None]:
    """Bounded routine checkpoint read; daily full scrub validates the full chain."""

    paths = sorted((evolution / "checkpoints").glob("*.json"))
    if not paths:
        return 0, None
    sequence = len(paths)
    path = paths[-1]
    row = _parse_canonical(
        path, reason="ten_symbol_factor_projection_checkpoint_invalid"
    )
    material = dict(row)
    claimed = material.pop("checkpoint_sha256", None)
    if (
        path.name != f"{sequence:012d}.json"
        or row.get("contract") != TEN_SYMBOL_FACTOR_PROJECTION_CHECKPOINT_CONTRACT
        or row.get("sequence") != sequence
        or claimed != _sha256(material)
        or any(row.get(key) != value for key, value in _non_authority_fields().items())
    ):
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_checkpoint_invalid"
        )
    return sequence, row


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def _labels(
    root: Path,
    record: Mapping[str, Any],
    future_records: Mapping[str, Mapping[str, Any]],
) -> int:
    created_or_verified = 0
    snapshots = record["snapshots"]
    prices = record["label_anchor_prices"]
    for symbol in _SYMBOLS:
        snapshot = snapshots[symbol]
        slot = _utc(
            snapshot.get("market_slot"),
            reason="ten_symbol_factor_projection_snapshot_slot_invalid",
        )
        for horizon in _HORIZONS:
            target = future_records.get(
                _iso(slot + timedelta(minutes=horizon))
            )
            if target is None:
                continue
            target_unit = target.get("unit")
            target_record = target.get("record")
            if not isinstance(target_unit, Mapping) or not isinstance(
                target_record, Mapping
            ):
                raise CryptoTenSymbolFactorProjectionError(
                    "ten_symbol_factor_projection_future_source_invalid"
                )
            if target_record.get("segment_id") != record.get("segment_id"):
                continue
            _, evidence, exit_price = _factor_input(target_unit, symbol)
            label = build_forward_label(
                snapshot=snapshot,
                horizon_minutes=horizon,
                future_market_slot=target_record["snapshots"][symbol]["market_slot"],
                entry_price=prices[symbol],
                exit_price=exit_price,
                future_evidence=evidence,
                feature_set_id=TEN_SYMBOL_FACTOR_SET_ID,
                feature_set_version=TEN_SYMBOL_FACTOR_SET_VERSION,
            )
            _write_immutable(
                _label_path(root, str(record["observation_id"]), symbol, horizon),
                label,
            )
            created_or_verified += 1
    return created_or_verified


def _learning_eligible_samples(
    root: Path,
    records: Mapping[str, Mapping[str, Any]],
    *,
    consumer_profile: Mapping[str, Any],
) -> tuple[list[tuple[Mapping[str, Any], Mapping[str, Any]]], list[str]]:
    """Use the registered required label only when it remains same-segment."""

    profile_id = consumer_profile.get("consumer_profile_id")
    required_horizon = consumer_profile.get("required_label_horizon_minutes")
    auxiliary_horizons = consumer_profile.get("auxiliary_attribution_horizons")
    if (
        profile_id != SEGMENTED_LEARNING_CONSUMER_PROFILE_ID
        or consumer_profile.get("feature_set_id") != TEN_SYMBOL_FACTOR_SET_ID
        or consumer_profile.get("feature_set_version") != TEN_SYMBOL_FACTOR_SET_VERSION
        or consumer_profile.get("symbols") != list(_SYMBOLS)
        or required_horizon != 60
        or auxiliary_horizons != list(_HORIZONS[1:])
    ):
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_consumer_profile_invalid"
        )
    samples: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    eligible_observation_ids: list[str] = []
    for item in records.values():
        record = item.get("record")
        if not isinstance(record, Mapping):
            raise CryptoTenSymbolFactorProjectionError(
                "ten_symbol_factor_projection_record_invalid"
            )
        observation_id = record.get("observation_id")
        segment_id = record.get("segment_id")
        if (
            not isinstance(observation_id, str)
            or not isinstance(segment_id, str)
            or record.get("segmented_learning_consumer_profile_id") != profile_id
        ):
            raise CryptoTenSymbolFactorProjectionError(
                "ten_symbol_factor_projection_record_invalid"
            )
        observation_eligible = True
        symbol_samples: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for symbol in _SYMBOLS:
            snapshot = record.get("snapshots", {}).get(symbol)
            if not isinstance(snapshot, Mapping):
                raise CryptoTenSymbolFactorProjectionError(
                    "ten_symbol_factor_projection_record_invalid"
                )
            slot = _utc(
                snapshot.get("market_slot"),
                reason="ten_symbol_factor_projection_snapshot_slot_invalid",
            )
            target = records.get(_iso(slot + timedelta(minutes=required_horizon)))
            if (
                not isinstance(target, Mapping)
                or not isinstance(target.get("record"), Mapping)
                or target["record"].get("segment_id") != segment_id
            ):
                observation_eligible = False
                break
            path = _label_path(root, observation_id, symbol, required_horizon)
            if not path.exists():
                observation_eligible = False
                break
            label = _parse_canonical(
                path, reason="ten_symbol_factor_projection_label_invalid"
            )
            material = dict(label)
            claimed = material.pop("forward_label_sha256", None)
            if (
                label.get("observation_id") != observation_id
                or label.get("symbol") != symbol
                or label.get("horizon_minutes") != required_horizon
                or label.get("source_factor_snapshot_sha256")
                != snapshot.get("factor_snapshot_sha256")
                or claimed != _sha256(material)
            ):
                raise CryptoTenSymbolFactorProjectionError(
                    "ten_symbol_factor_projection_label_invalid"
                )
            symbol_samples.append((snapshot, label))
        if observation_eligible:
            samples.extend(symbol_samples)
            eligible_observation_ids.append(observation_id)
    return samples, eligible_observation_ids


# ---------------------------------------------------------------------------
# Full scrub
# ---------------------------------------------------------------------------


def _deferred_time_budget_result(
    *,
    observation_count: int,
    recovered: int,
    verified_record_count: int,
    verified_label_source_count: int,
    label_count: int,
    checkpoints: list[dict[str, Any]],
) -> dict[str, Any]:
    return _result(
        status="deferred_time_budget",
        observation_count=observation_count,
        recovered_observation_count=recovered,
        verified_record_count=verified_record_count,
        verified_label_source_count=verified_label_source_count,
        label_count=label_count,
        checkpoint_head_sha256=(
            checkpoints[-1]["checkpoint_sha256"] if checkpoints else None
        ),
    )


def run_crypto_ten_symbol_factor_research_full_scrub(
    *,
    output_root: Path | str,
    _deadline: float | None = None,
) -> dict[str, Any]:
    """Full-scrub independent contiguous segments without crossing a gap."""

    _assert_simulation_only()
    deadline = (
        _deadline
        if _deadline is not None
        else monotonic() + _FULL_SCRUB_MAX_SECONDS
    )
    root = Path(output_root)
    policy = _segmented_learning_policy()
    consumer_profile = _segmented_learning_consumer_profile()
    store = _open_store(root)
    if store.pending_record_read_only() is not None:
        return _result(status="deferred_core_pending")
    units = _build_units(store, deadline=deadline)
    if units is None:
        return _result(
            status="deferred_inventory_time_budget", inventory_complete=False
        )
    if not units:
        return _result(status="deferred_core_pending")
    continuous = _latest_continuous(units)
    segments = _segment_ids(units)
    if monotonic() >= deadline:
        return _result(
            status="deferred_inventory_time_budget", inventory_complete=False
        )
    try:
        evolution = _ensure_root(root)
        with _lock(evolution):
            checkpoints = _read_checkpoints(evolution)
            if len(checkpoints) > len(units):
                raise CryptoTenSymbolFactorProjectionError(
                    "ten_symbol_factor_projection_checkpoint_orphaned"
                )
            records: dict[str, dict[str, Any]] = {}
            recovered = 0
            ineligible_slot_count = 0
            last_checkpoint_sha: str | None = None
            for sequence, unit in enumerate(units, start=1):
                if monotonic() >= deadline:
                    return _deferred_time_budget_result(
                        observation_count=len(units),
                        recovered=recovered,
                        verified_record_count=sequence - 1,
                        verified_label_source_count=0,
                        label_count=0,
                        checkpoints=checkpoints,
                    )
                observation_id = str(unit["observation_id"])
                segment_id = segments[observation_id]
                record: dict[str, Any] | None = None
                receipt: dict[str, Any] | None = None
                if unit["eligible"]:
                    record = _record(unit, segment_id=segment_id)
                    receipt = _receipt(record)
                else:
                    ineligible_slot_count += 1
                checkpoint = _checkpoint(
                    sequence,
                    last_checkpoint_sha,
                    unit,
                    segment_id=segment_id,
                    receipt=receipt,
                )
                last_checkpoint_sha = str(checkpoint["checkpoint_sha256"])
                paths = _paths(root, observation_id)
                if sequence <= len(checkpoints):
                    claimed = checkpoints[sequence - 1]
                    if _canonical_json(claimed) != _canonical_json(checkpoint):
                        raise CryptoTenSymbolFactorProjectionError(
                            "ten_symbol_factor_projection_checkpoint_source_mismatch"
                        )
                    if unit["eligible"]:
                        assert record is not None and receipt is not None
                        if any(
                            not path.is_file() or path.is_symlink()
                            for path in paths.values()
                        ):
                            raise CryptoTenSymbolFactorProjectionError(
                                "ten_symbol_factor_projection_claimed_record_missing"
                            )
                        for name, payload in (
                            ("record", record),
                            ("receipt", receipt),
                        ):
                            path = paths[name]
                            if _canonical_json(
                                _parse_canonical(
                                    path,
                                    reason="ten_symbol_factor_projection_record_invalid",
                                )
                            ) != _canonical_json(payload):
                                raise CryptoTenSymbolFactorProjectionError(
                                    "ten_symbol_factor_projection_not_derived"
                                )
                            _write_immutable(path, payload)
                    elif any(path.exists() or path.is_symlink() for path in paths.values()):
                        raise CryptoTenSymbolFactorProjectionError(
                            "ten_symbol_factor_projection_not_derived"
                        )
                else:
                    if unit["eligible"]:
                        assert record is not None and receipt is not None
                        _write_immutable(paths["record"], record)
                        _write_immutable(paths["receipt"], receipt)
                    _write_immutable(
                        evolution / "checkpoints" / f"{sequence:012d}.json",
                        checkpoint,
                    )
                    checkpoints.append(checkpoint)
                    recovered += 1
                if unit["eligible"]:
                    assert record is not None
                    records[_iso(unit["slot"])] = {
                        "record": record,
                        "unit": unit,
                    }
            label_count = 0
            verified_label_source_count = 0
            for item in records.values():
                if monotonic() >= deadline:
                    return _deferred_time_budget_result(
                        observation_count=len(units),
                        recovered=recovered,
                        verified_record_count=len(records),
                        verified_label_source_count=verified_label_source_count,
                        label_count=label_count,
                        checkpoints=checkpoints,
                    )
                label_count += _labels(root, item["record"], records)
                verified_label_source_count += 1
            if monotonic() >= deadline:
                return _deferred_time_budget_result(
                    observation_count=len(units),
                    recovered=recovered,
                    verified_record_count=len(records),
                    verified_label_source_count=verified_label_source_count,
                    label_count=label_count,
                    checkpoints=checkpoints,
                )
            samples, eligible_observation_ids = _learning_eligible_samples(
                root,
                records,
                consumer_profile=consumer_profile,
            )
            if monotonic() >= deadline:
                return _deferred_time_budget_result(
                    observation_count=len(units),
                    recovered=recovered,
                    verified_record_count=len(records),
                    verified_label_source_count=verified_label_source_count,
                    label_count=label_count,
                    checkpoints=checkpoints,
                )
            report = evaluate_factor_hypotheses(
                samples,
                feature_set_id=TEN_SYMBOL_FACTOR_SET_ID,
                feature_set_version=TEN_SYMBOL_FACTOR_SET_VERSION,
            )
    except CryptoFactorResearchError as exc:
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_factor_input_invalid"
        ) from exc
    return _result(
        status="recovered" if recovered else "scrubbed",
        observation_count=len(units),
        ineligible_slot_count=ineligible_slot_count,
        recovered_observation_count=recovered,
        label_count=label_count,
        hypothesis_report=report,
        checkpoint_head_sha256=checkpoints[-1]["checkpoint_sha256"],
        latest_continuous_completion_count=continuous,
        operational_maturity=continuous
        >= policy["operational_maturity_minimum_continuous_slots"],
        segmented_learning_policy=policy,
        segmented_learning_profile=consumer_profile,
        label_learning_eligible_sample_count=len(samples),
        label_learning_eligible_observation_ids=eligible_observation_ids,
    )


# ---------------------------------------------------------------------------
# Incremental
# ---------------------------------------------------------------------------


def run_crypto_ten_symbol_factor_research_incremental(
    *, output_root: Path | str
) -> dict[str, Any]:
    """Project new unlabelled observations after a verified full-scrub base.

    This routine deliberately reads only the verified event chain, the
    bounded backlog of unprojected terminal units, and the preceding
    projection checkpoint head.  It never queries TradingDatas and never
    retroactively scans history for labels; the daily full scrub performs
    that complete semantic validation and label completion.  A backlog is
    worked off in slot order, at most ``MAX_CATCHUP_UNITS`` per invocation,
    keeping exactly one checkpoint per terminal unit; a round that still
    lags reports ``backlog_remaining`` and the next round continues from the
    following sequence without ever skipping a slot.
    """

    _assert_simulation_only()
    root = Path(output_root)
    policy = _segmented_learning_policy()
    consumer_profile = _segmented_learning_consumer_profile()
    store = _open_store(root)
    if store.pending_record_read_only() is not None:
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_core_pending"
        )
    units = _terminal_events(store)
    unit_count = len(units)
    evolution = _root(root)
    if not evolution.exists():
        return _result(
            status="full_scrub_required",
            observation_count=unit_count,
            label_count=0,
            reason="ten_symbol_factor_projection_checkpoint_missing",
            segmented_learning_policy=policy,
            segmented_learning_profile=consumer_profile,
            label_learning_eligible_sample_count=0,
        )
    try:
        with _lock(evolution):
            sequence, previous_checkpoint = _latest_checkpoint(evolution)
            if unit_count < sequence:
                raise CryptoTenSymbolFactorProjectionError(
                    "ten_symbol_factor_projection_checkpoint_orphaned"
                )
            if unit_count == sequence:
                if sequence > 0:
                    assert previous_checkpoint is not None
                    last = units[-1]
                    if (
                        previous_checkpoint.get("observation_id")
                        != last["observation_id"]
                        or previous_checkpoint.get("source_event_checksum")
                        != last["source_event_checksum"]
                    ):
                        raise CryptoTenSymbolFactorProjectionError(
                            "ten_symbol_factor_projection_incremental_source_mismatch"
                        )
                return _result(
                    status="up_to_date",
                    observation_count=unit_count,
                    label_count=0,
                    segmented_learning_policy=policy,
                    segmented_learning_profile=consumer_profile,
                    label_learning_eligible_sample_count=0,
                )
            catchup_units = units[sequence : sequence + MAX_CATCHUP_UNITS]
            remaining_count = unit_count - sequence - len(catchup_units)
            previous_slot: datetime | None = None
            previous_outcome: str | None = None
            previous_segment_id: str | None = None
            previous_checkpoint_sha: str | None = None
            if previous_checkpoint is not None:
                previous_slot = _utc(
                    previous_checkpoint.get("market_slot"),
                    reason="ten_symbol_factor_projection_checkpoint_invalid",
                )
                previous_outcome = previous_checkpoint.get("projection_outcome")
                if previous_outcome not in {"projected", "sidecar_ineligible"}:
                    raise CryptoTenSymbolFactorProjectionError(
                        "ten_symbol_factor_projection_checkpoint_invalid"
                    )
                previous_segment_id = previous_checkpoint.get("segment_id")
                if not isinstance(previous_segment_id, str):
                    raise CryptoTenSymbolFactorProjectionError(
                        "ten_symbol_factor_projection_segment_invalid"
                    )
                previous_checkpoint_sha = previous_checkpoint["checkpoint_sha256"]
            projected_count = 0
            for offset, unit in enumerate(catchup_units, start=1):
                _attach_eligibility(store, unit)
                if (
                    previous_slot is None
                    or previous_outcome != "projected"
                    or previous_segment_id is None
                    or unit["slot"] - previous_slot != _FIVE_MINUTES
                ):
                    segment_id = "crypto-5m-segment-" + unit["slot"].strftime(
                        "%Y%m%dT%H%M%SZ"
                    )
                else:
                    segment_id = previous_segment_id
                record = None
                receipt = None
                if unit["eligible"]:
                    record = _record(unit, segment_id=segment_id)
                    receipt = _receipt(record)
                    paths = _paths(root, str(unit["observation_id"]))
                    _write_immutable(paths["record"], record)
                    _write_immutable(paths["receipt"], receipt)
                new_checkpoint = _checkpoint(
                    sequence + offset,
                    previous_checkpoint_sha,
                    unit,
                    segment_id=segment_id,
                    receipt=receipt,
                )
                _write_immutable(
                    evolution / "checkpoints" / f"{sequence + offset:012d}.json",
                    new_checkpoint,
                )
                previous_slot = unit["slot"]
                previous_outcome = (
                    "projected" if unit["eligible"] else "sidecar_ineligible"
                )
                previous_segment_id = segment_id
                previous_checkpoint_sha = str(new_checkpoint["checkpoint_sha256"])
                projected_count += 1
    except CryptoFactorResearchError as exc:
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_factor_input_invalid"
        ) from exc
    except CryptoTenSymbolFactorProjectionError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_incremental_invalid"
        ) from exc
    return _result(
        status=(
            "projected_incremental" if remaining_count == 0 else "backlog_remaining"
        ),
        observation_count=unit_count,
        projected_count=projected_count,
        remaining_count=remaining_count,
        label_count=0,
        label_status="observation_only_pending_daily_scrub",
        segmented_learning_policy=policy,
        segmented_learning_profile=consumer_profile,
        label_learning_eligible_sample_count=0,
    )


def ten_symbol_factor_projection_exit_code(result: Mapping[str, Any]) -> int:
    if not isinstance(result, Mapping) or result.get("status") not in {
        "recovered",
        "scrubbed",
        "projected_incremental",
        "backlog_remaining",
        "up_to_date",
        "full_scrub_required",
        "deferred_core_pending",
        "deferred_inventory_time_budget",
        "deferred_time_budget",
    }:
        return 2
    return (
        0
        if all(
            result.get(key) == value for key, value in _non_authority_fields().items()
        )
        else 2
    )


__all__ = [
    "CryptoTenSymbolFactorProjectionError",
    "MAX_CATCHUP_UNITS",
    "OPERATIONAL_MATURITY_CONTINUOUS_COMPLETIONS",
    "SEGMENTED_LEARNING_CONSUMER_PROFILE_ID",
    "TEN_SYMBOL_FACTOR_PROJECTION_CONTRACT",
    "run_crypto_ten_symbol_factor_research_full_scrub",
    "run_crypto_ten_symbol_factor_research_incremental",
    "ten_symbol_factor_projection_exit_code",
]
