#!/usr/bin/env python3
"""Append-only, sim-only journal for prediction and learning samples.

The journal is deliberately outside the execution path.  It records every
candidate/style prediction before strategy thresholds are considered, keeps
sample layers distinct, and projects immutable label updates into the existing
``sample_kpi`` read model.  It never calls a broker or creates style capital.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import errno
import fcntl
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import stat
from typing import Any, Iterator, Mapping, Optional, Sequence, Union

from shared.review.forward_labels import (
    build_prediction_snapshot,
    materialize_forward_labels,
    _stable_label_update_id,
)
from shared.review.sample_kpi import (
    SAMPLE_LAYERS,
    build_sample_kpi,
    classify_sample_layers,
)
from shared.execution.execution_lineage import (
    ASHARE_AUTHORITY_GENERATION,
    ASHARE_CAPITAL_AUTHORITY_ID,
    ASHARE_EXECUTION_LINEAGE_ID,
)


JOURNAL_SCHEMA_VERSION = 2

_LIVE_BOOLEAN_FIELDS = {
    "real_trading_enabled",
    "live_execution_enabled",
    "real_money_enabled",
    "live_broker_enabled",
    "direct_execution_enabled",
    "real_order_enabled",
    "production_execution_enabled",
    "is_live",
}
_LIVE_MODE_FIELDS = {
    "account_type",
    "capital_layer",
    "execution_mode",
    "trading_mode",
}
_TRUE_VALUES = {"1", "true", "yes", "on", "enabled", "live", "real", "production"}
_LIVE_MODE_VALUES = {"live", "real", "production", "real_money"}
_MUTUALLY_EXCLUSIVE_LAYERS = {
    "observation_counterfactual",
    "exploration_fill",
    "exploitation_fill",
    "risk_reject",
    "chain_validation",
}


class JournalError(RuntimeError):
    """Base class for sample-journal failures."""


class JournalConflictError(JournalError):
    """An idempotency identity was reused with different immutable content."""


class JournalSafetyError(JournalError):
    """A live, unsafe-path, malformed, or mixed-layer input was rejected."""


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )
    except (TypeError, ValueError) as exc:
        raise JournalSafetyError("sample payload is not canonical JSON: %s" % exc)


def _payload_sha256(value: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _prediction_content_sha256(value: Mapping[str, Any]) -> str:
    content = deepcopy(dict(value))
    for field in (
        "journal_schema_version",
        "journal_payload_sha256",
        "journal_event_type",
        "journal_event_id",
        "sample_cluster_id",
        "cluster_role",
        "maturity_weight",
        "prediction_content_sha256",
    ):
        content.pop(field, None)
    return _payload_sha256(content)


def _prediction_cluster_id(value: Mapping[str, Any]) -> str:
    raw_timestamp = str(value.get("prediction_at") or "").strip()
    try:
        parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if (
        parsed is not None
        and parsed.tzinfo is not None
        and parsed.utcoffset() is not None
    ):
        bucket = parsed.replace(
            minute=(parsed.minute // 5) * 5,
            second=0,
            microsecond=0,
        ).isoformat()
    else:
        bucket = "invalid_prediction_time:%s" % raw_timestamp
    marketgraph = value.get("marketgraph")
    ablation_group = (
        str(marketgraph.get("ablation_group") or "unknown")
        if isinstance(marketgraph, Mapping)
        else "unknown"
    )
    identity = {
        "capital_authority_id": value.get("capital_authority_id"),
        "authority_generation": value.get("authority_generation"),
        "execution_lineage_id": value.get("execution_lineage_id"),
        "market": str(value.get("market") or "").strip().lower(),
        "symbol": str(value.get("symbol") or "").strip().upper(),
        "style": value.get("style") or value.get("style_id"),
        "strategy_version": value.get("strategy_version") or value.get("style_version"),
        "ablation_group": ablation_group,
        "five_minute_bucket": bucket,
    }
    return "sample-cluster:" + _payload_sha256(identity)[:32]


def _decision_cluster_id(value: Mapping[str, Any]) -> str:
    """Collapse style, MG arm and horizon cells into one decision opportunity."""

    raw_base = str(value.get("base_snapshot_sha256") or "").strip().lower()
    if _is_64hex(raw_base):
        identity: Mapping[str, Any] = {
            "capital_authority_id": value.get("capital_authority_id"),
            "authority_generation": value.get("authority_generation"),
            "execution_lineage_id": value.get("execution_lineage_id"),
            "base_snapshot_sha256": raw_base,
        }
    else:
        raw_timestamp = str(value.get("prediction_at") or "").strip()
        try:
            parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            bucket = parsed.replace(
                minute=(parsed.minute // 5) * 5,
                second=0,
                microsecond=0,
            ).isoformat()
        else:
            bucket = "invalid_prediction_time:%s" % raw_timestamp
        identity = {
            "capital_authority_id": value.get("capital_authority_id"),
            "authority_generation": value.get("authority_generation"),
            "execution_lineage_id": value.get("execution_lineage_id"),
            "market": str(value.get("market") or "").strip().lower(),
            "symbol": str(value.get("symbol") or "").strip().upper(),
            "five_minute_bucket": bucket,
        }
    return "decision-cluster:" + _payload_sha256(identity)[:32]


def _current_authority_scope(value: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    scope = dict(value or {})
    authority_id = str(
        scope.get("capital_authority_id") or ASHARE_CAPITAL_AUTHORITY_ID
    ).strip()
    generation = scope.get("authority_generation", ASHARE_AUTHORITY_GENERATION)
    lineage_id = str(
        scope.get("execution_lineage_id") or ASHARE_EXECUTION_LINEAGE_ID
    ).strip()
    if authority_id != ASHARE_CAPITAL_AUTHORITY_ID:
        raise JournalSafetyError("current A-share capital authority required")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation != ASHARE_AUTHORITY_GENERATION
    ):
        raise JournalSafetyError("current A-share authority generation required")
    if lineage_id != ASHARE_EXECUTION_LINEAGE_ID:
        raise JournalSafetyError("current A-share execution lineage required")
    return {
        "capital_authority_id": authority_id,
        "authority_generation": generation,
        "execution_lineage_id": lineage_id,
    }


def _record_in_authority(
    record: Mapping[str, Any], authority_scope: Mapping[str, Any]
) -> bool:
    return (
        str(record.get("capital_authority_id") or "")
        == authority_scope["capital_authority_id"]
        and record.get("authority_generation")
        == authority_scope["authority_generation"]
        and str(record.get("execution_lineage_id") or "")
        == authority_scope["execution_lineage_id"]
    )


def _is_64hex(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return len(raw) == 64 and all(character in "0123456789abcdef" for character in raw)


def _strict_evolution_evidence(record: Mapping[str, Any]) -> bool:
    kind = (
        str(
            record.get("record_type")
            or record.get("event_type")
            or record.get("type")
            or ""
        )
        .strip()
        .lower()
    )
    if kind != "completed_round_trip" and record.get("round_trip_complete") is not True:
        return True
    if (
        record.get("round_trip_complete") is not True
        or record.get("execution_eligible") is not True
        or str(record.get("costs_cover") or "") != "round_trip"
        or not str(record.get("prediction_snapshot_id") or "").strip()
        or not _is_64hex(record.get("source_snapshot_sha256"))
        or not _is_64hex(record.get("content_sha256"))
    ):
        return False
    net_field = (
        "net_pnl_cny" if record.get("net_pnl_cny") is not None else "post_cost_pnl_cny"
    )
    for field in ("gross_pnl_cny", net_field, "fee_cny", "slippage_cny"):
        value = record.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        if not math.isfinite(float(value)):
            return False
        if field in {"fee_cny", "slippage_cny"} and float(value) < 0.0:
            return False
    return True


def _has_positive_maturity_weight(record: Mapping[str, Any]) -> bool:
    try:
        value = float(record.get("maturity_weight", 1.0))
    except (TypeError, ValueError):
        return False
    return math.isfinite(value) and value > 0.0


def _is_truthy_live_flag(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    return str(value or "").strip().lower() in _TRUE_VALUES


def _find_live_marker(value: Any, path: str = "payload") -> Optional[str]:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key).strip().lower()
            child_path = "%s.%s" % (path, raw_key)
            if key in _LIVE_BOOLEAN_FIELDS and _is_truthy_live_flag(nested):
                return child_path
            if (
                key in _LIVE_MODE_FIELDS
                and str(nested or "").strip().lower() in _LIVE_MODE_VALUES
            ):
                return child_path
            found = _find_live_marker(nested, child_path)
            if found:
                return found
    elif isinstance(value, (list, tuple, set)):
        for index, nested in enumerate(value):
            found = _find_live_marker(nested, "%s[%d]" % (path, index))
            if found:
                return found
    return None


def _reject_live_markers(value: Any) -> None:
    marker = _find_live_marker(value)
    if marker:
        raise JournalSafetyError("live trading marker rejected at %s" % marker)


def _force_sim_only(record: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(record))
    result["capital_layer"] = "simulated"
    result["account_type"] = "simulated"
    for field in _LIVE_BOOLEAN_FIELDS:
        result[field] = False
    return result


def _absolute_without_resolving(path: Union[str, os.PathLike[str]]) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if not os.path.lexists(str(current)):
            continue
        try:
            mode = os.lstat(str(current)).st_mode
        except OSError as exc:
            raise JournalSafetyError(
                "cannot inspect journal path %s: %s" % (current, exc)
            )
        if stat.S_ISLNK(mode):
            raise JournalSafetyError(
                "journal path or parent is a symlink: %s" % current
            )


def _nofollow_flag() -> int:
    return int(getattr(os, "O_NOFOLLOW", 0))


def _verified_event(raw: Any, line_number: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise JournalSafetyError("journal line %d is not an object" % line_number)
    event = dict(raw)
    expected = str(event.get("journal_payload_sha256") or "")
    if not expected:
        raise JournalSafetyError(
            "journal line %d has no payload fingerprint" % line_number
        )
    unsigned = dict(event)
    unsigned.pop("journal_payload_sha256", None)
    if _payload_sha256(unsigned) != expected:
        raise JournalSafetyError(
            "journal line %d payload fingerprint mismatch" % line_number
        )
    if event.get("real_trading_enabled") is not False:
        raise JournalSafetyError(
            "journal line %d is not explicitly sim-only" % line_number
        )
    _reject_live_markers(event)
    return event


class SampleJournal:
    """Process-locked append-only JSONL journal and its read projection."""

    def __init__(self, path: Union[str, os.PathLike[str]]) -> None:
        self.path = _absolute_without_resolving(path)
        self.lock_path = self.path.with_name(".%s.lock" % self.path.name)

    def _check_paths(self) -> None:
        _assert_no_symlink_components(self.path.parent)
        _assert_no_symlink_components(self.path)
        _assert_no_symlink_components(self.lock_path)

    def _prepare_for_write(self) -> None:
        self._check_paths()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise JournalSafetyError("cannot create journal parent: %s" % exc)
        self._check_paths()

    @contextmanager
    def _locked(self, *, exclusive: bool, create_parent: bool) -> Iterator[None]:
        if create_parent:
            self._prepare_for_write()
        else:
            self._check_paths()
        flags = os.O_RDWR | os.O_CREAT | _nofollow_flag()
        try:
            fd = os.open(str(self.lock_path), flags, 0o600)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise JournalSafetyError("journal lock is a symlink")
            raise JournalSafetyError("cannot open journal lock: %s" % exc)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            self._check_paths()
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _load_unlocked(self) -> list[dict[str, Any]]:
        if not os.path.exists(str(self.path)):
            return []
        flags = os.O_RDONLY | _nofollow_flag()
        try:
            fd = os.open(str(self.path), flags)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise JournalSafetyError("journal file is a symlink")
            raise JournalSafetyError("cannot open sample journal: %s" % exc)
        events: list[dict[str, Any]] = []
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                for line_number, raw_line in enumerate(handle, start=1):
                    if not raw_line.strip():
                        continue
                    try:
                        raw = json.loads(raw_line)
                    except json.JSONDecodeError as exc:
                        raise JournalSafetyError(
                            "journal line %d is malformed JSON: %s" % (line_number, exc)
                        )
                    events.append(_verified_event(raw, line_number))
        finally:
            if fd >= 0:
                os.close(fd)
        return events

    def _append_many_unlocked(self, events: Sequence[Mapping[str, Any]]) -> None:
        if not events:
            return
        payload = "".join(_canonical_json(event) + "\n" for event in events).encode(
            "utf-8"
        )
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _nofollow_flag()
        try:
            fd = os.open(str(self.path), flags, 0o600)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise JournalSafetyError("journal file is a symlink")
            raise JournalSafetyError("cannot append sample journal: %s" % exc)
        try:
            written = 0
            while written < len(payload):
                count = os.write(fd, payload[written:])
                if count <= 0:
                    raise JournalSafetyError(
                        "short write while appending sample journal"
                    )
                written += count
            os.fsync(fd)
        finally:
            os.close(fd)

    def _append_unlocked(self, event: Mapping[str, Any]) -> None:
        self._append_many_unlocked([event])

    @staticmethod
    def _seal_event(event: Mapping[str, Any]) -> dict[str, Any]:
        sealed = deepcopy(dict(event))
        sealed["journal_schema_version"] = JOURNAL_SCHEMA_VERSION
        sealed.pop("journal_payload_sha256", None)
        sealed["journal_payload_sha256"] = _payload_sha256(sealed)
        return sealed

    @staticmethod
    def _result(status: str, record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "status": status,
            "record": deepcopy(dict(record)),
            "real_trading_enabled": False,
            "live_execution_enabled": False,
        }

    @classmethod
    def _prediction_event(cls, candidate: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(candidate, Mapping):
            raise TypeError("candidate must be a mapping")
        _reject_live_markers(candidate)
        event = build_prediction_snapshot(candidate)
        event = _force_sim_only(event)
        event["record_type"] = "prediction"
        event["journal_event_type"] = "prediction_snapshot"
        event["journal_event_id"] = "prediction_snapshot:%s" % event["snapshot_id"]
        event["sample_cluster_id"] = _prediction_cluster_id(event)
        event["decision_cluster_id"] = str(
            event.get("decision_cluster_id") or _decision_cluster_id(event)
        )
        event["cluster_role"] = "origin"
        event["maturity_weight"] = 1.0
        event["prediction_content_sha256"] = _prediction_content_sha256(event)
        return cls._seal_event(event)

    def append_prediction(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        """Persist one candidate/style snapshot regardless of strategy thresholds."""

        return self.append_predictions([candidate])[0]

    def append_predictions(
        self, candidates: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Atomically validate and append a prediction batch with one fsync."""

        if isinstance(candidates, (str, bytes, bytearray)):
            raise TypeError("candidates must be a sequence of mappings")
        events = [self._prediction_event(candidate) for candidate in candidates]
        if not events:
            return []

        with self._locked(exclusive=True, create_parent=True):
            current_events = self._load_unlocked()
            existing = {
                str(row.get("snapshot_id") or ""): row
                for row in current_events
                if row.get("journal_event_type") == "prediction_snapshot"
                and str(row.get("snapshot_id") or "")
            }
            occupied_clusters = {
                str(row.get("sample_cluster_id") or "")
                for row in current_events
                if row.get("journal_event_type") == "prediction_snapshot"
                and str(row.get("sample_cluster_id") or "")
            }
            pending: dict[str, dict[str, Any]] = {}
            results: list[dict[str, Any]] = []
            for event in events:
                snapshot_id = str(event["snapshot_id"])
                prior = existing.get(snapshot_id) or pending.get(snapshot_id)
                if prior is not None:
                    if prior.get("prediction_content_sha256") != event.get(
                        "prediction_content_sha256"
                    ):
                        raise JournalConflictError(
                            "snapshot_id %s already exists with different content"
                            % snapshot_id
                        )
                    results.append(self._result("idempotent", prior))
                    continue
                cluster_id = str(event.get("sample_cluster_id") or "")
                if cluster_id in occupied_clusters:
                    event = deepcopy(event)
                    event["cluster_role"] = "duplicate"
                    event["maturity_weight"] = 0.0
                    event = self._seal_event(event)
                else:
                    occupied_clusters.add(cluster_id)
                pending[snapshot_id] = event
                results.append(self._result("appended", event))
            # No bytes are written until every identity has passed conflict
            # validation, so a late conflict cannot partially append the batch.
            self._append_many_unlocked(list(pending.values()))
        return results

    @staticmethod
    def _validated_layers(record: Mapping[str, Any]) -> tuple[str, ...]:
        explicit: set[str] = set()
        if isinstance(record.get("sample_layers"), (list, tuple, set)):
            explicit.update(
                str(value or "").strip().lower() for value in record["sample_layers"]
            )
        if record.get("sample_layer") is not None:
            explicit.add(str(record.get("sample_layer") or "").strip().lower())
        unknown = {value for value in explicit if value and value not in SAMPLE_LAYERS}
        if unknown:
            raise JournalSafetyError("unknown sample layer: %s" % sorted(unknown)[0])

        layers = classify_sample_layers(record)
        if not layers:
            raise JournalSafetyError("sample record has no recognized sample layer")
        exclusive = _MUTUALLY_EXCLUSIVE_LAYERS.intersection(layers)
        if len(exclusive) > 1:
            raise JournalSafetyError(
                "mutually exclusive sample layers cannot be mixed: %s"
                % ",".join(sorted(exclusive))
            )
        return layers

    @classmethod
    def _sample_event(cls, sample: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(sample, Mapping):
            raise TypeError("sample must be a mapping")
        _reject_live_markers(sample)
        kind = (
            str(
                sample.get("record_type")
                or sample.get("event_type")
                or sample.get("type")
                or ""
            )
            .strip()
            .lower()
        )
        if kind in {
            "prediction",
            "observation",
            "counterfactual",
            "candidate_prediction",
        }:
            raise JournalSafetyError("prediction samples must use append_prediction")

        event = _force_sim_only(sample)
        layers = cls._validated_layers(event)
        event["sample_layers"] = list(layers)
        if len(layers) == 1:
            event["sample_layer"] = layers[0]
        event["journal_event_type"] = "sample_event"
        supplied_id = str(
            event.get("journal_event_id")
            or event.get("event_id")
            or event.get("sample_id")
            or ""
        ).strip()
        if supplied_id:
            event["journal_event_id"] = "sample:%s" % supplied_id
        else:
            event["journal_event_id"] = "sample:%s" % _payload_sha256(event)[:32]
        return cls._seal_event(event)

    def append_samples(
        self,
        samples: Sequence[Mapping[str, Any]],
        *,
        expected_event_count: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Atomically append a sample batch against an optional journal head.

        ``expected_event_count`` lets a caller safely derive paired events from
        a prior replay.  Any concurrent append invalidates that replay instead
        of allowing a stale exit to close the same entry twice.
        """

        if isinstance(samples, (str, bytes, bytearray)):
            raise TypeError("samples must be a sequence of mappings")
        if expected_event_count is not None and (
            not isinstance(expected_event_count, int)
            or isinstance(expected_event_count, bool)
            or expected_event_count < 0
        ):
            raise ValueError("expected_event_count must be a non-negative integer")
        prepared = [self._sample_event(sample) for sample in samples]
        if not prepared:
            return []

        with self._locked(exclusive=True, create_parent=True):
            events = self._load_unlocked()
            if expected_event_count is not None and len(events) != expected_event_count:
                raise JournalConflictError(
                    "journal changed during outcome pairing: expected %d events, found %d"
                    % (expected_event_count, len(events))
                )
            existing = {
                str(row.get("journal_event_id") or ""): row
                for row in events
                if str(row.get("journal_event_id") or "")
            }
            pending: dict[str, dict[str, Any]] = {}
            results: list[dict[str, Any]] = []
            for event in prepared:
                event_id = str(event["journal_event_id"])
                prior = existing.get(event_id) or pending.get(event_id)
                if prior is not None:
                    if prior.get("journal_payload_sha256") != event.get(
                        "journal_payload_sha256"
                    ):
                        raise JournalConflictError(
                            "journal_event_id %s already exists with different content"
                            % event_id
                        )
                    results.append(self._result("idempotent", prior))
                    continue
                pending[event_id] = event
                results.append(self._result("appended", event))
            self._append_many_unlocked(list(pending.values()))
        return results

    def append_sample(self, sample: Mapping[str, Any]) -> dict[str, Any]:
        """Append a non-prediction sample while preserving layer separation."""

        return self.append_samples([sample])[0]

    def materialize_labels(
        self,
        snapshot_id: str,
        price_points: Sequence[Mapping[str, Any]],
        *,
        as_of: Any,
        horizon_targets: Optional[Mapping[str, Any]] = None,
        costs: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Append an idempotent forward-label update for an existing snapshot.

        The idempotency fingerprint includes the cost model version so that
        old 0-cost labels never silently collide with versioned labels.
        """

        _reject_live_markers(price_points)
        if costs is not None:
            _reject_live_markers(costs)
        with self._locked(exclusive=True, create_parent=True):
            events = self._load_unlocked()
            matches = [
                row
                for row in events
                if row.get("journal_event_type") == "prediction_snapshot"
                and row.get("snapshot_id") == snapshot_id
            ]
            if not matches:
                raise JournalSafetyError("unknown snapshot_id: %s" % snapshot_id)
            snapshot = matches[0]
            materialized = materialize_forward_labels(
                snapshot,
                price_points,
                as_of=as_of,
                horizon_targets=horizon_targets,
                costs=costs,
            )
            # Extract cost model version and evidence id from labels for fingerprint.
            cost_model_version = None
            cost_evidence_id = None
            for label in (materialized.get("labels") or {}).values():
                if isinstance(label, dict):
                    if label.get("cost_model_version") and cost_model_version is None:
                        cost_model_version = str(label["cost_model_version"])
                    if label.get("cost_evidence_event_id") and cost_evidence_id is None:
                        cost_evidence_id = str(label["cost_evidence_event_id"])

            update = _force_sim_only(
                {
                    "record_type": "label_update",
                    "journal_event_type": "forward_label_update",
                    "snapshot_id": snapshot_id,
                    "market": snapshot.get("market"),
                    "symbol": snapshot.get("symbol"),
                    "style": snapshot.get("style") or snapshot.get("style_id"),
                    "strategy_version": snapshot.get("strategy_version")
                    or snapshot.get("style_version"),
                    "labels_as_of": materialized["labels_as_of"],
                    "labels": materialized["labels"],
                    "label_aliases": materialized["label_aliases"],
                    "forward_label_eligibility": snapshot.get(
                        "forward_label_eligibility"
                    ),
                    "forward_label_rejection_reason": snapshot.get(
                        "forward_label_rejection_reason"
                    ),
                    "cost_model_version": cost_model_version,
                    "capital_authority_id": snapshot.get("capital_authority_id"),
                    "authority_generation": snapshot.get("authority_generation"),
                    "execution_lineage_id": snapshot.get("execution_lineage_id"),
                    "point_in_time_as_of": snapshot.get("point_in_time_as_of")
                    or snapshot.get("as_of")
                    or snapshot.get("prediction_at"),
                    "source_snapshot_sha256": snapshot.get("source_snapshot_sha256"),
                    "base_snapshot_sha256": snapshot.get("base_snapshot_sha256"),
                    "pair_id": snapshot.get("pair_id"),
                    "sample_cluster_id": snapshot.get("sample_cluster_id"),
                    "decision_cluster_id": snapshot.get("decision_cluster_id"),
                    "cluster_role": snapshot.get("cluster_role"),
                    "maturity_weight": snapshot.get("maturity_weight"),
                    "primary_label_horizon": snapshot.get("primary_label_horizon"),
                    "primary_horizon_policy_version": snapshot.get(
                        "primary_horizon_policy_version"
                    ),
                    "sample_science_contract_version": snapshot.get(
                        "sample_science_contract_version"
                    ),
                }
            )
            # Use cost-versioned idempotency fingerprint; include evidence id
            # so that actual cost revisions do not silently collide.
            update["journal_event_id"] = _stable_label_update_id(
                snapshot_id,
                materialized["labels_as_of"],
                cost_model_version,
                cost_evidence_id,
            )
            update = self._seal_event(update)

            existing = [
                row
                for row in events
                if row.get("journal_event_id") == update["journal_event_id"]
            ]
            if existing:
                if (
                    existing[0].get("journal_payload_sha256")
                    != update["journal_payload_sha256"]
                ):
                    raise JournalConflictError(
                        "journal_event_id %s already exists with different content"
                        % update["journal_event_id"]
                    )
                return self._result("idempotent", existing[0])
            self._append_unlocked(update)
        return self._result("appended", update)

    def read_events(self) -> list[dict[str, Any]]:
        """Read and integrity-check immutable journal events."""

        self._check_paths()
        if not os.path.exists(str(self.path)):
            return []
        with self._locked(exclusive=False, create_parent=False):
            return deepcopy(self._load_unlocked())

    @staticmethod
    def _label_update_sort_key(
        event: Mapping[str, Any], sequence: int
    ) -> tuple[float, str, int]:
        raw = str(event.get("labels_as_of") or "")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            chronological = parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            chronological = float("-inf")
        return chronological, raw, sequence

    def latest_sample_records(self) -> list[dict[str, Any]]:
        """Merge the latest label update into each prediction for read-side KPIs."""

        events = self.read_events()
        latest_updates: dict[str, tuple[tuple[float, str, int], dict[str, Any]]] = {}
        for sequence, event in enumerate(events):
            if event.get("journal_event_type") != "forward_label_update":
                continue
            snapshot_id = str(event.get("snapshot_id") or "")
            sort_key = self._label_update_sort_key(event, sequence)
            current = latest_updates.get(snapshot_id)
            if current is None or sort_key >= current[0]:
                latest_updates[snapshot_id] = (sort_key, event)

        projected: list[dict[str, Any]] = []
        for event in events:
            event_type = event.get("journal_event_type")
            if event_type == "forward_label_update":
                continue
            row = deepcopy(event)
            if event_type == "prediction_snapshot":
                latest = latest_updates.get(str(row.get("snapshot_id") or ""))
                if latest is not None:
                    update = latest[1]
                    row["labels_as_of"] = update.get("labels_as_of")
                    row["labels"] = deepcopy(update.get("labels") or {})
                    row["label_aliases"] = deepcopy(update.get("label_aliases") or {})
            projected.append(_force_sim_only(row))
        return projected

    def build_kpi(
        self,
        *,
        portfolio_snapshot: Optional[Mapping[str, Any]] = None,
        authority_scope: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Build KPIs only from the current fresh-start authority generation."""

        if portfolio_snapshot is not None:
            _reject_live_markers(portfolio_snapshot)
        current_authority = _current_authority_scope(authority_scope)
        if portfolio_snapshot is not None and not _record_in_authority(
            portfolio_snapshot, current_authority
        ):
            raise JournalSafetyError(
                "portfolio authority does not match current A-share sample authority"
            )
        records = self.latest_sample_records()
        current_records = [
            record
            for record in records
            if _record_in_authority(record, current_authority)
        ]
        excluded_legacy = len(records) - len(current_records)
        valid_current_records = [
            record for record in current_records if _strict_evolution_evidence(record)
        ]
        invalid_evolution_evidence_count = len(current_records) - len(
            valid_current_records
        )
        maturity_records = [
            record
            for record in valid_current_records
            if _has_positive_maturity_weight(record)
        ]
        duplicate_count = len(valid_current_records) - len(maturity_records)
        result = build_sample_kpi(
            maturity_records,
            portfolio_snapshot=deepcopy(portfolio_snapshot),
        )
        result["authority_scope"] = deepcopy(current_authority)
        result["raw_current_authority_record_count"] = len(current_records)
        result["excluded_legacy_count"] = excluded_legacy
        result["invalid_evolution_evidence_count"] = invalid_evolution_evidence_count
        result["maturity_duplicate_count"] = duplicate_count
        result["maturity_effective_record_count"] = len(maturity_records)
        result["automatic_promotion_enabled"] = False
        result["automatic_risk_expansion_enabled"] = False
        result["promotion_state"] = "manual_review_only"
        result["real_trading_enabled"] = False
        result["live_execution_enabled"] = False
        return result


__all__ = [
    "JOURNAL_SCHEMA_VERSION",
    "JournalConflictError",
    "JournalError",
    "JournalSafetyError",
    "SampleJournal",
]
