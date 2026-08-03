"""Closed-5m server wrapper for the isolated Crypto round-trip candidate."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

from Crypto.delayed_paper_ledger import CryptoDelayedPaperObservationStore
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
from Crypto.five_minute_data import CryptoFiveMinuteWindowRequest
from shared.data.sharedsignals_v1 import HTTPTransport
from shared.data.tradingdatas_transport import build_runtime_transport


ROUND_TRIP_RUNTIME_CONTRACT = "tradingagent.crypto.round_trip_server_runtime.v1"
ROUND_TRIP_RUNTIME_JOURNAL_CONTRACT = "tradingagent.crypto.round_trip_server_journal.v1"
ROUND_TRIP_SETTLED_BAR_DELAY = timedelta(minutes=5)


def crypto_round_trip_window_request(now: datetime) -> CryptoFiveMinuteWindowRequest:
    """Observe one fully settled bar without relaxing the PIT cutoff.

    The Crypto collector and this paper runtime are deliberately independent.
    Consuming the prior closed bar gives the collector a full five-minute
    interval to publish its receipt, while retaining the current cycle's fixed
    observation cutoff for the historical query.
    """

    current = crypto_runtime_window_request(now)
    return CryptoFiveMinuteWindowRequest(
        window_end=current.window_end - ROUND_TRIP_SETTLED_BAR_DELAY,
        observation_cutoff=current.observation_cutoff,
    )


def round_trip_runtime_journal_summary(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bounded systemd-journal projection of one runtime receipt.

    The complete nested core result remains in the Crypto audit ledger.  The
    process journal contains only the fields needed to identify a cycle, its
    data contract, and its simulation-only boundary.
    """

    core_result = receipt.get("core_result")
    if not isinstance(core_result, Mapping):
        raise RuntimeError("round_trip_runtime_core_result_invalid")
    return {
        "contract": ROUND_TRIP_RUNTIME_JOURNAL_CONTRACT,
        "runtime_contract": receipt.get("contract"),
        "status": receipt.get("status"),
        "market_slot": core_result.get("market_slot"),
        "recovered_pending": core_result.get("recovered_pending"),
        "idempotent_replay": core_result.get("idempotent_replay"),
        "replay_mode": core_result.get("replay_mode"),
        "requested_window_end": receipt.get("requested_window_end"),
        "requested_observation_cutoff": receipt.get("requested_observation_cutoff"),
        "settled_bar_delay_seconds": receipt.get("settled_bar_delay_seconds"),
        "runtime_manifest_sha256": receipt.get("runtime_manifest_sha256"),
        "fresh_query_catalog_version": receipt.get("fresh_query_catalog_version"),
        "fresh_query_profile_sha256": receipt.get("fresh_query_profile_sha256"),
        "epoch_id": receipt.get("epoch_id"),
        "epoch_generation": receipt.get("epoch_generation"),
        "market_data_access_attempt_count": receipt.get(
            "market_data_access_attempt_count"
        ),
        "market_data_network_used": receipt.get("market_data_network_used"),
        "learning_mode": receipt.get("learning_mode"),
        "learning_authority": receipt.get("learning_authority"),
        "learning_invoked": receipt.get("learning_invoked"),
        "real_trading_enabled": receipt.get("real_trading_enabled"),
        "execution_eligible": receipt.get("execution_eligible"),
        "execution_authority": receipt.get("execution_authority"),
        "production_eligible": receipt.get("production_eligible"),
        "testnet_used": receipt.get("testnet_used"),
        "live_broker_used": receipt.get("live_broker_used"),
        "model_network_used": receipt.get("model_network_used"),
        "promotion_authorized": receipt.get("promotion_authorized"),
        "automatic_promotion_enabled": receipt.get(
            "automatic_promotion_enabled"
        ),
        "automatic_risk_expansion_enabled": receipt.get(
            "automatic_risk_expansion_enabled"
        ),
        "outbox_id": receipt.get("outbox_id"),
        "capital_commit_id": receipt.get("capital_commit_id"),
    }


def run_crypto_delayed_paper_round_trip_server_once(
    *,
    epoch_manifest: Path | str,
    runtime_manifest: Path | str,
    token_file: Path | str,
    now: datetime,
    transport_factory: Callable[..., HTTPTransport] = build_runtime_transport,
) -> dict[str, Any]:
    """Run exactly one new/pending closed-bar cycle in the isolated epoch."""

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
    request = crypto_round_trip_window_request(now)
    port = _LazyCryptoFiveMinutePort(
        manifest=manifest,
        token_file=RUNTIME_TOKEN_FILE,
        transport_factory=transport_factory,
    )
    checkpoint = CryptoDelayedPaperObservationStore(
        prepared.output_root
    ).runtime_checkpoint()
    requested_market_slot = (
        (request.window_end - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    )
    if (
        checkpoint.get("pending") is None
        and checkpoint.get("latest_market_slot") == requested_market_slot
    ):
        # A completed slot is immutable. Do not re-query a mutable current
        # view and risk accepting a different payload for the same slot.
        result = {
            "contract": "tradingagent.crypto.delayed_paper_round_trip_runner.v1",
            "status": "completed",
            "market": "crypto",
            "market_slot": requested_market_slot,
            "recovered_pending": False,
            "idempotent_replay": True,
            "replay_mode": "completed_slot_without_fresh_query",
        }
    else:
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
        "settled_bar_delay_seconds": int(ROUND_TRIP_SETTLED_BAR_DELAY.total_seconds()),
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
    try:
        rendered = json.dumps(
            round_trip_runtime_journal_summary(receipt),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except Exception:
        print("crypto round-trip runtime failed closed", file=sys.stderr)
        return 2
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ROUND_TRIP_RUNTIME_CONTRACT",
    "ROUND_TRIP_RUNTIME_JOURNAL_CONTRACT",
    "ROUND_TRIP_SETTLED_BAR_DELAY",
    "crypto_round_trip_window_request",
    "main",
    "round_trip_runtime_journal_summary",
    "run_crypto_delayed_paper_round_trip_server_once",
]
