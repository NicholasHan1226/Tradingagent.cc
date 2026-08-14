"""CLI boundary for detached G4/G5 round-trip learning projections.

The command deliberately accepts a versioned round-trip epoch manifest only;
there is no free output-root argument and no network input.  It rechecks the
epoch before and after the projection so learning cannot silently follow a
different generation while the core keeps running.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from time import monotonic
from typing import Any

from Crypto.delayed_paper_round_trip_epoch import (
    ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY,
    CryptoRoundTripEpochError,
    load_round_trip_epoch_manifest,
    prepare_round_trip_epoch_candidate,
)
from Crypto.delayed_paper_round_trip_learning import (
    CryptoRoundTripLearningError,
    round_trip_learning_exit_code,
    run_crypto_delayed_paper_round_trip_learning_full_scrub,
    run_crypto_delayed_paper_round_trip_learning_incremental,
)
from Crypto.delayed_paper_factor_research import (
    CryptoFactorProjectionError,
    run_crypto_delayed_paper_factor_research_full_scrub,
    run_crypto_delayed_paper_factor_research_incremental,
)
from Crypto.factor_strategy_post_projection import (
    CryptoFactorStrategyPostProjectionError,
    run_factor_strategy_post_projection,
)
from Crypto.fixture_sim.contracts import _assert_simulation_only


ROUND_TRIP_LEARNING_EPOCH_ROOTS = {
    4: Path(
        "/var/lib/tradingagent/crypto-delayed-paper-epochs/"
        "crypto-delayed-paper-round-trip-epoch-g4-20260731"
    ),
    5: Path(
        "/var/lib/tradingagent/crypto-delayed-paper-epochs/"
        "crypto-delayed-paper-round-trip-epoch-g5-20260801"
    ),
}

ROUND_TRIP_LEARNING_FAILURE_REASONS = frozenset(
    {
        "round_trip_learning_checkpoint_invalid",
        "round_trip_learning_checkpoint_orphaned",
        "round_trip_learning_checkpoint_source_mismatch",
        "round_trip_learning_claimed_projection_missing",
        "round_trip_learning_core_checkpoint_mismatch",
        "round_trip_learning_core_invalid",
        "round_trip_learning_core_inventory_invalid",
        "round_trip_learning_core_regressed",
        "round_trip_learning_decision_index_invalid",
        "round_trip_learning_decision_index_missing",
        "round_trip_learning_directory_invalid",
        "round_trip_learning_epoch_generation_invalid",
        "round_trip_learning_epoch_identity_changed",
        "round_trip_learning_epoch_invalid",
        "round_trip_learning_epoch_root_invalid",
        "round_trip_learning_immutable_conflict",
        "round_trip_learning_lock_invalid",
        "round_trip_learning_manifest_path_invalid",
        "round_trip_learning_mode_invalid",
        "round_trip_learning_payload_invalid",
        "round_trip_learning_projection_invalid",
        "round_trip_learning_projection_not_derived",
        "round_trip_learning_root_incomplete",
        "round_trip_learning_source_invalid",
        "round_trip_learning_state_invalid",
        "round_trip_learning_state_write_failed",
        "round_trip_learning_write_failed",
    }
)

_TIME_BUDGET_DEFERRED_STATUSES = frozenset(
    {"deferred_inventory_time_budget", "deferred_time_budget"}
)
_FULL_SCRUB_WORKER_MAX_SECONDS = 110.0


def _post_projection_debt(*, stage: str, reason: str) -> dict[str, Any]:
    """Return one compact, safe retry marker without changing learning output."""

    return {
        "contract": "tradingagent.crypto.factor_strategy_evaluation_debt.v1",
        "status": "evaluation_debt",
        "stage": stage,
        "reason": reason,
        "retry_on_next_learning_cadence": True,
        "authority": "none",
        "research_only": True,
        "learning_authority": False,
        "execution_authority": False,
        "production_eligible": False,
        "promotion_authorized": False,
        "automatic_champion_replacement": False,
        "automatic_risk_expansion_enabled": False,
        "real_trading_enabled": False,
        "network_used": False,
        "model_network_used": False,
        "live_broker_used": False,
    }


def _failure_event(
    *,
    mode: str,
    exc: Exception | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the public, secret-free failure provenance for one CLI invocation."""

    reason = "round_trip_learning_failed"
    if (
        isinstance(exc, CryptoRoundTripLearningError)
        and len(exc.args) == 1
        and isinstance(exc.args[0], str)
        and exc.args[0] in ROUND_TRIP_LEARNING_FAILURE_REASONS
    ):
        reason = exc.args[0]
    event: dict[str, Any] = {
        "contract": "tradingagent.crypto.round_trip_learning_worker_failure.v1",
        "status": "failed_closed",
        "failure_phase": mode.replace("-", "_"),
        "failure_reason": reason,
    }
    if context:
        for key in (
            "stage",
            "epoch_generation",
            "epoch_manifest_sha256",
            "checkpoint_head_sha256",
            "checkpoint_source_completion_sha256",
            "checkpoint_projection_receipt_sha256",
            "projected_completion_count",
            "core_completion_count",
        ):
            value = context.get(key)
            if value is not None:
                event[key] = value
    return event


