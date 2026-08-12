"""Manifest-independent, offline factor projections for a G4 round-trip epoch.

This is deliberately a research sink, not a runtime participant.  It reads
already-completed core observations, writes only an append-only factor-research
namespace, and refuses to start before one continuous 24-hour core window.
Neither this module nor its caller has access to orders, capital, Champion, or
network inputs.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterator, Mapping
import uuid

from Crypto.delayed_paper_ledger import (
    CryptoDelayedPaperLedgerError,
    CryptoDelayedPaperObservationStore,
    _market_slot,
    _read_json,
)
from Crypto.factor_research import (
    FACTOR_SET_ID,
    FACTOR_SET_VERSION,
    WINDOW_BARS,
    CryptoFactorResearchError,
    build_cross_asset_features,
    build_factor_snapshot,
    build_forward_label,
    evaluate_factor_hypotheses,
)
from Crypto.fixture_sim.contracts import _assert_simulation_only
from shared.governance.evidence_readiness import load_evidence_readiness_contract


FACTOR_PROJECTION_CONTRACT = "tradingagent.crypto.factor_projection.v1"
FACTOR_PROJECTION_RECEIPT_CONTRACT = "tradingagent.crypto.factor_projection_receipt.v1"
FACTOR_PROJECTION_CHECKPOINT_CONTRACT = (
    "tradingagent.crypto.factor_projection_checkpoint.v1"
)
OPERATIONAL_MATURITY_CONTINUOUS_COMPLETIONS = 288
_SYMBOLS = ("BTCUSDT", "ETHUSDT")
_HORIZONS = (60, 240, 720, 1440)
_MAX_FILE_BYTES = 2 * 1024 * 1024
SEGMENTED_LEARNING_CONSUMER_PROFILE_ID = "crypto-5m-ohlcv-13bar-forward-labels-v1"


class CryptoFactorProjectionError(RuntimeError):
    """Stable fail-closed error for detached factor projections."""


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
        raise CryptoFactorProjectionError("factor_projection_payload_invalid") from exc


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
        "contract": FACTOR_PROJECTION_CONTRACT,
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
        raise CryptoFactorProjectionError(
            "factor_projection_readiness_contract_invalid"
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
        raise CryptoFactorProjectionError(
            "factor_projection_readiness_contract_invalid"
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
    """Return the frozen feature/label definition for detached research only."""

    return {
        "consumer_profile_id": SEGMENTED_LEARNING_CONSUMER_PROFILE_ID,
        "feature_set_id": FACTOR_SET_ID,
        "feature_set_version": FACTOR_SET_VERSION,
        "window_bars": WINDOW_BARS,
        "bar_interval_minutes": 5,
        "required_label_horizon_minutes": 60,
        "auxiliary_attribution_horizons": list(_HORIZONS[1:]),
    }


def _utc(value: Any, *, reason: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CryptoFactorProjectionError(reason)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CryptoFactorProjectionError(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise CryptoFactorProjectionError(reason)
    return parsed.astimezone(timezone.utc)


def _assert_regular(path: Path, *, reason: str) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise CryptoFactorProjectionError(reason) from exc
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
            raise CryptoFactorProjectionError(reason)
        encoded = os.read(descriptor, _MAX_FILE_BYTES + 1)
        after = os.fstat(descriptor)
        if len(encoded) != before.st_size or after.st_size != before.st_size:
            raise CryptoFactorProjectionError(reason)
    except CryptoFactorProjectionError:
        raise
    except OSError as exc:
        raise CryptoFactorProjectionError(reason) from exc
    finally:
        os.close(descriptor)
    if not encoded.endswith(b"\n") or b"\x00" in encoded:
        raise CryptoFactorProjectionError(reason)
    return encoded


def _parse_canonical(path: Path, *, reason: str) -> dict[str, Any]:
    try:
        payload = json.loads(_assert_regular(path, reason=reason).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoFactorProjectionError(reason) from exc
    if not isinstance(payload, dict) or (
        _canonical_json(payload) + "\n"
    ).encode() != _assert_regular(path, reason=reason):
        raise CryptoFactorProjectionError(reason)
    return payload


def _write_immutable(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    if path.exists() or path.is_symlink():
        if (
            _assert_regular(path, reason="factor_projection_immutable_conflict")
            != encoded
        ):
            raise CryptoFactorProjectionError("factor_projection_immutable_conflict")
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
            _assert_regular(path, reason="factor_projection_immutable_conflict")
            != encoded
        ):
            raise CryptoFactorProjectionError("factor_projection_immutable_conflict")
    except OSError as exc:
        raise CryptoFactorProjectionError("factor_projection_write_failed") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _root(root: Path) -> Path:
    return root / "evolution" / "factor_research"


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
        raise CryptoFactorProjectionError("factor_projection_directory_invalid")
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
                raise CryptoFactorProjectionError("factor_projection_directory_invalid")
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
            raise CryptoFactorProjectionError("factor_projection_lock_invalid")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except CryptoFactorProjectionError:
        raise
    except OSError as exc:
        raise CryptoFactorProjectionError("factor_projection_lock_invalid") from exc
    finally:
        if "descriptor" in locals():
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _source(
    store: CryptoDelayedPaperObservationStore, observation_id: str
) -> dict[str, Any]:
    try:
        observation = _read_json(store._observation_path(observation_id))
        completion = _read_json(store._completion_path(observation_id))
        store._verify_observation(observation)
        store._verify_completion(completion, observation=observation)
    except (CryptoDelayedPaperLedgerError, OSError, ValueError) as exc:
        raise CryptoFactorProjectionError("factor_projection_source_invalid") from exc
    completion_sha256 = completion.get("completion_sha256")
    if (
        completion.get("status") != "completed"
        or not isinstance(completion_sha256, str)
        or len(completion_sha256) != 64
    ):
        raise CryptoFactorProjectionError("factor_projection_source_invalid")
    return {
        "observation": observation,
        "completion": completion,
        "completion_sha256": completion_sha256,
    }


def _sources(
    root: Path,
) -> tuple[CryptoDelayedPaperObservationStore, list[dict[str, Any]]]:
    required = (
        root / "delayed_paper",
        root / "delayed_paper" / "observations",
        root / "delayed_paper" / "completions",
        root / "delayed_paper" / ".lock",
    )
    if any(not path.exists() or path.is_symlink() for path in required):
        raise CryptoFactorProjectionError("factor_projection_root_incomplete")
    try:
        store = CryptoDelayedPaperObservationStore(root)
        checkpoint = store.runtime_checkpoint_read_only()
        state = store._observation_state_read_only()
    except (CryptoDelayedPaperLedgerError, OSError, ValueError) as exc:
        raise CryptoFactorProjectionError("factor_projection_core_invalid") from exc
    if checkpoint.get("pending") is not None:
        return store, []
    rows = [_source(store, path.stem) for path in store.completions_dir.glob("*.json")]
    rows.sort(
        key=lambda source: (
            _market_slot(source["observation"].get("market_slot")),
            source["observation"]["observation_id"],
        )
    )
    if (
        len(rows) != checkpoint.get("completion_count")
        or checkpoint.get("completion_count") != checkpoint.get("observation_count")
        or (
            rows
            and rows[-1]["observation"]["observation_id"]
            != state.get("latest_observation_id")
        )
    ):
        raise CryptoFactorProjectionError("factor_projection_core_inventory_invalid")
    return store, rows


def _latest_source(
    root: Path,
) -> tuple[CryptoDelayedPaperObservationStore, Mapping[str, Any], dict[str, Any]]:
    """Read only the current durable completion for routine projection work."""

    try:
        store = CryptoDelayedPaperObservationStore(root)
        checkpoint = store.runtime_checkpoint_read_only()
        state = store._observation_state_read_only()
    except (CryptoDelayedPaperLedgerError, OSError, ValueError) as exc:
        raise CryptoFactorProjectionError("factor_projection_core_invalid") from exc
    if checkpoint.get("pending") is not None:
        raise CryptoFactorProjectionError("factor_projection_core_pending")
    observation_id = state.get("latest_observation_id")
    if not isinstance(observation_id, str) or not observation_id:
        raise CryptoFactorProjectionError("factor_projection_core_inventory_invalid")
    completion_count = checkpoint.get("completion_count")
    if isinstance(completion_count, bool) or not isinstance(completion_count, int):
        raise CryptoFactorProjectionError("factor_projection_core_inventory_invalid")
    return store, checkpoint, _source(store, observation_id)


def _latest_continuous(rows: list[dict[str, Any]]) -> int:
    count = 0
    expected: datetime | None = None
    for source in reversed(rows):
        slot = _market_slot(source["observation"].get("market_slot"))
        if expected is not None and slot != expected:
            break
        count += 1
        expected = slot - timedelta(minutes=5)
    return count


def _segment_ids(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Assign a stable identifier to every contiguous completion segment."""

    result: dict[str, str] = {}
    previous: datetime | None = None
    segment_start: datetime | None = None
    for source in rows:
        observation = source["observation"]
        observation_id = observation.get("observation_id")
        slot = _market_slot(observation.get("market_slot"))
        if not isinstance(observation_id, str) or not observation_id:
            raise CryptoFactorProjectionError("factor_projection_source_invalid")
        if previous is None or slot - previous != timedelta(minutes=5):
            segment_start = slot
        if segment_start is None:
            raise CryptoFactorProjectionError("factor_projection_segment_invalid")
        result[observation_id] = "crypto-5m-segment-" + segment_start.strftime(
            "%Y%m%dT%H%M%SZ"
        )
        previous = slot
    return result


