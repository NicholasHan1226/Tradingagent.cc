from __future__ import annotations

from dataclasses import replace

import pytest

from Ashare.event_evidence import (
    PRIMARY_DATASET_IDS,
    AshareEvidenceAuditLedger,
    TradingDatasAshareEvidencePort,
)
from Ashare.trading_copilot_event_consumer_profile import (
    TradingCopilotEventConsumerProfileError,
    load_event_consumer_profiles,
    validate_event_consumer_profile_contract,
    validate_event_consumer_runtime_evidence,
)
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)

_CATALOG_VERSION = "fixture-copilot-event-profile-v1"
_DECLARED_DATASET_IDS = (
    *PRIMARY_DATASET_IDS,
    "cn.dataset.major_news",
)


def _catalog_row(dataset_id: str) -> dict[str, object]:
    fields = [
        "event_id",
        "ts_code",
        "event_time",
        "entity",
        "title",
        "content",
        "url",
        "source",
    ]
    return {
        "dataset_id": dataset_id,
        "schema_major": 1,
        "default_fields": fields,
        "default_order": ["event_id:asc"],
        "filter_operators": {
            field: ["eq", "in", "gte", "lte", "between"] for field in fields
        },
        "limits": {"max_page_size": 100},
        "identity_fields": ["event_id"],
        "availability": {"activation_states": ["active"]},
    }


class _CatalogOnlyTransport:
    """Local fixture; it deliberately exposes no live TradingDatas capability."""

    def __call__(self, **kwargs: object) -> HTTPResponse:
        assert kwargs["method"] == "GET"
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": _CATALOG_VERSION,
                "request_id": "fixture-catalog",
                "data": [_catalog_row(dataset_id) for dataset_id in _DECLARED_DATASET_IDS],
            },
        )


def _frozen_profiles():
    client = SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="https://fixture.invalid",
            expected_catalog_version=_CATALOG_VERSION,
            dataset_ids=frozenset(_DECLARED_DATASET_IDS),
            access_policy_id="fixture-only",
            catalog_version_policy="evidence_only",
            cache_ttl_seconds=0,
        ),
        transport=_CatalogOnlyTransport(),
    )
    return TradingDatasAshareEvidencePort(client).freeze_profiles(
        audit_ledger=AshareEvidenceAuditLedger()
    )


def test_declared_profiles_require_compatible_frozen_td_catalog_capabilities() -> None:
    frozen = _frozen_profiles()

    for consumer_profile in load_event_consumer_profiles():
        validate_event_consumer_profile_contract(
            consumer_profile=consumer_profile,
            evidence_profile=frozen.by_dataset[consumer_profile.dataset_id],
        )


def test_rejects_profile_cadence_or_symbol_binding_not_supported_by_catalog_fixture() -> None:
    profiles = {profile.dataset_id: profile for profile in load_event_consumer_profiles()}
    frozen = _frozen_profiles()

    with pytest.raises(
        TradingCopilotEventConsumerProfileError,
        match="copilot_event_consumer_catalog_cadence_incompatible",
    ):
        validate_event_consumer_profile_contract(
            consumer_profile=profiles["cn.dataset.major_news"],
            evidence_profile=replace(
                frozen.by_dataset["cn.dataset.major_news"],
                omit_as_of=False,
            ),
        )

    with pytest.raises(
        TradingCopilotEventConsumerProfileError,
        match="copilot_event_consumer_catalog_symbol_binding_incompatible",
    ):
        validate_event_consumer_profile_contract(
            consumer_profile=profiles["cn.dataset.anns_d"],
            evidence_profile=replace(
                frozen.by_dataset["cn.dataset.anns_d"],
                symbol_field=None,
            ),
        )


def test_rejects_missing_or_unbound_runtime_evidence() -> None:
    consumer_profile = next(
        profile
        for profile in load_event_consumer_profiles()
        if profile.dataset_id == "cn.dataset.anns_d"
    )
    evidence_profile = _frozen_profiles().by_dataset[consumer_profile.dataset_id]

    with pytest.raises(
        TradingCopilotEventConsumerProfileError,
        match="copilot_event_consumer_runtime_evidence_missing",
    ):
        validate_event_consumer_runtime_evidence(
            consumer_profile=consumer_profile,
            evidence_profile=evidence_profile,
            snapshot=None,
        )

    with pytest.raises(
        TradingCopilotEventConsumerProfileError,
        match="copilot_event_consumer_runtime_evidence_unbound",
    ):
        validate_event_consumer_runtime_evidence(
            consumer_profile=consumer_profile,
            evidence_profile=evidence_profile,
            snapshot=object(),
        )
