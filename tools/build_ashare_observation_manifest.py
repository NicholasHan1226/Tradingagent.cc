#!/usr/bin/env python3
"""Build the next TradingDatas-bound A-share observation manifest."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.data.tradingdatas_transport import (  # noqa: E402
    RuntimeGateConfigurationError,
    TradingDatasAuthenticationError,
    build_runtime_transport,
)
from shared.runtime.ashare_observation import (  # noqa: E402
    AshareObservationConfigurationError,
    assert_no_plaintext_token_environment,
)
from shared.runtime.ashare_observation_manifest import (  # noqa: E402
    AshareObservationManifestBlocked,
    AshareObservationManifestBuildConfig,
    AshareObservationManifestConfigurationError,
    build_ashare_observation_manifest,
)


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _absolute_path(value: Path, *, field_name: str) -> Path:
    if not value.is_absolute() or ".." in value.parts:
        raise AshareObservationManifestConfigurationError(
            f"{field_name}_must_be_absolute"
        )
    return value


def _decision_time(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(tz=_SHANGHAI)
    normalized = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
    try:
        value = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AshareObservationManifestConfigurationError(
            "decision_as_of_invalid"
        ) from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise AshareObservationManifestConfigurationError(
            "decision_as_of_must_be_timezone_aware"
        )
    return value


def _failure(reason_code: str) -> dict[str, object]:
    return {
        "status": "fail",
        "blocking": True,
        "reason_code": reason_code,
        "historical_pit_eligible": False,
        "execution_authority": False,
        "simulation_started": False,
        "real_trading_enabled": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Discover the current TradingDatas catalog and publish one "
            "simulation-only A-share observation manifest"
        )
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--access-policy-id", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--manifest-root", type=Path, required=True)
    parser.add_argument("--decision-as-of")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        assert_no_plaintext_token_environment(os.environ)
        if os.environ.get("REAL_TRADING_ENABLED") != "false":
            raise AshareObservationManifestConfigurationError(
                "real_trading_environment_must_equal_false"
            )
        token_file = _absolute_path(args.token_file, field_name="token_file")
        config = AshareObservationManifestBuildConfig(
            base_url=args.base_url,
            access_policy_id=args.access_policy_id,
            transport_id="http-json-v1",
            timeout_seconds=args.timeout_seconds,
            manifest_root=_absolute_path(
                args.manifest_root,
                field_name="manifest_root",
            ),
            decision_as_of=_decision_time(args.decision_as_of),
            real_trading_enabled=False,
        )
        transport = build_runtime_transport(
            config.transport_id,
            token_file=token_file,
            base_url=config.base_url,
        )
        result = build_ashare_observation_manifest(
            config,
            transport=transport,
        )
    except (
        AshareObservationConfigurationError,
        AshareObservationManifestConfigurationError,
        RuntimeGateConfigurationError,
    ) as exc:
        print(json.dumps(_failure(str(exc)), ensure_ascii=False, sort_keys=True))
        return 64
    except (
        AshareObservationManifestBlocked,
        TradingDatasAuthenticationError,
    ) as exc:
        reason = (
            exc.reason_code
            if isinstance(exc, AshareObservationManifestBlocked)
            else "tradingdatas_authentication_rejected"
        )
        print(json.dumps(_failure(reason), ensure_ascii=False, sort_keys=True))
        return 2
    except Exception:
        print(
            json.dumps(
                _failure("observation_manifest_build_failed"),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"pass observation_session={result.observation_session} "
            f"catalog_version={result.catalog_version} "
            f"manifest_sha256={result.manifest_sha256} "
            f"reused={str(result.reused).lower()}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