def _factor_input(
    source: Mapping[str, Any], symbol: str
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    observation = source["observation"]
    symbols = observation.get("symbols")
    if not isinstance(symbols, Mapping) or not isinstance(symbols.get(symbol), Mapping):
        raise CryptoFactorProjectionError("factor_projection_symbol_missing")
    item = symbols[symbol]
    raw_bars = item.get("bars")
    if not isinstance(raw_bars, list) or len(raw_bars) != 13:
        raise CryptoFactorProjectionError("factor_projection_window_invalid")
    converted: list[dict[str, Any]] = []
    for row in raw_bars:
        if not isinstance(row, Mapping):
            raise CryptoFactorProjectionError("factor_projection_window_invalid")
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
    last = raw_bars[-1]
    evidence = {
        "receipt_id": last.get("source_receipt_id"),
        "lineage_sha256": last.get("source_lineage_sha256"),
        "data_through": last.get("data_through"),
        "observed_at": last.get("observed_at"),
    }
    close = last.get("close")
    if not isinstance(close, str) or not close:
        raise CryptoFactorProjectionError("factor_projection_anchor_price_invalid")
    return converted, evidence, close


def _record(source: Mapping[str, Any], *, segment_id: str) -> dict[str, Any]:
    observation = source["observation"]
    snapshots: dict[str, dict[str, Any]] = {}
    prices: dict[str, str] = {}
    for symbol in _SYMBOLS:
        bars, evidence, price = _factor_input(source, symbol)
        snapshots[symbol] = build_factor_snapshot(
            observation_id=str(observation["observation_id"]),
            symbol=symbol,
            bars=bars,
            evidence=evidence,
        )
        prices[symbol] = price
    if not isinstance(segment_id, str) or not segment_id.startswith(
        "crypto-5m-segment-"
    ):
        raise CryptoFactorProjectionError("factor_projection_segment_invalid")
    record = {
        "contract": FACTOR_PROJECTION_CONTRACT,
        "event_type": "factor_projection",
        "observation_id": observation["observation_id"],
        "source_completion_sha256": source["completion_sha256"],
        "source_observation_content_sha256": observation.get(
            "observation_content_sha256"
        ),
        "market_slot": observation.get("market_slot"),
        "segment_id": segment_id,
        "segmented_learning_consumer_profile_id": (
            SEGMENTED_LEARNING_CONSUMER_PROFILE_ID
        ),
        "snapshots": snapshots,
        "cross_asset": build_cross_asset_features(
            snapshots=[snapshots["BTCUSDT"], snapshots["ETHUSDT"]]
        ),
        "label_anchor_prices": prices,
        **_non_authority_fields(),
    }
    record["factor_projection_sha256"] = _sha256(record)
    return record


def _receipt(record: Mapping[str, Any]) -> dict[str, Any]:
    row = {
        "contract": FACTOR_PROJECTION_RECEIPT_CONTRACT,
        "observation_id": record["observation_id"],
        "source_completion_sha256": record["source_completion_sha256"],
        "factor_projection_sha256": record["factor_projection_sha256"],
        **_non_authority_fields(),
    }
    row["projection_receipt_sha256"] = _sha256(row)
    return row


def _checkpoint(
    sequence: int, previous: str | None, receipt: Mapping[str, Any]
) -> dict[str, Any]:
    row = {
        "contract": FACTOR_PROJECTION_CHECKPOINT_CONTRACT,
        "sequence": sequence,
        "previous_checkpoint_sha256": previous,
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
        row = _parse_canonical(path, reason="factor_projection_checkpoint_invalid")
        material = dict(row)
        claimed = material.pop("checkpoint_sha256", None)
        expected = len(rows) + 1
        previous = rows[-1]["checkpoint_sha256"] if rows else None
        if (
            path.name != f"{expected:012d}.json"
            or row.get("contract") != FACTOR_PROJECTION_CHECKPOINT_CONTRACT
            or row.get("sequence") != expected
            or row.get("previous_checkpoint_sha256") != previous
            or claimed != _sha256(material)
            or any(
                row.get(key) != value for key, value in _non_authority_fields().items()
            )
        ):
            raise CryptoFactorProjectionError("factor_projection_checkpoint_invalid")
        rows.append(row)
    return rows


def _latest_checkpoint(evolution: Path) -> tuple[int, dict[str, Any] | None]:
    """Bounded routine checkpoint read; daily full scrub validates the full chain."""

    paths = sorted((evolution / "checkpoints").glob("*.json"))
    if not paths:
        return 0, None
    sequence = len(paths)
    path = paths[-1]
    row = _parse_canonical(path, reason="factor_projection_checkpoint_invalid")
    material = dict(row)
    claimed = material.pop("checkpoint_sha256", None)
    if (
        path.name != f"{sequence:012d}.json"
        or row.get("contract") != FACTOR_PROJECTION_CHECKPOINT_CONTRACT
        or row.get("sequence") != sequence
        or claimed != _sha256(material)
        or any(row.get(key) != value for key, value in _non_authority_fields().items())
    ):
        raise CryptoFactorProjectionError("factor_projection_checkpoint_invalid")
    return sequence, row


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
            reason="factor_projection_snapshot_slot_invalid",
        )
        for horizon in _HORIZONS:
            target = future_records.get(
                (slot + timedelta(minutes=horizon)).isoformat().replace("+00:00", "Z")
            )
            if target is None:
                continue
            target_source = target.get("source")
            target_record = target.get("record")
            if not isinstance(target_source, Mapping) or not isinstance(
                target_record, Mapping
            ):
                raise CryptoFactorProjectionError(
                    "factor_projection_future_source_invalid"
                )
            if target_record.get("segment_id") != record.get("segment_id"):
                continue
            _, evidence, exit_price = _factor_input(target_source, symbol)
            label = build_forward_label(
                snapshot=snapshot,
                horizon_minutes=horizon,
                future_market_slot=target_record["snapshots"][symbol]["market_slot"],
                entry_price=prices[symbol],
                exit_price=exit_price,
                future_evidence=evidence,
            )
            _write_immutable(
                _label_path(root, str(record["observation_id"]), symbol, horizon), label
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
        or required_horizon != 60
        or auxiliary_horizons != list(_HORIZONS[1:])
    ):
        raise CryptoFactorProjectionError("factor_projection_consumer_profile_invalid")
    samples: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    eligible_observation_ids: list[str] = []
    for item in records.values():
        record = item.get("record")
        if not isinstance(record, Mapping):
            raise CryptoFactorProjectionError("factor_projection_record_invalid")
        observation_id = record.get("observation_id")
        segment_id = record.get("segment_id")
        if (
            not isinstance(observation_id, str)
            or not isinstance(segment_id, str)
            or record.get("segmented_learning_consumer_profile_id") != profile_id
        ):
            raise CryptoFactorProjectionError("factor_projection_record_invalid")
        observation_eligible = True
        symbol_samples: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for symbol in _SYMBOLS:
            snapshot = record.get("snapshots", {}).get(symbol)
            if not isinstance(snapshot, Mapping):
                raise CryptoFactorProjectionError("factor_projection_record_invalid")
            slot = _utc(
                snapshot.get("market_slot"),
                reason="factor_projection_snapshot_slot_invalid",
            )
            labels: dict[int, Mapping[str, Any]] = {}
            for horizon in (required_horizon,):
                target = records.get(
                    (slot + timedelta(minutes=horizon))
                    .isoformat()
                    .replace("+00:00", "Z")
                )
                if (
                    not isinstance(target, Mapping)
                    or not isinstance(target.get("record"), Mapping)
                    or target["record"].get("segment_id") != segment_id
                ):
                    observation_eligible = False
                    break
                path = _label_path(root, observation_id, symbol, horizon)
                if not path.exists():
                    observation_eligible = False
                    break
                label = _parse_canonical(path, reason="factor_projection_label_invalid")
                material = dict(label)
                claimed = material.pop("forward_label_sha256", None)
                if (
                    label.get("observation_id") != observation_id
                    or label.get("symbol") != symbol
                    or label.get("horizon_minutes") != horizon
                    or label.get("source_factor_snapshot_sha256")
                    != snapshot.get("factor_snapshot_sha256")
                    or claimed != _sha256(material)
                ):
                    raise CryptoFactorProjectionError("factor_projection_label_invalid")
                labels[horizon] = label
            if not observation_eligible:
                break
            symbol_samples.append((snapshot, labels[required_horizon]))
        if observation_eligible:
            samples.extend(symbol_samples)
            eligible_observation_ids.append(observation_id)
    return samples, eligible_observation_ids


def run_crypto_delayed_paper_factor_research_full_scrub(
    *, output_root: Path | str, input_root: Path | str | None = None
) -> dict[str, Any]:
    """Full-scrub independent contiguous segments without crossing a gap."""

    _assert_simulation_only()
    root = Path(output_root)
    source_root = Path(input_root) if input_root is not None else root
    policy = _segmented_learning_policy()
    consumer_profile = _segmented_learning_consumer_profile()
    _, sources = _sources(source_root)
    if not sources:
        return _result(status="deferred_core_pending")
    continuous = _latest_continuous(sources)
    segments = _segment_ids(sources)
    try:
        evolution = _ensure_root(root)
        with _lock(evolution):
            checkpoints = _read_checkpoints(evolution)
            if len(checkpoints) > len(sources):
                raise CryptoFactorProjectionError(
                    "factor_projection_checkpoint_orphaned"
                )
            records: dict[str, dict[str, Any]] = {}
            recovered = 0
            for sequence, source in enumerate(sources, start=1):
                observation_id = str(source["observation"]["observation_id"])
                record = _record(source, segment_id=segments[observation_id])
                receipt = _receipt(record)
                paths = _paths(root, observation_id)
                if sequence <= len(checkpoints) and any(
                    not path.is_file() or path.is_symlink() for path in paths.values()
                ):
                    raise CryptoFactorProjectionError(
                        "factor_projection_claimed_record_missing"
                    )
                for name, payload in (("record", record), ("receipt", receipt)):
                    path = paths[name]
                    if path.exists() or path.is_symlink():
                        if _canonical_json(
                            _parse_canonical(
                                path, reason="factor_projection_record_invalid"
                            )
                        ) != _canonical_json(payload):
                            raise CryptoFactorProjectionError(
                                "factor_projection_not_derived"
                            )
                    _write_immutable(path, payload)
                if sequence <= len(checkpoints):
                    checkpoint = checkpoints[sequence - 1]
                    if (
                        checkpoint.get("observation_id") != observation_id
                        or checkpoint.get("source_completion_sha256")
                        != receipt["source_completion_sha256"]
                        or checkpoint.get("projection_receipt_sha256")
                        != receipt["projection_receipt_sha256"]
                    ):
                        raise CryptoFactorProjectionError(
                            "factor_projection_checkpoint_source_mismatch"
                        )
                else:
                    checkpoint = _checkpoint(
                        sequence,
                        checkpoints[-1]["checkpoint_sha256"] if checkpoints else None,
                        receipt,
                    )
                    _write_immutable(
                        evolution / "checkpoints" / f"{sequence:012d}.json", checkpoint
                    )
                    checkpoints.append(checkpoint)
                    recovered += 1
                records[record["snapshots"]["BTCUSDT"]["market_slot"]] = {
                    "record": record,
                    "source": source,
                }
            label_count = sum(
                _labels(root, item["record"], records) for item in records.values()
            )
            samples, eligible_observation_ids = _learning_eligible_samples(
                root,
                records,
                consumer_profile=consumer_profile,
            )
            report = evaluate_factor_hypotheses(samples)
    except CryptoFactorResearchError as exc:
        raise CryptoFactorProjectionError(
            "factor_projection_factor_input_invalid"
        ) from exc
    return _result(
        status="recovered" if recovered else "scrubbed",
        completion_count=len(sources),
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


def run_crypto_delayed_paper_factor_research_incremental(
    *, output_root: Path | str
) -> dict[str, Any]:
    """Append one new unlabelled observation after a verified full-scrub base.

    This routine deliberately reads only the current core checkpoint, its
    current completion, and the preceding projected record. It never queries
    TradingDatas and never retroactively scans history for labels; daily full
    scrub performs that complete semantic validation and label completion.
    """

    _assert_simulation_only()
    root = Path(output_root)
    policy = _segmented_learning_policy()
    consumer_profile = _segmented_learning_consumer_profile()
    _, checkpoint, source = _latest_source(root)
    core_count = checkpoint["completion_count"]
    evolution = _root(root)
    if not evolution.exists():
        return _result(
            status="full_scrub_required",
            completion_count=core_count,
            label_count=0,
            reason="factor_projection_checkpoint_missing",
            segmented_learning_policy=policy,
            segmented_learning_profile=consumer_profile,
            label_learning_eligible_sample_count=0,
        )
    try:
        with _lock(evolution):
            sequence, previous_checkpoint = _latest_checkpoint(evolution)
            observation = source["observation"]
            observation_id = str(observation["observation_id"])
            source_completion = source["completion_sha256"]
            if core_count < sequence:
                raise CryptoFactorProjectionError(
                    "factor_projection_checkpoint_orphaned"
                )
            if core_count == sequence:
                if (
                    previous_checkpoint is None
                    or previous_checkpoint.get("observation_id") != observation_id
                    or previous_checkpoint.get("source_completion_sha256")
                    != source_completion
                ):
                    raise CryptoFactorProjectionError(
                        "factor_projection_incremental_source_mismatch"
                    )
                return _result(
                    status="up_to_date",
                    completion_count=core_count,
                    label_count=0,
                    segmented_learning_policy=policy,
                    segmented_learning_profile=consumer_profile,
                    label_learning_eligible_sample_count=0,
                )
            if core_count != sequence + 1:
                return _result(
                    status="full_scrub_required",
                    completion_count=core_count,
                    checkpoint_count=sequence,
                    label_count=0,
                    reason="factor_projection_incremental_backlog",
                    segmented_learning_policy=policy,
                    segmented_learning_profile=consumer_profile,
                    label_learning_eligible_sample_count=0,
                )
            if previous_checkpoint is None:
                segment_id = "crypto-5m-segment-" + _market_slot(
                    observation["market_slot"]
                ).strftime("%Y%m%dT%H%M%SZ")
            else:
                previous_record = _parse_canonical(
                    _paths(root, str(previous_checkpoint["observation_id"]))["record"],
                    reason="factor_projection_record_invalid",
                )
                previous_slot = _utc(
                    previous_record.get("market_slot"),
                    reason="factor_projection_record_invalid",
                )
                current_slot = _market_slot(observation["market_slot"])
                existing_segment = previous_record.get("segment_id")
                if not isinstance(existing_segment, str):
                    raise CryptoFactorProjectionError(
                        "factor_projection_segment_invalid"
                    )
                segment_id = (
                    existing_segment
                    if current_slot - previous_slot == timedelta(minutes=5)
                    else "crypto-5m-segment-" + current_slot.strftime("%Y%m%dT%H%M%SZ")
                )
            record = _record(source, segment_id=segment_id)
            receipt = _receipt(record)
            paths = _paths(root, observation_id)
            _write_immutable(paths["record"], record)
            _write_immutable(paths["receipt"], receipt)
            new_checkpoint = _checkpoint(
                sequence + 1,
                previous_checkpoint["checkpoint_sha256"]
                if previous_checkpoint is not None
                else None,
                receipt,
            )
            _write_immutable(
                evolution / "checkpoints" / f"{sequence + 1:012d}.json",
                new_checkpoint,
            )
    except CryptoFactorProjectionError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise CryptoFactorProjectionError(
            "factor_projection_incremental_invalid"
        ) from exc
    return _result(
        status="projected_incremental",
        completion_count=core_count,
        label_count=0,
        label_status="observation_only_pending_daily_scrub",
        segmented_learning_policy=policy,
        segmented_learning_profile=consumer_profile,
        label_learning_eligible_sample_count=0,
    )


def factor_projection_exit_code(result: Mapping[str, Any]) -> int:
    if not isinstance(result, Mapping) or result.get("status") not in {
        "recovered",
        "scrubbed",
        "projected_incremental",
        "up_to_date",
        "full_scrub_required",
        "deferred_core_pending",
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
    "CryptoFactorProjectionError",
    "FACTOR_PROJECTION_CONTRACT",
    "OPERATIONAL_MATURITY_CONTINUOUS_COMPLETIONS",
    "factor_projection_exit_code",
    "run_crypto_delayed_paper_factor_research_incremental",
    "run_crypto_delayed_paper_factor_research_full_scrub",
]
