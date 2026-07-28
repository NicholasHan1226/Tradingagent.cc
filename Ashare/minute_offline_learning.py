"""Offline-only learning projections for completed delayed-paper sessions.

This module is intentionally downstream of the fixture bundle.  It neither
queries market data nor touches the shared SampleJournal, capital, broker, or
promotion paths.  Its small append-only journal is A-share-local evidence for
later human review only.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterator, Mapping

from .minute_data import SHANGHAI
from .minute_day_report import MinuteDayReportError, build_minute_day_report


JOURNAL_NAME = "minute_fixture_learning_journal.jsonl"
LATEST_NAME = "minute_fixture_learning_latest.json"
SCHEMA = "tradingagent.ashare.minute_fixture_learning.v1"


class MinuteOfflineLearningError(ValueError):
    """Raised when fixture learning evidence is unsafe to project."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise MinuteOfflineLearningError("minute_learning_payload_invalid") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_bundle_bytes(path: Path) -> bytes:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise MinuteOfflineLearningError("minute_learning_bundle_invalid")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise MinuteOfflineLearningError("minute_learning_bundle_invalid") from exc


def build_minute_offline_learning_projection(
    *, state_bundle: Path | str
) -> dict[str, Any]:
    """Build a secret-free, non-authoritative daily learning projection."""

    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise MinuteOfflineLearningError("real_trading_must_remain_disabled")
    path = Path(state_bundle)
    bundle_sha256 = _sha256_bytes(_load_bundle_bytes(path))
    try:
        report = build_minute_day_report(state_bundle=path)
    except MinuteDayReportError as exc:
        raise MinuteOfflineLearningError("minute_learning_report_invalid") from exc
    authority = report.get("authority")
    if not isinstance(authority, Mapping) or any(
        authority.get(key) is not False
        for key in (
            "execution_authority",
            "training_authority",
            "promotion_authority",
            "real_trading_enabled",
        )
    ):
        raise MinuteOfflineLearningError("minute_learning_authority_invalid")
    missing = report.get("missing_bar_slots")
    if not isinstance(missing, list) or any(
        not isinstance(item, str) for item in missing
    ):
        raise MinuteOfflineLearningError("minute_learning_coverage_invalid")
    evidence = report.get("evidence")
    if not isinstance(evidence, Mapping):
        raise MinuteOfflineLearningError("minute_learning_evidence_invalid")
    rejected_count = evidence.get("rejected_count")
    if (
        isinstance(rejected_count, bool)
        or not isinstance(rejected_count, int)
        or rejected_count < 0
    ):
        raise MinuteOfflineLearningError("minute_learning_evidence_invalid")
    reconciliation = report.get("reconciliation_status")
    if not isinstance(reconciliation, Mapping) or not reconciliation:
        raise MinuteOfflineLearningError("minute_learning_reconciliation_invalid")
    reconciliation_complete = all(
        value == "fixture_reconciled" for value in reconciliation.values()
    )
    differences = report.get("shadow_book_differences")
    if not isinstance(differences, Mapping):
        raise MinuteOfflineLearningError("minute_learning_counterfactual_invalid")
    learning_key = f"{report['trading_date']}:{bundle_sha256}"
    blockers: list[str] = []
    if missing:
        blockers.append("fixture_session_incomplete")
    if rejected_count:
        blockers.append("fixture_evidence_rejected")
    if not reconciliation_complete:
        blockers.append("fixture_reconciliation_incomplete")
    complete = not blockers
    return {
        "schema": SCHEMA,
        "learning_key": learning_key,
        "trading_date": report["trading_date"],
        "capital_layer": "simulated",
        "account_type": "simulated",
        "source": {
            "state_bundle_sha256": bundle_sha256,
            "authority_tier": "non_production_fixture",
            "report_contract": "Ashare.minute_day_report",
        },
        "status": "complete_fixture_projection" if complete else "blocked",
        "blockers": blockers,
        "coverage": {
            "expected_bar_count": len(report["expected_bar_slots"]),
            "observed_bar_count": len(report["observed_bar_slots"]),
            "missing_bar_count": len(missing),
        },
        "sample_summary": {
            "fixture_observation_count": len(report["observed_bar_slots"]),
            "candidate_count": report["candidate_and_rejections"]["candidate_count"],
            "simulated_fill_count": report["simulated_execution"]["simulated_fills"],
            "simulated_not_filled_count": report["simulated_execution"][
                "simulated_not_filled"
            ],
            "training_sample_count": 0,
            "training_eligible": False,
            "promotion_eligible": False,
        },
        "kpi": {
            "fees_cny": report["simulated_execution"]["fees_cny"],
            "reconciliation_status": dict(reconciliation),
            "rejection_reason_counts": dict(
                report["candidate_and_rejections"]["rejection_reason_counts"]
            ),
        },
        "calibration": {
            "status": "blocked_missing_forward_labels",
            "calibrated_probability": None,
            "expected_return_bps": None,
            "reason": "fixture_delayed_paper_has_no_forward_label_authority",
        },
        "missed_opportunities": {
            "status": "counterfactual_only",
            "by_sleeve": {name: dict(value) for name, value in differences.items()},
        },
        "challenger": {
            "recommendation": "observe_only",
            "challenger_eligible": False,
            "reason": "no_calibration_or_oos_label_authority",
        },
        "authority": {
            "capital_authority": False,
            "execution_authority": False,
            "training_authority": False,
            "promotion_authority": False,
            "durable": False,
            "automatic_model_change_enabled": False,
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
            "real_trading_enabled": False,
        },
    }


