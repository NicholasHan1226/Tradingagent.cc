#!/usr/bin/env python3
"""Run one authenticated, simulation-only A-share current observation."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.runtime.ashare_observation import (  # noqa: E402
    AshareObservationBlocked,
    AshareObservationConfig,
    AshareObservationConfigurationError,
    assert_no_plaintext_token_environment,
    run_ashare_observation,
)


def _absolute_path(value: Path | None, *, field_name: str) -> Path:
    if value is None or not value.is_absolute() or ".." in value.parts:
        raise AshareObservationConfigurationError(f"{field_name}_must_be_absolute")
    return value


def _token_path(argument: Path | None) -> Path:
    if argument is not None:
        return _absolute_path(argument, field_name="token_file")
    raw = os.environ.get("TRADINGDATAS_API_TOKEN_FILE")
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise AshareObservationConfigurationError("token_file_must_be_absolute")
    return _absolute_path(Path(raw), field_name="token_file")


def _failure(reason_code: str) -> dict[str, object]:
    return {
        "status": "fail",
        "blocking": True,
        "reason_code": reason_code,
        "real_trading_enabled": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Persist one bounded TradingDatas-driven A-share current observation"
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--token-file",
        type=Path,
        help=(
            "absolute TA-scoped token path; otherwise use TRADINGDATAS_API_TOKEN_FILE"
        ),
    )
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument(
        "--runtime-root",
        type=Path,
        required=True,
        help="explicit dedicated-worker runtime boundary (no business authority)",
    )
    parser.add_argument(
        "--log-root",
        type=Path,
        required=True,
        help="explicit dedicated-worker log boundary (stdout remains secret-free)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        assert_no_plaintext_token_environment(os.environ)
    except AshareObservationConfigurationError as exc:
        failure = _failure(str(exc))
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
        return 64

    if os.environ.get("REAL_TRADING_ENABLED") != "false":
        failure = _failure("real_trading_environment_must_equal_false")
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
        return 64

    try:
        manifest = _absolute_path(args.manifest, field_name="manifest_path")
        state_root = _absolute_path(args.state_root, field_name="state_root")
        # These two paths are explicit systemd boundaries.  The observation
        # runtime does not create another business authority inside either.
        _absolute_path(args.runtime_root, field_name="runtime_root")
        _absolute_path(args.log_root, field_name="log_root")
        config = AshareObservationConfig(
            manifest_path=manifest,
            token_file=_token_path(args.token_file),
            snapshot_root=state_root / "research-snapshots",
            marketgraph_mode="mg_off",
            real_trading_enabled=False,
        )
        result = run_ashare_observation(config)
    except AshareObservationConfigurationError as exc:
        failure = _failure(str(exc))
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
        return 64
    except AshareObservationBlocked as exc:
        failure = _failure(exc.reason_code)
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
        return 2
    except Exception:
        failure = _failure("observation_runtime_failed")
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
        return 2

    payload = result.to_dict(include_tradable_symbols=False)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"pass snapshot_sha256={result.snapshot_sha256} "
            f"observation_session={result.observation_session} "
            f"observation_universe_count={result.observation_universe_count} "
            f"observation_ledger_sha256={result.observation_ledger_sha256} "
            f"idempotent_replay={str(result.idempotent_replay).lower()}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
