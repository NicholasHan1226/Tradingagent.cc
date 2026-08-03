from __future__ import annotations

import copy

import pytest

from CNFutures.tradingdatas_handoff_acceptance import (
    PROFILE_ID,
    evaluate_handoff_fixture,
)
from shared.governance.evidence_readiness import dataset_contract_fingerprint


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


def _query_identity(
    *,
    filters: dict[str, object],
    sort_field: str,
    identity_fields: list[str],
) -> dict[str, object]:
    return {
        "filters": {
            field: {"operator": "eq", "value": value}
            for field, value in filters.items()
        },
        "sort": [{"field": sort_field, "direction": "asc"}],
        "identity_fields": identity_fields,
        "cursor": None,
    }


def _catalog_contract(role: str, dataset_id: str) -> dict[str, object]:
    if role == "contract_master":
        default_fields = [
            "symbol",
            "product",
            "exchange",
            "tradeability",
            "multiplier",
            "tick_size",
            "price_limit",
        ]
        filter_operators = {"product": ["eq"]}
        default_order = ["symbol:asc"]
        identity_fields = ["symbol"]
    elif role == "calendar_session":
        default_fields = [
            "symbol",
            "trade_date",
            "calendar_eligible",
            "session_kind",
            "session_id",
            "session_windows",
            "authority",
        ]
        filter_operators = {"symbol": ["eq"], "trade_date": ["eq"]}
        default_order = ["trade_date:asc"]
        identity_fields = ["symbol", "trade_date"]
    else:
        default_fields = [
            "symbol",
            "trade_date",
            "session_id",
            "completed",
            "bar_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]
        filter_operators = {"symbol": ["eq"], "trade_date": ["eq"]}
        default_order = ["bar_time:asc"]
        identity_fields = ["symbol", "bar_time"]
    return {
        "dataset_id": dataset_id,
        "schema_major": 1,
        "default_fields": default_fields,
        "filter_operators": filter_operators,
        "default_order": default_order,
        "limits": {"max_page_size": 100, "max_lookback_days": 31},
        "identity_fields": identity_fields,
        "state": "ready",
        "degraded": False,
    }


def _fixture() -> dict[str, object]:
    dataset_ids = {
        "contract_master": "fixture.td.m.contract-master",
        "bars_5min": "fixture.td.m.bars-5min",
        "calendar_session": "fixture.td.m.calendar-session",
    }
    schema_major = {role: 1 for role in dataset_ids}
    catalog_contracts = {
        role: _catalog_contract(role, dataset_id)
        for role, dataset_id in dataset_ids.items()
    }
    return {
        "fixture_only": True,
        "real_trading_enabled": False,
        "decision_time": "2026-07-31T09:40:05+08:00",
        "profile": {
            "profile_id": PROFILE_ID,
            "roles": {
                role: {
                    "dataset_id": dataset_id,
                    "schema_major": schema_major[role],
                    "expected_contract_fingerprint": dataset_contract_fingerprint(
                        catalog_contracts[role]
                    ),
                }
                for role, dataset_id in dataset_ids.items()
            },
        },
        "catalog": {
            "route": "GET /v1/catalog",
            "api_version": "v1",
            "catalog_version": "fixture-catalog-v1",
            "datasets": list(catalog_contracts.values()),
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
                    filters={"product": "M"},
                    sort_field="symbol",
                    identity_fields=["symbol"],
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
                            "effective_from": "2026-07-01T00:00:00+08:00",
                            "effective_until": "2026-08-01T00:00:00+08:00",
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
                    filters={"symbol": "M2609.DCE", "trade_date": "20260731"},
                    sort_field="trade_date",
                    identity_fields=["symbol", "trade_date"],
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
                        "authority": {
                            "product": "M",
                            "exchange": "DCE",
                            "timezone": "Asia/Shanghai",
                            "effective_windows": [
                                {
                                    "effective_from": "2026-07-01T00:00:00+08:00",
                                    "effective_until": "2026-08-01T00:00:00+08:00",
                                }
                            ],
                        },
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
                    filters={"symbol": "M2609.DCE", "trade_date": "20260731"},
                    sort_field="bar_time",
                    identity_fields=["symbol", "bar_time"],
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


def _rollover_cohort_fixture() -> dict[str, object]:
    fixture = _fixture()
    current = fixture["queries"]["contract_master"]["data"][0]
    assert isinstance(current, dict)

    def contract(
        symbol: str, effective_from: str, effective_until: str
    ) -> dict[str, object]:
        row = copy.deepcopy(current)
        row["symbol"] = symbol
        row["tradeability"] = {
            "state": "tradeable",
            "trade_date": "20260731",
            "effective_from": effective_from,
            "effective_until": effective_until,
        }
        return row

    fixture["queries"]["contract_master"]["data"] = [
        contract("M2607.DCE", "2026-06-01T00:00:00+08:00", "2026-07-01T00:00:00+08:00"),
        contract("M2609.DCE", "2026-07-01T00:00:00+08:00", "2026-08-01T00:00:00+08:00"),
        contract("M2611.DCE", "2026-08-01T00:00:00+08:00", "2026-09-01T00:00:00+08:00"),
    ]
    return fixture


def _night_fixture() -> dict[str, object]:
    fixture = _fixture()
    fixture["decision_time"] = "2026-08-01T00:56:05+08:00"
    for query in fixture["queries"].values():
        query["metadata"].update(
            {
                "data_through": "2026-08-01T00:55:00+08:00",
                "observed_at": "2026-08-01T00:56:00+08:00",
            }
        )
    contract = fixture["queries"]["contract_master"]["data"][0]
    contract["tradeability"].update(
        {
            "trade_date": "20260801",
            "effective_until": "2026-08-02T00:00:00+08:00",
        }
    )
    calendar = fixture["queries"]["calendar_session"]["data"][0]
    calendar.update(
        {
            "trade_date": "20260801",
            "session_kind": "night",
            "session_id": "fixture-dce-m-night-session",
            "session_windows": [
                {
                    "start": "2026-07-31T21:00:00+08:00",
                    "end": "2026-08-01T01:00:00+08:00",
                }
            ],
            "authority": {
                "product": "M",
                "exchange": "DCE",
                "timezone": "Asia/Shanghai",
                "effective_windows": [
                    {
                        "effective_from": "2026-07-01T00:00:00+08:00",
                        "effective_until": "2026-08-02T00:00:00+08:00",
                    }
                ],
            },
        }
    )
    bars = fixture["queries"]["bars_5min"]["data"]
    for row, bar_time in zip(
        bars,
        ("2026-08-01T00:50:00+08:00", "2026-08-01T00:55:00+08:00"),
    ):
        row.update(
            {
                "trade_date": "20260801",
                "session_id": "fixture-dce-m-night-session",
                "bar_time": bar_time,
                "available_at": bar_time.replace(":00+", ":01+"),
            }
        )
    return fixture


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
        filters={"symbol": "M2609.DCE", "trade_date": "20260731"},
        sort_field="bar_time",
        identity_fields=["symbol", "bar_time"],
    )
    assert result["evidence"]["dataset_contract_fingerprints"]["bars_5min"] == (
        dataset_contract_fingerprint(
            _catalog_contract("bars_5min", "fixture.td.m.bars-5min")
        )
    )


def test_receipt_bound_m_night_session_rolls_trade_date_across_midnight_offline_only(
) -> None:
    result = evaluate_handoff_fixture(_night_fixture())

    assert result["disposition"] == "observation"
    assert result["evidence"]["trade_date"] == "20260801"
    assert result["evidence"]["session_kind"] == "night"
    assert result["evidence"]["session_windows"] == [
        {
            "start": "2026-07-31T21:00:00+08:00",
            "end": "2026-08-01T01:00:00+08:00",
        }
    ]
    assert result["evidence"]["session_authority"] == {
        "product": "M",
        "exchange": "DCE",
        "timezone": "Asia/Shanghai",
        "effective_from": "2026-07-01T00:00:00+08:00",
        "effective_until": "2026-08-02T00:00:00+08:00",
    }
    assert result["execution_eligible"] is False
    assert result["delayed_paper_eligible"] is False


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (
            lambda fixture: fixture["queries"]["calendar_session"]["data"][0].pop(
                "authority"
            ),
            "mapping_required:calendar.authority",
        ),
        (
            lambda fixture: fixture["queries"]["calendar_session"]["data"][0][
                "authority"
            ].update({"product": "RB"}),
            "calendar_authority_product_m_required",
        ),
        (
            lambda fixture: fixture["queries"]["calendar_session"]["data"][0][
                "authority"
            ].update({"exchange": "SHFE"}),
            "calendar_authority_exchange_dce_required",
        ),
        (
            lambda fixture: fixture["queries"]["calendar_session"]["data"][0][
                "authority"
            ].update({"timezone": "UTC"}),
            "calendar_authority_timezone_required",
        ),
        (
            lambda fixture: fixture["queries"]["calendar_session"]["data"][0][
                "authority"
            ].update(
                {
                    "effective_windows": [
                        {
                            "effective_from": "2026-07-01T00:00:00+08:00",
                            "effective_until": "2026-07-15T00:00:00+08:00",
                        },
                        {
                            "effective_from": "2026-07-14T00:00:00+08:00",
                            "effective_until": "2026-08-01T00:00:00+08:00",
                        },
                    ]
                }
            ),
            "calendar_authority_effective_windows_overlap",
        ),
        (
            lambda fixture: fixture["queries"]["calendar_session"]["data"][0][
                "authority"
            ].update(
                {
                    "effective_windows": [
                        {
                            "effective_from": "2026-07-01T00:00:00+08:00",
                            "effective_until": "2026-07-15T00:00:00+08:00",
                        },
                        {
                            "effective_from": "2026-07-16T00:00:00+08:00",
                            "effective_until": "2026-08-01T00:00:00+08:00",
                        },
                    ]
                }
            ),
            "calendar_authority_effective_windows_gap",
        ),
        (
            lambda fixture: fixture["queries"]["calendar_session"]["metadata"].update(
                {"receipt_id": ""}
            ),
            "text_required:calendar_session.receipt_id",
        ),
        (
            lambda fixture: fixture["queries"]["calendar_session"]["metadata"].update(
                {"lineage": {"complete": False, "provider_neutral": True}}
            ),
            "query_evidence_not_eligible:calendar_session",
        ),
        (
            lambda fixture: fixture["queries"]["calendar_session"]["metadata"].update(
                {"observed_at": "2026-07-31T09:40:06+08:00"}
            ),
            "query_pit_order_invalid:calendar_session",
        ),
    ],
    ids=(
        "missing-authority",
        "wrong-product",
        "wrong-exchange",
        "wrong-timezone",
        "authority-overlap",
        "authority-gap",
        "missing-receipt",
        "incomplete-lineage",
        "pit-ineligible",
    ),
)
def test_calendar_session_authority_fails_closed(
    mutate: object, reason: str
) -> None:
    fixture = _fixture()
    assert callable(mutate)
    mutate(fixture)

    result = evaluate_handoff_fixture(fixture)

    assert result["disposition"] == "hold"
    assert result["reason"] == reason


