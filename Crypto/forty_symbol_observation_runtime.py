"""Minimal server CLI for the Crypto forty-symbol observation accumulator.

This is the versioned forty-symbol sibling of
``ten_symbol_observation_runtime``.  It reuses the exact same parameterized
runtime core but pins the forty-symbol universe, contracts and a distinct
store root, so the frozen ten-symbol chain stays read-only under its own root.
This family is not deployed or installed this round; it exists so the
forty-symbol observation path can be exercised against fixtures and then
activated once TradingDatas serves the forty-symbol datasets.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Mapping

from Crypto.ten_symbol_observation_runtime import (
    FORTY_SYMBOL_RUNTIME_CONFIG,
    RUNTIME_TOKEN_FILE,
    CryptoTenSymbolObservationRuntimeManifest,
    crypto_ten_symbol_observation_exit_code,
    load_crypto_ten_symbol_observation_runtime_manifest,
    run_crypto_ten_symbol_observation_once,
)


FORTY_SYMBOL_OUTPUT_ROOT = FORTY_SYMBOL_RUNTIME_CONFIG.output_root
FORTY_SYMBOL_RUNTIME_CONTRACT = FORTY_SYMBOL_RUNTIME_CONFIG.runtime_contract
FORTY_SYMBOL_MANIFEST_CONTRACT = FORTY_SYMBOL_RUNTIME_CONFIG.manifest_contract
FORTY_SYMBOL_TOKEN_FILE = RUNTIME_TOKEN_FILE


def load_crypto_forty_symbol_observation_runtime_manifest(
    path: Path | str,
) -> CryptoTenSymbolObservationRuntimeManifest:
    """Load and pin the forty-symbol runtime manifest to its own root."""

    return load_crypto_ten_symbol_observation_runtime_manifest(
        path,
        config=FORTY_SYMBOL_RUNTIME_CONFIG,
    )


def run_crypto_forty_symbol_observation_once(
    *,
    runtime_manifest: Path | str,
    token_file: Path | str,
    output_root: Path | str,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run one forty-symbol accumulation invocation under the shared core."""

    return run_crypto_ten_symbol_observation_once(
        runtime_manifest=runtime_manifest,
        token_file=token_file,
        output_root=output_root,
        now=now,
        config=FORTY_SYMBOL_RUNTIME_CONFIG,
        **kwargs,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one loopback-only Crypto forty-symbol observation accumulation"
        )
    )
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = run_crypto_forty_symbol_observation_once(
            runtime_manifest=args.runtime_manifest,
            token_file=args.token_file,
            output_root=FORTY_SYMBOL_OUTPUT_ROOT,
            now=datetime.now(tz=timezone.utc),
        )
        exit_code = crypto_ten_symbol_observation_exit_code(receipt)
    except Exception:
        print(
            "crypto forty symbol observation runtime failed closed",
            file=sys.stderr,
        )
        return 2
    if exit_code != 0:
        print(
            "crypto forty symbol observation runtime failed closed",
            file=sys.stderr,
        )
        return exit_code
    print(
        json.dumps(
            receipt,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FORTY_SYMBOL_MANIFEST_CONTRACT",
    "FORTY_SYMBOL_OUTPUT_ROOT",
    "FORTY_SYMBOL_RUNTIME_CONTRACT",
    "FORTY_SYMBOL_TOKEN_FILE",
    "load_crypto_forty_symbol_observation_runtime_manifest",
    "main",
    "run_crypto_forty_symbol_observation_once",
]
