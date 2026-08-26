"""Detached, non-authoritative learning projections for a round-trip epoch.

The five-minute round-trip runtime is the only writer of observations,
decisions and simulated capital.  This module reads that evidence after a
completion is durable, then writes a separate append-only learning projection.
It has no order, capital, Champion, network, or promotion authority.

Incremental work is bounded and resumable.  A full scrub is the integrity
boundary: it validates every completion to projection mapping and
deterministically fills only projections that have never been checkpointed.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from time import monotonic
from typing import Any, Iterator, Mapping
import uuid

from Crypto.delayed_paper_ledger import (
    CryptoDelayedPaperLedgerError,
    CryptoDelayedPaperObservationStore,
    _market_slot,
    _read_json,
)
from Crypto.fixture_sim.contracts import _assert_simulation_only


ROUND_TRIP_LEARNING_CONTRACT = "tradingagent.crypto.round_trip_learning.v1"
ROUND_TRIP_LEARNING_RECEIPT_CONTRACT = (
    "tradingagent.crypto.round_trip_learning_receipt.v1"
)
ROUND_TRIP_LEARNING_CHECKPOINT_CONTRACT = (
    "tradingagent.crypto.round_trip_learning_checkpoint.v1"
)
ROUND_TRIP_LEARNING_SCRUB_CONTRACT = "tradingagent.crypto.round_trip_learning_scrub.v1"
ROUND_TRIP_LEARNING_INVENTORY_CACHE_CONTRACT = (
    "tradingagent.crypto.round_trip_learning_inventory_cache.v1"
)
_MAX_FILE_BYTES = 2 * 1024 * 1024
# The suffix cache stays well below _MAX_FILE_BYTES; past this size the full
# scan fallback simply runs without caching instead of risking parse limits.
_INVENTORY_CACHE_MAX_IDS = 30000
_SYMBOLS = ("BTCUSDT", "ETHUSDT")
ROUND_TRIP_LEARNING_FULL_SCRUB_MAX_SECONDS = 90.0
# The production incremental oneshot has a 45 second systemd start timeout.
# Keep a deliberate margin so inventory/head validation can return a durable
# deferral instead of being terminated before the suffix cursor advances.
ROUND_TRIP_LEARNING_INCREMENTAL_MAX_SECONDS = 30.0
ROUND_TRIP_LEARNING_INCREMENTAL_MAX_RECORDS = 8


class CryptoRoundTripLearningError(RuntimeError):
    """Stable fail-closed error for a round-trip learning projection."""


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
        raise CryptoRoundTripLearningError(
            "round_trip_learning_payload_invalid"
        ) from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _non_authority_fields() -> dict[str, Any]:
    return {
        "authority": "none",
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
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "outbox_id": None,
        "capital_commit_id": None,
    }


def _result(*, status: str, **fields: Any) -> dict[str, Any]:
    return {
        "contract": ROUND_TRIP_LEARNING_CONTRACT,
        "status": status,
        "learning_mode": "detached_offline_worker",
        "manual_review_required": False,
        "automatic_champion_replacement": False,
        **fields,
        **_non_authority_fields(),
    }


def _learning_root(root: Path) -> Path:
    return root / "evolution" / "round_trip_learning"


def _paths(root: Path, observation_id: str) -> dict[str, Path]:
    evolution = _learning_root(root)
    return {
        "sample": evolution / "samples" / f"{observation_id}.json",
        "kpi": evolution / "kpis" / f"{observation_id}.json",
        "challenger": evolution / "challengers" / f"{observation_id}.json",
        "receipt": evolution / "receipts" / f"{observation_id}.json",
    }


def _assert_regular(path: Path, *, reason: str) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise CryptoRoundTripLearningError(reason) from exc
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
            raise CryptoRoundTripLearningError(reason)
        encoded = os.read(descriptor, _MAX_FILE_BYTES + 1)
        after = os.fstat(descriptor)
        if len(encoded) != before.st_size or after.st_size != before.st_size:
            raise CryptoRoundTripLearningError(reason)
    except CryptoRoundTripLearningError:
        raise
    except OSError as exc:
        raise CryptoRoundTripLearningError(reason) from exc
    finally:
        os.close(descriptor)
    if not encoded.endswith(b"\n") or b"\x00" in encoded:
        raise CryptoRoundTripLearningError(reason)
    return encoded


def _parse_canonical(path: Path, *, reason: str) -> dict[str, Any]:
    encoded = _assert_regular(path, reason=reason)
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoRoundTripLearningError(reason) from exc
    if (
        not isinstance(payload, dict)
        or encoded != (_canonical_json(payload) + "\n").encode()
    ):
        raise CryptoRoundTripLearningError(reason)
    return payload


def _ensure_evolution_root(root: Path) -> Path:
    evolution = _learning_root(root)
    parent = root / "evolution"
    if parent.exists():
        if parent.is_symlink() or not parent.is_dir():
            raise CryptoRoundTripLearningError("round_trip_learning_directory_invalid")
    else:
        parent.mkdir(mode=0o700)
    for directory in (
        evolution,
        evolution / "samples",
        evolution / "kpis",
        evolution / "challengers",
        evolution / "receipts",
        evolution / "checkpoints",
        evolution / "scrubs",
    ):
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise CryptoRoundTripLearningError(
                    "round_trip_learning_directory_invalid"
                )
        else:
            directory.mkdir(mode=0o700)
    return evolution


@contextmanager
def _learning_lock(evolution: Path) -> Iterator[None]:
    path = evolution / ".lock"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        metadata = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
        ):
            raise CryptoRoundTripLearningError("round_trip_learning_lock_invalid")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except CryptoRoundTripLearningError:
        raise
    except OSError as exc:
        raise CryptoRoundTripLearningError("round_trip_learning_lock_invalid") from exc
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _write_immutable(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    if path.exists() or path.is_symlink():
        if (
            _assert_regular(path, reason="round_trip_learning_immutable_conflict")
            != encoded
        ):
            raise CryptoRoundTripLearningError("round_trip_learning_immutable_conflict")
        return
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
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
            _assert_regular(path, reason="round_trip_learning_immutable_conflict")
            != encoded
        ):
            raise CryptoRoundTripLearningError("round_trip_learning_immutable_conflict")
    except OSError as exc:
        raise CryptoRoundTripLearningError("round_trip_learning_write_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_state(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "xb") as stream:
            os.chmod(temporary, 0o600)
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
        raise CryptoRoundTripLearningError(
            "round_trip_learning_state_write_failed"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _indexed_event(
    *,
    store: CryptoDelayedPaperObservationStore,
    observation_id: str,
    symbol: str,
    completion: Mapping[str, Any],
    strict_ledger_membership: bool,
) -> dict[str, Any]:
    path = store.observation_event_index_dir / observation_id / f"{symbol}.json"
    row = _read_json(path)
    if strict_ledger_membership:
        row = store._verify_indexed_event(row)
    else:
        material = dict(row)
        claimed = material.pop("checksum", None)
        references = completion.get("bundle_references")
        reference = references.get(symbol) if isinstance(references, Mapping) else None
        if (
            not isinstance(reference, Mapping)
            or claimed != _sha256(material)
            or row.get("event_type") != "decision"
            or row.get("observation_id") != observation_id
            or row.get("symbol") != symbol
            or row.get("run_id") != reference.get("run_id")
            or row.get("business_bundle_sha256")
            != reference.get("business_bundle_sha256")
            or row.get("decision_id") != reference.get("decision_id")
            or row.get("execution_authority") is not False
            or row.get("production_eligible") is not False
            or row.get("real_trading_enabled") is not False
            or row.get("outbox_id") is not None
            or row.get("capital_commit_id") is not None
        ):
            raise CryptoRoundTripLearningError(
                "round_trip_learning_decision_index_invalid"
            )
    if row.get("observation_id") != observation_id or row.get("symbol") != symbol:
        raise CryptoRoundTripLearningError("round_trip_learning_decision_index_invalid")
    return row


def _source_record(
    store: CryptoDelayedPaperObservationStore,
    observation_id: str,
    *,
    strict_ledger_membership: bool,
) -> dict[str, Any]:
    try:
        observation = _read_json(store._observation_path(observation_id))
        completion = _read_json(store._completion_path(observation_id))
        store._verify_observation(observation)
        store._verify_completion(completion, observation=observation)
        indexed = store.observation_event_index_dir / observation_id
        if not indexed.is_dir() or indexed.is_symlink():
            raise CryptoRoundTripLearningError(
                "round_trip_learning_decision_index_missing"
            )
        events: list[dict[str, Any]] = []
        for symbol in _SYMBOLS:
            row = _indexed_event(
                store=store,
                observation_id=observation_id,
                symbol=symbol,
                completion=completion,
                strict_ledger_membership=strict_ledger_membership,
            )
            events.append(row)
    except CryptoRoundTripLearningError:
        raise
    except (CryptoDelayedPaperLedgerError, OSError, ValueError) as exc:
        raise CryptoRoundTripLearningError(
            "round_trip_learning_source_invalid"
        ) from exc
    completion_sha256 = completion.get("completion_sha256")
    if (
        completion.get("status") != "completed"
        or not isinstance(completion_sha256, str)
        or len(completion_sha256) != 64
        or observation.get("observation_id") != observation_id
        or completion.get("observation_id") != observation_id
        or len({event.get("event_id") for event in events}) != 2
    ):
        raise CryptoRoundTripLearningError("round_trip_learning_source_invalid")
    return {
        "observation": observation,
        "completion": completion,
        "events": events,
        "source_completion_sha256": completion_sha256,
    }


def _core_snapshot(
    root: Path,
) -> tuple[CryptoDelayedPaperObservationStore, dict[str, Any], dict[str, Any]]:
    try:
        store = CryptoDelayedPaperObservationStore(root)
        checkpoint = store.runtime_checkpoint_read_only()
        state = store._observation_state_read_only()
    except (CryptoDelayedPaperLedgerError, OSError, ValueError) as exc:
        raise CryptoRoundTripLearningError("round_trip_learning_core_invalid") from exc
    return store, checkpoint, state


def _completion_inventory(
    store: CryptoDelayedPaperObservationStore,
    checkpoint: Mapping[str, Any],
    core_state: Mapping[str, Any],
    *,
    deadline: float,
) -> list[str] | None:
    completed: list[tuple[Any, str]] = []
    for path in store.completions_dir.glob("*.json"):
        if monotonic() >= deadline:
            return None
        source = _source_record(store, path.stem, strict_ledger_membership=True)
        completed.append(
            (_market_slot(source["observation"].get("market_slot")), path.stem)
        )
    completed.sort()
    ids = [observation_id for _, observation_id in completed]
    if (
        len(ids) != int(checkpoint.get("completion_count") or -1)
        or checkpoint.get("completion_count") != checkpoint.get("observation_count")
        or (ids and ids[-1] != core_state.get("latest_observation_id"))
    ):
        raise CryptoRoundTripLearningError("round_trip_learning_core_inventory_invalid")
    return ids


def _inventory_cache_path(evolution: Path) -> Path:
    return evolution / "inventory_cache.json"


def _load_inventory_cache_ids(evolution: Path) -> list[str] | None:
    """Return the persisted ordered id cache, or ``None`` when unusable.

    The cache is a pure accelerator: every caller must treat ``None`` as
    "run the full scan", so correctness never depends on this file.
    """

    path = _inventory_cache_path(evolution)
    if not path.exists() or path.is_symlink():
        return None
    try:
        row = _parse_canonical(path, reason="round_trip_learning_state_invalid")
    except CryptoRoundTripLearningError:
        return None
    material = dict(row)
    claimed = material.pop("inventory_cache_sha256", None)
    ids = row.get("inventory_ids")
    if (
        claimed != _sha256(material)
        or row.get("contract") != ROUND_TRIP_LEARNING_INVENTORY_CACHE_CONTRACT
        or not isinstance(ids, list)
        or any(not isinstance(item, str) or not item for item in ids)
        or len(set(ids)) != len(ids)
    ):
        return None
    return ids


def _store_inventory_cache(evolution: Path, ids: list[str]) -> None:
    """Persist the ordered id list; a cache write failure is never fatal."""

    if len(ids) > _INVENTORY_CACHE_MAX_IDS:
        return
    payload: dict[str, Any] = {
        "contract": ROUND_TRIP_LEARNING_INVENTORY_CACHE_CONTRACT,
        "inventory_ids": list(ids),
    }
    payload["inventory_cache_sha256"] = _sha256(payload)
    try:
        _write_state(_inventory_cache_path(evolution), payload)
    except CryptoRoundTripLearningError:
        return


def _completion_inventory_cached(
    store: CryptoDelayedPaperObservationStore,
    checkpoint: Mapping[str, Any],
    core_state: Mapping[str, Any],
    *,
    evolution: Path,
    deadline: float,
) -> list[str] | None:
    """Ordered completion ids via the suffix cache when possible.

    Steady state appends one completion per five-minute slot, so re-reading
    every historical record each run made the incremental cost O(history)
    while processing O(new) records.  The cache shortens that to loading
    only the appended suffix; any structural surprise (missing cache,
    shrinkage, out-of-order arrival) falls back to the full-scan inventory,
    which rebuilds the cache.
    """

    try:
        names = {path.stem for path in store.completions_dir.glob("*.json")}
        cached = _load_inventory_cache_ids(evolution)
        if cached and len(cached) < len(names) and set(cached) <= names:
            appended: list[tuple[Any, str]] = []
            for stem in sorted(names - set(cached)):
                if monotonic() >= deadline:
                    return None
                source = _source_record(store, stem, strict_ledger_membership=True)
                appended.append(
                    (_market_slot(source["observation"].get("market_slot")), stem)
                )
            appended.sort()
            tail_source = _source_record(
                store, cached[-1], strict_ledger_membership=True
            )
            tail_slot = _market_slot(tail_source["observation"].get("market_slot"))
            if appended[0][0] >= tail_slot:
                ids = [*cached, *(stem for _, stem in appended)]
                if (
                    len(ids) == int(checkpoint.get("completion_count") or -1)
                    and checkpoint.get("completion_count")
                    == checkpoint.get("observation_count")
                    and ids[-1] == core_state.get("latest_observation_id")
                ):
                    return ids
    except (AttributeError, OSError, CryptoRoundTripLearningError):
        # The cache is advisory; any surprise falls through to the enforced
        # full-scan inventory below.
        pass
    return _completion_inventory(store, checkpoint, core_state, deadline=deadline)


def _projection(
    source: Mapping[str, Any], *, legacy_manual_gate: bool = False
) -> dict[str, dict[str, Any]]:
    observation = source["observation"]
    completion = source["completion"]
    events = source["events"]
    observation_id = str(observation["observation_id"])
    completion_sha256 = str(source["source_completion_sha256"])
    samples = {
        "contract": ROUND_TRIP_LEARNING_CONTRACT,
        "event_type": "simulation_sample",
        "observation_id": observation_id,
        "market_slot": observation.get("market_slot"),
        "source_completion_sha256": completion_sha256,
        "symbol_decisions": [
            {
                "symbol": event["symbol"],
                "decision_action": event.get("decision_action"),
                "decision_reason": event.get("decision_reason"),
                "order_side": event.get("round_trip_order_side"),
                "receipt_status": event.get("round_trip_receipt_status"),
                "exit_reason": event.get("exit_reason"),
                "source_event_id": event.get("event_id"),
                "source_event_checksum": event.get("checksum"),
            }
            for event in events
        ],
        "label_status": "pending_simulation_outcome",
        "strategy_edge_established": False,
        **_non_authority_fields(),
    }
    samples["sample_sha256"] = _sha256(samples)
    kpi = {
        "contract": ROUND_TRIP_LEARNING_CONTRACT,
        "event_type": "learning_kpi",
        "scope": "single_observation",
        "observation_id": observation_id,
        "source_completion_sha256": completion_sha256,
        "decision_event_count": 2,
        "buy_decision_count": sum(
            event.get("round_trip_order_side") == "buy" for event in events
        ),
        "sell_decision_count": sum(
            event.get("round_trip_order_side") == "sell" for event in events
        ),
        "pending_outcome_count": 2,
        "realized_pnl": None,
        "win_rate": None,
        "strategy_edge_established": False,
        **_non_authority_fields(),
    }
    kpi["kpi_sha256"] = _sha256(kpi)
    if legacy_manual_gate:
        suggestion = "collect_audited_simulation_outcomes_before_manual_review"
        reason_codes = [
            "insufficient_independent_outcomes",
            "manual_review_required",
        ]
    else:
        suggestion = "continue_simulation_outcome_accumulation"
        reason_codes = [
            "insufficient_independent_outcomes",
            "deterministic_non_live_gate_pending",
        ]
    challenger = {
        "contract": ROUND_TRIP_LEARNING_CONTRACT,
        "event_type": "challenger_suggestion",
        "observation_id": observation_id,
        "source_completion_sha256": completion_sha256,
        "suggestion": suggestion,
        "reason_codes": reason_codes,
        "eligible_for_champion_replacement": False,
        "proposed_parameter_changes": [],
        **_non_authority_fields(),
    }
    challenger["challenger_sha256"] = _sha256(challenger)
    receipt = {
        "contract": ROUND_TRIP_LEARNING_RECEIPT_CONTRACT,
        "observation_id": observation_id,
        "market_slot": observation.get("market_slot"),
        "source_completion_sha256": completion_sha256,
        "source_observation_content_sha256": completion.get(
            "observation_content_sha256"
        ),
        "sample_sha256": samples["sample_sha256"],
        "kpi_sha256": kpi["kpi_sha256"],
        "challenger_sha256": challenger["challenger_sha256"],
        **_non_authority_fields(),
    }
    receipt["projection_receipt_sha256"] = _sha256(receipt)
    return {"sample": samples, "kpi": kpi, "challenger": challenger, "receipt": receipt}


def _verify_or_project(
    root: Path,
    store: CryptoDelayedPaperObservationStore,
    observation_id: str,
    *,
    strict_ledger_membership: bool,
) -> dict[str, Any]:
    source = _source_record(
        store, observation_id, strict_ledger_membership=strict_ledger_membership
    )
    material = _projection(source)
    legacy_material = _projection(source, legacy_manual_gate=True)
    paths = _paths(root, observation_id)
    existing: dict[str, dict[str, Any]] = {}
    for name, path in paths.items():
        if path.exists() or path.is_symlink():
            existing[name] = _parse_canonical(
                path, reason="round_trip_learning_projection_invalid"
            )
    if existing:
        variants = (material, legacy_material)
        matched = next(
            (
                variant
                for variant in variants
                if all(
                    _canonical_json(actual) == _canonical_json(variant[name])
                    for name, actual in existing.items()
                )
            ),
            None,
        )
        if matched is None:
            raise CryptoRoundTripLearningError(
                "round_trip_learning_projection_not_derived"
            )
        material = matched
    for name, path in paths.items():
        _write_immutable(path, material[name])
    return material["receipt"]


def _verify_projected_head(
    root: Path,
    store: CryptoDelayedPaperObservationStore,
    *,
    observation_id: str,
    checkpoint: Mapping[str, Any],
) -> None:
    if any(
        not path.is_file() or path.is_symlink()
        for path in _paths(root, observation_id).values()
    ):
        raise CryptoRoundTripLearningError(
            "round_trip_learning_claimed_projection_missing"
        )
    receipt = _verify_or_project(
        root, store, observation_id, strict_ledger_membership=True
    )
    if (
        checkpoint.get("observation_id") != observation_id
        or checkpoint.get("source_completion_sha256")
        != receipt["source_completion_sha256"]
        or checkpoint.get("projection_receipt_sha256")
        != receipt["projection_receipt_sha256"]
    ):
        raise CryptoRoundTripLearningError(
            "round_trip_learning_checkpoint_source_mismatch"
        )


def _checkpoint_payload(
    *, sequence: int, previous_sha256: str | None, receipt: Mapping[str, Any]
) -> dict[str, Any]:
    row = {
        "contract": ROUND_TRIP_LEARNING_CHECKPOINT_CONTRACT,
        "sequence": sequence,
        "previous_checkpoint_sha256": previous_sha256,
        "observation_id": receipt["observation_id"],
        "source_completion_sha256": receipt["source_completion_sha256"],
        "projection_receipt_sha256": receipt["projection_receipt_sha256"],
        **_non_authority_fields(),
    }
    row["checkpoint_sha256"] = _sha256(row)
    return row


def _read_checkpoints(evolution: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((evolution / "checkpoints").glob("*.json")):
        row = _parse_canonical(path, reason="round_trip_learning_checkpoint_invalid")
        material = dict(row)
        claimed = material.pop("checkpoint_sha256", None)
        expected_sequence = len(rows) + 1
        previous = rows[-1]["checkpoint_sha256"] if rows else None
        if (
            row.get("contract") != ROUND_TRIP_LEARNING_CHECKPOINT_CONTRACT
            or path.name != f"{expected_sequence:012d}.json"
            or row.get("sequence") != expected_sequence
            or row.get("previous_checkpoint_sha256") != previous
            or claimed != _sha256(material)
            or any(
                row.get(key) != value for key, value in _non_authority_fields().items()
            )
        ):
            raise CryptoRoundTripLearningError("round_trip_learning_checkpoint_invalid")
        rows.append(row)
    return rows


def _state_payload(
    checkpoints: list[Mapping[str, Any]], *, scrubbed_count: int
) -> dict[str, Any]:
    latest = checkpoints[-1] if checkpoints else None
    return _state_payload_from_head(
        projected_completion_count=len(checkpoints),
        latest_checkpoint=latest,
        scrubbed_count=scrubbed_count,
    )


def _state_payload_from_head(
    *,
    projected_completion_count: int,
    latest_checkpoint: Mapping[str, Any] | None,
    scrubbed_count: int,
) -> dict[str, Any]:
    row = {
        "contract": ROUND_TRIP_LEARNING_CONTRACT,
        "projected_completion_count": projected_completion_count,
        "checkpoint_head_sha256": (
            latest_checkpoint.get("checkpoint_sha256") if latest_checkpoint else None
        ),
        "latest_observation_id": (
            latest_checkpoint.get("observation_id") if latest_checkpoint else None
        ),
        "scrubbed_completion_count": scrubbed_count,
        **_non_authority_fields(),
    }
    row["worker_state_sha256"] = _sha256(row)
    return row


def _verify_state(
    evolution: Path, checkpoints: list[Mapping[str, Any]]
) -> dict[str, Any] | None:
    path = evolution / "worker_state.json"
    if not path.exists() and not path.is_symlink():
        return None
    row = _parse_canonical(path, reason="round_trip_learning_state_invalid")
    material = dict(row)
    claimed = material.pop("worker_state_sha256", None)
    if claimed != _sha256(material):
        raise CryptoRoundTripLearningError("round_trip_learning_state_invalid")
    if row.get("projected_completion_count") != len(checkpoints):
        raise CryptoRoundTripLearningError("round_trip_learning_state_invalid")
    return row


def _read_incremental_state(
    evolution: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    path = evolution / "worker_state.json"
    if not path.exists() and not path.is_symlink():
        return None, None
    row = _parse_canonical(path, reason="round_trip_learning_state_invalid")
    material = dict(row)
    claimed = material.pop("worker_state_sha256", None)
    projected = row.get("projected_completion_count")
    if (
        claimed != _sha256(material)
        or isinstance(projected, bool)
        or not isinstance(projected, int)
        or projected < 0
        or row.get("scrubbed_completion_count") != projected
        or any(row.get(key) != value for key, value in _non_authority_fields().items())
    ):
        raise CryptoRoundTripLearningError("round_trip_learning_state_invalid")
    checkpoint = None
    if projected:
        checkpoint = _parse_canonical(
            evolution / "checkpoints" / f"{projected:012d}.json",
            reason="round_trip_learning_checkpoint_invalid",
        )
        material = dict(checkpoint)
        claimed_checkpoint = material.pop("checkpoint_sha256", None)
        if (
            checkpoint.get("contract") != ROUND_TRIP_LEARNING_CHECKPOINT_CONTRACT
            or checkpoint.get("sequence") != projected
            or claimed_checkpoint != _sha256(material)
            or row.get("checkpoint_head_sha256") != claimed_checkpoint
            or row.get("latest_observation_id") != checkpoint.get("observation_id")
            or any(
                checkpoint.get(key) != value
                for key, value in _non_authority_fields().items()
            )
        ):
            raise CryptoRoundTripLearningError("round_trip_learning_checkpoint_invalid")
    elif (
        row.get("checkpoint_head_sha256") is not None
        or row.get("latest_observation_id") is not None
    ):
        raise CryptoRoundTripLearningError("round_trip_learning_state_invalid")
    return row, checkpoint


def run_crypto_delayed_paper_round_trip_learning_incremental(
    *, output_root: Path | str
) -> dict[str, Any]:
    """Project a bounded append-only completion suffix without core writes."""

    _assert_simulation_only()
    deadline = monotonic() + ROUND_TRIP_LEARNING_INCREMENTAL_MAX_SECONDS
    root = Path(output_root)
    store, checkpoint, core_state = _core_snapshot(root)
    completion_count = int(checkpoint.get("completion_count") or 0)
    common = {
        "processed_count": 0,
        "remaining_backlog_count": 0,
        "projected_completion_count": 0,
        "core_completion_count": completion_count,
        "latest_projected_observation_id": None,
        "checkpoint_head_sha256": None,
    }
    if checkpoint.get("pending") is not None:
        return _result(status="deferred_core_pending", **common)
    latest_observation_id = core_state.get("latest_observation_id")
    if completion_count and not isinstance(latest_observation_id, str):
        raise CryptoRoundTripLearningError("round_trip_learning_core_invalid")
    evolution = _ensure_evolution_root(root)
    with _learning_lock(evolution):
        state, latest = _read_incremental_state(evolution)
        projected = int(state["projected_completion_count"]) if state else 0
        if projected > completion_count:
            raise CryptoRoundTripLearningError("round_trip_learning_core_regressed")
        if completion_count == 0:
            if projected:
                raise CryptoRoundTripLearningError("round_trip_learning_core_regressed")
            return _result(status="none", **common)
        if projected == completion_count:
            if (
                state is not None
                and state.get("latest_observation_id") != latest_observation_id
            ):
                raise CryptoRoundTripLearningError(
                    "round_trip_learning_core_checkpoint_mismatch"
                )
            if latest is not None:
                _verify_projected_head(
                    root,
                    store,
                    observation_id=latest_observation_id,
                    checkpoint=latest,
                )
            return _result(
                status="current",
                **{
                    **common,
                    "remaining_backlog_count": completion_count - projected,
                    "projected_completion_count": projected,
                    "latest_projected_observation_id": (
                        latest.get("observation_id") if latest else None
                    ),
                    "checkpoint_head_sha256": (
                        latest.get("checkpoint_sha256") if latest else None
                    ),
                },
            )
        if state is None:
            return _result(
                status="full_scrub_required",
                **{
                    **common,
                    "remaining_backlog_count": completion_count - projected,
                    "projected_completion_count": projected,
                },
            )
        completed = _completion_inventory_cached(
            store, checkpoint, core_state, evolution=evolution, deadline=deadline
        )
        if completed is None:
            return _result(
                status="deferred_inventory_time_budget",
                inventory_complete=False,
                **{
                    **common,
                    "projected_completion_count": projected,
                },
            )
        _store_inventory_cache(evolution, completed)
        if completed[-1] != latest_observation_id:
            raise CryptoRoundTripLearningError("round_trip_learning_core_inventory_invalid")
        # Incremental work trusts the already-validated checkpoint chain and
        # revalidates only its exact head before appending a bounded suffix.
        # Replaying every historical projection here made the worker O(history)
        # and prevented the suffix cursor from ever advancing.  The separately
        # scheduled full scrub remains the all-history integrity boundary.
        if projected:
            observation_id = completed[projected - 1]
            _verify_projected_head(
                root,
                store,
                observation_id=observation_id,
                checkpoint=latest,
            )
        remaining = len(completed) - projected
        processed = 0
        for sequence in range(projected + 1, len(completed) + 1):
            if processed >= ROUND_TRIP_LEARNING_INCREMENTAL_MAX_RECORDS:
                break
            if monotonic() >= deadline:
                break
            observation_id = completed[sequence - 1]
            receipt = _verify_or_project(
                root,
                store,
                observation_id,
                strict_ledger_membership=True,
            )
            row = _checkpoint_payload(
                sequence=sequence,
                previous_sha256=latest.get("checkpoint_sha256") if latest else None,
                receipt=receipt,
            )
            _write_immutable(evolution / "checkpoints" / f"{sequence:012d}.json", row)
            latest = row
            processed += 1
            _write_state(
                evolution / "worker_state.json",
                _state_payload_from_head(
                    projected_completion_count=sequence,
                    latest_checkpoint=row,
                    scrubbed_count=sequence,
                ),
            )
        projected += processed
        remaining = len(completed) - projected
        if processed == 0 and remaining:
            return _result(
                status="deferred_time_budget",
                **{
                    **common,
                    "remaining_backlog_count": remaining,
                    "projected_completion_count": projected,
                    "latest_projected_observation_id": (
                        latest.get("observation_id") if latest else None
                    ),
                    "checkpoint_head_sha256": (
                        latest.get("checkpoint_sha256") if latest else None
                    ),
                },
            )
        return _result(
            status="projected" if remaining == 0 else "backlog_remaining",
            **{
                **common,
                "processed_count": processed,
                "remaining_backlog_count": remaining,
                "projected_completion_count": projected,
                "latest_projected_observation_id": (
                    latest.get("observation_id") if latest else None
                ),
                "checkpoint_head_sha256": (
                    latest.get("checkpoint_sha256") if latest else None
                ),
                "incremental_source_records": processed,
            },
        )


def run_crypto_delayed_paper_round_trip_learning_full_scrub(
    *, output_root: Path | str
) -> dict[str, Any]:
    """Verify the whole source-to-projection mapping and fill only new entries."""

    _assert_simulation_only()
    deadline = monotonic() + ROUND_TRIP_LEARNING_FULL_SCRUB_MAX_SECONDS
    root = Path(output_root)
    store, checkpoint, core_state = _core_snapshot(root)
    if checkpoint.get("pending") is not None:
        return _result(status="deferred_core_pending")
    completed = _completion_inventory(
        store, checkpoint, core_state, deadline=deadline
    )
    if completed is None:
        return _result(
            status="deferred_inventory_time_budget", inventory_complete=False
        )
    evolution = _ensure_evolution_root(root)
    with _learning_lock(evolution):
        checkpoints = _read_checkpoints(evolution)
        if len(checkpoints) > len(completed):
            raise CryptoRoundTripLearningError(
                "round_trip_learning_checkpoint_orphaned"
            )
        recovered: list[str] = []
        for sequence, observation_id in enumerate(completed, start=1):
            if monotonic() >= deadline:
                return _result(
                    status="deferred_time_budget",
                    completion_count=len(completed),
                    projected_completion_count=len(checkpoints),
                    verified_completion_count=sequence - 1,
                )
            if sequence <= len(checkpoints) and any(
                not path.is_file() or path.is_symlink()
                for path in _paths(root, observation_id).values()
            ):
                raise CryptoRoundTripLearningError(
                    "round_trip_learning_claimed_projection_missing"
                )
            receipt = _verify_or_project(
                root, store, observation_id, strict_ledger_membership=True
            )
            if sequence <= len(checkpoints):
                row = checkpoints[sequence - 1]
                if (
                    row.get("observation_id") != observation_id
                    or row.get("source_completion_sha256")
                    != receipt["source_completion_sha256"]
                    or row.get("projection_receipt_sha256")
                    != receipt["projection_receipt_sha256"]
                ):
                    raise CryptoRoundTripLearningError(
                        "round_trip_learning_checkpoint_source_mismatch"
                    )
                continue
            row = _checkpoint_payload(
                sequence=sequence,
                previous_sha256=checkpoints[-1]["checkpoint_sha256"]
                if checkpoints
                else None,
                receipt=receipt,
            )
            _write_immutable(evolution / "checkpoints" / f"{sequence:012d}.json", row)
            checkpoints.append(row)
            recovered.append(observation_id)
        state = _state_payload(checkpoints, scrubbed_count=len(checkpoints))
        _write_state(evolution / "worker_state.json", state)
        # The verified scrub inventory doubles as the suffix cache seed.
        _store_inventory_cache(evolution, list(completed))
        scrub = {
            "contract": ROUND_TRIP_LEARNING_SCRUB_CONTRACT,
            "completion_count": len(completed),
            "checkpoint_head_sha256": state["checkpoint_head_sha256"],
            "inventory_sha256": _sha256(
                [
                    {
                        "observation_id": row["observation_id"],
                        "source_completion_sha256": row["source_completion_sha256"],
                        "projection_receipt_sha256": row["projection_receipt_sha256"],
                    }
                    for row in checkpoints
                ]
            ),
            **_non_authority_fields(),
        }
        scrub["full_scrub_sha256"] = _sha256(scrub)
        _write_immutable(
            evolution / "scrubs" / f"{scrub['checkpoint_head_sha256'] or 'empty'}.json",
            scrub,
        )
        return _result(
            status="recovered" if recovered else "scrubbed",
            recovered_observation_count=len(recovered),
            completion_count=len(completed),
            checkpoint_head_sha256=state["checkpoint_head_sha256"],
            full_scrub_sha256=scrub["full_scrub_sha256"],
        )


def round_trip_learning_exit_code(result: Mapping[str, Any]) -> int:
    if not isinstance(result, Mapping) or result.get("status") not in {
        "current",
        "none",
        "projected",
        "backlog_remaining",
        "recovered",
        "scrubbed",
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
    "CryptoRoundTripLearningError",
    "ROUND_TRIP_LEARNING_CONTRACT",
    "round_trip_learning_exit_code",
    "run_crypto_delayed_paper_round_trip_learning_full_scrub",
    "run_crypto_delayed_paper_round_trip_learning_incremental",
]