def test_receipt_bound_rollover_cohort_selects_one_active_contract_offline_only(
) -> None:
    result = evaluate_handoff_fixture(_rollover_cohort_fixture())

    assert result["disposition"] == "observation"
    assert result["evidence"]["symbol"] == "M2609.DCE"
    assert result["evidence"]["rollover_cohort"] == [
        {
            "symbol": "M2607.DCE",
            "effective_from": "2026-06-01T00:00:00+08:00",
            "effective_until": "2026-07-01T00:00:00+08:00",
        },
        {
            "symbol": "M2609.DCE",
            "effective_from": "2026-07-01T00:00:00+08:00",
            "effective_until": "2026-08-01T00:00:00+08:00",
        },
        {
            "symbol": "M2611.DCE",
            "effective_from": "2026-08-01T00:00:00+08:00",
            "effective_until": "2026-09-01T00:00:00+08:00",
        },
    ]
    assert result["execution_eligible"] is False
    assert result["delayed_paper_eligible"] is False


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (
            lambda fixture: fixture["queries"]["contract_master"]["data"][1][
                "tradeability"
            ].update({"effective_until": "2026-07-31T09:40:00+08:00"}),
            "contract_rollover_no_active_contract",
        ),
        (
            lambda fixture: fixture["queries"]["contract_master"]["data"][-1][
                "tradeability"
            ].update({"effective_from": "2026-07-15T00:00:00+08:00"}),
            "contract_rollover_cohort_overlap",
        ),
        (
            lambda fixture: fixture["queries"]["contract_master"]["data"][-1][
                "tradeability"
            ].update({"effective_from": "2026-08-02T00:00:00+08:00"}),
            "contract_rollover_cohort_gap",
        ),
        (
            lambda fixture: fixture["queries"]["contract_master"]["data"][1][
                "tradeability"
            ].pop("effective_until"),
            "contract_rollover_effective_until_required",
        ),
        (
            lambda fixture: (
                fixture["queries"]["contract_master"]["data"][0][
                    "tradeability"
                ].update({"effective_until": "2026-07-31T09:40:03+08:00"}),
                fixture["queries"]["contract_master"]["data"][1][
                    "tradeability"
                ].update({"effective_from": "2026-07-31T09:40:03+08:00"}),
            ),
            "contract_rollover_effective_time_pit_ineligible",
        ),
    ],
    ids=("no-active", "overlap", "gap", "missing-effective-time", "pit-ineligible"),
)
def test_rollover_cohort_fails_closed_on_unusable_effective_tradeability(
    mutate: object, reason: str
) -> None:
    fixture = _rollover_cohort_fixture()
    assert callable(mutate)
    mutate(fixture)

    result = evaluate_handoff_fixture(fixture)

    assert result["disposition"] == "hold"
    assert result["reason"] == reason
    assert result["execution_eligible"] is False
    assert result["delayed_paper_eligible"] is False


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
    "mutate",
    [
        lambda row: row.update({"dataset_id": "fixture.td.m.bars-5min-drift"}),
        lambda row: row.update({"schema_major": 2}),
        lambda row: row.update({"default_fields": ["symbol", "bar_time"]}),
        lambda row: row.update({"filter_operators": {"symbol": ["eq", "in"]}}),
        lambda row: row.update({"default_order": ["bar_time:desc"]}),
        lambda row: row.update(
            {"limits": {"max_page_size": 101, "max_lookback_days": 31}}
        ),
        lambda row: row.update({"identity_fields": ["bar_time", "symbol"]}),
    ],
    ids=(
        "dataset-id",
        "schema-major",
        "default-fields",
        "filter-operators",
        "default-order",
        "limits",
        "identity-fields",
    ),
)
def test_catalog_contract_field_drift_blocks_observation(mutate: object) -> None:
    fixture = _fixture()
    row = fixture["catalog"]["datasets"][1]
    assert callable(mutate)
    mutate(row)

    result = evaluate_handoff_fixture(fixture)

    assert result["disposition"] == "hold"
    assert result["readiness"]["observation_ready"] is False


