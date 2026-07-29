"""Detached offline worker for Crypto delayed-paper learning projections."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from Crypto.delayed_paper_learning import (
    _non_authority_fields,
    run_crypto_delayed_paper_learning_full_scrub,
    run_crypto_delayed_paper_learning_incremental,
)


PRODUCTION_OUTPUT_ROOT = Path("/var/lib/tradingagent/crypto-delayed-paper")


def learning_worker_exit_code(result: Mapping[str, Any]) -> int:
    """Return success only for bounded non-authoritative outcomes."""

    if not isinstance(result, Mapping):
        return 2
    if result.get("learning_mode") != "detached_offline_worker":
        return 2
    if any(
        result.get(field) != expected
        for field, expected in _non_authority_fields().items()
    ):
        return 2
    if result.get("status") in {
        "current",
        "deferred_core_pending",
        "none",
        "projected",
        "recovered",
        "scrubbed",
    }:
        return 0
    return 2


def run_learning_worker_once(
    *,
    mode: str,
    output_root: Path | str,
) -> dict[str, Any]:
    if mode == "incremental":
        return run_crypto_delayed_paper_learning_incremental(output_root=output_root)
    if mode == "full-scrub":
        return run_crypto_delayed_paper_learning_full_scrub(output_root=output_root)
    raise ValueError("unsupported_learning_worker_mode")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=("Run the detached Crypto delayed-paper learning worker")
    )
    parser.add_argument(
        "--mode",
        choices=("incremental", "full-scrub"),
        required=True,
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PRODUCTION_OUTPUT_ROOT,
    )
    args = parser.parse_args(argv)
    if args.output_root != PRODUCTION_OUTPUT_ROOT:
        print(
            "crypto delayed-paper learning worker failed closed",
            file=sys.stderr,
        )
        return 2
    try:
        result = run_learning_worker_once(
            mode=args.mode,
            output_root=args.output_root,
        )
    except Exception:
        print(
            "crypto delayed-paper learning worker failed closed",
            file=sys.stderr,
        )
        return 2
    exit_code = learning_worker_exit_code(result)
    if exit_code:
        print(
            "crypto delayed-paper learning worker failed closed",
            file=sys.stderr,
        )
        return exit_code
    print(
        json.dumps(
            result,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
