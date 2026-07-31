"""CLI boundary for the detached G4 round-trip learning worker.

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
from Crypto.fixture_sim.contracts import _assert_simulation_only


def _validated_manifest_path(value: Path | str) -> Path:
    path = Path(value)
    if (
        path.parent != ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY
        or path.name.startswith("generation-")
        or not path.name.startswith("crypto-delayed-paper-round-trip-epoch-g4-")
        or path.suffix != ".json"
    ):
        raise CryptoRoundTripLearningError("round_trip_learning_manifest_path_invalid")
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


def run_round_trip_learning_worker_once(
    *, mode: str, epoch_manifest: Path | str
) -> dict[str, Any]:
    """Run an isolated projection after binding it to exactly one G4 epoch."""

    _assert_simulation_only()
    manifest_path = _validated_manifest_path(epoch_manifest)
    try:
        context = load_round_trip_epoch_manifest(manifest_path)
        if context.epoch_generation != 4:
            raise CryptoRoundTripLearningError(
                "round_trip_learning_epoch_generation_invalid"
            )
        _existing_epoch_root(context)
        prepared = prepare_round_trip_epoch_candidate(context)
        identity_before = prepared.identity_path.read_bytes()
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
        prepared_after = prepare_round_trip_epoch_candidate(context)
        if prepared_after.identity_path.read_bytes() != identity_before:
            raise CryptoRoundTripLearningError(
                "round_trip_learning_epoch_identity_changed"
            )
    except CryptoRoundTripLearningError:
        raise
    except (CryptoRoundTripEpochError, OSError) as exc:
        raise CryptoRoundTripLearningError("round_trip_learning_epoch_invalid") from exc
    return {
        **result,
        "epoch_id": context.epoch_id,
        "epoch_generation": context.epoch_generation,
        "epoch_output_root": str(context.output_root),
        "epoch_manifest_sha256": context.manifest_sha256,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one detached Crypto G4 round-trip learning projection"
    )
    parser.add_argument("--mode", choices=("incremental", "full-scrub"), required=True)
    parser.add_argument("--epoch-manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_round_trip_learning_worker_once(
            mode=args.mode, epoch_manifest=args.epoch_manifest
        )
    except Exception:
        print("crypto round-trip learning worker failed closed", file=sys.stderr)
        return 2
    if round_trip_learning_exit_code(result):
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
