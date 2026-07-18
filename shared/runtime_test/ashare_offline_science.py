#!/usr/bin/env python3
"""Materialize content-addressed, read-only A-share science projections.

The command requires an explicit journal, authority envelope, cutoff, and
external output root.  It does not append facts, schedule itself, contact a
network service, select a model, or create capital/position/order authority.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sys
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from shared.models.lifecycle import ValidationPlan
from shared.review.calibration_ablation import (
    build_calibration_ablation_report,
    verify_calibration_ablation_report,
)
from shared.review.counterfactual_books import (
    build_counterfactual_books,
    verify_counterfactual_books,
)
from shared.review.offline_science import (
    recompute_offline_metrics,
    verify_offline_metrics_report,
)
from shared.review.outcome_evaluation import (
    build_outcome_evaluation,
    canonical_sha256,
    verify_outcome_evaluation,
)
from shared.review.sample_journal import (
    FrozenJournalView,
    JournalSafetyError,
    SampleJournal,
)
from shared.runtime_test.ashare_forward_label_ops import (
    load_validation_plan_artifact_with_provenance,
)


ROOT = Path(__file__).resolve().parents[2]
_AUTHORITY_KEYS = {
    "capital_authority_id",
    "authority_generation",
    "execution_lineage_id",
}
_OUTPUT_FILES = {
    "outcome_evaluation.json",
    "counterfactual_books.json",
    "offline_metrics.json",
    "calibration_ablation.json",
    "run_receipt.json",
}


class AshareOfflineScienceError(RuntimeError):
    """Raised before any output is published when the run is unsafe."""


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _load_authority(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise AshareOfflineScienceError("authority_manifest_file_required")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AshareOfflineScienceError("authority_manifest_invalid") from exc
    if not isinstance(value, Mapping) or set(value) != _AUTHORITY_KEYS:
        raise AshareOfflineScienceError("authority_manifest_contract_invalid")
    authority_id = str(value.get("capital_authority_id") or "").strip()
    lineage_id = str(value.get("execution_lineage_id") or "").strip()
    generation = value.get("authority_generation")
    if (
        not authority_id
        or not lineage_id
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
    ):
        raise AshareOfflineScienceError("authority_manifest_contract_invalid")
    return {
        "capital_authority_id": authority_id,
        "authority_generation": generation,
        "execution_lineage_id": lineage_id,
    }


def _external_output_root(path: Path) -> Path:
    expanded = path.expanduser()
    requested = expanded if expanded.is_absolute() else Path.cwd() / expanded
    current = Path(requested.anchor)
    for part in requested.parts[1:]:
        current /= part
        if current.is_symlink():
            raise AshareOfflineScienceError("output_root_symlink_forbidden")

    resolved = requested.resolve(strict=False)
    root = ROOT.resolve()
    if resolved == root or root in resolved.parents:
        raise AshareOfflineScienceError("output_root_must_be_outside_repository")
    return resolved


def _write_once(directory: Path, artifacts: Mapping[str, Any]) -> None:
    if set(artifacts) != _OUTPUT_FILES:
        raise AshareOfflineScienceError("artifact_set_invalid")
    if directory.exists():
        if not directory.is_dir() or directory.is_symlink():
            raise AshareOfflineScienceError("existing_run_directory_invalid")
        if {path.name for path in directory.iterdir()} != _OUTPUT_FILES:
            raise AshareOfflineScienceError("existing_run_artifact_set_mismatch")
        for name, value in artifacts.items():
            path = directory / name
            if (
                not path.is_file()
                or path.is_symlink()
                or path.read_bytes() != _json_bytes(value)
            ):
                raise AshareOfflineScienceError("existing_run_content_mismatch")
        return
    directory.parent.mkdir(parents=True, exist_ok=True)
    temporary = directory.parent / (".%s.tmp.%d" % (directory.name, os.getpid()))
    if temporary.exists():
        raise AshareOfflineScienceError("temporary_run_directory_exists")
    temporary.mkdir(mode=0o700)
    try:
        for name, value in sorted(artifacts.items()):
            path = temporary / name
            with path.open("xb") as handle:
                handle.write(_json_bytes(value))
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary, directory)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _build_bundle(
    *,
    frozen_view: FrozenJournalView,
    authority: Mapping[str, Any],
    validation_plan: ValidationPlan,
    validation_plan_provenance: Mapping[str, Any],
    expected_as_of: str,
    bootstrap_iterations: int,
) -> tuple[str, dict[str, Any]]:
    if type(frozen_view) is not FrozenJournalView:
        raise AshareOfflineScienceError("frozen_journal_view_required")
    try:
        frozen_view.verify_integrity()
        frozen_as_of = datetime.fromisoformat(
            frozen_view.data_as_of.replace("Z", "+00:00")
        )
        required_as_of = datetime.fromisoformat(expected_as_of.replace("Z", "+00:00"))
    except (JournalSafetyError, TypeError, ValueError) as exc:
        raise AshareOfflineScienceError(
            "frozen_journal_view_integrity_invalid"
        ) from exc
    if (
        frozen_as_of.tzinfo is None
        or frozen_as_of.utcoffset() is None
        or required_as_of.tzinfo is None
        or required_as_of.utcoffset() is None
    ):
        raise AshareOfflineScienceError("frozen_journal_view_as_of_invalid")
    normalized_as_of = required_as_of.astimezone(timezone.utc).isoformat()
    if frozen_as_of.astimezone(timezone.utc).isoformat() != normalized_as_of:
        raise AshareOfflineScienceError("frozen_journal_view_as_of_mismatch")
    events = frozen_view.copy_events()
    journal_view = frozen_view.metadata()
    outcome = build_outcome_evaluation(
        events,
        as_of=normalized_as_of,
        authority_scope=authority,
        validation_plan=validation_plan,
        validation_plan_provenance=validation_plan_provenance,
    )
    books = build_counterfactual_books(
        outcome,
        events=events,
        expected_as_of=normalized_as_of,
        expected_authority_scope=authority,
        validation_plan=validation_plan,
        validation_plan_provenance=validation_plan_provenance,
    )
    metrics = recompute_offline_metrics(
        events=events,
        outcome_report=outcome,
        expected_as_of=normalized_as_of,
        expected_authority_scope=authority,
        validation_plan=validation_plan,
        validation_plan_provenance=validation_plan_provenance,
        bootstrap_iterations=bootstrap_iterations,
    )
    calibration = build_calibration_ablation_report(
        events=events,
        outcome_report=outcome,
        expected_as_of=normalized_as_of,
        expected_authority_scope=authority,
        validation_plan=validation_plan,
        validation_plan_provenance=validation_plan_provenance,
    )
    verify_outcome_evaluation(
        outcome,
        events=events,
        expected_as_of=normalized_as_of,
        expected_authority_scope=authority,
        validation_plan=validation_plan,
        validation_plan_provenance=validation_plan_provenance,
    )
    verify_counterfactual_books(
        books,
        outcome_report=outcome,
        events=events,
        expected_as_of=normalized_as_of,
        expected_authority_scope=authority,
        validation_plan=validation_plan,
        validation_plan_provenance=validation_plan_provenance,
    )
    verify_offline_metrics_report(
        metrics,
        events=events,
        outcome_report=outcome,
        expected_as_of=normalized_as_of,
        expected_authority_scope=authority,
        validation_plan=validation_plan,
        validation_plan_provenance=validation_plan_provenance,
        bootstrap_iterations=bootstrap_iterations,
    )
    verify_calibration_ablation_report(
        calibration,
        events=events,
        outcome_report=outcome,
        expected_as_of=normalized_as_of,
        expected_authority_scope=authority,
        validation_plan=validation_plan,
        validation_plan_provenance=validation_plan_provenance,
    )
    run_identity = {
        "journal_view": deepcopy(dict(journal_view)),
        "authority_scope": deepcopy(dict(authority)),
        "source_events_sha256": outcome["source_events_sha256"],
        "validation_plan_binding": outcome["validation_plan_binding"],
        "validation_plan_provenance": outcome["validation_plan_provenance"],
        "validation_plan_provenance_verification": outcome[
            "validation_plan_provenance_verification"
        ],
        "as_of": outcome["as_of"],
        "bootstrap_iterations": bootstrap_iterations,
        "artifact_report_sha256": {
            "outcome_evaluation": outcome["report_sha256"],
            "counterfactual_books": books["report_sha256"],
            "offline_metrics": metrics["report_sha256"],
            "calibration_ablation": calibration["report_sha256"],
        },
    }
    run_sha = canonical_sha256(run_identity)
    receipt = {
        "record_type": "ashare_offline_science_run_receipt",
        "schema_version": "ashare-offline-science-run.v1",
        "run_sha256": run_sha,
        **run_identity,
        "projection_only": True,
        "journal_write_count": 0,
        "network_call_count": 0,
        "capital_authority": False,
        "position_authority": False,
        "order_authority": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "live_transition_authorized": False,
        "real_trading_enabled": False,
    }
    return run_sha, {
        "outcome_evaluation.json": outcome,
        "counterfactual_books.json": books,
        "offline_metrics.json": metrics,
        "calibration_ablation.json": calibration,
        "run_receipt.json": receipt,
    }


def verify_offline_science_bundle(
    artifacts: Mapping[str, Any],
    *,
    frozen_view: FrozenJournalView,
    authority: Mapping[str, Any],
    validation_plan: ValidationPlan,
    validation_plan_provenance: Mapping[str, Any],
    expected_as_of: str,
    bootstrap_iterations: int,
) -> bool:
    """Rebuild all five files from exact sources and require byte-equivalent data."""

    if set(artifacts) != _OUTPUT_FILES:
        raise AshareOfflineScienceError("artifact_set_invalid")
    run_sha, expected = _build_bundle(
        frozen_view=frozen_view,
        authority=authority,
        validation_plan=validation_plan,
        validation_plan_provenance=validation_plan_provenance,
        expected_as_of=expected_as_of,
        bootstrap_iterations=bootstrap_iterations,
    )
    if dict(artifacts) != expected:
        raise AshareOfflineScienceError("offline_science_bundle_exact_source_mismatch")
    receipt = artifacts.get("run_receipt.json")
    if not isinstance(receipt, Mapping) or receipt.get("run_sha256") != run_sha:
        raise AshareOfflineScienceError("offline_science_run_receipt_mismatch")
    return True


def run_offline_science(
    *,
    journal_path: Path,
    authority_manifest_path: Path,
    validation_plan_path: Path,
    as_of: str,
    output_root: Path,
    bootstrap_iterations: int = 1000,
) -> dict[str, Any]:
    """Build one immutable projection bundle from one frozen journal view."""

    authority = _load_authority(authority_manifest_path)
    validation_plan, validation_plan_provenance = (
        load_validation_plan_artifact_with_provenance(validation_plan_path)
    )
    external_root = _external_output_root(output_root)
    if journal_path.is_symlink():
        raise AshareOfflineScienceError("journal_symlink_forbidden")
    frozen = SampleJournal(journal_path).read_frozen(as_of=as_of)
    run_sha, artifacts = _build_bundle(
        frozen_view=frozen,
        authority=authority,
        validation_plan=validation_plan,
        validation_plan_provenance=validation_plan_provenance,
        expected_as_of=as_of,
        bootstrap_iterations=bootstrap_iterations,
    )
    verify_offline_science_bundle(
        artifacts,
        frozen_view=frozen,
        authority=authority,
        validation_plan=validation_plan,
        validation_plan_provenance=validation_plan_provenance,
        expected_as_of=as_of,
        bootstrap_iterations=bootstrap_iterations,
    )
    destination = external_root / run_sha
    _write_once(destination, artifacts)
    return {
        "status": "published_projection",
        "output_dir": str(destination),
        **artifacts["run_receipt.json"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal-path", type=Path, required=True)
    parser.add_argument("--authority-manifest", type=Path, required=True)
    parser.add_argument("--validation-plan-path", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--bootstrap-iterations", type=int, default=1000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_offline_science(
            journal_path=args.journal_path,
            authority_manifest_path=args.authority_manifest,
            validation_plan_path=args.validation_plan_path,
            as_of=args.as_of,
            output_root=args.output_root,
            bootstrap_iterations=args.bootstrap_iterations,
        )
    except (AshareOfflineScienceError, OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI.
    raise SystemExit(main())


__all__ = [
    "AshareOfflineScienceError",
    "main",
    "run_offline_science",
    "verify_offline_science_bundle",
]
