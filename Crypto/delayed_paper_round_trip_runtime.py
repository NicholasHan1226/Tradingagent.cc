"""Closed-5m server wrapper for the isolated Crypto round-trip candidate."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Callable

from Crypto.delayed_paper_round_trip import run_crypto_delayed_paper_round_trip_once
from Crypto.delayed_paper_round_trip_epoch import (
    ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY,
    ROUND_TRIP_EPOCH_MANIFEST_PATH,
    load_round_trip_epoch_manifest,
    prepare_round_trip_epoch_candidate,
)
from Crypto.delayed_paper_runtime import (
    RUNTIME_TOKEN_FILE,
    _LazyCryptoFiveMinutePort,
    crypto_runtime_receipt_exit_code,
    crypto_runtime_window_request,
    load_crypto_delayed_paper_runtime_manifest,
)
from Crypto.fixture_sim.contracts import _assert_simulation_only
from shared.data.sharedsignals_v1 import HTTPTransport
from shared.data.tradingdatas_transport import build_runtime_transport


ROUND_TRIP_RUNTIME_CONTRACT = "tradingagent.crypto.round_trip_server_runtime.v1"


def run_crypto_delayed_paper_round_trip_server_once(
    *,
    epoch_manifest: Path | str,
    runtime_manifest: Path | str,
    token_file: Path | str,
    now: datetime,
    transport_factory: Callable[..., HTTPTransport] = build_runtime_transport,
) -> dict[str, Any]:
    """Run exactly one new/pending closed-bar cycle in the isolated g3 root."""

    _assert_simulation_only()
    manifest_path = Path(epoch_manifest)
    if (
        manifest_path != ROUND_TRIP_EPOCH_MANIFEST_PATH
        and manifest_path.parent != ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY
    ):
        raise RuntimeError("round_trip_epoch_manifest_path_invalid")
    if Path(token_file) != RUNTIME_TOKEN_FILE:
        raise RuntimeError("round_trip_token_file_path_invalid")
    context = load_round_trip_epoch_manifest(manifest_path)
    prepared = prepare_round_trip_epoch_candidate(context)
    identity_before = prepared.identity_path.read_bytes()
    manifest = load_crypto_delayed_paper_runtime_manifest(runtime_manifest)
    request = crypto_runtime_window_request(now)
    port = _LazyCryptoFiveMinutePort(
        manifest=manifest,
        token_file=RUNTIME_TOKEN_FILE,
        transport_factory=transport_factory,
    )
    result = run_crypto_delayed_paper_round_trip_once(
        port=port,
        profile=manifest.profile,
        request=request,
        output_root=prepared.output_root,
    )
    # Re-read both anchors after the write: neither a changed g3 manifest nor a
    # changed g2 archive may be hidden by a successful local capital cycle.
    prepared_after = prepare_round_trip_epoch_candidate(context)
    if prepared_after.identity_path.read_bytes() != identity_before:
        raise RuntimeError("round_trip_epoch_identity_changed")
    return {
        "contract": ROUND_TRIP_RUNTIME_CONTRACT,
        "status": result.get("status"),
        "core_result": result,
        "requested_window_end": request.window_end.isoformat().replace("+00:00", "Z"),
        "requested_observation_cutoff": request.observation_cutoff.isoformat().replace(
            "+00:00", "Z"
        ),
        "runtime_manifest_sha256": manifest.sha256,
        "fresh_query_catalog_version": manifest.catalog_version,
        "fresh_query_profile_sha256": manifest.profile.sha256,
        "epoch_id": context.epoch_id,
        "epoch_generation": context.epoch_generation,
        "capital_authority_id": "crypto-round-trip-capital-v1",
        "capital_generation": 2,
        "aggregate_with_prior_generations": False,
        "market_data_transport": "loopback_tradingdatas_v1",
        "market_data_access_attempt_count": port.load_snapshot_calls,
        "market_data_network_used": port.transport_constructed_count > 0,
        "learning_mode": "detached_offline_worker",
        "learning_authority": False,
        "learning_invoked": False,
        "real_trading_enabled": False,
        "execution_eligible": False,
        "execution_authority": False,
        "production_eligible": False,
        "testnet_used": False,
        "live_broker_used": False,
        "model_network_used": False,
        "promotion_authorized": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "outbox_id": None,
        "capital_commit_id": None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one Crypto round-trip simulated cycle"
    )
    parser.add_argument("--epoch-manifest", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = run_crypto_delayed_paper_round_trip_server_once(
            epoch_manifest=args.epoch_manifest,
            runtime_manifest=args.runtime_manifest,
            token_file=args.token_file,
            now=datetime.now(tz=timezone.utc),
        )
        code = crypto_runtime_receipt_exit_code(receipt)
    except Exception:
        print("crypto round-trip runtime failed closed", file=sys.stderr)
        return 2
    if code:
        print("crypto round-trip runtime failed closed", file=sys.stderr)
        return code
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


__all__ = [
    "ROUND_TRIP_RUNTIME_CONTRACT",
    "main",
    "run_crypto_delayed_paper_round_trip_server_once",
]
