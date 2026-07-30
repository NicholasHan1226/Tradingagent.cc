"""Offline, append-only learning projections for Crypto delayed-paper.

Each completed observation receives immutable sample/KPI/Challenger journal
segments plus a projection receipt written last.  The projections never feed
the active Champion, size positions, authorize risk, or contact a model.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping

from Crypto.delayed_paper_ledger import (
    CryptoDelayedPaperLedgerError,
    CryptoDelayedPaperObservationStore,
    _canonical_value,
    _read_json,
)
from Crypto.fixture_sim.contracts import _assert_simulation_only


LEARNING_SAMPLE_CONTRACT = "tradingagent.crypto.learning_sample.v1"
LEARNING_KPI_CONTRACT = "tradingagent.crypto.learning_kpi.v1"
CHALLENGER_SUGGESTION_CONTRACT = "tradingagent.crypto.challenger_suggestion.v1"
LEARNING_PROJECTION_RECEIPT_CONTRACT = (
    "tradingagent.crypto.learning_projection_receipt.v1"
)
LEARNING_CHECKPOINT_CONTRACT = "tradingagent.crypto.learning_checkpoint.v1"
LEARNING_WORKER_STATE_CONTRACT = "tradingagent.crypto.learning_worker_state.v1"
LEARNING_FULL_SCRUB_CONTRACT = "tradingagent.crypto.learning_full_scrub.v1"
MAX_SEGMENT_BYTES = 1024 * 1024
MAX_RECEIPT_BYTES = 64 * 1024

_SAMPLE_DIRECTORY = "sample_journal"
_KPI_DIRECTORY = "kpi_journal"
_CHALLENGER_DIRECTORY = "challenger_suggestions"
_RECEIPT_DIRECTORY = "projection_receipts"
_CHECKPOINT_DIRECTORY = "incremental_checkpoints"
_SCRUB_DIRECTORY = "full_scrub_receipts"
_STATE_FILENAME = "worker_state.json"


class CryptoDelayedPaperLearningError(RuntimeError):
    """Raised when an offline learning projection cannot be trusted."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            _canonical_value(value),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, CryptoDelayedPaperLedgerError) as exc:
        raise CryptoDelayedPaperLearningError("learning_payload_not_canonical") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _non_authority_fields() -> dict[str, Any]:
    return {
        "authority": "none",
        "execution_eligible": False,
        "execution_authority": False,
        "production_eligible": False,
        "real_trading_enabled": False,
        "testnet_enabled": False,
        "live_broker_enabled": False,
        "model_network_used": False,
        "promotion_authorized": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "live_transition_enabled": False,
        "manual_review_required": True,
        "outbox_id": None,
        "capital_commit_id": None,
        "durability_scope": "local_learning_fsync_only",
    }


def _ensure_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise CryptoDelayedPaperLearningError("learning_directory_invalid")
        metadata = path.stat()
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise CryptoDelayedPaperLearningError(
                "learning_directory_permissions_invalid"
            )
        return
    path.mkdir(parents=True, mode=0o700, exist_ok=False)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _regular_single_link(path: Path, *, max_bytes: int) -> os.stat_result:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or metadata.st_size <= 0
        or metadata.st_size > max_bytes
    ):
        raise CryptoDelayedPaperLearningError("learning_artifact_file_invalid")
    return metadata


def _read_exact_bytes(path: Path, *, max_bytes: int) -> bytes:
    before = _regular_single_link(path, max_bytes=max_bytes)
    try:
        encoded = path.read_bytes()
    except OSError as exc:
        raise CryptoDelayedPaperLearningError("learning_artifact_read_failed") from exc
    after = path.lstat()
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(encoded) != before.st_size
    ):
        raise CryptoDelayedPaperLearningError("learning_artifact_changed_during_read")
    return encoded


def _write_immutable_bytes(path: Path, encoded: bytes, *, max_bytes: int) -> None:
    if not encoded or len(encoded) > max_bytes:
        raise CryptoDelayedPaperLearningError("learning_artifact_size_invalid")
    if path.exists() or path.is_symlink():
        if _read_exact_bytes(path, max_bytes=max_bytes) != encoded:
            raise CryptoDelayedPaperLearningError(
                "learning_immutable_artifact_conflict"
            )
        return
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise CryptoDelayedPaperLearningError(
            "learning_artifact_persist_failed"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    encoded = (_canonical_json(value) + "\n").encode("utf-8")
    if not encoded or len(encoded) > MAX_RECEIPT_BYTES:
        raise CryptoDelayedPaperLearningError("learning_state_size_invalid")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise CryptoDelayedPaperLearningError("learning_state_persist_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _learning_lock(
    root: Path,
    *,
    filename: str = ".lock",
) -> Iterator[None]:
    lock_path = root / filename
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise CryptoDelayedPaperLearningError("learning_lock_file_invalid")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _validated_observation_id(
    store: CryptoDelayedPaperObservationStore,
    value: Any,
) -> str:
    try:
        return store._observation_id({"observation_id": value})
    except CryptoDelayedPaperLedgerError as exc:
        raise CryptoDelayedPaperLearningError(
            "learning_observation_id_invalid"
        ) from exc


def _observation_id_text(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("crypto-delayed-observation-")
        or len(value) > 96
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in value
        )
    ):
        raise CryptoDelayedPaperLearningError("learning_observation_id_invalid")
    return value


def _validated_run_id(value: Any) -> str:
    prefix = "crypto-fixture-run-"
    if (
        not isinstance(value, str)
        or not value.startswith(prefix)
        or len(value) != len(prefix) + 24
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in value
        )
    ):
        raise CryptoDelayedPaperLearningError(
            "learning_source_bundle_reference_invalid"
        )
    return value


def _verify_bundle(
    *,
    root: Path,
    reference: Mapping[str, Any],
) -> dict[str, Any]:
    run_id = _validated_run_id(reference.get("run_id"))
    claimed_reference_sha = reference.get("business_bundle_sha256")
    if not isinstance(claimed_reference_sha, str) or len(claimed_reference_sha) != 64:
        raise CryptoDelayedPaperLearningError(
            "learning_source_bundle_reference_invalid"
        )
    bundle_path = root / "runs" / f"{run_id}.json"
    if not bundle_path.is_file() or bundle_path.is_symlink():
        raise CryptoDelayedPaperLearningError("learning_source_bundle_missing")
    try:
        bundle = _read_json(bundle_path)
    except CryptoDelayedPaperLedgerError as exc:
        raise CryptoDelayedPaperLearningError("learning_source_bundle_invalid") from exc
    claimed_bundle_sha = bundle.get("business_bundle_sha256")
    material = dict(bundle)
    material.pop("business_bundle_sha256", None)
    if (
        bundle.get("run_id") != run_id
        or claimed_bundle_sha != claimed_reference_sha
        or claimed_bundle_sha != _sha256(material)
        or not isinstance(bundle.get("sample_review"), Mapping)
        or not isinstance(bundle.get("decision"), Mapping)
        or bundle["decision"].get("decision_id") != reference.get("decision_id")
    ):
        raise CryptoDelayedPaperLearningError("learning_source_bundle_invalid")
    return bundle


