"""CLI boundary for the detached, manifest-bound Crypto factor-research worker."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from Crypto.delayed_paper_factor_research import (
    CryptoFactorProjectionError,
    factor_projection_exit_code,
    run_crypto_delayed_paper_factor_research_full_scrub,
)
from Crypto.delayed_paper_round_trip_epoch import (
    ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY,
    CryptoRoundTripEpochError,
    load_round_trip_epoch_manifest,
    prepare_round_trip_epoch_candidate,
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
        raise CryptoFactorProjectionError("factor_projection_manifest_path_invalid")
    return path


def _existing_epoch_root(context: Any) -> None:
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
    )
    if any(not path.exists() or path.is_symlink() for path in required):
        raise CryptoFactorProjectionError("factor_projection_root_incomplete")


def run_factor_research_worker_once(*, epoch_manifest: Path | str) -> dict[str, Any]:
    """Run the full detached factor scrub for exactly one existing G4 epoch."""

    _assert_simulation_only()
    manifest_path = _validated_manifest_path(epoch_manifest)
    try:
        context = load_round_trip_epoch_manifest(manifest_path)
        if context.epoch_generation != 4:
            raise CryptoFactorProjectionError(
                "factor_projection_epoch_generation_invalid"
            )
        _existing_epoch_root(context)
        prepared = prepare_round_trip_epoch_candidate(context)
        identity_before = prepared.identity_path.read_bytes()
        result = run_crypto_delayed_paper_factor_research_full_scrub(
            output_root=prepared.output_root
        )
        prepared_after = prepare_round_trip_epoch_candidate(context)
        if prepared_after.identity_path.read_bytes() != identity_before:
            raise CryptoFactorProjectionError(
                "factor_projection_epoch_identity_changed"
            )
    except CryptoFactorProjectionError:
        raise
    except (CryptoRoundTripEpochError, OSError) as exc:
        raise CryptoFactorProjectionError("factor_projection_epoch_invalid") from exc
    return {
        **result,
        "epoch_id": context.epoch_id,
        "epoch_generation": context.epoch_generation,
        "epoch_output_root": str(context.output_root),
        "epoch_manifest_sha256": context.manifest_sha256,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one detached Crypto G4 factor-research full scrub"
    )
    parser.add_argument("--epoch-manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_factor_research_worker_once(epoch_manifest=args.epoch_manifest)
    except Exception:
        print("crypto factor-research worker failed closed", file=sys.stderr)
        return 2
    if factor_projection_exit_code(result):
        print("crypto factor-research worker failed closed", file=sys.stderr)
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


__all__ = ["main", "run_factor_research_worker_once"]
