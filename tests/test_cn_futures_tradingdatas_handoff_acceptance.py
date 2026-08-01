from __future__ import annotations

import copy

import pytest

from CNFutures.tradingdatas_handoff_acceptance import (
    PROFILE_ID,
    evaluate_handoff_fixture,
)


def _metadata(receipt_id: str) -> dict[str, object]:
    return {
        "state": "ready",
        "degraded": False,
        "freshness": {"state": "fresh", "stale": False},
        "quality": {"state": "valid", "valid": True},
        "lineage": {
            "complete": True,
            "provider_neutral": True,
            "provider": "fixture-provider",
            "transport_service": "fixture-transport",
        },
        "receipt_id": receipt_id,
        "data_through": "2026-07-31T09:35:00+08:00",
        "observed_at": "2026-07-31T09:40:00+08:00",
    }


def _query_identity(*, symbol: str, sort_field: str) -> dict[str, object]:
    return {
        "filters": {"symbol": symbol},
        "sort": [{"field": sort_field, "direction": "asc"}],
        "cursor": None,
    }


def _fixture() -> dict[str, object]:
    dataset_ids = {
        "contract_master": "fixture.td.m.contract-master",
        "bars_5min": "fixture.td.m.bars-5min",
        "calendar_session": "fixture.td.m.calendar-session",
    }
    schema_major = {role: 1 for role in dataset_ids}
    return {
        "fixture_only": True,
        "real_trading_enabled": False,
        "decision_time": "2026-07-31T09:40:05+08:00",
        "profile": {
            "profile_id": PROFILE_ID,
            "roles": {
                role: {"dataset_id": dataset_id, "schema_major": schema_major[role]}
                for role, dataset_id in dataset_ids.items()
            },
        },
        "catalog": {
            "route": "GET /v1/catalog",
            "api_version": "v1",
            "catalog_version": "fixture-catalog-v1",
            "datasets": [
                {
                    "dataset_id": dataset_id,
                    "schema_major": schema_major[role],
                    "state": "ready",
                    "degraded": False,
                }
                for role, dataset_id in dataset_ids.items()
            ],
        },
        "queries": {
            "contract_master": {
                "route": "POST /v1/query",
                "api_version": "v1",
                "dataset_id": dataset_ids["contract_master"],
                "schema_major": 1,
                "catalog_version": "fixture-catalog-v1",
                "next_cursor": None,
                "query_identity": _query_identity(
                    symbol="M2609.DCE", sort_field="symbol"
                ),
                "metadata": _metadata("receipt-contract"),
                "data": [
                    {
                        "symbol": "M2609.DCE",
                        "product": "M",
                        "exchange": "DCE",
                        "tradeability": {
                            "state": "tradeable",
                            "trade_date": "20260731",
                        },
                        "multiplier": 10,
                        "tick_size": 1,
                        "price_limit": 1000,
                    }
                ],
            },
            "calendar_session": {
                "route": "POST /v1/query",
                "api_version": "v1",
                "dataset_id": dataset_ids["calendar_session"],
                "schema_major": 1,
                "catalog_version": "fixture-catalog-v1",
                "next_cursor": None,
                "query_identity": _query_identity(
                    symbol="M2609.DCE", sort_field="trade_date"
                ),
                "metadata": _metadata("receipt-calendar"),
                "data": [
                    {
                        "symbol": "M2609.DCE",
                        "trade_date": "20260731",
                        "calendar_eligible": True,
                        "session_kind": "day",
                        "session_id": "fixture-dce-day-session",
                        "session_windows": [
                            {
                                "start": "2026-07-31T09:00:00+08:00",
                                "end": "2026-07-31T10:15:00+08:00",
                            },
                            {
                                "start": "2026-07-31T10:30:00+08:00",
                                "end": "2026-07-31T11:30:00+08:00",
                            },
                            {
                                "start": "2026-07-31T13:30:00+08:00",
                                "end": "2026-07-31T15:00:00+08:00",
                            },
                        ],
                    }
                ],
            },
            "bars_5min": {
                "route": "POST /v1/query",
                "api_version": "v1",
                "dataset_id": dataset_ids["bars_5min"],
                "schema_major": 1,
                "catalog_version": "fixture-catalog-v1",
                "next_cursor": None,
                "query_identity": _query_identity(
                    symbol="M2609.DCE", sort_field="bar_time"
                ),
                "metadata": _metadata("receipt-bars"),
                "data": [
                    {
                        "symbol": "M2609.DCE",
                        "trade_date": "20260731",
                        "session_id": "fixture-dce-day-session",
                        "completed": True,
                        "bar_time": "2026-07-31T09:30:00+08:00",
                        "available_at": "2026-07-31T09:30:01+08:00",
                        "open": 3000,
                        "high": 3004,
                        "low": 2999,
                        "close": 3002,
                        "volume": 100,
                    },
                    {
                        "symbol": "M2609.DCE",
                        "trade_date": "20260731",
                        "session_id": "fixture-dce-day-session",
                        "completed": True,
                        "bar_time": "2026-07-31T09:35:00+08:00",
                        "available_at": "2026-07-31T09:35:01+08:00",
                        "open": 3002,
                        "high": 3008,
                        "low": 3001,
                        "close": 3006,
                        "volume": 120,
                    },
                ],
            },
        },
    }