def _validate_root(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise MinuteOfflineLearningError("minute_learning_root_invalid")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as exc:
        raise MinuteOfflineLearningError("minute_learning_root_invalid") from exc


def _existing_keys(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise MinuteOfflineLearningError("minute_learning_journal_invalid")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            value = json.loads(line)
            if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
                raise MinuteOfflineLearningError("minute_learning_journal_invalid")
            key = value.get("learning_key")
            source = value.get("source")
            sha = (
                source.get("state_bundle_sha256")
                if isinstance(source, Mapping)
                else None
            )
            if not isinstance(key, str) or not isinstance(sha, str):
                raise MinuteOfflineLearningError("minute_learning_journal_invalid")
            if key in values and values[key] != sha:
                raise MinuteOfflineLearningError("minute_learning_journal_conflict")
            values[key] = sha
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinuteOfflineLearningError("minute_learning_journal_invalid") from exc
    return values


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = (_canonical_json(dict(value)) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise MinuteOfflineLearningError(
            "minute_learning_projection_persist_failed"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _exclusive_lock(root: Path) -> Iterator[None]:
    lock_path = root / ".minute-fixture-learning.lock"
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise MinuteOfflineLearningError("minute_learning_lock_failed") from exc
    try:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MinuteOfflineLearningError("minute_learning_already_running") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def write_minute_offline_learning_projection(
    *, state_bundle: Path | str, learning_root: Path | str
) -> dict[str, Any]:
    """Append one immutable projection and refresh a rebuildable latest view."""

    projection = build_minute_offline_learning_projection(state_bundle=state_bundle)
    root = Path(learning_root)
    _validate_root(root)
    journal = root / JOURNAL_NAME
    with _exclusive_lock(root):
        existing = _existing_keys(journal)
        key = projection["learning_key"]
        source_sha = projection["source"]["state_bundle_sha256"]
        if key in existing:
            if existing[key] != source_sha:
                raise MinuteOfflineLearningError("minute_learning_journal_conflict")
            _atomic_json(root / LATEST_NAME, projection)
            return {"appended": False, "projection": projection}
        encoded = (_canonical_json(projection) + "\n").encode("utf-8")
        try:
            descriptor = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "ab") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(journal, 0o600)
        except OSError as exc:
            raise MinuteOfflineLearningError(
                "minute_learning_journal_persist_failed"
            ) from exc
        _atomic_json(root / LATEST_NAME, projection)
    return {"appended": True, "projection": projection}


def state_bundle_for_current_session(*, state_root: Path | str) -> Path:
    """Resolve today's bundle without scanning or modifying a state root."""

    root = Path(state_root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise MinuteOfflineLearningError("minute_learning_state_root_invalid")
    day_root = root / datetime.now(tz=SHANGHAI).strftime("%Y%m%d")
    if day_root.is_symlink() or not day_root.is_dir():
        raise MinuteOfflineLearningError("minute_learning_bundle_invalid")
    bundle = day_root / "state-bundle.json"
    if bundle.is_symlink() or not bundle.is_file():
        raise MinuteOfflineLearningError("minute_learning_bundle_invalid")
    return bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Project one A-share fixture session offline"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--state-bundle", type=Path)
    source.add_argument("--state-root", type=Path)
    parser.add_argument("--learning-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        bundle = (
            args.state_bundle
            if args.state_bundle is not None
            else state_bundle_for_current_session(state_root=args.state_root)
        )
        result = write_minute_offline_learning_projection(
            state_bundle=bundle, learning_root=args.learning_root
        )
    except MinuteOfflineLearningError:
        print("minute offline learning failed closed", file=sys.stderr)
        return 2
    print(_canonical_json(result))
    return 0


__all__ = [
    "JOURNAL_NAME",
    "LATEST_NAME",
    "MinuteOfflineLearningError",
    "build_minute_offline_learning_projection",
    "state_bundle_for_current_session",
    "write_minute_offline_learning_projection",
]
