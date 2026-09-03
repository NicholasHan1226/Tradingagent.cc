"""Minimal server CLI for the Crypto forty-symbol observation accumulator.

This is the versioned forty-symbol sibling of
``ten_symbol_observation_runtime``.  It reuses the exact same parameterized
runtime core but pins the forty-symbol universe, contracts and a distinct
store root, so the frozen ten-symbol chain stays read-only under its own root.
The forty-symbol lane has its own bounded invocation budget because its
independent 40-symbol bar and spread legs need more requests than the frozen
ten-symbol cadence. It remains simulation-only and has no promotion or
capital authority.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
from typing import Any, Mapping

from Crypto.ten_symbol_observation_runtime import (
    FORTY_SYMBOL_RUNTIME_CONFIG,
    RUNTIME_TOKEN_FILE,
    CryptoTenSymbolObservationRuntimeError,
    CryptoTenSymbolObservationRuntimeManifest,
    crypto_ten_symbol_observation_exit_code,
    load_crypto_ten_symbol_observation_runtime_manifest,
    run_crypto_ten_symbol_observation_once,
)


FORTY_SYMBOL_OUTPUT_ROOT = FORTY_SYMBOL_RUNTIME_CONFIG.output_root
FORTY_SYMBOL_RUNTIME_CONTRACT = FORTY_SYMBOL_RUNTIME_CONFIG.runtime_contract
FORTY_SYMBOL_MANIFEST_CONTRACT = FORTY_SYMBOL_RUNTIME_CONFIG.manifest_contract
FORTY_SYMBOL_TOKEN_FILE = RUNTIME_TOKEN_FILE
FORTY_SYMBOL_INVOCATION_BUDGET_SECONDS = 300.0
_PUBLIC_RUNTIME_FAILURE_CODE = re.compile(r"runtime_[a-z0-9_]+")


def _public_runtime_failure_code(exc: Exception) -> str:
    """Return only a stable core failure code, never raw exception detail."""

    if isinstance(exc, CryptoTenSymbolObservationRuntimeError):
        reason = str(exc)
        if _PUBLIC_RUNTIME_FAILURE_CODE.fullmatch(reason):
            return reason
    return "runtime_unexpected_error"


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
            invocation_budget_seconds=FORTY_SYMBOL_INVOCATION_BUDGET_SECONDS,
        )
        exit_code = crypto_ten_symbol_observation_exit_code(receipt)
    except Exception as exc:
        print(
            "crypto forty symbol observation runtime failed closed",
            file=sys.stderr,
        )
        print(
            json.dumps(
                {
                    "contract": "tradingagent.crypto.forty_symbol_runtime_failure.v1",
                    "failure_code": _public_runtime_failure_code(exc),
                    "status": "failed_closed",
                },
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
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
    "FORTY_SYMBOL_INVOCATION_BUDGET_SECONDS",
    "FORTY_SYMBOL_OUTPUT_ROOT",
    "FORTY_SYMBOL_RUNTIME_CONTRACT",
    "FORTY_SYMBOL_TOKEN_FILE",
    "load_crypto_forty_symbol_observation_runtime_manifest",
    "main",
    "run_crypto_forty_symbol_observation_once",
]