def test_valid_injected_catalog_query_projection_is_observation_only() -> None:
    result = evaluate_handoff_fixture(_fixture())

    assert result["disposition"] == "observation"
    assert result["execution_eligible"] is False
    assert result["delayed_paper_eligible"] is False
    assert result["learning_evidence_eligible"] is False
    assert result["readiness"] == {
        "contract_id": "tradingagent.evidence_readiness.v1",
        "observation_ready": True,
        "historical_pit_ready": False,
        "delayed_paper_ready": False,
        "execution_ready": False,
    }
    assert result["evidence"]["symbol"] == "M2609.DCE"
    assert result["evidence"]["bar_ends"] == [
        "2026-07-31T09:30:00+08:00",
        "2026-07-31T09:35:00+08:00",
    ]
    watermark = result["evidence"]["query_receipt_watermarks"]["bars_5min"]
    assert watermark["receipt_id"] == "receipt-bars"
    assert watermark["observed_at"] == "2026-07-31T09:40:00+08:00"
    assert len(watermark["lineage_sha256"]) == 64
    assert result["evidence"]["query_identities"]["bars_5min"] == _query_identity(
        symbol="M2609.DCE", sort_field="bar_time"
    )


def test_missing_multiplier_is_risk_reject_not_a_fallback_spec() -> None:
    fixture = _fixture()
    contract = fixture["queries"]["contract_master"]["data"][0]
    contract["multiplier"] = None

    result = evaluate_handoff_fixture(fixture)

    assert result["disposition"] == "risk_reject"
    assert result["reason"] == "contract_multiplier_missing_or_invalid"
    assert result["execution_eligible"] is False
    assert result["readiness"] == {
        "contract_id": None,
        "observation_ready": False,
        "historical_pit_ready": False,
        "delayed_paper_ready": False,
        "execution_ready": False,
    }


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (
            lambda fixture: fixture["queries"]["bars_5min"]["metadata"].update(
                {"degraded": True}
            ),
            "query_evidence_not_eligible:bars_5min",
        ),
        (
            lambda fixture: fixture["queries"]["calendar_session"]["data"][0].update(
                {"calendar_eligible": False}
            ),
            "calendar_not_eligible",
        ),
        (
            lambda fixture: fixture["queries"]["bars_5min"]["data"][1].update(
                {"completed": False}
            ),
            "completed_5min_bar_required",
        ),
        (
            lambda fixture: (
                fixture["queries"]["bars_5min"]["data"][1].update(
                    {"bar_time": "2026-07-31T09:40:00+08:00"}
                ),
                fixture["queries"]["bars_5min"]["metadata"].update(
                    {"data_through": "2026-07-31T09:40:00+08:00"}
                ),
            ),
            "bars_not_adjacent_5min",
        ),
        (
            lambda fixture: fixture["queries"]["bars_5min"].update(
                {"next_cursor": "still-paginating"}
            ),
            "query_page_not_complete:bars_5min",
        ),
    ],
)
def test_incomplete_evidence_returns_a_hold(mutate: object, reason: str) -> None:
    fixture = _fixture()
    assert callable(mutate)
    mutate(fixture)

    result = evaluate_handoff_fixture(fixture)

    assert result["disposition"] == "hold"
    assert result["reason"] == reason


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (
            lambda fixture: fixture["queries"]["bars_5min"]["metadata"].update(
                {"observed_at": "2026-07-31T09:40:06+08:00"}
            ),
            "query_pit_order_invalid:bars_5min",
        ),
        (
            lambda fixture: fixture["queries"]["bars_5min"]["metadata"].update(
                {"data_through": "2026-07-31T09:40:01+08:00"}
            ),
            "query_pit_order_invalid:bars_5min",
        ),
    ],
)
def test_query_envelope_pit_order_fails_closed(mutate: object, reason: str) -> None:
    fixture = _fixture()
    assert callable(mutate)
    mutate(fixture)

    result = evaluate_handoff_fixture(fixture)

    assert result["disposition"] == "hold"
    assert result["reason"] == reason


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (
            lambda fixture: fixture["queries"]["bars_5min"]["metadata"].update(
                {"data_through": "2026-07-31T09:34:00+08:00"}
            ),
            "bar_pit_order_invalid",
        ),
        (
            lambda fixture: fixture["queries"]["bars_5min"]["data"][1].update(
                {"bar_time": "2026-07-31T09:36:00+08:00"}
            ),
            "bar_not_on_5min_grid",
        ),
        (
            lambda fixture: fixture["queries"]["calendar_session"]["data"][0].update(
                {
                    "session_windows": [
                        {
                            "start": "2026-07-31T09:00:00+08:00",
                            "end": "2026-08-01T10:15:00+08:00",
                        }
                    ]
                }
            ),
            "calendar_session_window_invalid",
        ),
        (
            lambda fixture: fixture["queries"]["calendar_session"]["data"][0].update(
                {
                    "session_windows": [
                        {
                            "start": "2026-07-31T09:00:00+08:00",
                            "end": "2026-07-31T09:25:00+08:00",
                        }
                    ]
                }
            ),
            "bar_outside_calendar_session",
        ),
    ],
)
def test_bar_coverage_grid_and_calendar_windows_fail_closed(
    mutate: object, reason: str
) -> None:
    fixture = _fixture()
    assert callable(mutate)
    mutate(fixture)

    result = evaluate_handoff_fixture(fixture)

    assert result["disposition"] == "hold"
    assert result["reason"] == reason


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (
            lambda fixture: fixture["queries"]["contract_master"]["data"][0][
                "tradeability"
            ].update({"trade_date": "20260801"}),
            "contract_tradeability_trade_date_mismatch",
        ),
        (
            lambda fixture: fixture["queries"]["bars_5min"].pop("query_identity"),
            "mapping_required:query_identity",
        ),
        (
            lambda fixture: fixture["queries"]["bars_5min"]["query_identity"].update(
                {"cursor": "not-terminal"}
            ),
            "query_identity_cursor_required_null:bars_5min",
        ),
    ],
)
def test_tradeability_and_query_identity_are_bound_to_handoff(
    mutate: object, reason: str
) -> None:
    fixture = _fixture()
    assert callable(mutate)
    mutate(fixture)

    result = evaluate_handoff_fixture(fixture)

    assert result["disposition"] == "hold"
    assert result["reason"] == reason


