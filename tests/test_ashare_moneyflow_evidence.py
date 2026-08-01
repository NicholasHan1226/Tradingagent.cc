from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any

import pytest

from Ashare.moneyflow_evidence import (
    FIXED_CATALOG_ROUTE,
    FIXED_QUERY_ROUTE,
    MONEYFLOW_DATASET_IDS,
    AshareMoneyflowAuditLedger,
    AshareMoneyflowAuditRecord,
    AshareMoneyflowEvidenceError,
    TradingDatasAshareMoneyflowPort,
)
from shared.data.sharedsignals_v1 import (
    CatalogEnvelope,
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)


CATALOG = "fixture-ashare-moneyflow-catalog-v1"
DECISION_TIME = datetime.fromisoformat("2026-08-03T10:30:00+08:00")
FIELDS = ["ts_code", "trade_date", "net_mf_amount"]


def _catalog_row(
    dataset_id: str,
    *,
    active: bool = True,
    fields: list[str] | None = None,
    default_order: list[str] | None = None,
    max_page_size: int = 2,
    identity_fields: list[str] | None = None,
) -> dict[str, Any]:
    names = fields or FIELDS
    order = (
        default_order
        if default_order is not None
        else ["ts_code:asc", "trade_date:asc"]
    )
    return {
        "dataset_id": dataset_id,
        "schema_major": 1,
        "default_fields": list(names),
        "default_order": order,
        "filter_operators": {
            name: ["eq", "in", "gte", "lte", "between"] for name in names
        },
        "limits": {"max_page_size": max_page_size},
        "identity_fields": identity_fields or ["trade_date", "ts_code"],
        "availability": {"activation_states": ["active" if active else "paused"]},
    }


def _metadata(**overrides: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "state": "ready",
        "degraded": False,
        "freshness": {"state": "fresh", "stale": False},
        "quality": {"state": "valid"},
        "lineage": {
            "complete": True,
            "provider_neutral": True,
            "providers": ["fixture-provider"],
            "transport_service": "fixture-v1",
        },
        "receipt_id": "receipt-moneyflow-1",
        "data_through": "2026-08-03T10:20:00+08:00",
        "observed_at": "2026-08-03T10:25:00+08:00",
        "reasons": [],
    }
    metadata.update(overrides)
    return metadata


def _row(
    symbol: str = "600000.SH",
    *,
    trade_date: str = "20260803",
    net_mf_amount: float = 1_250_000.0,
) -> dict[str, Any]:
    return {
        "ts_code": symbol,
        "trade_date": trade_date,
        "net_mf_amount": net_mf_amount,
    }


class _Transport:
    def __init__(
        self,
        *,
        catalog_rows: list[dict[str, Any]] | None = None,
        rows_by_dataset: dict[str, list[dict[str, Any]]] | None = None,
        metadata_by_dataset: dict[str, dict[str, Any]] | None = None,
        replay_drift: bool = False,
        catalog_responses: list[tuple[str, list[dict[str, Any]]]] | None = None,
        query_catalog_versions: list[str] | None = None,
    ) -> None:
        self.catalog_rows = catalog_rows or [
            _catalog_row(dataset_id) for dataset_id in MONEYFLOW_DATASET_IDS
        ]
        self.rows_by_dataset = rows_by_dataset or {
            dataset_id: [_row()] for dataset_id in MONEYFLOW_DATASET_IDS
        }
        self.metadata_by_dataset = metadata_by_dataset or {
            dataset_id: _metadata() for dataset_id in MONEYFLOW_DATASET_IDS
        }
        self.replay_drift = replay_drift
        self.catalog_responses = catalog_responses
        self.query_catalog_versions = query_catalog_versions or []
        self.calls: list[dict[str, Any]] = []
        self._traversals: dict[str, int] = {}
        self._catalog_call_count = 0
        self._query_call_count = 0
        self._current_catalog_version = CATALOG

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        self.calls.append(copy.deepcopy(kwargs))
        if kwargs["method"] == "GET":
            if self.catalog_responses:
                index = min(
                    self._catalog_call_count,
                    len(self.catalog_responses) - 1,
                )
                catalog_version, catalog_rows = self.catalog_responses[index]
            else:
                catalog_version, catalog_rows = CATALOG, self.catalog_rows
            self._catalog_call_count += 1
            self._current_catalog_version = catalog_version
            return HTTPResponse(
                200,
                {
                    "api_version": "v1",
                    "catalog_version": catalog_version,
                    "request_id": f"catalog-{len(self.calls)}",
                    "data": copy.deepcopy(catalog_rows),
                },
            )
        body = kwargs["json_body"]
        assert body is not None
        dataset_id = body["dataset_id"]
        rows = copy.deepcopy(self.rows_by_dataset.get(dataset_id, []))
        cursor = body.get("cursor")
        if cursor is None:
            traversal = self._traversals.get(dataset_id, 0)
            self._traversals[dataset_id] = traversal + 1
            index = 0
        else:
            traversal_text, index_text = cursor.split(":", 1)
            traversal = int(traversal_text)
            index = int(index_text)
        limit = int(body["limit"])
        page = rows[index : index + limit]
        if self.replay_drift and traversal == 1 and page:
            page[0]["net_mf_amount"] = -page[0]["net_mf_amount"]
        next_index = index + len(page)
        next_cursor = f"{traversal}:{next_index}" if next_index < len(rows) else None
        query_catalog_version = (
            self.query_catalog_versions[
                min(self._query_call_count, len(self.query_catalog_versions) - 1)
            ]
            if self.query_catalog_versions
            else self._current_catalog_version
        )
        self._query_call_count += 1
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": query_catalog_version,
                "request_id": f"query-{len(self.calls)}",
                "dataset_id": dataset_id,
                "data": page,
                "next_cursor": next_cursor,
                "metadata": copy.deepcopy(self.metadata_by_dataset[dataset_id]),
            },
        )


