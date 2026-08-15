"""CLI boundary for the detached ten-symbol Crypto factor-research worker.

The worker derives the observation store root exclusively from the fixed,
repository-external observation runtime manifest; there is deliberately no
free output-root flag.  The manifest bytes and the derived root identity are
re-verified before and after every operation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from Crypto.fixture_sim.contracts import _assert_simulation_only
from Crypto.ten_symbol_factor_research import (
    CryptoTenSymbolFactorProjectionError,
    run_crypto_ten_symbol_factor_research_full_scrub,
    run_crypto_ten_symbol_factor_research_incremental,
    ten_symbol_factor_projection_exit_code,
)
from Crypto.ten_symbol_observation_runtime import (
    CryptoTenSymbolObservationRuntimeError,
    load_crypto_ten_symbol_observation_runtime_manifest,
)


TEN_SYMBOL_OBSERVATION_RUNTIME_MANIFEST = Path(
    "/etc/tradingagent/crypto-ten-symbol-observation.runtime.json"
)


def _fixed_manifest_path(value: Path | str) -> Path:
    path = Path(value)
    if path != TEN_SYMBOL_OBSERVATION_RUNTIME_MANIFEST:
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_manifest_path_invalid"
        )
    return path


def run_ten_symbol_factor_research_worker_once(
    *,
    runtime_manifest: Path | str | None = None,
    mode: str = "incremental",
) -> dict[str, Any]:
    """Run one detached factor operation against the manifest-bound store."""

    _assert_simulation_only()
    manifest_path = _fixed_manifest_path(
        TEN_SYMBOL_OBSERVATION_RUNTIME_MANIFEST
        if runtime_manifest is None
        else runtime_manifest
    )
    try:
        bytes_before = manifest_path.read_bytes()
        manifest_before = load_crypto_ten_symbol_observation_runtime_manifest(
            manifest_path
        )
        store_root = manifest_before.output_root
        if mode == "incremental":
            result = run_crypto_ten_symbol_factor_research_incremental(
                output_root=store_root
            )
        elif mode == "full-scrub":
            result = run_crypto_ten_symbol_factor_research_full_scrub(
                output_root=store_root
            )
        else:
            raise CryptoTenSymbolFactorProjectionError(
                "ten_symbol_factor_projection_mode_invalid"
            )
        manifest_after = load_crypto_ten_symbol_observation_runtime_manifest(
            manifest_path
        )
        if (
            manifest_path.read_bytes() != bytes_before
            or manifest_after.sha256 != manifest_before.sha256
            or manifest_after.output_root != store_root
        ):
            raise CryptoTenSymbolFactorProjectionError(
                "ten_symbol_factor_projection_manifest_changed"
            )
    except CryptoTenSymbolFactorProjectionError:
        raise
    except (CryptoTenSymbolObservationRuntimeError, OSError) as exc:
        raise CryptoTenSymbolFactorProjectionError(
            "ten_symbol_factor_projection_manifest_invalid"
        ) from exc
    return {
        **result,
        "mode": mode,
        "store_root": str(store_root),
        "runtime_manifest_sha256": manifest_before.sha256,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one detached Crypto ten-symbol factor-research operation"
    )
    parser.add_argument(
        "--mode", choices=("incremental", "full-scrub"), default="incremental"
    )
    args = parser.parse_args(argv)
    try:
        result = run_ten_symbol_factor_research_worker_once(mode=args.mode)
    except Exception:
        print("crypto ten-symbol factor-research worker failed closed", file=sys.stderr)
        return 2
    if ten_symbol_factor_projection_exit_code(result):
        print("crypto ten-symbol factor-research worker failed closed", file=sys.stderr)
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


__all__ = [
    "TEN_SYMBOL_OBSERVATION_RUNTIME_MANIFEST",
    "main",
    "run_ten_symbol_factor_research_worker_once",
]
