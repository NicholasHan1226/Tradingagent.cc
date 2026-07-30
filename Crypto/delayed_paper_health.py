"""Read-only health snapshot for the Crypto delayed-paper runtime."""

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
    CryptoDelayedPaperExitShadowError,
    _latest_capital_snapshot,
)
from Crypto.delayed_paper_learning import (
    CryptoDelayedPaperLearningError,
    _verified_sources,
)
from Crypto.delayed_paper_ledger import (
    CryptoDelayedPaperLedgerError,
    CryptoDelayedPaperObservationStore,
    _read_json,
    _sha256,
)
from Crypto.fixture_sim.ledger import CryptoLedgerError


HEALTH_CONTRACT = "tradingagent.crypto.delayed_paper_health.v1"
PRODUCTION_EPOCH_MANIFEST = EPOCH_MANIFEST_PATH


class CryptoDelayedPaperHealthError(RuntimeError):
    """Stable fail-closed error for a corrupt or inconsistent health source."""


def _non_authority_fields() -> dict[str, Any]:
    return {
        "authority": "none",
        "read_only": True,
        "execution_eligible": False,
        "execution_authority": False,
        "production_eligible": False,
        "real_trading_enabled": False,
        "network_used": False,
        "model_network_used": False,
        "testnet_used": False,
        "live_broker_used": False,
        "promotion_authorized": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "outbox_id": None,
        "capital_commit_id": None,
    }


def _projection_state(
    *,
    root: Path,
    observation_id: str,
) -> dict[str, Any]:
    path = root / "evolution" / "exit_shadow" / f"{observation_id}.json"
    if not path.exists():
        return {
            "state": "absent",
            "observation_id": None,
            "projection_sha256": None,
            "shadow_exit_count": None,
        }
    try:
        payload = _read_json(path)
    except CryptoDelayedPaperLedgerError as exc:
        raise CryptoDelayedPaperHealthError(
            "crypto_health_exit_shadow_invalid"
        ) from exc
    material = dict(payload)
    claimed = material.pop("projection_sha256", None)
    if (
        payload.get("contract") != "tradingagent.crypto.delayed_paper_exit_shadow.v1"
        or payload.get("observation_id") != observation_id
        or claimed != _sha256(material)
        or payload.get("execution_authority") is not False
        or payload.get("real_trading_enabled") is not False
    ):
        raise CryptoDelayedPaperHealthError("crypto_health_exit_shadow_invalid")
    return {
        "state": "current",
        "observation_id": observation_id,
        "projection_sha256": claimed,
        "shadow_exit_count": payload.get("shadow_exit_count"),
    }


def _learning_state(root: Path, *, observation_id: str) -> dict[str, Any]:
    path = root / "evolution" / "worker_state.json"
    if not path.exists():
        return {
            "state": "absent",
            "last_projected_observation_id": None,
        }
    try:
        payload = _read_json(path)
    except CryptoDelayedPaperLedgerError as exc:
        raise CryptoDelayedPaperHealthError(
            "crypto_health_learning_state_invalid"
        ) from exc
    projected = payload.get("last_projected_observation_id")
    return {
        "state": "current" if projected == observation_id else "behind",
        "last_projected_observation_id": projected,
    }


def build_crypto_delayed_paper_health(
    *,
    output_root: Path | str,
) -> dict[str, Any]:
    """Build a no-write snapshot from current verified runtime evidence."""

    root = Path(output_root)
    try:
        store = CryptoDelayedPaperObservationStore(root)
        checkpoint = store.runtime_checkpoint()
        state = _read_json(store.observation_state_path)
        observation_id = state.get("latest_observation_id")
        if (
            not isinstance(observation_id, str)
            or checkpoint.get("pending") is not None
            or checkpoint.get("observation_count") != checkpoint.get("completion_count")
        ):
            raise CryptoDelayedPaperHealthError("crypto_health_core_incomplete")
        observation, completion, trusted = _verified_sources(
            root=root,
            observation_id=observation_id,
            supplied_symbols=None,
        )
        snapshot, head_sequence, head_checksum = _latest_capital_snapshot(
            output_root=root,
            trusted=trusted,
        )
        positions = snapshot["positions"]
        decisions = {
            symbol: item["bundle"]["decision"].get("action")
            for symbol, item in sorted(trusted.items())
        }
        exit_shadow = _projection_state(
            root=root,
            observation_id=observation_id,
        )
        learning = _learning_state(root, observation_id=observation_id)
    except CryptoDelayedPaperHealthError:
        raise
    except (
        OSError,
        CryptoDelayedPaperExitShadowError,
        CryptoDelayedPaperLearningError,
        CryptoDelayedPaperLedgerError,
        CryptoLedgerError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise CryptoDelayedPaperHealthError("crypto_health_source_invalid") from exc
    return {
        "contract": HEALTH_CONTRACT,
        "status": "healthy",
        "market": "crypto",
        "market_session": "24x7",
        "core": {
            "observation_count": checkpoint["observation_count"],
            "completion_count": checkpoint["completion_count"],
            "pending": False,
            "latest_observation_id": observation_id,
            "latest_market_slot": observation.get("market_slot"),
            "latest_completion_sha256": completion.get("completion_sha256"),
            "symbol_decisions": decisions,
        },
        "capital": {
            "account_id": snapshot.get("account_id"),
            "account_type": snapshot.get("account_type"),
            "generation": snapshot.get("generation"),
            "currency": snapshot.get("currency"),
            "cash": snapshot.get("cash"),
            "equity": snapshot.get("equity"),
            "fees": snapshot.get("fees"),
            "position_count": len(positions),
            "position_symbols": sorted(positions),
            "balanced": snapshot.get("balanced"),
            "head_sequence": head_sequence,
            "head_checksum": head_checksum,
        },
        "exit_shadow": exit_shadow,
        "learning": learning,
        **_non_authority_fields(),
    }


def health_exit_code(result: Mapping[str, Any]) -> int:
    if not isinstance(result, Mapping) or result.get("status") != "healthy":
        return 2
    if any(
        result.get(field) != expected
        for field, expected in _non_authority_fields().items()
    ):
        return 2
    return 0


def run_health_once(*, epoch_manifest: Path | str) -> dict[str, Any]:
    manifest_path = Path(epoch_manifest)
    if manifest_path != PRODUCTION_EPOCH_MANIFEST:
        raise ValueError("crypto_health_epoch_manifest_path_invalid")
    context = load_crypto_delayed_paper_epoch_manifest(manifest_path)
    validate_epoch_runtime_context(context, output_root=context.output_root)
    result = build_crypto_delayed_paper_health(output_root=context.output_root)
    validate_epoch_runtime_context(context, output_root=context.output_root)
    return {
        **result,
        **epoch_runtime_receipt_fields(context),
        "epoch_output_root": str(context.output_root),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read the current Crypto delayed-paper health"
    )
    parser.add_argument(
        "--epoch-manifest",
        type=Path,
        default=PRODUCTION_EPOCH_MANIFEST,
    )
    args = parser.parse_args(argv)
    if args.epoch_manifest != PRODUCTION_EPOCH_MANIFEST:
        print("crypto delayed-paper health failed closed", file=sys.stderr)
        return 2
    try:
        result = run_health_once(epoch_manifest=args.epoch_manifest)
    except Exception:
        print("crypto delayed-paper health failed closed", file=sys.stderr)
        return 2
    code = health_exit_code(result)
    if code:
        print("crypto delayed-paper health failed closed", file=sys.stderr)
        return code
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