class _ManualCatalogClient(SharedSignalsV1Client):
    """Fixture-only client for adapter checks beneath the shared parser."""

    def __init__(
        self,
        transport: _Transport,
        catalogs: list[CatalogEnvelope],
    ) -> None:
        super().__init__(_client(transport).config, transport=transport)
        self._catalogs = list(catalogs)

    def get_catalog(self) -> CatalogEnvelope:
        if not self._catalogs:
            raise AssertionError("fixture catalog exhausted")
        catalog = self._catalogs.pop(0)
        self._observed_catalog_version = catalog.catalog_version
        return catalog


def _catalog_envelope(
    catalog_version: str,
    rows: list[dict[str, Any]],
) -> CatalogEnvelope:
    return CatalogEnvelope(
        api_version="v1",
        catalog_version=catalog_version,
        request_id=f"fixture-{catalog_version}",
        data=tuple(rows),
    )


def _client(
    transport: _Transport,
    *,
    dataset_ids: frozenset[str] = frozenset(MONEYFLOW_DATASET_IDS),
    catalog_version_policy: str = "evidence_only",
) -> SharedSignalsV1Client:
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="https://moneyflow-evidence.fixture.invalid",
            expected_catalog_version=CATALOG,
            dataset_ids=dataset_ids,
            access_policy_id="ashare-moneyflow-fixture",
            catalog_version_policy=catalog_version_policy,
            max_limit=100,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )


def _port_and_profile(
    transport: _Transport | None = None,
    *,
    dataset_id: str = "cn.dataset.moneyflow",
) -> tuple[
    TradingDatasAshareMoneyflowPort, Any, AshareMoneyflowAuditLedger, _Transport
]:
    actual = transport or _Transport()
    port = TradingDatasAshareMoneyflowPort(_client(actual))
    audit = AshareMoneyflowAuditLedger()
    profiles = port.freeze_profiles(audit_ledger=audit)
    return port, profiles.by_dataset[dataset_id], audit, actual


def test_single_source_profile_and_snapshot_are_catalog_bound_shadow_only() -> None:
    transport = _Transport(catalog_rows=[_catalog_row("cn.dataset.moneyflow")])
    port = TradingDatasAshareMoneyflowPort(
        _client(transport, dataset_ids=frozenset({"cn.dataset.moneyflow"}))
    )
    audit = AshareMoneyflowAuditLedger()

    profiles = port.freeze_profiles(audit_ledger=audit)
    snapshot = port.load_shadow_snapshot(
        profile=profiles.by_dataset["cn.dataset.moneyflow"],
        filters={"ts_code": {"eq": "600000.SH"}},
        decision_time=DECISION_TIME,
        audit_ledger=audit,
        allowed_symbols=("600000.SH",),
    )

    assert tuple(profiles.by_dataset) == ("cn.dataset.moneyflow",)
    assert profiles.by_dataset["cn.dataset.moneyflow"].identity_fields == (
        "trade_date",
        "ts_code",
    )
    assert snapshot.query_route == FIXED_QUERY_ROUTE == "POST /v1/query"
    assert snapshot.catalog_route == FIXED_CATALOG_ROUTE == "GET /v1/catalog"
    assert snapshot.same_observation is True
    assert len(snapshot.features) == 1
    feature = snapshot.features[0]
    assert feature.symbol == "600000.SH"
    assert feature.available_at.isoformat() == "2026-08-03T10:25:00+08:00"
    assert feature.pit_feature_eligible is True
    assert feature.counterfactual_only is True
    assert feature.candidate_eligible is False
    assert feature.execution_eligible is False
    assert feature.training_eligible is False
    assert feature.promotion_eligible is False
    assert feature.execution_authority is False
    assert feature.real_trading_enabled is False
    assert [call["method"] for call in transport.calls] == [
        "GET",
        "GET",
        "POST",
        "POST",
    ]


