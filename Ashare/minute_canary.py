"""Read-only TradingDatas five-minute canary for the A-share lane.

The command consumes a secret-free, catalog-bound profile plus a separate
TA-owned reference-fact file.  It never calls a provider directly and cannot
create orders, mutate capital, or enable a scheduler.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
import json
import os
from pathlib import Path
import sys
from typing import Any

from Ashare.minute_data import (
    MinuteDataContractError,
    MinuteBarSnapshot,
    MinuteDatasetProfile,
    MinuteEvidenceAuditLedger,
    MinuteEvidenceUse,
    MinuteReferenceFact,
    MinuteTimestampSemantics,
    TradingDatasMinuteMarketDataPort,
)
from shared.data.sharedsignals_v1 import (
    HTTPTransport,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)
from shared.data.tradingdatas_transport import (
    RuntimeGateConfigurationError,
    build_runtime_transport,
)


class MinuteCanaryConfigurationError(ValueError):
    """Fail-closed canary configuration failure."""


TransportFactory = Callable[..., HTTPTransport]


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MinuteCanaryConfigurationError(f"{field_name}_invalid")
    return value


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MinuteCanaryConfigurationError(f"{field_name}_invalid")
    return value


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MinuteCanaryConfigurationError(f"{field_name}_invalid")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, field_name)


@dataclass(frozen=True)
class MinuteCanaryConfig:
    """External, secret-free runtime inputs for one bounded canary."""

    base_url: str
    expected_catalog_version: str
    dataset_id: str
    access_policy_id: str
    transport_id: str
    timeout_seconds: float
    filters: Mapping[str, Any]
    profile: Mapping[str, Any]

    def __post_init__(self) -> None:
        for field_name in (
            "base_url",
            "expected_catalog_version",
            "dataset_id",
            "access_policy_id",
            "transport_id",
        ):
            _text(getattr(self, field_name), field_name)
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise MinuteCanaryConfigurationError("timeout_seconds_invalid")
        _mapping(self.filters, "filters")
        _mapping(self.profile, "profile")

    @property
    def page_limit(self) -> int:
        return _positive_int(self.profile.get("page_limit"), "profile_page_limit")

    def client_config(self) -> SharedSignalsV1Config:
        """Return the query client after catalog-bound profile validation.

        ``evidence_only`` does not make a global catalog version authoritative:
        the target row is independently fingerprinted before any query and each
        query envelope remains bound to the catalog this client observed.
        """

        return SharedSignalsV1Config(
            base_url=self.base_url,
            expected_catalog_version=self.expected_catalog_version,
            dataset_ids=frozenset({self.dataset_id}),
            access_policy_id=self.access_policy_id,
            catalog_version_policy="evidence_only",
            timeout_seconds=float(self.timeout_seconds),
            max_limit=self.page_limit,
            cache_ttl_seconds=0,
        )

    def build_profile(
        self,
        client: SharedSignalsV1Client,
        *,
        require_declared_bindings: bool = True,
    ) -> MinuteDatasetProfile:
        values = self.profile
        try:
            timestamp_semantics = MinuteTimestampSemantics(
                _text(
                    values.get("timestamp_semantics"),
                    "profile_timestamp_semantics",
                )
            )
        except ValueError as exc:
            raise MinuteCanaryConfigurationError(
                "profile_timestamp_semantics_invalid"
            ) from exc
        identity_fields = values.get("identity_fields")
        if not isinstance(identity_fields, list) or not identity_fields:
            raise MinuteCanaryConfigurationError("profile_identity_fields_invalid")
        expected_fingerprint = values.get("dataset_contract_fingerprint")
        expected_consumer_sha = values.get("consumer_profile_sha256")
        if require_declared_bindings:
            expected_fingerprint = _text(
                expected_fingerprint,
                "profile_dataset_contract_fingerprint",
            )
            expected_consumer_sha = _text(
                expected_consumer_sha,
                "profile_consumer_profile_sha256",
            )
        elif expected_fingerprint is not None:
            expected_fingerprint = _text(
                expected_fingerprint,
                "profile_dataset_contract_fingerprint",
            )
        profile = MinuteDatasetProfile.from_catalog(
            client.get_catalog(),
            expected_catalog_version=self.expected_catalog_version,
            expected_dataset_contract_fingerprint=expected_fingerprint,
            dataset_id=self.dataset_id,
            identity_fields=tuple(
                _text(value, "profile_identity_field") for value in identity_fields
            ),
            symbol_field=_text(values.get("symbol_field"), "profile_symbol_field"),
            timestamp_field=_text(
                values.get("timestamp_field"), "profile_timestamp_field"
            ),
            open_field=_text(values.get("open_field"), "profile_open_field"),
            high_field=_text(values.get("high_field"), "profile_high_field"),
            low_field=_text(values.get("low_field"), "profile_low_field"),
            close_field=_text(values.get("close_field"), "profile_close_field"),
            volume_field=_text(values.get("volume_field"), "profile_volume_field"),
            amount_field=_text(values.get("amount_field"), "profile_amount_field"),
            previous_close_field=_optional_text(
                values.get("previous_close_field"),
                "profile_previous_close_field",
            ),
            suspension_field=_optional_text(
                values.get("suspension_field"), "profile_suspension_field"
            ),
            frequency_field=_optional_text(
                values.get("frequency_field"), "profile_frequency_field"
            ),
            frequency_value=_optional_text(
                values.get("frequency_value"), "profile_frequency_value"
            ),
            timestamp_format=_text(
                values.get("timestamp_format"), "profile_timestamp_format"
            ),
            timestamp_semantics=timestamp_semantics,
            volume_multiplier_to_shares=values.get("volume_multiplier_to_shares"),
            amount_multiplier_to_cny=values.get("amount_multiplier_to_cny"),
            price_adjustment=_text(
                values.get("price_adjustment"), "profile_price_adjustment"
            ),
            max_pages=_positive_int(values.get("max_pages"), "profile_max_pages"),
            max_rows=_positive_int(values.get("max_rows"), "profile_max_rows"),
            page_limit=self.page_limit,
        )
        if (
            expected_consumer_sha is not None
            and profile.consumer_profile_sha256 != expected_consumer_sha
        ):
            raise MinuteCanaryConfigurationError("profile_consumer_profile_drift")
        return profile


def load_minute_canary_config(path: Path | str) -> MinuteCanaryConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinuteCanaryConfigurationError("minute_canary_manifest_invalid") from exc
    value = _mapping(raw, "minute_canary_manifest")
    if "expected_catalog_version" in value:
        expected_catalog_version = value.get("expected_catalog_version")
        if "catalog_version" in value and (
            _text(expected_catalog_version, "expected_catalog_version")
            != _text(value.get("catalog_version"), "catalog_version")
        ):
            raise MinuteCanaryConfigurationError(
                "catalog_version_compatibility_mismatch"
            )
    else:
        expected_catalog_version = value.get("catalog_version")
    return MinuteCanaryConfig(
        base_url=value.get("base_url"),
        expected_catalog_version=expected_catalog_version,
        dataset_id=value.get("dataset_id"),
        access_policy_id=value.get("access_policy_id"),
        transport_id=value.get("transport_id"),
        timeout_seconds=value.get("timeout_seconds"),
        filters=_mapping(value.get("filters"), "filters"),
        profile=_mapping(value.get("profile"), "profile"),
    )


def load_reference_facts(path: Path | str) -> dict[str, MinuteReferenceFact]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinuteCanaryConfigurationError(
            "minute_reference_manifest_invalid"
        ) from exc
    if not isinstance(raw, list) or not raw:
        raise MinuteCanaryConfigurationError("minute_reference_manifest_invalid")
    result: dict[str, MinuteReferenceFact] = {}
    for item in raw:
        row = _mapping(item, "minute_reference_row")
        symbol = _text(row.get("symbol"), "minute_reference_symbol").upper()
        try:
            trade_date = date.fromisoformat(
                _text(row.get("trade_date"), "minute_reference_trade_date")
            )
            fact = MinuteReferenceFact(
                symbol=symbol,
                trade_date=trade_date,
                previous_close_cny=row.get("previous_close_cny"),
                suspended=row.get("suspended"),
                evidence_sha256=row.get("evidence_sha256"),
            )
        except (ValueError, MinuteDataContractError) as exc:
            raise MinuteCanaryConfigurationError(
                "minute_reference_row_invalid"
            ) from exc
        if symbol in result:
            raise MinuteCanaryConfigurationError("minute_reference_duplicate_symbol")
        result[symbol] = fact
    return result


def run_minute_canary(
    config: MinuteCanaryConfig,
    *,
    token_file: Path | str,
    decision_time: datetime,
    trading_date: date,
    reference_facts: Mapping[str, MinuteReferenceFact],
    evidence_use: MinuteEvidenceUse = MinuteEvidenceUse.LOW_LATENCY_EXECUTION,
    transport_factory: TransportFactory = build_runtime_transport,
) -> dict[str, Any]:
    profile, snapshot, audit = load_minute_snapshot(
        config,
        token_file=token_file,
        decision_time=decision_time,
        trading_date=trading_date,
        reference_facts=reference_facts,
        evidence_use=evidence_use,
        transport_factory=transport_factory,
    )
    return {
        "status": "pass",
        "authority_tier": "observation_only",
        "evidence_use": evidence_use.value,
        "execution_latency_eligible": all(
            bar.execution_latency_eligible for bar in snapshot.bars
        ),
        "real_trading_enabled": False,
        "trading_date": trading_date.isoformat(),
        "decision_time": decision_time.isoformat(),
        "dataset_id": profile.dataset_id,
        "expected_catalog_version": profile.expected_catalog_version,
        "observed_catalog_version": snapshot.observed_catalog_version,
        "catalog_version_drift": snapshot.catalog_version_drift,
        "dataset_contract_fingerprint": profile.dataset_contract_fingerprint,
        "consumer_profile_sha256": profile.consumer_profile_sha256,
        "row_count": snapshot.row_count,
        "page_count": snapshot.page_count,
        "same_observation": snapshot.same_observation,
        "lineage_complete": True,
        "snapshot_sha256": snapshot.sha256,
        "bars": [
            {
                "symbol": bar.symbol,
                "bar_end": bar.bar_end.isoformat(),
                "receipt_id": bar.receipt_id,
                "observed_at": bar.observed_at.isoformat(),
                "sha256": bar.sha256,
            }
            for bar in snapshot.bars
        ],
        "audit_rejections": len(audit.records()),
    }


def load_minute_snapshot(
    config: MinuteCanaryConfig,
    *,
    token_file: Path | str,
    decision_time: datetime,
    trading_date: date,
    reference_facts: Mapping[str, MinuteReferenceFact],
    evidence_use: MinuteEvidenceUse = MinuteEvidenceUse.LOW_LATENCY_EXECUTION,
    transport_factory: TransportFactory = build_runtime_transport,
) -> tuple[MinuteDatasetProfile, MinuteBarSnapshot, MinuteEvidenceAuditLedger]:
    """Load one exact-bar snapshot for observation or explicit delayed paper."""

    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise MinuteCanaryConfigurationError("real_trading_must_remain_disabled")
    transport = transport_factory(
        config.transport_id,
        token_file=token_file,
        base_url=config.base_url,
    )
    client = SharedSignalsV1Client(config.client_config(), transport=transport)
    profile = config.build_profile(client)
    audit = MinuteEvidenceAuditLedger()
    snapshot = TradingDatasMinuteMarketDataPort(client).load_snapshot(
        profile=profile,
        filters=config.filters,
        decision_time=decision_time,
        trading_dates=frozenset({trading_date}),
        audit_ledger=audit,
        reference_facts=reference_facts,
        evidence_use=evidence_use,
    )
    return profile, snapshot, audit


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(
            dict(receipt),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise MinuteCanaryConfigurationError(
            "minute_canary_receipt_persist_failed"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only A-share five-minute TradingDatas canary"
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--reference-facts", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--decision-time", required=True)
    parser.add_argument("--trading-date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--evidence-use",
        choices=tuple(value.value for value in MinuteEvidenceUse),
        default=MinuteEvidenceUse.LOW_LATENCY_EXECUTION.value,
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.token_file.is_absolute():
            raise MinuteCanaryConfigurationError("token_file_must_be_absolute")
        receipt = run_minute_canary(
            load_minute_canary_config(args.manifest),
            token_file=args.token_file,
            decision_time=datetime.fromisoformat(args.decision_time),
            trading_date=date.fromisoformat(args.trading_date),
            reference_facts=load_reference_facts(args.reference_facts),
            evidence_use=MinuteEvidenceUse(args.evidence_use),
        )
        _write_receipt(args.output, receipt)
    except (
        MinuteCanaryConfigurationError,
        MinuteDataContractError,
        RuntimeGateConfigurationError,
        OSError,
        ValueError,
    ):
        print("minute canary failed closed", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"PASS dataset={receipt['dataset_id']} rows={receipt['row_count']} "
            f"pages={receipt['page_count']} replay={receipt['same_observation']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MinuteCanaryConfig",
    "MinuteCanaryConfigurationError",
    "load_minute_canary_config",
    "load_minute_snapshot",
    "load_reference_facts",
    "main",
    "run_minute_canary",
]
