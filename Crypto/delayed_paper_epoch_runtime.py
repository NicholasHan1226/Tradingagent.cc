"""Server wrapper for an explicit Crypto delayed-paper outage epoch."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Callable

import Crypto.delayed_paper_epoch as epoch_contract
from Crypto.delayed_paper_epoch import (
    load_crypto_delayed_paper_epoch_manifest,
    prepare_crypto_delayed_paper_epoch,
)
from Crypto.delayed_paper_runtime import (
    HTTPTransport,
    RUNTIME_TOKEN_FILE,
    build_runtime_transport,
    crypto_runtime_receipt_exit_code,
    run_crypto_delayed_paper_server_once,
)


def run_crypto_delayed_paper_epoch_once(
    *,
    epoch_manifest: Path | str,
    runtime_manifest: Path | str,
    token_file: Path | str,
    now: datetime,
    transport_factory: Callable[..., HTTPTransport] = (build_runtime_transport),
) -> dict[str, Any]:
    context = load_crypto_delayed_paper_epoch_manifest(epoch_manifest)
    prepared = prepare_crypto_delayed_paper_epoch(context)
    core = run_crypto_delayed_paper_server_once(
        runtime_manifest=runtime_manifest,
        token_file=token_file,
        output_root=prepared.output_root,
        now=now,
        transport_factory=transport_factory,
        epoch_context=context,
    )
    if core.get("epoch_identity_sha256") != prepared.identity_sha256:
        raise RuntimeError("epoch_receipt_identity_mismatch")
    return core


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=("Run one explicit Crypto delayed-paper outage epoch")
    )
    parser.add_argument(
        "--epoch-manifest",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--runtime-manifest",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        required=True,
    )
    args = parser.parse_args(argv)
    if (
        args.epoch_manifest != epoch_contract.EPOCH_MANIFEST_PATH
        or args.token_file != RUNTIME_TOKEN_FILE
    ):
        print(
            "crypto delayed paper epoch runtime failed closed",
            file=sys.stderr,
        )
        return 2
    try:
        receipt = run_crypto_delayed_paper_epoch_once(
            epoch_manifest=args.epoch_manifest,
            runtime_manifest=args.runtime_manifest,
            token_file=args.token_file,
            now=datetime.now(tz=timezone.utc),
        )
        exit_code = crypto_runtime_receipt_exit_code(receipt)
    except Exception:
        print(
            "crypto delayed paper epoch runtime failed closed",
            file=sys.stderr,
        )
        return 2
    if exit_code:
        print(
            "crypto delayed paper epoch runtime failed closed",
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
    "main",
    "run_crypto_delayed_paper_epoch_once",
]
