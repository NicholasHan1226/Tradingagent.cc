"""Epoch-bound one-shot worker for detached Crypto exit-shadow projection."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from Crypto.delayed_paper_epoch import (
    EPOCH_MANIFEST_PATH,
    epoch_runtime_receipt_fields,
    load_crypto_delayed_paper_epoch_manifest,
    validate_epoch_runtime_context,
)
from Crypto.delayed_paper_exit_shadow import (
    _non_authority_fields,
    project_crypto_delayed_paper_exit_shadow,
)


PRODUCTION_EPOCH_MANIFEST = EPOCH_MANIFEST_PATH


def exit_shadow_worker_exit_code(result: Mapping[str, Any]) -> int:
    if not isinstance(result, Mapping):
        return 2
    if result.get("mode") != "detached_exit_shadow":
        return 2
    if any(
        result.get(field) != expected
        for field, expected in _non_authority_fields().items()
    ):
        return 2
    return 0 if result.get("status") == "projected" else 2


def run_exit_shadow_worker_once(
    *,
    epoch_manifest: Path | str,
) -> dict[str, Any]:
    manifest_path = Path(epoch_manifest)
    if manifest_path != PRODUCTION_EPOCH_MANIFEST:
        raise ValueError("exit_shadow_epoch_manifest_path_invalid")
    context = load_crypto_delayed_paper_epoch_manifest(manifest_path)
    validate_epoch_runtime_context(context, output_root=context.output_root)
    result = project_crypto_delayed_paper_exit_shadow(output_root=context.output_root)
    validate_epoch_runtime_context(context, output_root=context.output_root)
    return {
        **result,
        **epoch_runtime_receipt_fields(context),
        "epoch_output_root": str(context.output_root),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the detached Crypto delayed-paper exit shadow"
    )
    parser.add_argument(
        "--epoch-manifest",
        type=Path,
        default=PRODUCTION_EPOCH_MANIFEST,
    )
    args = parser.parse_args(argv)
    if args.epoch_manifest != PRODUCTION_EPOCH_MANIFEST:
        print("crypto exit-shadow worker failed closed", file=sys.stderr)
        return 2
    try:
        result = run_exit_shadow_worker_once(epoch_manifest=args.epoch_manifest)
    except Exception:
        print("crypto exit-shadow worker failed closed", file=sys.stderr)
        return 2
    exit_code = exit_shadow_worker_exit_code(result)
    if exit_code:
        print("crypto exit-shadow worker failed closed", file=sys.stderr)
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