def test_unrelated_global_catalog_drift_is_audited_without_blocking_moneyflow() -> None:
    initial_rows = [_catalog_row(dataset_id) for dataset_id in MONEYFLOW_DATASET_IDS]
    current_rows = copy.deepcopy(initial_rows)
    current_rows.append(
        _catalog_row(
            "cn.dataset.unrelated",
            fields=["id"],
            default_order=["id:asc"],
            identity_fields=["id"],
        )
    )
    transport = _Transport(
        catalog_responses=[
            (CATALOG, initial_rows),
            ("fixture-global-catalog-v2", current_rows),
        ]
    )
    port, profile, audit, _ = _port_and_profile(transport)

    snapshot = port.load_shadow_snapshot(
        profile=profile,
        filters={},
        decision_time=DECISION_TIME,
        audit_ledger=audit,
    )

    assert snapshot.expected_catalog_version == CATALOG
    assert snapshot.observed_catalog_version == "fixture-global-catalog-v2"
    assert snapshot.catalog_version_drift is True
    assert snapshot.dataset_contract_fingerprint == profile.dataset_contract_fingerprint
    assert snapshot.consumer_profile_sha256 == profile.consumer_profile_sha256
    assert audit.records() == ()


def test_unrelated_catalog_duplicate_does_not_block_moneyflow_profile_freeze() -> None:
    unrelated = _catalog_row(
        "cn.dataset.unrelated",
        fields=["id"],
        default_order=["id:asc"],
        identity_fields=["id"],
    )
    transport = _Transport()
    client = _ManualCatalogClient(
        transport,
        [
            _catalog_envelope(
                CATALOG,
                [
                    *[_catalog_row(dataset_id) for dataset_id in MONEYFLOW_DATASET_IDS],
                    unrelated,
                    unrelated,
                ],
            )
        ],
    )

    profiles = TradingDatasAshareMoneyflowPort(client).freeze_profiles(
        audit_ledger=AshareMoneyflowAuditLedger()
    )

    assert set(profiles.by_dataset) == set(MONEYFLOW_DATASET_IDS)


def test_catalog_to_query_version_drift_fails_closed_with_audit_binding() -> None:
    rows = [_catalog_row(dataset_id) for dataset_id in MONEYFLOW_DATASET_IDS]
    transport = _Transport(
        catalog_responses=[(CATALOG, rows), ("fixture-global-catalog-v2", rows)],
        query_catalog_versions=["fixture-query-catalog-v3"],
    )
    port, profile, audit, _ = _port_and_profile(transport)

    with pytest.raises(AshareMoneyflowEvidenceError, match="query_failed"):
        port.load_shadow_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    record = audit.records()[-1]
    assert record.expected_catalog_version == CATALOG
    assert record.observed_catalog_version == "fixture-global-catalog-v2"
    assert record.catalog_version_drift is True
    assert record.dataset_contract_fingerprint == profile.dataset_contract_fingerprint
    assert record.consumer_profile_sha256 == profile.consumer_profile_sha256


