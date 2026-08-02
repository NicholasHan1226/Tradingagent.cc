"""Read-only formal-parity harness for A-share event and moneyflow evidence.

This module is intentionally a sidecar, not a minute-runtime integration.  It
uses only the public TradingDatas V1 catalog/query client supplied by its
caller, writes a secret-free receipt only when explicitly requested, and never
constructs a candidate, training sample, order, capital action, or LLM request.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

from Ashare.event_evidence import (
    AshareEvidenceAuditLedger,
    AshareEvidenceContractError,
    EvidenceDatasetProfile,
    TradingDatasAshareEvidencePort,
)
from Ashare.moneyflow_evidence import (
    AshareMoneyflowAuditLedger,
    AshareMoneyflowEvidenceError,
    MoneyflowDatasetProfile,
    TradingDatasAshareMoneyflowPort,
)
from shared.data.sharedsignals_v1 import (
    HTTPTransport,
    CatalogEnvelope,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)
from shared.data.tradingdatas_transport import (
    RuntimeGateConfigurationError,
    build_runtime_transport,
)
from shared.universe.policy import classify_instrument


FORMAL_BASE_URL = "http://127.0.0.1:18082"
DATASET_IDS = (
    "cn.dataset.anns_d",
    "cn.dataset.major_news",
    "cn.dataset.moneyflow",
    "cn.dataset.moneyflow_ths",
)
EVENT_DATASET_IDS = DATASET_IDS[:2]
MACRO_EVENT_DATASET_IDS = frozenset({"cn.dataset.major_news"})
MONEYFLOW_DATASET_IDS = DATASET_IDS[2:]
REQUIRED_DATASET_IDS = frozenset(("cn.dataset.anns_d", *MONEYFLOW_DATASET_IDS))
RECEIPT_SCHEMA = "tradingagent.ashare.evidence-shadow-parity.v1"
TransportFactory = Callable[..., HTTPTransport]


class ShadowParityError(ValueError):
    """A stable, fail-closed reason for the non-authoritative sidecar."""


def _text(value: object, reason: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise ShadowParityError(reason)
    return value


def _aware(value: object, reason: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ShadowParityError(reason)
    return value


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ShadowParityError("shadow_parity_payload_not_canonical") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _mainboard_symbols(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ShadowParityError("shadow_parity_allowed_symbols_invalid")
    symbols = tuple(value)
    if len(symbols) != len(set(symbols)):
        raise ShadowParityError("shadow_parity_allowed_symbols_invalid")
    for symbol in symbols:
        text = _text(symbol, "shadow_parity_allowed_symbols_invalid")
        eligibility = classify_instrument(text, instrument_type="common_stock")
        if (
            not eligibility.order_identity_allowed
            or eligibility.normalized_symbol != text
        ):
            raise ShadowParityError("shadow_parity_allowed_symbols_invalid")
    return symbols


@dataclass(frozen=True)
class ShadowParityPlan:
    """All secret-free inputs for one bounded four-source parity run."""

    expected_catalog_version: str
    decision_time: datetime
    allowed_symbols: tuple[str, ...]
    filters_by_dataset: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        _text(
            self.expected_catalog_version,
            "shadow_parity_expected_catalog_version_invalid",
        )
        _aware(self.decision_time, "shadow_parity_decision_time_timezone_required")
        object.__setattr__(
            self, "allowed_symbols", _mainboard_symbols(self.allowed_symbols)
        )
        if not isinstance(self.filters_by_dataset, Mapping):
            raise ShadowParityError("shadow_parity_filters_invalid")
        filters = dict(self.filters_by_dataset)
        if set(filters) != set(DATASET_IDS):
            raise ShadowParityError("shadow_parity_dataset_scope")
        if any(not isinstance(value, Mapping) for value in filters.values()):
            raise ShadowParityError("shadow_parity_filters_invalid")
        object.__setattr__(
            self,
            "filters_by_dataset",
            {dataset_id: dict(filters[dataset_id]) for dataset_id in DATASET_IDS},
        )


@dataclass(frozen=True)
class ShadowParityRuntimeConfig:
    """Runtime wiring; preflight parses this without reading a token."""

    base_url: str
    expected_catalog_version: str
    access_policy_id: str
    transport_id: str
    timeout_seconds: float
    plan: ShadowParityPlan

    def __post_init__(self) -> None:
        if _text(self.base_url, "shadow_parity_base_url_invalid") != FORMAL_BASE_URL:
            raise ShadowParityError("shadow_parity_base_url_invalid")
        for field_name in (
            "expected_catalog_version",
            "access_policy_id",
            "transport_id",
        ):
            _text(getattr(self, field_name), f"shadow_parity_{field_name}_invalid")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ShadowParityError("shadow_parity_timeout_invalid")
        if self.plan.expected_catalog_version != self.expected_catalog_version:
            raise ShadowParityError("shadow_parity_manifest_catalog_binding_invalid")

    def client_config(self, dataset_ids: frozenset[str]) -> SharedSignalsV1Config:
        if dataset_ids not in (
            frozenset(EVENT_DATASET_IDS),
            frozenset(MONEYFLOW_DATASET_IDS),
        ):
            raise ShadowParityError("shadow_parity_client_dataset_scope")
        return SharedSignalsV1Config(
            base_url=self.base_url,
            expected_catalog_version=self.expected_catalog_version,
            dataset_ids=dataset_ids,
            access_policy_id=self.access_policy_id,
            catalog_version_policy="evidence_only",
            timeout_seconds=float(self.timeout_seconds),
            max_limit=500,
            cache_ttl_seconds=0,
        )


def _target_rows(catalog: CatalogEnvelope) -> dict[str, Mapping[str, Any]]:
    rows: dict[str, list[Mapping[str, Any]]] = {
        dataset_id: [] for dataset_id in DATASET_IDS
    }
    for row in catalog.data:
        if not isinstance(row, Mapping):
            continue
        dataset_id = row.get("dataset_id")
        if dataset_id in rows:
            rows[dataset_id].append(row)
    if any(not matches for matches in rows.values()):
        raise ShadowParityError("shadow_parity_dataset_missing")
    if any(len(matches) != 1 for matches in rows.values()):
        raise ShadowParityError("shadow_parity_dataset_duplicate")
    return {dataset_id: matches[0] for dataset_id, matches in rows.items()}


def _source_accepted(
    *,
    profile: object,
    snapshot: object,
    audit_rejections: int,
) -> dict[str, object]:
    return {
        "status": "accepted",
        "reason_code": None,
        "expected_catalog_version": profile.expected_catalog_version,
        "observed_catalog_version": snapshot.observed_catalog_version,
        "catalog_version_drift": (
            profile.expected_catalog_version != snapshot.observed_catalog_version
        ),
        "dataset_contract_fingerprint": profile.dataset_contract_fingerprint,
        "consumer_profile_sha256": profile.consumer_profile_sha256,
        "identity_fields": list(profile.identity_fields),
        "row_count": snapshot.row_count,
        "page_count": snapshot.page_count,
        "same_observation": snapshot.same_observation,
        "receipt_lineage_verified": True,
        "audit_rejections": audit_rejections,
    }


def _source_rejected(
    *,
    profile: object,
    reason_code: str,
    audit_rejections: int,
) -> dict[str, object]:
    return {
        "status": "rejected",
        "reason_code": reason_code,
        "expected_catalog_version": profile.expected_catalog_version,
        "observed_catalog_version": None,
        "catalog_version_drift": None,
        "dataset_contract_fingerprint": profile.dataset_contract_fingerprint,
        "consumer_profile_sha256": profile.consumer_profile_sha256,
        "identity_fields": list(profile.identity_fields),
        "row_count": 0,
        "page_count": 0,
        "same_observation": False,
        "receipt_lineage_verified": False,
        "audit_rejections": audit_rejections,
    }


def _reason(exc: Exception, fallback: str) -> str:
    value = getattr(exc, "reason_code", None)
    return value if isinstance(value, str) and value else fallback


def _validate_client_scope(
    client: SharedSignalsV1Client,
    *,
    dataset_ids: frozenset[str],
    plan: ShadowParityPlan,
) -> None:
    config = getattr(client, "config", None)
    if getattr(config, "catalog_version_policy", None) != "evidence_only":
        raise ShadowParityError("shadow_parity_catalog_policy")
    if getattr(config, "dataset_ids", None) != dataset_ids:
        raise ShadowParityError("shadow_parity_client_dataset_scope")
    if (
        getattr(config, "expected_catalog_version", None)
        != plan.expected_catalog_version
    ):
        raise ShadowParityError("shadow_parity_client_catalog_scope")


def run_shadow_parity(
    event_client: SharedSignalsV1Client,
    *,
    moneyflow_client: SharedSignalsV1Client,
    plan: ShadowParityPlan,
) -> dict[str, object]:
    """Execute four independent, zero-notional adapter parity checks."""

    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise ShadowParityError("real_trading_must_remain_disabled")
    _validate_client_scope(
        event_client,
        dataset_ids=frozenset(EVENT_DATASET_IDS),
        plan=plan,
    )
    _validate_client_scope(
        moneyflow_client,
        dataset_ids=frozenset(MONEYFLOW_DATASET_IDS),
        plan=plan,
    )
    catalog = event_client.get_catalog()
    if not isinstance(catalog, CatalogEnvelope):
        raise ShadowParityError("shadow_parity_catalog_invalid")
    rows = _target_rows(catalog)
    try:
        event_profiles = {
            dataset_id: EvidenceDatasetProfile.from_catalog_row(
                catalog,
                rows[dataset_id],
                expected_catalog_version=plan.expected_catalog_version,
            )
            for dataset_id in EVENT_DATASET_IDS
        }
        moneyflow_profiles = {
            dataset_id: MoneyflowDatasetProfile.from_catalog_row(
                catalog,
                rows[dataset_id],
                expected_catalog_version=plan.expected_catalog_version,
            )
            for dataset_id in MONEYFLOW_DATASET_IDS
        }
    except (AshareEvidenceContractError, AshareMoneyflowEvidenceError) as exc:
        raise ShadowParityError(_reason(exc, "shadow_parity_profile_invalid")) from exc

    sources: dict[str, dict[str, object]] = {}
    event_port = TradingDatasAshareEvidencePort(event_client)
    for dataset_id in EVENT_DATASET_IDS:
        profile = event_profiles[dataset_id]
        event_audit = AshareEvidenceAuditLedger()
        try:
            event_snapshot = event_port.load_event_snapshot(
                profile=profile,
                filters=plan.filters_by_dataset[dataset_id],
                decision_time=plan.decision_time,
                audit_ledger=event_audit,
                allowed_symbols=(
                    None
                    if dataset_id in MACRO_EVENT_DATASET_IDS
                    else plan.allowed_symbols
                ),
            )
            sources[dataset_id] = _source_accepted(
                profile=profile,
                snapshot=event_snapshot,
                audit_rejections=len(event_audit.records()),
            )
        except AshareEvidenceContractError as exc:
            sources[dataset_id] = _source_rejected(
                profile=profile,
                reason_code=_reason(exc, "ashare_evidence_query_failed"),
                audit_rejections=len(event_audit.records()),
            )

    moneyflow_port = TradingDatasAshareMoneyflowPort(moneyflow_client)
    for dataset_id in MONEYFLOW_DATASET_IDS:
        profile = moneyflow_profiles[dataset_id]
        audit = AshareMoneyflowAuditLedger()
        try:
            snapshot = moneyflow_port.load_shadow_snapshot(
                profile=profile,
                filters=plan.filters_by_dataset[dataset_id],
                decision_time=plan.decision_time,
                audit_ledger=audit,
                allowed_symbols=plan.allowed_symbols,
            )
            sources[dataset_id] = _source_accepted(
                profile=profile,
                snapshot=snapshot,
                audit_rejections=len(audit.records()),
            )
        except AshareMoneyflowEvidenceError as exc:
            sources[dataset_id] = _source_rejected(
                profile=profile,
                reason_code=_reason(exc, "ashare_moneyflow_query_failed"),
                audit_rejections=len(audit.records()),
            )

    required_accepted = all(
        sources[dataset_id]["status"] == "accepted"
        for dataset_id in REQUIRED_DATASET_IDS
    )
    optional_accepted = all(
        sources[dataset_id]["status"] == "accepted"
        for dataset_id in MACRO_EVENT_DATASET_IDS
    )
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": ("pass" if optional_accepted else "partial")
        if required_accepted
        else "blocked",
        "decision_time": plan.decision_time.isoformat(),
        "dataset_ids": list(DATASET_IDS),
        "required_dataset_ids": sorted(REQUIRED_DATASET_IDS),
        "optional_dataset_ids": sorted(MACRO_EVENT_DATASET_IDS),
        "catalog_route": "GET /v1/catalog",
        "query_route": "POST /v1/query",
        "initial_observed_catalog_version": catalog.catalog_version,
        "initial_catalog_version_drift": (
            plan.expected_catalog_version != catalog.catalog_version
        ),
        "sources": sources,
        "zero_notional_cny": 0,
        "candidate_authority": False,
        "candidate_eligible": False,
        "training_authority": False,
        "training_eligible": False,
        "capital_authority": False,
        "order_authority": False,
        "execution_authority": False,
        "execution_eligible": False,
        "risk_authority": False,
        "position_authority": False,
        "llm_authority": False,
        "llm_network_used": False,
        "promotion_eligible": False,
        "promotion_authorized": False,
        "real_trading_enabled": False,
        "timer_changed": False,
        "current_release_changed": False,
        "scale500_triggered": False,
    }
    receipt["receipt_sha256"] = _sha256(receipt)
    return receipt


def load_shadow_parity_config(path: Path | str) -> ShadowParityRuntimeConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShadowParityError("shadow_parity_manifest_invalid") from exc
    if not isinstance(raw, Mapping):
        raise ShadowParityError("shadow_parity_manifest_invalid")
    try:
        decision_time = datetime.fromisoformat(
            _text(raw.get("decision_time"), "shadow_parity_decision_time_invalid")
        )
    except ValueError as exc:
        raise ShadowParityError("shadow_parity_decision_time_invalid") from exc
    plan = ShadowParityPlan(
        expected_catalog_version=raw.get("expected_catalog_version"),
        decision_time=decision_time,
        allowed_symbols=tuple(raw.get("allowed_symbols", ())),
        filters_by_dataset=raw.get("filters_by_dataset"),
    )
    return ShadowParityRuntimeConfig(
        base_url=raw.get("base_url"),
        expected_catalog_version=raw.get("expected_catalog_version"),
        access_policy_id=raw.get("access_policy_id"),
        transport_id=raw.get("transport_id"),
        timeout_seconds=raw.get("timeout_seconds"),
        plan=plan,
    )


def run_runtime_shadow_parity(
    config: ShadowParityRuntimeConfig,
    *,
    token_file: Path | str,
    transport_factory: TransportFactory = build_runtime_transport,
) -> dict[str, object]:
    """Explicit runtime entry; no token is touched by manifest preflight."""

    transport = transport_factory(
        config.transport_id,
        token_file=token_file,
        base_url=config.base_url,
    )
    return run_shadow_parity(
        SharedSignalsV1Client(
            config.client_config(frozenset(EVENT_DATASET_IDS)),
            transport=transport,
        ),
        moneyflow_client=SharedSignalsV1Client(
            config.client_config(frozenset(MONEYFLOW_DATASET_IDS)),
            transport=transport,
        ),
        plan=config.plan,
    )


def _write_receipt(path: Path, receipt: Mapping[str, object]) -> None:
    if path.exists():
        raise ShadowParityError("shadow_parity_receipt_already_exists")
    payload = (
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise ShadowParityError("shadow_parity_receipt_persist_failed") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only A-share event/moneyflow shadow parity sidecar"
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_shadow_parity_config(args.manifest)
        if args.preflight:
            if args.token_file is not None or args.output is not None:
                raise ShadowParityError("shadow_parity_preflight_arguments_invalid")
            receipt: Mapping[str, object] = {
                "status": "preflight_pass",
                "schema": RECEIPT_SCHEMA,
                "dataset_ids": list(DATASET_IDS),
                "base_url": config.base_url,
                "real_trading_enabled": False,
                "execution_authority": False,
                "token_read": False,
            }
        else:
            if (
                args.token_file is None
                or args.output is None
                or not args.token_file.is_absolute()
            ):
                raise ShadowParityError("shadow_parity_runtime_arguments_invalid")
            receipt = run_runtime_shadow_parity(config, token_file=args.token_file)
            _write_receipt(args.output, receipt)
    except (ShadowParityError, RuntimeGateConfigurationError, OSError, ValueError):
        print("evidence shadow parity failed closed", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    else:
        print(receipt["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DATASET_IDS",
    "FORMAL_BASE_URL",
    "RECEIPT_SCHEMA",
    "ShadowParityError",
    "ShadowParityPlan",
    "ShadowParityRuntimeConfig",
    "load_shadow_parity_config",
    "main",
    "run_runtime_shadow_parity",
    "run_shadow_parity",
]