def _matching_decision_event(
    *,
    events: list[Mapping[str, Any]],
    observation_id: str,
    symbol: str,
    reference: Mapping[str, Any],
    bundle: Mapping[str, Any],
    observation_content_sha256: Any,
) -> Mapping[str, Any]:
    matches = [
        row
        for row in events
        if row.get("observation_id") == observation_id
        and row.get("symbol") == symbol
        and row.get("event_type") in {"decision", "risk_reject"}
    ]
    if len(matches) != 1:
        raise CryptoDelayedPaperLearningError("learning_source_decision_event_invalid")
    event = matches[0]
    disposition = reference.get("disposition")
    if (
        event.get("disposition") != disposition
        or event.get("decision_id") != reference.get("decision_id")
        or event.get("observation_content_sha256") != observation_content_sha256
        or not isinstance(event.get("counterfactual"), Mapping)
    ):
        raise CryptoDelayedPaperLearningError("learning_source_decision_event_invalid")
    if disposition == "risk_rejected":
        if (
            event.get("event_type") != "risk_reject"
            or event.get("run_id") is not None
            or event.get("business_bundle_sha256") is not None
        ):
            raise CryptoDelayedPaperLearningError(
                "learning_source_decision_event_invalid"
            )
    elif (
        event.get("event_type") != "decision"
        or event.get("run_id") != bundle.get("run_id")
        or event.get("business_bundle_sha256") != bundle.get("business_bundle_sha256")
    ):
        raise CryptoDelayedPaperLearningError("learning_source_decision_event_invalid")
    return event


def _trusted_symbol_results(
    *,
    root: Path,
    observation_id: str,
    completion: Mapping[str, Any],
    events: list[Mapping[str, Any]],
    supplied_symbols: Mapping[str, Any] | None,
    observation_content_sha256: Any,
) -> dict[str, dict[str, Any]]:
    references = completion.get("bundle_references")
    if not isinstance(references, Mapping) or not references:
        raise CryptoDelayedPaperLearningError("learning_source_completion_invalid")
    if supplied_symbols is not None and set(supplied_symbols) != set(references):
        raise CryptoDelayedPaperLearningError("learning_result_symbol_set_mismatch")
    trusted: dict[str, dict[str, Any]] = {}
    for symbol, reference in sorted(references.items()):
        if (
            not isinstance(symbol, str)
            or not symbol
            or not isinstance(reference, Mapping)
        ):
            raise CryptoDelayedPaperLearningError(
                "learning_source_bundle_reference_invalid"
            )
        bundle = _verify_bundle(root=root, reference=reference)
        event = _matching_decision_event(
            events=events,
            observation_id=observation_id,
            symbol=symbol,
            reference=reference,
            bundle=bundle,
            observation_content_sha256=observation_content_sha256,
        )
        disposition = reference.get("disposition")
        trusted_item: dict[str, Any] = {
            "disposition": disposition,
            "bundle": bundle,
            "counterfactual": event["counterfactual"],
        }
        if disposition == "risk_rejected":
            trusted_item["risk_reject"] = {
                "event_id": event.get("event_id"),
                "reason_code": event.get("reason_code"),
                "decision": bundle.get("decision"),
            }
        if supplied_symbols is not None:
            supplied = supplied_symbols.get(symbol)
            if not isinstance(supplied, Mapping):
                raise CryptoDelayedPaperLearningError("learning_result_symbol_invalid")
            for field in ("disposition", "bundle", "counterfactual"):
                if _canonical_json(supplied.get(field)) != _canonical_json(
                    trusted_item[field]
                ):
                    raise CryptoDelayedPaperLearningError(
                        "learning_result_source_mismatch"
                    )
            if disposition == "risk_rejected" and _canonical_json(
                supplied.get("risk_reject")
            ) != _canonical_json(trusted_item["risk_reject"]):
                raise CryptoDelayedPaperLearningError("learning_result_source_mismatch")
        trusted[symbol] = trusted_item
    return trusted