@pytest.mark.parametrize(
    "field_name",
    (
        "dataset_id",
        "schema_major",
        "default_fields",
        "filter_operators",
        "default_order",
        "limits",
        "identity_fields",
    ),
)
def test_target_catalog_seven_field_drift_fails_before_query(field_name: str) -> None:
    initial_rows = [_catalog_row(dataset_id) for dataset_id in MONEYFLOW_DATASET_IDS]
    current_rows = copy.deepcopy(initial_rows)
    target = next(
        row for row in current_rows if row["dataset_id"] == "cn.dataset.moneyflow"
    )
    replacements: dict[str, Any] = {
        "dataset_id": "cn.dataset.moneyflow.drifted",
        "schema_major": 2,
        "default_fields": [*target["default_fields"], "contract_drift"],
        "filter_operators": {
            **target["filter_operators"],
            "ts_code": ["eq"],
        },
        "default_order": ["ts_code:desc", "trade_date:asc"],
        "limits": {"max_page_size": 1},
        "identity_fields": ["ts_code", "trade_date"],
    }
    target[field_name] = replacements[field_name]
    transport = _Transport(
        catalog_responses=[
            (CATALOG, initial_rows),
            ("fixture-global-catalog-v2", current_rows),
        ]
    )
    port, profile, audit, _ = _port_and_profile(transport)

    with pytest.raises(AshareMoneyflowEvidenceError):
        port.load_shadow_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    assert not [call for call in transport.calls if call["method"] == "POST"]
    assert audit.records()[-1].dataset_contract_fingerprint == (
        profile.dataset_contract_fingerprint
    )


def test_catalog_identity_field_order_mismatch_fails_before_query() -> None:
    rows = [_catalog_row("cn.dataset.moneyflow")]
    rows[0]["identity_fields"] = ["ts_code", "trade_date"]
    transport = _Transport(catalog_rows=rows)
    port = TradingDatasAshareMoneyflowPort(
        _client(transport, dataset_ids=frozenset({"cn.dataset.moneyflow"}))
    )

    with pytest.raises(AshareMoneyflowEvidenceError, match="catalog_identity_mismatch"):
        port.freeze_profiles(audit_ledger=AshareMoneyflowAuditLedger())

    assert not [call for call in transport.calls if call["method"] == "POST"]


@pytest.mark.parametrize("mode", ("missing", "duplicate"))
def test_target_catalog_missing_or_duplicate_fails_before_query(mode: str) -> None:
    initial_rows = [_catalog_row(dataset_id) for dataset_id in MONEYFLOW_DATASET_IDS]
    current_rows = copy.deepcopy(initial_rows)
    if mode == "missing":
        current_rows = [
            row for row in current_rows if row["dataset_id"] != "cn.dataset.moneyflow"
        ]
    else:
        current_rows.append(
            copy.deepcopy(
                next(
                    row
                    for row in current_rows
                    if row["dataset_id"] == "cn.dataset.moneyflow"
                )
            )
        )
    transport = _Transport()
    client = _ManualCatalogClient(
        transport,
        [
            _catalog_envelope(CATALOG, initial_rows),
            _catalog_envelope("fixture-global-catalog-v2", current_rows),
        ],
    )
    port = TradingDatasAshareMoneyflowPort(client)
    audit = AshareMoneyflowAuditLedger()
    profile = port.freeze_profiles(audit_ledger=audit).by_dataset[
        "cn.dataset.moneyflow"
    ]

    with pytest.raises(
        AshareMoneyflowEvidenceError,
        match="dataset_catalog_row_missing",
    ):
        port.load_shadow_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    assert not [call for call in transport.calls if call["method"] == "POST"]


def test_audit_catalog_drift_is_derived_and_rejects_forged_value() -> None:
    with pytest.raises(
        AshareMoneyflowEvidenceError,
        match="audit_catalog_drift_mismatch",
    ):
        AshareMoneyflowAuditRecord(
            reason_code="fixture_rejection",
            dataset_id="catalog",
            expected_catalog_version="expected-v1",
            observed_catalog_version="observed-v2",
            catalog_version_drift=False,
            dataset_contract_fingerprint=None,
            consumer_profile_sha256=None,
            decision_time=DECISION_TIME,
            rejected_payload_sha256="0" * 64,
        )


def test_runtime_requires_evidence_only_catalog_version_policy() -> None:
    transport = _Transport(catalog_rows=[_catalog_row("cn.dataset.moneyflow")])
    port = TradingDatasAshareMoneyflowPort(
        _client(
            transport,
            dataset_ids=frozenset({"cn.dataset.moneyflow"}),
            catalog_version_policy="strict",
        )
    )

    with pytest.raises(
        AshareMoneyflowEvidenceError,
        match="catalog_version_policy_invalid",
    ):
        port.freeze_profiles(audit_ledger=AshareMoneyflowAuditLedger())

    assert not transport.calls