@pytest.mark.parametrize(
    "field, value, reason",
    [
        ("observed_at", None, "text_required:bars_5min.observed_at"),
        ("data_through", None, "text_required:bars_5min.data_through"),
        ("receipt_id", None, "text_required:bars_5min.receipt_id"),
        ("lineage", None, "mapping_required:lineage"),
    ],
)
def test_required_query_envelope_provenance_fields_fail_closed(
    field: str, value: object, reason: str
) -> None:
    fixture = _fixture()
    fixture["queries"]["bars_5min"]["metadata"][field] = value

    result = evaluate_handoff_fixture(fixture)

    assert result["disposition"] == "hold"
    assert result["reason"] == reason


def test_row_available_at_is_not_a_pit_authority_or_lineage_input() -> None:
    baseline = evaluate_handoff_fixture(_fixture())
    decorated = _fixture()
    decorated["queries"]["contract_master"]["data"][0]["available_at"] = (
        "1970-01-01T00:00:00+00:00"
    )
    decorated["queries"]["calendar_session"]["data"][0]["available_at"] = (
        "1970-01-01T00:00:00+00:00"
    )
    for row in decorated["queries"]["bars_5min"]["data"]:
        row["available_at"] = "1970-01-01T00:00:00+00:00"

    replay = evaluate_handoff_fixture(decorated)

    assert replay == baseline


@pytest.mark.parametrize(
    "path, value, reason",
    [
        (("catalog", "route"), "GET /tushare", "catalog_route_required"),
        (
            ("real_trading_enabled",),
            True,
            "forbidden_fixture_marker:real_trading_enabled",
        ),
        (
            ("queries", "bars_5min", "metadata", "receipt_id"),
            "  ",
            "text_required:bars_5min.receipt_id",
        ),
    ],
)
def test_routes_live_markers_and_receipts_fail_closed_to_hold(
    path: tuple[str, ...], value: object, reason: str
) -> None:
    fixture = _fixture()
    target: dict[str, object] = fixture
    for key in path[:-1]:
        target = target[key]  # type: ignore[assignment,index]
    target[path[-1]] = value

    result = evaluate_handoff_fixture(fixture)

    assert result["disposition"] == "hold"
    assert result["reason"] == reason


def test_replay_is_deterministic_and_never_claims_execution_authority() -> None:
    first = evaluate_handoff_fixture(_fixture())
    second = evaluate_handoff_fixture(copy.deepcopy(_fixture()))

    assert first["handoff_lineage_sha256"] == second["handoff_lineage_sha256"]

    def walk(value: object) -> list[dict[str, object]]:
        if isinstance(value, dict):
            return [value, *(item for child in value.values() for item in walk(child))]
        if isinstance(value, list):
            return [item for child in value for item in walk(child)]
        return []

    for item in walk(first):
        assert item.get("execution_eligible") is not True
        assert item.get("execution_authority") is not True
        assert item.get("delayed_paper_eligible") is not True
        assert item.get("learning_evidence_eligible") is not True
        assert item.get("historical_pit_ready") is not True
        assert item.get("delayed_paper_ready") is not True
        assert item.get("durable") is not True
        assert item.get("capital_commit_id") in (None,)
        assert item.get("outbox_id") in (None,)