def _verified_sources(
    *,
    root: Path,
    observation_id: str,
    supplied_symbols: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    store = CryptoDelayedPaperObservationStore(root)
    validated_id = _validated_observation_id(store, observation_id)
    observation_path = store.observations_dir / f"{validated_id}.json"
    completion_path = store.completions_dir / f"{validated_id}.json"
    if not observation_path.is_file() or not completion_path.is_file():
        raise CryptoDelayedPaperLearningError("learning_source_completion_missing")
    try:
        observation = _read_json(observation_path)
        completion = _read_json(completion_path)
        store._verify_observation(observation)
        store._verify_completion(completion, observation=observation)
        events = store.events_for_observation(validated_id)
    except CryptoDelayedPaperLedgerError as exc:
        raise CryptoDelayedPaperLearningError(
            "learning_source_completion_invalid"
        ) from exc
    if (
        observation.get("observation_id") != validated_id
        or completion.get("observation_id") != validated_id
        or completion.get("status") != "completed"
    ):
        raise CryptoDelayedPaperLearningError("learning_source_completion_invalid")
    trusted = _trusted_symbol_results(
        root=root,
        observation_id=validated_id,
        completion=completion,
        events=events,
        supplied_symbols=supplied_symbols,
        observation_content_sha256=observation.get("observation_content_sha256"),
    )
    return observation, completion, trusted


def _sample_event(
    *,
    observation_id: str,
    symbol: str,
    item: Mapping[str, Any],
    completion_sha256: str,
) -> dict[str, Any]:
    bundle = item["bundle"]
    sample = bundle["sample_review"]
    material = {
        "observation_id": observation_id,
        "symbol": symbol,
        "sample_id": sample.get("sample_id"),
        "counterfactual": item["counterfactual"],
        "completion_sha256": completion_sha256,
        "business_bundle_sha256": bundle.get("business_bundle_sha256"),
    }
    return {
        "contract": LEARNING_SAMPLE_CONTRACT,
        "event_id": f"crypto-learning-sample-{_sha256(material)[:24]}",
        "event_type": "learning_sample",
        "market": "crypto",
        "observation_id": observation_id,
        "symbol": symbol,
        "disposition": item["disposition"],
        "source_completion_sha256": completion_sha256,
        "source_business_bundle_sha256": bundle.get("business_bundle_sha256"),
        "sample_review": sample,
        "counterfactual": item["counterfactual"],
        "label_status": sample.get("label_status"),
        **_non_authority_fields(),
    }


def _journal_rows(events: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    previous: str | None = None
    for sequence, event in enumerate(events, start=1):
        row = {
            **_canonical_value(event),
            "sequence": sequence,
            "previous_checksum": previous,
        }
        row["checksum"] = _sha256(row)
        rows.append(row)
        previous = row["checksum"]
    return rows


def _journal_bytes(events: list[Mapping[str, Any]]) -> bytes:
    return b"".join(
        (_canonical_json(row) + "\n").encode("utf-8") for row in _journal_rows(events)
    )


def _kpi_event(
    *,
    observation_id: str,
    completion_sha256: str,
    samples: list[Mapping[str, Any]],
) -> dict[str, Any]:
    fill_count = sum(
        row.get("disposition") == "fixture_simulated_fill" for row in samples
    )
    risk_reject_count = sum(
        row.get("disposition") == "risk_rejected" for row in samples
    )
    pending_count = sum(row.get("label_status") == "pending" for row in samples)
    material = {
        "observation_id": observation_id,
        "completion_sha256": completion_sha256,
        "sample_event_ids": [row.get("event_id") for row in samples],
    }
    return {
        "contract": LEARNING_KPI_CONTRACT,
        "event_id": f"crypto-learning-kpi-{_sha256(material)[:24]}",
        "event_type": "learning_kpi",
        "market": "crypto",
        "scope": "single_observation",
        "observation_id": observation_id,
        "source_completion_sha256": completion_sha256,
        "sample_event_ids": material["sample_event_ids"],
        "sample_count": len(samples),
        "fixture_fill_sample_count": fill_count,
        "risk_reject_sample_count": risk_reject_count,
        "pending_label_count": pending_count,
        "mature_label_count": 0,
        "win_rate": None,
        "net_return_after_costs": None,
        "promotion_evidence_ready": False,
        **_non_authority_fields(),
    }


def _challenger_event(
    *,
    observation_id: str,
    completion_sha256: str,
    kpi: Mapping[str, Any],
) -> dict[str, Any]:
    material = {
        "observation_id": observation_id,
        "completion_sha256": completion_sha256,
        "kpi_event_id": kpi.get("event_id"),
    }
    return {
        "contract": CHALLENGER_SUGGESTION_CONTRACT,
        "event_id": f"crypto-challenger-suggestion-{_sha256(material)[:24]}",
        "event_type": "challenger_suggestion",
        "market": "crypto",
        "observation_id": observation_id,
        "source_completion_sha256": completion_sha256,
        "kpi_event_id": kpi.get("event_id"),
        "suggestion": "collect_mature_labels_before_parameter_change",
        "reason_codes": [
            "labels_pending",
            "predictive_evidence_not_established",
        ],
        "proposed_parameter_changes": [],
        "eligible_for_champion_replacement": False,
        "challenger_generation_authority": False,
        **_non_authority_fields(),
    }


def _projection_paths(root: Path, observation_id: str) -> dict[str, Path]:
    return {
        "sample": root / _SAMPLE_DIRECTORY / f"{observation_id}.jsonl",
        "kpi": root / _KPI_DIRECTORY / f"{observation_id}.jsonl",
        "challenger": root / _CHALLENGER_DIRECTORY / f"{observation_id}.jsonl",
        "receipt": root / _RECEIPT_DIRECTORY / f"{observation_id}.json",
    }


def _projection_receipt(
    *,
    observation_id: str,
    completion_sha256: str,
    market_slot: str,
    encoded: Mapping[str, bytes],
    sample_count: int,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "contract": LEARNING_PROJECTION_RECEIPT_CONTRACT,
        "observation_id": observation_id,
        "source_completion_sha256": completion_sha256,
        "market_slot": market_slot,
        "sample_segment_sha256": _bytes_sha256(encoded["sample"]),
        "kpi_segment_sha256": _bytes_sha256(encoded["kpi"]),
        "challenger_segment_sha256": _bytes_sha256(encoded["challenger"]),
        "sample_count": sample_count,
        **_non_authority_fields(),
    }
    receipt["projection_receipt_sha256"] = _sha256(receipt)
    return receipt


def _projection_material(
    *,
    observation_id: str,
    observation: Mapping[str, Any],
    completion: Mapping[str, Any],
    trusted: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    completion_sha256 = completion.get("completion_sha256")
    if not isinstance(completion_sha256, str) or len(completion_sha256) != 64:
        raise CryptoDelayedPaperLearningError("learning_source_completion_invalid")
    samples = [
        _sample_event(
            observation_id=observation_id,
            symbol=symbol,
            item=trusted[symbol],
            completion_sha256=completion_sha256,
        )
        for symbol in sorted(trusted)
    ]
    kpi = _kpi_event(
        observation_id=observation_id,
        completion_sha256=completion_sha256,
        samples=samples,
    )
    challenger = _challenger_event(
        observation_id=observation_id,
        completion_sha256=completion_sha256,
        kpi=kpi,
    )
    encoded = {
        "sample": _journal_bytes(samples),
        "kpi": _journal_bytes([kpi]),
        "challenger": _journal_bytes([challenger]),
    }
    receipt = _projection_receipt(
        observation_id=observation_id,
        completion_sha256=completion_sha256,
        market_slot=str(observation.get("market_slot") or ""),
        encoded=encoded,
        sample_count=len(samples),
    )
    return {
        "samples": samples,
        "kpi": kpi,
        "challenger": challenger,
        "encoded": encoded,
        "receipt": receipt,
    }


def _read_journal_segment(path: Path) -> tuple[bytes, list[dict[str, Any]]]:
    encoded = _read_exact_bytes(path, max_bytes=MAX_SEGMENT_BYTES)
    if not encoded.endswith(b"\n") or b"\x00" in encoded:
        raise CryptoDelayedPaperLearningError("learning_segment_invalid")
    rows: list[dict[str, Any]] = []
    previous: str | None = None
    try:
        for sequence, line in enumerate(encoded.splitlines(), start=1):
            row = json.loads(line)
            if not isinstance(row, dict):
                raise CryptoDelayedPaperLearningError("learning_segment_invalid")
            material = dict(row)
            checksum = material.pop("checksum", None)
            if (
                material.get("sequence") != sequence
                or material.get("previous_checksum") != previous
                or checksum != _sha256(material)
            ):
                raise CryptoDelayedPaperLearningError(
                    "learning_segment_checksum_invalid"
                )
            rows.append(row)
            previous = str(checksum)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoDelayedPaperLearningError("learning_segment_invalid") from exc
    if (
        b"".join((_canonical_json(row) + "\n").encode("utf-8") for row in rows)
        != encoded
    ):
        raise CryptoDelayedPaperLearningError("learning_segment_not_canonical")
    return encoded, rows


def _verify_projection_receipt(
    *,
    evolution_root: Path,
    observation_id: str,
) -> dict[str, Any]:
    validated_id = _observation_id_text(observation_id)
    paths = _projection_paths(evolution_root, validated_id)
    encoded_receipt = _read_exact_bytes(
        paths["receipt"],
        max_bytes=MAX_RECEIPT_BYTES,
    )
    try:
        receipt = json.loads(encoded_receipt.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoDelayedPaperLearningError(
            "learning_projection_receipt_invalid"
        ) from exc
    if (
        not isinstance(receipt, dict)
        or (_canonical_json(receipt) + "\n").encode("utf-8") != encoded_receipt
    ):
        raise CryptoDelayedPaperLearningError("learning_projection_receipt_invalid")
    material = dict(receipt)
    claimed = material.pop("projection_receipt_sha256", None)
    completion_sha256 = receipt.get("source_completion_sha256")
    market_slot = receipt.get("market_slot")
    try:
        parsed_market_slot = datetime.fromisoformat(
            str(market_slot).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise CryptoDelayedPaperLearningError(
            "learning_projection_receipt_invalid"
        ) from exc
    if (
        receipt.get("contract") != LEARNING_PROJECTION_RECEIPT_CONTRACT
        or receipt.get("observation_id") != validated_id
        or claimed != _sha256(material)
        or not isinstance(completion_sha256, str)
        or len(completion_sha256) != 64
        or receipt.get("sample_count") != 2
        or parsed_market_slot.tzinfo is None
        or parsed_market_slot.utcoffset() != timedelta(0)
    ):
        raise CryptoDelayedPaperLearningError("learning_projection_receipt_invalid")
    for key, expected in _non_authority_fields().items():
        if receipt.get(key) != expected:
            raise CryptoDelayedPaperLearningError("learning_projection_receipt_invalid")
    segment_rows: dict[str, list[dict[str, Any]]] = {}
    for name in ("sample", "kpi", "challenger"):
        encoded, rows = _read_journal_segment(paths[name])
        if receipt.get(f"{name}_segment_sha256") != _bytes_sha256(encoded):
            raise CryptoDelayedPaperLearningError(
                "learning_projection_segment_digest_mismatch"
            )
        segment_rows[name] = rows
    if (
        len(segment_rows["sample"]) != receipt["sample_count"]
        or len(segment_rows["kpi"]) != 1
        or len(segment_rows["challenger"]) != 1
        or any(
            row.get("contract") != LEARNING_SAMPLE_CONTRACT
            or row.get("observation_id") != validated_id
            or row.get("source_completion_sha256") != completion_sha256
            for row in segment_rows["sample"]
        )
        or segment_rows["kpi"][0].get("contract") != LEARNING_KPI_CONTRACT
        or segment_rows["kpi"][0].get("observation_id") != validated_id
        or segment_rows["kpi"][0].get("source_completion_sha256") != completion_sha256
        or segment_rows["challenger"][0].get("contract")
        != CHALLENGER_SUGGESTION_CONTRACT
        or segment_rows["challenger"][0].get("observation_id") != validated_id
        or segment_rows["challenger"][0].get("source_completion_sha256")
        != completion_sha256
    ):
        raise CryptoDelayedPaperLearningError(
            "learning_projection_segment_binding_invalid"
        )
    return receipt


def _verify_projection_against_core(
    *,
    root: Path,
    evolution_root: Path,
    observation_id: str,
) -> dict[str, Any]:
    observation, completion, trusted = _verified_sources(
        root=root,
        observation_id=observation_id,
        supplied_symbols=None,
    )
    expected = _projection_material(
        observation_id=observation_id,
        observation=observation,
        completion=completion,
        trusted=trusted,
    )
    receipt = _verify_projection_receipt(
        evolution_root=evolution_root,
        observation_id=observation_id,
    )
    paths = _projection_paths(evolution_root, observation_id)
    for name in ("sample", "kpi", "challenger"):
        if (
            _read_exact_bytes(
                paths[name],
                max_bytes=MAX_SEGMENT_BYTES,
            )
            != expected["encoded"][name]
        ):
            raise CryptoDelayedPaperLearningError(
                "learning_projection_not_derived_from_core"
            )
    if _canonical_json(receipt) != _canonical_json(expected["receipt"]):
        raise CryptoDelayedPaperLearningError(
            "learning_projection_not_derived_from_core"
        )
    return receipt


def _verified_completion_record(
    *,
    store: CryptoDelayedPaperObservationStore,
    observation_id: str,
) -> dict[str, str]:
    validated_id = _validated_observation_id(store, observation_id)
    observation_path = store.observations_dir / f"{validated_id}.json"
    completion_path = store.completions_dir / f"{validated_id}.json"
    try:
        observation = _read_json(observation_path)
        completion = _read_json(completion_path)
        store._verify_observation(observation)
        store._verify_completion(completion, observation=observation)
    except CryptoDelayedPaperLedgerError as exc:
        raise CryptoDelayedPaperLearningError(
            "learning_source_completion_invalid"
        ) from exc
    completion_sha256 = completion.get("completion_sha256")
    market_slot = observation.get("market_slot")
    if (
        completion.get("status") != "completed"
        or completion.get("observation_id") != validated_id
        or not isinstance(completion_sha256, str)
        or len(completion_sha256) != 64
        or not isinstance(market_slot, str)
        or not market_slot
    ):
        raise CryptoDelayedPaperLearningError("learning_source_completion_invalid")
    return {
        "observation_id": validated_id,
        "market_slot": market_slot,
        "source_completion_sha256": completion_sha256,
    }


@contextmanager
def _core_read_lock(
    store: CryptoDelayedPaperObservationStore,
) -> Iterator[None]:
    """Share the existing core lock without creating or writing it."""

    path = store.lock_path
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
            or path.resolve(strict=True) != path
        ):
            raise CryptoDelayedPaperLearningError("learning_core_lock_untrusted")
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        yield
    except CryptoDelayedPaperLearningError:
        raise
    except OSError as exc:
        raise CryptoDelayedPaperLearningError("learning_core_lock_untrusted") from exc
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _core_completion_state(
    store: CryptoDelayedPaperObservationStore,
) -> dict[str, Any]:
    try:
        with _core_read_lock(store):
            state = store._observation_state()
    except CryptoDelayedPaperLedgerError as exc:
        raise CryptoDelayedPaperLearningError(
            "learning_core_checkpoint_invalid"
        ) from exc
    completion_count = state.get("completion_count")
    observation_count = state.get("observation_count")
    if (
        isinstance(completion_count, bool)
        or not isinstance(completion_count, int)
        or isinstance(observation_count, bool)
        or not isinstance(observation_count, int)
        or completion_count < 0
        or observation_count < completion_count
    ):
        raise CryptoDelayedPaperLearningError("learning_core_checkpoint_invalid")
    return state


def _checkpoint_path(evolution_root: Path, sequence: int) -> Path:
    return evolution_root / _CHECKPOINT_DIRECTORY / f"{sequence:012d}.json"


def _checkpoint_payload(
    *,
    sequence: int,
    previous_checkpoint_sha256: str | None,
    completion: Mapping[str, str],
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    checkpoint: dict[str, Any] = {
        "contract": LEARNING_CHECKPOINT_CONTRACT,
        "sequence": sequence,
        "previous_checkpoint_sha256": previous_checkpoint_sha256,
        "observation_id": completion["observation_id"],
        "market_slot": completion["market_slot"],
        "source_completion_sha256": completion["source_completion_sha256"],
        "projection_receipt_sha256": receipt["projection_receipt_sha256"],
        **_non_authority_fields(),
    }
    checkpoint["checkpoint_sha256"] = _sha256(checkpoint)
    return checkpoint


def _verify_checkpoint(
    *,
    path: Path,
    expected_sequence: int,
    expected_previous: str | None = None,
    verify_previous: bool = True,
) -> dict[str, Any]:
    encoded = _read_exact_bytes(path, max_bytes=MAX_RECEIPT_BYTES)
    try:
        checkpoint = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoDelayedPaperLearningError("learning_checkpoint_invalid") from exc
    if (
        not isinstance(checkpoint, dict)
        or (_canonical_json(checkpoint) + "\n").encode("utf-8") != encoded
    ):
        raise CryptoDelayedPaperLearningError("learning_checkpoint_invalid")
    material = dict(checkpoint)
    claimed = material.pop("checkpoint_sha256", None)
    completion_sha256 = checkpoint.get("source_completion_sha256")
    receipt_sha256 = checkpoint.get("projection_receipt_sha256")
    market_slot = checkpoint.get("market_slot")
    try:
        parsed_market_slot = datetime.fromisoformat(
            str(market_slot).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise CryptoDelayedPaperLearningError("learning_checkpoint_invalid") from exc
    if (
        checkpoint.get("contract") != LEARNING_CHECKPOINT_CONTRACT
        or checkpoint.get("sequence") != expected_sequence
        or (
            verify_previous
            and checkpoint.get("previous_checkpoint_sha256") != expected_previous
        )
        or claimed != _sha256(material)
        or not isinstance(completion_sha256, str)
        or len(completion_sha256) != 64
        or not isinstance(receipt_sha256, str)
        or len(receipt_sha256) != 64
        or parsed_market_slot.tzinfo is None
        or parsed_market_slot.utcoffset() != timedelta(0)
    ):
        raise CryptoDelayedPaperLearningError("learning_checkpoint_chain_invalid")
    _observation_id_text(checkpoint.get("observation_id"))
    for key, expected in _non_authority_fields().items():
        if checkpoint.get(key) != expected:
            raise CryptoDelayedPaperLearningError("learning_checkpoint_invalid")
    return checkpoint


def _worker_state_payload(
    *,
    checkpoint: Mapping[str, Any] | None,
) -> dict[str, Any]:
    sequence = int(checkpoint["sequence"]) if checkpoint else 0
    state: dict[str, Any] = {
        "contract": LEARNING_WORKER_STATE_CONTRACT,
        "checkpoint_sequence": sequence,
        "checkpoint_head_sha256": (
            checkpoint["checkpoint_sha256"] if checkpoint else None
        ),
        "projected_completion_count": sequence,
        "latest_observation_id": (checkpoint["observation_id"] if checkpoint else None),
        "latest_market_slot": (checkpoint["market_slot"] if checkpoint else None),
        "latest_completion_sha256": (
            checkpoint["source_completion_sha256"] if checkpoint else None
        ),
        "latest_projection_receipt_sha256": (
            checkpoint["projection_receipt_sha256"] if checkpoint else None
        ),
        **_non_authority_fields(),
    }
    state["worker_state_sha256"] = _sha256(state)
    return state


def _read_worker_state(path: Path) -> dict[str, Any]:
    encoded = _read_exact_bytes(path, max_bytes=MAX_RECEIPT_BYTES)
    try:
        state = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoDelayedPaperLearningError("learning_worker_state_invalid") from exc
    if (
        not isinstance(state, dict)
        or (_canonical_json(state) + "\n").encode("utf-8") != encoded
    ):
        raise CryptoDelayedPaperLearningError("learning_worker_state_invalid")
    material = dict(state)
    claimed = material.pop("worker_state_sha256", None)
    sequence = state.get("checkpoint_sequence")
    if (
        state.get("contract") != LEARNING_WORKER_STATE_CONTRACT
        or claimed != _sha256(material)
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 0
        or state.get("projected_completion_count") != sequence
    ):
        raise CryptoDelayedPaperLearningError("learning_worker_state_invalid")
    for key, expected in _non_authority_fields().items():
        if state.get(key) != expected:
            raise CryptoDelayedPaperLearningError("learning_worker_state_invalid")
    return state


def _verify_incremental_head(
    *,
    evolution_root: Path,
    state: Mapping[str, Any],
) -> dict[str, Any] | None:
    sequence = int(state["checkpoint_sequence"])
    if sequence == 0:
        if state != _worker_state_payload(checkpoint=None):
            raise CryptoDelayedPaperLearningError("learning_worker_state_invalid")
        return None
    checkpoint = _verify_checkpoint(
        path=_checkpoint_path(evolution_root, sequence),
        expected_sequence=sequence,
        expected_previous=(
            None
            if sequence == 1
            else str(
                _verify_checkpoint(
                    path=_checkpoint_path(
                        evolution_root,
                        sequence - 1,
                    ),
                    expected_sequence=sequence - 1,
                    expected_previous=None,
                    verify_previous=sequence == 2,
                )["checkpoint_sha256"]
            )
        ),
    )
    expected_state = _worker_state_payload(checkpoint=checkpoint)
    if _canonical_json(state) != _canonical_json(expected_state):
        raise CryptoDelayedPaperLearningError("learning_worker_state_head_mismatch")
    receipt = _verify_projection_against_core(
        root=evolution_root.parent,
        evolution_root=evolution_root,
        observation_id=str(checkpoint["observation_id"]),
    )
    if receipt.get("projection_receipt_sha256") != checkpoint.get(
        "projection_receipt_sha256"
    ) or receipt.get("source_completion_sha256") != checkpoint.get(
        "source_completion_sha256"
    ):
        raise CryptoDelayedPaperLearningError("learning_checkpoint_receipt_mismatch")
    return checkpoint


def project_crypto_delayed_paper_learning(
    *,
    result: Mapping[str, Any],
    output_root: Path | str,
) -> dict[str, Any]:
    """Write immutable observation-scoped learning segments and a final receipt."""

    if not isinstance(result, Mapping):
        raise CryptoDelayedPaperLearningError("learning_result_invalid")
    if result.get("status") != "completed":
        return {
            "status": "skipped",
            "reason": "no_completed_observation",
            "execution_authority": False,
            "production_eligible": False,
            "promotion_authorized": False,
        }
    observation_id = result.get("observation_id")
    supplied_symbols = result.get("symbols")
    if not isinstance(observation_id, str) or not isinstance(supplied_symbols, Mapping):
        raise CryptoDelayedPaperLearningError("learning_completed_result_invalid")
    root = Path(output_root)
    if root.exists() and root.is_symlink():
        raise CryptoDelayedPaperLearningError("learning_output_root_symlink_forbidden")
    observation, completion, trusted = _verified_sources(
        root=root,
        observation_id=observation_id,
        supplied_symbols=supplied_symbols,
    )
    if result.get("observation_content_sha256") != observation.get(
        "observation_content_sha256"
    ):
        raise CryptoDelayedPaperLearningError("learning_result_source_mismatch")
    material = _projection_material(
        observation_id=observation_id,
        observation=observation,
        completion=completion,
        trusted=trusted,
    )
    samples = material["samples"]
    kpi = material["kpi"]
    challenger = material["challenger"]
    encoded = material["encoded"]
    receipt = material["receipt"]

    _ensure_directory(root)
    evolution_root = root / "evolution"
    _ensure_directory(evolution_root)
    for directory in (
        _SAMPLE_DIRECTORY,
        _KPI_DIRECTORY,
        _CHALLENGER_DIRECTORY,
        _RECEIPT_DIRECTORY,
    ):
        _ensure_directory(evolution_root / directory)
    paths = _projection_paths(evolution_root, observation_id)
    with _learning_lock(evolution_root):
        for name in ("sample", "kpi", "challenger"):
            _write_immutable_bytes(
                paths[name],
                encoded[name],
                max_bytes=MAX_SEGMENT_BYTES,
            )
        _write_immutable_bytes(
            paths["receipt"],
            (_canonical_json(receipt) + "\n").encode("utf-8"),
            max_bytes=MAX_RECEIPT_BYTES,
        )
    return {
        "status": "projected",
        "sample_count": len(samples),
        "sample_event_ids": [row["event_id"] for row in samples],
        "kpi_event_id": kpi["event_id"],
        "challenger_suggestion_event_id": challenger["event_id"],
        "projection_receipt_sha256": receipt["projection_receipt_sha256"],
        "execution_authority": False,
        "production_eligible": False,
        "promotion_authorized": False,
    }


def _result_for_recovery(
    *,
    root: Path,
    observation_id: str,
) -> tuple[dict[str, Any], str]:
    observation, _, trusted = _verified_sources(
        root=root,
        observation_id=observation_id,
        supplied_symbols=None,
    )
    return (
        {
            "contract": "tradingagent.crypto.delayed_paper_runner.v1",
            "status": "completed",
            "observation_id": observation_id,
            "observation_content_sha256": observation.get("observation_content_sha256"),
            "symbols": trusted,
            "execution_authority": False,
            "production_eligible": False,
        },
        str(observation.get("market_slot") or ""),
    )


def _ensure_learning_directories(root: Path) -> Path:
    _ensure_directory(root)
    evolution_root = root / "evolution"
    _ensure_directory(evolution_root)
    for directory in (
        _SAMPLE_DIRECTORY,
        _KPI_DIRECTORY,
        _CHALLENGER_DIRECTORY,
        _RECEIPT_DIRECTORY,
        _CHECKPOINT_DIRECTORY,
        _SCRUB_DIRECTORY,
    ):
        _ensure_directory(evolution_root / directory)
    return evolution_root


def _append_checkpoint(
    *,
    evolution_root: Path,
    state: Mapping[str, Any],
    completion: Mapping[str, str],
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    sequence = int(state["checkpoint_sequence"]) + 1
    previous = state.get("checkpoint_head_sha256")
    if previous is not None and (not isinstance(previous, str) or len(previous) != 64):
        raise CryptoDelayedPaperLearningError("learning_worker_state_invalid")
    checkpoint = _checkpoint_payload(
        sequence=sequence,
        previous_checkpoint_sha256=previous,
        completion=completion,
        receipt=receipt,
    )
    path = _checkpoint_path(evolution_root, sequence)
    encoded = (_canonical_json(checkpoint) + "\n").encode("utf-8")
    _write_immutable_bytes(
        path,
        encoded,
        max_bytes=MAX_RECEIPT_BYTES,
    )
    persisted = _verify_checkpoint(
        path=path,
        expected_sequence=sequence,
        expected_previous=previous,
    )
    if _canonical_json(persisted) != _canonical_json(checkpoint):
        raise CryptoDelayedPaperLearningError("learning_checkpoint_conflict")
    new_state = _worker_state_payload(checkpoint=persisted)
    _write_json_atomic(evolution_root / _STATE_FILENAME, new_state)
    return new_state


def _project_or_verify_completion(
    *,
    root: Path,
    evolution_root: Path,
    completion: Mapping[str, str],
) -> dict[str, Any]:
    observation_id = completion["observation_id"]
    receipt_path = _projection_paths(
        evolution_root,
        observation_id,
    )["receipt"]
    if receipt_path.exists() or receipt_path.is_symlink():
        receipt = _verify_projection_against_core(
            root=root,
            evolution_root=evolution_root,
            observation_id=observation_id,
        )
    else:
        result, _ = _result_for_recovery(
            root=root,
            observation_id=observation_id,
        )
        project_crypto_delayed_paper_learning(
            result=result,
            output_root=root,
        )
        receipt = _verify_projection_against_core(
            root=root,
            evolution_root=evolution_root,
            observation_id=observation_id,
        )
    if (
        receipt.get("source_completion_sha256")
        != completion["source_completion_sha256"]
        or receipt.get("market_slot") != completion["market_slot"]
    ):
        raise CryptoDelayedPaperLearningError("learning_projection_completion_mismatch")
    return receipt


def _full_checkpoint_chain(
    evolution_root: Path,
) -> list[dict[str, Any]]:
    checkpoint_directory = evolution_root / _CHECKPOINT_DIRECTORY
    paths = sorted(checkpoint_directory.iterdir())
    checkpoints: list[dict[str, Any]] = []
    previous: str | None = None
    for sequence, path in enumerate(paths, start=1):
        if (
            path.name != f"{sequence:012d}.json"
            or path.is_symlink()
            or not path.is_file()
        ):
            raise CryptoDelayedPaperLearningError(
                "learning_checkpoint_inventory_invalid"
            )
        checkpoint = _verify_checkpoint(
            path=path,
            expected_sequence=sequence,
            expected_previous=previous,
        )
        previous = str(checkpoint["checkpoint_sha256"])
        checkpoints.append(checkpoint)
    return checkpoints


def _completion_inventory(
    store: CryptoDelayedPaperObservationStore,
) -> list[dict[str, str]]:
    completions: list[dict[str, str]] = []
    for path in sorted(store.completions_dir.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise CryptoDelayedPaperLearningError(
                "learning_completion_inventory_invalid"
            )
        observation_id = _observation_id_text(path.stem)
        if path.name != f"{observation_id}.json":
            raise CryptoDelayedPaperLearningError(
                "learning_completion_inventory_invalid"
            )
        completions.append(
            _verified_completion_record(
                store=store,
                observation_id=observation_id,
            )
        )
    completions.sort(
        key=lambda item: (
            item["market_slot"],
            item["observation_id"],
        )
    )
    return completions


def _receipt_inventory(evolution_root: Path) -> set[str]:
    receipt_directory = evolution_root / _RECEIPT_DIRECTORY
    observation_ids: set[str] = set()
    for path in sorted(receipt_directory.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise CryptoDelayedPaperLearningError("learning_receipt_inventory_invalid")
        observation_id = _observation_id_text(path.stem)
        if path.name != f"{observation_id}.json":
            raise CryptoDelayedPaperLearningError("learning_receipt_inventory_invalid")
        if observation_id in observation_ids:
            raise CryptoDelayedPaperLearningError("learning_receipt_inventory_invalid")
        observation_ids.add(observation_id)
    return observation_ids


def _learning_result(
    *,
    status: str,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "status": status,
        "learning_mode": "detached_offline_worker",
        **fields,
        **_non_authority_fields(),
    }


def run_crypto_delayed_paper_learning_incremental(
    *,
    output_root: Path | str,
) -> dict[str, Any]:
    """Project at most one newly completed observation in O(delta) work."""

    _assert_simulation_only()
    root = Path(output_root)
    delayed_root = root / "delayed_paper"
    if not delayed_root.is_dir():
        return _learning_result(
            status="none",
            projected_completion_count=0,
        )
    store = CryptoDelayedPaperObservationStore(root)
    core = _core_completion_state(store)
    if core.get("pending_observation_id") is not None:
        return _learning_result(
            status="deferred_core_pending",
            projected_completion_count=None,
        )
    completion_count = int(core["completion_count"])
    evolution_root = root / "evolution"
    if completion_count == 0 and not evolution_root.exists():
        return _learning_result(
            status="none",
            projected_completion_count=0,
        )
    evolution_root = _ensure_learning_directories(root)
    with _learning_lock(evolution_root, filename=".worker.lock"):
        state_path = evolution_root / _STATE_FILENAME
        if state_path.exists() or state_path.is_symlink():
            state = _read_worker_state(state_path)
            _verify_incremental_head(
                evolution_root=evolution_root,
                state=state,
            )
        else:
            state = _worker_state_payload(checkpoint=None)
        projected = int(state["projected_completion_count"])
        if completion_count < projected:
            raise CryptoDelayedPaperLearningError("learning_core_completion_regressed")
        if completion_count == projected:
            if completion_count:
                if core.get("latest_observation_id") != state.get(
                    "latest_observation_id"
                ) or core.get("latest_completion_sha256") != state.get(
                    "latest_completion_sha256"
                ):
                    raise CryptoDelayedPaperLearningError(
                        "learning_core_checkpoint_mismatch"
                    )
            return _learning_result(
                status="current",
                projected_completion_count=projected,
            )
        if completion_count != projected + 1:
            return _learning_result(
                status="full_scrub_required",
                projected_completion_count=projected,
                core_completion_count=completion_count,
            )
        latest_observation_id = core.get("latest_observation_id")
        latest_completion_sha256 = core.get("latest_completion_sha256")
        if not isinstance(latest_observation_id, str) or not isinstance(
            latest_completion_sha256, str
        ):
            raise CryptoDelayedPaperLearningError("learning_core_checkpoint_invalid")
        completion = _verified_completion_record(
            store=store,
            observation_id=latest_observation_id,
        )
        if completion["source_completion_sha256"] != latest_completion_sha256:
            raise CryptoDelayedPaperLearningError("learning_core_checkpoint_mismatch")
        receipt = _project_or_verify_completion(
            root=root,
            evolution_root=evolution_root,
            completion=completion,
        )
        state = _append_checkpoint(
            evolution_root=evolution_root,
            state=state,
            completion=completion,
            receipt=receipt,
        )
        return _learning_result(
            status="projected",
            observation_id=latest_observation_id,
            projected_completion_count=state["projected_completion_count"],
            checkpoint_head_sha256=state["checkpoint_head_sha256"],
        )


def _scrub_receipt(
    *,
    checkpoints: list[Mapping[str, Any]],
) -> dict[str, Any]:
    head = str(checkpoints[-1]["checkpoint_sha256"]) if checkpoints else None
    inventory = [
        {
            "sequence": checkpoint["sequence"],
            "observation_id": checkpoint["observation_id"],
            "source_completion_sha256": checkpoint["source_completion_sha256"],
            "projection_receipt_sha256": checkpoint["projection_receipt_sha256"],
        }
        for checkpoint in checkpoints
    ]
    receipt: dict[str, Any] = {
        "contract": LEARNING_FULL_SCRUB_CONTRACT,
        "completion_count": len(checkpoints),
        "checkpoint_head_sha256": head,
        "inventory_sha256": _sha256(inventory),
        **_non_authority_fields(),
    }
    receipt["full_scrub_sha256"] = _sha256(receipt)
    return receipt


def run_crypto_delayed_paper_learning_full_scrub(
    *,
    output_root: Path | str,
) -> dict[str, Any]:
    """Validate and repair the complete completion-to-projection chain."""

    _assert_simulation_only()
    root = Path(output_root)
    delayed_root = root / "delayed_paper"
    if not delayed_root.is_dir():
        return _learning_result(
            status="none",
            recovered_observation_count=0,
            completion_count=0,
        )
    store = CryptoDelayedPaperObservationStore(root)
    core = _core_completion_state(store)
    if core.get("pending_observation_id") is not None:
        return _learning_result(
            status="deferred_core_pending",
            recovered_observation_count=0,
        )
    evolution_root = _ensure_learning_directories(root)
    with _learning_lock(evolution_root, filename=".worker.lock"):
        completions = _completion_inventory(store)
        if len(completions) != int(core["completion_count"]):
            raise CryptoDelayedPaperLearningError(
                "learning_completion_inventory_mismatch"
            )
        checkpoints = _full_checkpoint_chain(evolution_root)
        if len(checkpoints) > len(completions):
            raise CryptoDelayedPaperLearningError("learning_checkpoint_orphaned")
        for checkpoint, completion in zip(
            checkpoints,
            completions,
        ):
            if (
                checkpoint.get("observation_id") != completion["observation_id"]
                or checkpoint.get("market_slot") != completion["market_slot"]
                or checkpoint.get("source_completion_sha256")
                != completion["source_completion_sha256"]
            ):
                raise CryptoDelayedPaperLearningError(
                    "learning_checkpoint_completion_order_mismatch"
                )
        claimed: dict[str, dict[str, Any]] = {}
        for checkpoint in checkpoints:
            observation_id = str(checkpoint["observation_id"])
            if observation_id in claimed:
                raise CryptoDelayedPaperLearningError(
                    "learning_checkpoint_duplicate_completion"
                )
            claimed[observation_id] = checkpoint
        completion_by_id = {
            completion["observation_id"]: completion for completion in completions
        }
        if not set(claimed).issubset(completion_by_id):
            raise CryptoDelayedPaperLearningError("learning_checkpoint_orphaned")
        receipts = _receipt_inventory(evolution_root)
        if not receipts.issubset(completion_by_id):
            raise CryptoDelayedPaperLearningError(
                "learning_projection_receipt_orphaned"
            )
        state_path = evolution_root / _STATE_FILENAME
        expected_state = _worker_state_payload(
            checkpoint=checkpoints[-1] if checkpoints else None
        )
        if state_path.exists() or state_path.is_symlink():
            persisted_state = _read_worker_state(state_path)
            if _canonical_json(persisted_state) != _canonical_json(expected_state):
                persisted_sequence = int(persisted_state["checkpoint_sequence"])
                persisted_checkpoint = _verify_incremental_head(
                    evolution_root=evolution_root,
                    state=persisted_state,
                )
                if persisted_sequence > len(checkpoints) or (
                    persisted_sequence
                    and checkpoints[persisted_sequence - 1].get("checkpoint_sha256")
                    != persisted_checkpoint.get("checkpoint_sha256")
                ):
                    raise CryptoDelayedPaperLearningError(
                        "learning_worker_state_head_mismatch"
                    )
                _write_json_atomic(state_path, expected_state)
        else:
            _write_json_atomic(state_path, expected_state)
        state = expected_state
        recovered: list[str] = []
        for completion in completions:
            observation_id = completion["observation_id"]
            checkpoint = claimed.get(observation_id)
            if checkpoint is not None:
                if observation_id not in receipts:
                    raise CryptoDelayedPaperLearningError(
                        "learning_claimed_projection_missing"
                    )
                receipt = _verify_projection_against_core(
                    root=root,
                    evolution_root=evolution_root,
                    observation_id=observation_id,
                )
                if (
                    checkpoint.get("source_completion_sha256")
                    != completion["source_completion_sha256"]
                    or checkpoint.get("market_slot") != completion["market_slot"]
                    or checkpoint.get("projection_receipt_sha256")
                    != receipt.get("projection_receipt_sha256")
                ):
                    raise CryptoDelayedPaperLearningError(
                        "learning_checkpoint_projection_mismatch"
                    )
                continue
            receipt = _project_or_verify_completion(
                root=root,
                evolution_root=evolution_root,
                completion=completion,
            )
            state = _append_checkpoint(
                evolution_root=evolution_root,
                state=state,
                completion=completion,
                receipt=receipt,
            )
            recovered.append(observation_id)
        final_checkpoints = _full_checkpoint_chain(evolution_root)
        if len(final_checkpoints) != len(completions):
            raise CryptoDelayedPaperLearningError("learning_full_scrub_incomplete")
        final_state = _worker_state_payload(
            checkpoint=(final_checkpoints[-1] if final_checkpoints else None)
        )
        _write_json_atomic(state_path, final_state)
        scrub = _scrub_receipt(checkpoints=final_checkpoints)
        scrub_key = scrub["checkpoint_head_sha256"] or "empty"
        _write_immutable_bytes(
            evolution_root / _SCRUB_DIRECTORY / f"{scrub_key}.json",
            (_canonical_json(scrub) + "\n").encode("utf-8"),
            max_bytes=MAX_RECEIPT_BYTES,
        )
        return _learning_result(
            status="recovered" if recovered else "scrubbed",
            recovered_observation_count=len(recovered),
            observation_ids=recovered,
            completion_count=len(completions),
            checkpoint_head_sha256=final_state["checkpoint_head_sha256"],
            full_scrub_sha256=scrub["full_scrub_sha256"],
        )


def recover_crypto_delayed_paper_learning(
    *,
    output_root: Path | str,
) -> dict[str, Any]:
    """Compatibility entry point for the independent full scrub."""

    return run_crypto_delayed_paper_learning_full_scrub(output_root=output_root)


__all__ = [
    "CHALLENGER_SUGGESTION_CONTRACT",
    "CryptoDelayedPaperLearningError",
    "LEARNING_CHECKPOINT_CONTRACT",
    "LEARNING_FULL_SCRUB_CONTRACT",
    "LEARNING_KPI_CONTRACT",
    "LEARNING_PROJECTION_RECEIPT_CONTRACT",
    "LEARNING_SAMPLE_CONTRACT",
    "LEARNING_WORKER_STATE_CONTRACT",
    "project_crypto_delayed_paper_learning",
    "recover_crypto_delayed_paper_learning",
    "run_crypto_delayed_paper_learning_full_scrub",
    "run_crypto_delayed_paper_learning_incremental",
]