def test_two_variants_are_independent_and_never_implicitly_substituted() -> None:
    transport = _Transport(
        rows_by_dataset={
            "cn.dataset.moneyflow": [_row(net_mf_amount=100.0)],
            "cn.dataset.moneyflow_ths": [_row(net_mf_amount=-200.0)],
        }
    )
    port = TradingDatasAshareMoneyflowPort(_client(transport))
    audit = AshareMoneyflowAuditLedger()
    profiles = port.freeze_profiles(audit_ledger=audit)

    first = port.load_shadow_snapshot(
        profile=profiles.by_dataset["cn.dataset.moneyflow"],
        filters={},
        decision_time=DECISION_TIME,
        audit_ledger=audit,
    )
    second = port.load_shadow_snapshot(
        profile=profiles.by_dataset["cn.dataset.moneyflow_ths"],
        filters={},
        decision_time=DECISION_TIME,
        audit_ledger=audit,
    )

    assert first.features[0].dataset_id == "cn.dataset.moneyflow"
    assert second.features[0].dataset_id == "cn.dataset.moneyflow_ths"
    assert first.features[0].net_flow_amount_cny == 100.0
    assert second.features[0].net_flow_amount_cny == -200.0


def test_catalog_drift_is_audit_only_and_never_queries_a_fallback_source() -> None:
    transport = _Transport()
    port, profile, audit, actual = _port_and_profile(transport)
    actual.catalog_rows[0]["limits"]["max_page_size"] = 3

    with pytest.raises(AshareMoneyflowEvidenceError, match="dataset_contract_drift"):
        port.load_shadow_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    assert audit.records()[-1].reason_code == "ashare_moneyflow_dataset_contract_drift"
    assert [call for call in actual.calls if call["method"] == "POST"] == []


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        (_metadata(state="failed"), "metadata_not_ready"),
        (_metadata(degraded=True), "metadata_not_ready"),
        (_metadata(freshness={"state": "stale", "stale": True}), "metadata_not_fresh"),
        (_metadata(quality={"state": "invalid"}), "metadata_quality_invalid"),
        (_metadata(receipt_id=None), "query_failed"),
        (_metadata(lineage={"complete": False}), "metadata_lineage_incomplete"),
        (
            _metadata(
                lineage={
                    "complete": True,
                    "provider_neutral": True,
                    "providers": [{"not": "a-provider"}],
                    "transport_service": "fixture-v1",
                }
            ),
            "metadata_lineage_incomplete",
        ),
    ],
)
def test_bad_metadata_isolated_to_the_selected_feature(
    metadata: dict[str, Any], reason: str
) -> None:
    transport = _Transport(
        metadata_by_dataset={
            dataset_id: metadata for dataset_id in MONEYFLOW_DATASET_IDS
        }
    )
    port, profile, audit, actual = _port_and_profile(transport)

    with pytest.raises(AshareMoneyflowEvidenceError, match=reason):
        port.load_shadow_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    assert audit.records()[-1].dataset_id == "cn.dataset.moneyflow"
    assert audit.records()[-1].execution_eligible is False
    post_datasets = [
        call["json_body"]["dataset_id"]
        for call in actual.calls
        if call["method"] == "POST"
    ]
    assert post_datasets
    assert set(post_datasets) == {"cn.dataset.moneyflow"}


def test_empty_or_pit_invalid_source_cannot_create_a_feature_or_fallback() -> None:
    empty = _Transport(rows_by_dataset={"cn.dataset.moneyflow": []})
    port, profile, audit, actual = _port_and_profile(empty)

    with pytest.raises(AshareMoneyflowEvidenceError, match="query_returned_no_rows"):
        port.load_shadow_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    assert all(
        call["json_body"]["dataset_id"] == "cn.dataset.moneyflow"
        for call in actual.calls
        if call["method"] == "POST"
    )

    future = _Transport(
        metadata_by_dataset={
            dataset_id: _metadata(observed_at="2026-08-03T10:35:00+08:00")
            for dataset_id in MONEYFLOW_DATASET_IDS
        }
    )
    port, profile, audit, _ = _port_and_profile(future)
    with pytest.raises(AshareMoneyflowEvidenceError, match="available_after_decision"):
        port.load_shadow_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )
    assert audit.records()[-1].pit_feature_eligible is False