def test_cross_role_contract_fingerprint_transplant_blocks_observation() -> None:
    fixture = _fixture()
    fixture["profile"]["roles"]["bars_5min"]["expected_contract_fingerprint"] = fixture[
        "profile"
    ]["roles"]["calendar_session"]["expected_contract_fingerprint"]

    result = evaluate_handoff_fixture(fixture)

    assert result["disposition"] == "hold"
    assert result["reason"] == "catalog_contract_fingerprint_mismatch:bars_5min"
    assert result["readiness"]["observation_ready"] is False


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (
            lambda identity: identity["filters"].update(
                {"unknown": {"operator": "eq", "value": "x"}}
            ),
            "query_identity_filter_field_not_declared:bars_5min:unknown",
        ),
        (
            lambda identity: identity["filters"]["symbol"].update({"operator": "in"}),
            "query_identity_filter_operator_not_declared:bars_5min:symbol",
        ),
        (
            lambda identity: identity.update(
                {"sort": [{"field": "close", "direction": "asc"}]}
            ),
            "query_identity_sort_not_declared:bars_5min:close:asc",
        ),
        (
            lambda identity: identity.update({"identity_fields": ["symbol"]}),
            "query_identity_fields_mismatch:bars_5min",
        ),
    ],
    ids=("filter-field", "filter-operator", "sort", "identity"),
)
def test_query_identity_must_match_declared_catalog_contract(
    mutate: object, reason: str
) -> None:
    fixture = _fixture()
    identity = fixture["queries"]["bars_5min"]["query_identity"]
    assert callable(mutate)
    mutate(identity)

    result = evaluate_handoff_fixture(fixture)

    assert result["disposition"] == "hold"
    assert result["reason"] == reason
    assert result["readiness"]["observation_ready"] is False


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
    assert first["readiness"]["historical_pit_ready"] is False
    assert first["readiness"]["delayed_paper_ready"] is False
    assert first["readiness"]["execution_ready"] is False

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
        assert item.get("durable") is not True
        assert item.get("capital_commit_id") in (None,)
        assert item.get("outbox_id") in (None,)