def _emit_failure(
    *, mode: str, exc: Exception | None = None, context: dict[str, Any] | None = None
) -> None:
    print(
        json.dumps(
            _failure_event(mode=mode, exc=exc, context=context),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _read_failure_context(root: Path, context: dict[str, Any]) -> None:
    """Add only non-secret checkpoint/core counters to a failure event."""

    try:
        state_path = root / "delayed_paper" / "observation_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        context.setdefault("core_completion_count", state.get("completion_count"))
        evolution = root / "evolution" / "round_trip_learning"
        worker_path = evolution / "worker_state.json"
        if worker_path.is_file():
            worker = json.loads(worker_path.read_text(encoding="utf-8"))
            context.setdefault(
                "projected_completion_count", worker.get("projected_completion_count")
            )
            context.setdefault("checkpoint_head_sha256", worker.get("checkpoint_head_sha256"))
        head = context.get("checkpoint_head_sha256")
        if isinstance(head, str):
            checkpoint_path = evolution / "checkpoints" / (
                f"{context.get('projected_completion_count', 0):012d}.json"
            )
            if checkpoint_path.is_file():
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                context.setdefault(
                    "checkpoint_source_completion_sha256",
                    checkpoint.get("source_completion_sha256"),
                )
                context.setdefault(
                    "checkpoint_projection_receipt_sha256",
                    checkpoint.get("projection_receipt_sha256"),
                )
    except (OSError, TypeError, ValueError):
        return


def _manifest_generation(path: Path) -> int:
    """Return the generation encoded by one accepted round-trip manifest name."""

    prefixes = {
        "crypto-delayed-paper-round-trip-epoch-g4-": 4,
        "crypto-delayed-paper-round-trip-epoch-g5-": 5,
    }
    for prefix, generation in prefixes.items():
        if path.name.startswith(prefix):
            return generation
    raise CryptoRoundTripLearningError("round_trip_learning_manifest_path_invalid")


def _validated_manifest_path(value: Path | str) -> Path:
    path = Path(value)
    if (
        path.parent != ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY
        or path.name.startswith("generation-")
        or path.suffix != ".json"
    ):
        raise CryptoRoundTripLearningError("round_trip_learning_manifest_path_invalid")
    _manifest_generation(path)
    return path


def _existing_epoch_root(context: Any) -> None:
    """Refuse incomplete roots before the epoch helper could create anything."""

    root = context.output_root
    required = (
        root,
        context.identity_path,
        root / "delayed_paper",
        root / "delayed_paper" / "observations",
        root / "delayed_paper" / "completions",
        root / "delayed_paper" / ".lock",
        root / "round_trip_capital",
        root / "round_trip_capital" / ".lock",
        root / "evolution",
    )
    if any(not path.exists() or path.is_symlink() for path in required):
        raise CryptoRoundTripLearningError("round_trip_learning_root_incomplete")


def _validated_epoch_root(context: Any, *, expected_generation: int) -> None:
    """Bind an accepted manifest generation to its one immutable epoch root."""

    if context.output_root != ROUND_TRIP_LEARNING_EPOCH_ROOTS[expected_generation]:
        raise CryptoRoundTripLearningError("round_trip_learning_epoch_root_invalid")


def run_round_trip_learning_worker_once(
    *, mode: str, epoch_manifest: Path | str, failure_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Run an isolated projection after binding it to one exact G4/G5 epoch."""

    _assert_simulation_only()
    context_data = failure_context if failure_context is not None else {}
    context_data["stage"] = "manifest_validation"
    try:
        manifest_path = _validated_manifest_path(epoch_manifest)
        expected_generation = _manifest_generation(manifest_path)
        epoch_context = load_round_trip_epoch_manifest(manifest_path)
        if epoch_context.epoch_generation != expected_generation:
            raise CryptoRoundTripLearningError(
                "round_trip_learning_epoch_generation_invalid"
            )
        context_data.update(
            {
                "stage": "epoch_manifest_loaded",
                "epoch_generation": epoch_context.epoch_generation,
                "epoch_manifest_sha256": getattr(epoch_context, "manifest_sha256", None),
            }
        )
        _validated_epoch_root(epoch_context, expected_generation=expected_generation)
        _existing_epoch_root(epoch_context)
        context_data["stage"] = "epoch_root_validated"
        prepared = prepare_round_trip_epoch_candidate(epoch_context)
        identity_before = prepared.identity_path.read_bytes()
        context_data["stage"] = "projection_started"
        full_scrub_deadline = (
            monotonic() + _FULL_SCRUB_WORKER_MAX_SECONDS
            if mode == "full-scrub"
            else None
        )
        if mode == "incremental":
            result = run_crypto_delayed_paper_round_trip_learning_incremental(
                output_root=prepared.output_root
            )
        elif mode == "full-scrub":
            result = run_crypto_delayed_paper_round_trip_learning_full_scrub(
                output_root=prepared.output_root
            )
        else:
            raise CryptoRoundTripLearningError("round_trip_learning_mode_invalid")
        if result.get("status") in _TIME_BUDGET_DEFERRED_STATUSES:
            factor_result = _post_projection_debt(
                stage="factor_projection",
                reason="factor_projection_time_budget",
            )
            evaluation_result = factor_result
        else:
            try:
                if mode == "incremental":
                    factor_result = (
                        run_crypto_delayed_paper_factor_research_incremental(
                            output_root=prepared.output_root
                        )
                    )
                else:
                    factor_result = run_crypto_delayed_paper_factor_research_full_scrub(
                        output_root=prepared.output_root,
                        _deadline=full_scrub_deadline,
                    )
                if factor_result.get("status") in _TIME_BUDGET_DEFERRED_STATUSES:
                    evaluation_result = _post_projection_debt(
                        stage="factor_projection",
                        reason="factor_projection_time_budget",
                    )
                else:
                    evaluation_result = run_factor_strategy_post_projection(
                        output_root=prepared.output_root
                    )
            except CryptoFactorProjectionError:
                factor_result = _post_projection_debt(
                    stage="factor_projection",
                    reason="factor_projection_failed",
                )
                evaluation_result = factor_result
            except CryptoFactorStrategyPostProjectionError:
                evaluation_result = _post_projection_debt(
                    stage="factor_strategy_evaluation",
                    reason="factor_strategy_evaluation_failed",
                )
        context_data.update(
            {
                "stage": "projection_returned",
                "projected_completion_count": result.get("projected_completion_count"),
                "core_completion_count": result.get("completion_count"),
                "checkpoint_head_sha256": result.get("checkpoint_head_sha256"),
            }
        )
        prepared_after = prepare_round_trip_epoch_candidate(epoch_context)
        if prepared_after.identity_path.read_bytes() != identity_before:
            raise CryptoRoundTripLearningError(
                "round_trip_learning_epoch_identity_changed"
            )
    except (CryptoRoundTripLearningError, CryptoRoundTripEpochError, OSError) as exc:
        root = locals().get("prepared", None) or locals().get("epoch_context", None)
        output_root = getattr(root, "output_root", None)
        if isinstance(output_root, Path):
            _read_failure_context(output_root, context_data)
        if isinstance(exc, CryptoRoundTripLearningError):
            raise
        raise CryptoRoundTripLearningError("round_trip_learning_epoch_invalid") from exc
    return {
        **result,
        "factor_projection": factor_result,
        "factor_strategy_evaluation": evaluation_result,
        "epoch_id": epoch_context.epoch_id,
        "epoch_generation": epoch_context.epoch_generation,
        "epoch_output_root": str(epoch_context.output_root),
        "epoch_manifest_sha256": epoch_context.manifest_sha256,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one detached Crypto G4/G5 round-trip learning projection"
    )
    parser.add_argument("--mode", choices=("incremental", "full-scrub"), required=True)
    parser.add_argument("--epoch-manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    failure_context: dict[str, Any] = {}
    try:
        result = run_round_trip_learning_worker_once(
            mode=args.mode,
            epoch_manifest=args.epoch_manifest,
            failure_context=failure_context,
        )
    except Exception as exc:
        _emit_failure(mode=args.mode, exc=exc, context=failure_context)
        print("crypto round-trip learning worker failed closed", file=sys.stderr)
        return 2
    if round_trip_learning_exit_code(result):
        failure_context.update(
            {
                "stage": "projection_returned",
                "projected_completion_count": result.get("projected_completion_count"),
                "core_completion_count": result.get("completion_count"),
                "checkpoint_head_sha256": result.get("checkpoint_head_sha256"),
            }
        )
        output_root = result.get("epoch_output_root")
        if isinstance(output_root, str):
            _read_failure_context(Path(output_root), failure_context)
        _emit_failure(mode=args.mode, context=failure_context)
        print("crypto round-trip learning worker failed closed", file=sys.stderr)
        return 2
    print(
        json.dumps(
            result,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_round_trip_learning_worker_once"]