def test_replay_drift_and_off_scope_symbol_fail_closed_before_scoring() -> None:
    replay = _Transport(replay_drift=True)
    port, profile, audit, _ = _port_and_profile(replay)
    with pytest.raises(AshareMoneyflowEvidenceError, match="same_observation_mismatch"):
        port.load_shadow_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    off_scope = _Transport(
        rows_by_dataset={
            dataset_id: [_row("300001.SZ")] for dataset_id in MONEYFLOW_DATASET_IDS
        }
    )
    port, profile, audit, _ = _port_and_profile(off_scope)
    with pytest.raises(
        AshareMoneyflowEvidenceError, match="symbol_outside_mainboard_scope"
    ):
        port.load_shadow_snapshot(
            profile=profile,
            filters={},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )


def test_accepted_feature_can_only_be_explicitly_projected_to_flow_shadow() -> None:
    port, profile, audit, _ = _port_and_profile()
    feature = port.load_shadow_snapshot(
        profile=profile,
        filters={},
        decision_time=DECISION_TIME,
        audit_ledger=audit,
    ).features[0]

    auxiliary = feature.to_minute_auxiliary(
        expires_at=DECISION_TIME + timedelta(minutes=5),
        normalization_scale_cny=1_000_000.0,
    )

    assert auxiliary.evidence_type == "flow"
    assert auxiliary.execution_authority is False
    assert auxiliary.normalized_score > 0
    assert feature.to_minute_auxiliary.__module__ == "Ashare.moneyflow_evidence"


def test_profile_cannot_use_unknown_fields_or_noncanonical_symbol() -> None:
    transport = _Transport(
        catalog_rows=[
            _catalog_row(
                "cn.dataset.moneyflow",
                fields=["ts_code", "trade_date", "net_mf_amount"],
            )
        ]
    )
    port = TradingDatasAshareMoneyflowPort(
        _client(transport, dataset_ids=frozenset({"cn.dataset.moneyflow"}))
    )
    audit = AshareMoneyflowAuditLedger()
    profile = port.freeze_profiles(audit_ledger=audit).by_dataset[
        "cn.dataset.moneyflow"
    ]

    with pytest.raises(
        AshareMoneyflowEvidenceError, match="filter_not_catalog_allowed"
    ):
        port.load_shadow_snapshot(
            profile=profile,
            filters={"unregistered": {"eq": "x"}},
            decision_time=DECISION_TIME,
            audit_ledger=audit,
        )

    assert audit.records()[-1].candidate_eligible is False


def test_duplicate_catalog_source_variant_fails_closed_without_query() -> None:
    transport = _Transport(
        catalog_rows=[
            _catalog_row("cn.dataset.moneyflow"),
            _catalog_row("cn.dataset.moneyflow"),
        ]
    )
    port = TradingDatasAshareMoneyflowPort(
        _client(transport, dataset_ids=frozenset({"cn.dataset.moneyflow"}))
    )
    audit = AshareMoneyflowAuditLedger()

    with pytest.raises(AshareMoneyflowEvidenceError, match="catalog_failed"):
        port.freeze_profiles(audit_ledger=audit)

    assert audit.records()[-1].dataset_id == "catalog"
    assert audit.records()[-1].reason_code == "ashare_moneyflow_catalog_failed"
    assert not [call for call in transport.calls if call["method"] == "POST"]


@pytest.mark.parametrize(
    ("default_order", "reason"),
    [
        ([], "catalog_default_order_missing"),
        (["ts_code:ascending", "trade_date:asc"], "catalog_default_order_invalid"),
        (["unknown:asc", "trade_date:asc"], "catalog_default_order_invalid"),
        (
            ["ts_code:asc", "ts_code:desc", "trade_date:asc"],
            "catalog_default_order_invalid",
        ),
        (
            ["ts_code:asc", "net_mf_amount:asc"],
            "catalog_default_order_identity_incomplete",
        ),
    ],
)
def test_catalog_default_order_is_the_only_pagination_identity(
    default_order: list[str], reason: str
) -> None:
    transport = _Transport(
        catalog_rows=[_catalog_row("cn.dataset.moneyflow", default_order=default_order)]
    )
    port = TradingDatasAshareMoneyflowPort(
        _client(transport, dataset_ids=frozenset({"cn.dataset.moneyflow"}))
    )
    audit = AshareMoneyflowAuditLedger()

    with pytest.raises(AshareMoneyflowEvidenceError, match=reason):
        port.freeze_profiles(audit_ledger=audit)

    assert audit.records()[-1].dataset_id == "catalog"
    assert not [call for call in transport.calls if call["method"] == "POST"]
