from __future__ import annotations

import copy

import pytest

from CNFutures.training_baseline import (
    TrainingBaselineError,
    run_fixture_training_baseline,
)


def _bars() -> list[dict[str, object]]:
    return [
        {
            "bar_time": f"2026-07-30T09:{minute:02d}:00+08:00",
            "open": price,
            "high": price + 2.0,
            "low": price - 1.0,
            "close": price + 1.0,
            "volume": volume,
            "previous_close": 2998.0,
        }
        for minute, price, volume in (
            (0, 3000.0, 100.0),
            (5, 3004.0, 110.0),
            (10, 3008.0, 120.0),
            (15, 3013.0, 140.0),
            (20, 3019.0, 180.0),
            (25, 3026.0, 240.0),
            (30, 3034.0, 320.0),
        )
    ]


def _fixture(**overrides: object) -> dict[str, object]:
    fixture: dict[str, object] = {
        "fixture_only": True,
        "real_trading_enabled": False,
        "generation": 9,
        "trade_date": "20260730",
        "decision_time": "2026-07-30T09:30:02+08:00",
        "contract": {"symbol": "M2609.DCE"},
        "data_evidence": {
            "source_kind": "fixture_mock",
            "catalog_route": "GET /v1/catalog",
            "query_route": "POST /v1/query",
            "catalog_state": "ready",
            "query_state": "ready",
            "degraded": False,
            "freshness": "fresh",
            "quality": "valid",
            "lineage_ref": "fixture-m-lineage",
            "receipt_id": "fixture-m-receipt",
            "available_at": "2026-07-30T09:30:01+08:00",
        },
        "bars": {"rows": _bars()},
    }
    fixture.update(overrides)
    return fixture


def test_m_day_session_fixture_creates_one_lot_non_authoritative_sample() -> None:
    result = run_fixture_training_baseline(_fixture())

    assert result["mode"] == "fixture_mock_training_baseline"
    assert result["not_real_market_data_training"] is True
    assert result["learning_evidence_eligible"] is False
    assert result["automatic_promotion"] is False
    assert result["strategy"]["name"] == "commodity_intraday_trend"
    assert result["candidate"]["execution_eligible"] is False
    assert result["candidate"]["fixture_simulation_eligible"] is True
    sample = result["sample_records"][0]
    assert sample["quantity"] == 1
    assert sample["exit_reason"] == "fixture_same_session_flatten_no_overnight"
    assert sample["execution_authority"] is False
    assert sample["durable"] is False
    assert sample["learning_evidence_eligible"] is False
    assert result["daily_reconcile"]["non_authoritative"] is True


def test_rollover_is_a_non_eligible_auditable_sample() -> None:
    result = run_fixture_training_baseline(_fixture(contract={"symbol": "M2608.DCE"}))

    assert result["candidate"]["reason"] == "rollover_guard"
    assert result["candidate"]["fixture_simulation_eligible"] is False
    assert result["sample_records"][0]["reason"] == "rollover_guard"


def test_contract_scope_fails_closed_for_non_m_symbol() -> None:
    with pytest.raises(
        TrainingBaselineError, match="only_concrete_m_dce_contracts_are_supported"
    ):
        run_fixture_training_baseline(_fixture(contract={"symbol": "RB2610.SHF"}))


@pytest.mark.parametrize(
    "bars_patch, reason",
    [
        (
            lambda bars: [
                *bars[:-1],
                {**bars[-1], "bar_time": "2026-07-30T12:00:00+08:00"},
            ],
            "lunch_or_offsession_bar",
        ),
        (
            lambda bars: [bars[0], *bars[2:]],
            "missing_5min_bar",
        ),
    ],
)
def test_lunch_and_missing_bar_fixtures_are_non_eligible_auditable_samples(
    bars_patch: object, reason: str
) -> None:
    assert callable(bars_patch)
    result = run_fixture_training_baseline(_fixture(bars={"rows": bars_patch(_bars())}))

    assert result["candidate"]["reason"] == reason
    assert result["candidate"]["fixture_simulation_eligible"] is False
    assert result["sample_records"][0]["reason"] == reason


def test_one_lot_margin_reject_is_a_non_authoritative_hold_sample() -> None:
    bars = _bars()
    bars[-1]["close"] = 30_000.0
    bars[-1]["open"] = 29_999.0
    bars[-1]["high"] = 30_001.0
    bars[-1]["low"] = 29_998.0
    result = run_fixture_training_baseline(_fixture(bars={"rows": bars}))

    assert result["candidate"]["reason"] == "one_lot_margin_reject"
    assert (
        result["sample_records"][0]["sample_class"]
        == "fixture_training_hold_or_risk_reject"
    )
    assert result["sample_records"][0]["execution_eligible"] is False


def test_no_overnight_contract_returns_a_session_close_guard_sample() -> None:
    bars = _bars()
    for index, minute in enumerate((20, 25, 30, 35, 40, 45, 50)):
        bars[index]["bar_time"] = f"2026-07-30T14:{minute:02d}:00+08:00"
    evidence = dict(_fixture()["data_evidence"])  # type: ignore[arg-type]
    evidence["available_at"] = "2026-07-30T14:50:01+08:00"
    result = run_fixture_training_baseline(
        _fixture(
            bars={"rows": bars},
            data_evidence=evidence,
            decision_time="2026-07-30T14:50:02+08:00",
        )
    )

    assert result["candidate"]["reason"] == "session_close_guard"
    assert result["sample_records"][0]["reason"] == "session_close_guard"


@pytest.mark.parametrize(
    "path, value",
    [
        (("fixture_only",), False),
        (("network_enabled",), True),
        (("data_evidence", "degraded"), True),
        (("data_evidence", "catalog_route"), "GET /tushare"),
    ],
)
def test_fixture_evidence_and_live_markers_fail_closed(
    path: tuple[str, ...], value: object
) -> None:
    fixture = copy.deepcopy(_fixture())
    target: dict[str, object] = fixture
    for key in path[:-1]:
        target = target[key]  # type: ignore[assignment,index]
    target[path[-1]] = value

    with pytest.raises(TrainingBaselineError):
        run_fixture_training_baseline(fixture)


def test_replay_is_deterministic_and_never_claims_real_training() -> None:
    first = run_fixture_training_baseline(_fixture())
    second = run_fixture_training_baseline(_fixture())

    assert first["fixture_lineage_sha256"] == second["fixture_lineage_sha256"]
    assert first["candidate"]["intent_id"] == second["candidate"]["intent_id"]

    def walk(value: object) -> list[dict[str, object]]:
        if isinstance(value, dict):
            return [value, *(item for child in value.values() for item in walk(child))]
        if isinstance(value, list):
            return [item for child in value for item in walk(child)]
        return []

    for mapping in walk(first):
        assert mapping.get("real_trading_enabled") is not True
        assert mapping.get("execution_eligible") is not True
        assert mapping.get("execution_authority") is not True
        assert mapping.get("learning_evidence_eligible") is not True
        assert mapping.get("durable") is not True
        assert mapping.get("capital_commit_id") in (None,)
        assert mapping.get("outbox_id") in (None,)


@pytest.mark.parametrize(
    "patch, reason",
    [
        (
            {"decision_time": "2026-07-30T09:30:00+08:00"},
            "evidence_available_after_decision",
        ),
        (
            {
                "data_evidence": {
                    **_fixture()["data_evidence"],  # type: ignore[dict-item]
                    "available_at": "2026-07-30T09:29:59+08:00",
                }
            },
            "bar_not_available_at_evidence_time",
        ),
    ],
)
def test_pit_order_fails_closed(patch: dict[str, object], reason: str) -> None:
    with pytest.raises(TrainingBaselineError, match=reason):
        run_fixture_training_baseline(_fixture(**patch))


def test_invalid_ohlc_relationship_fails_closed() -> None:
    bars = _bars()
    bars[-1]["high"] = float(bars[-1]["close"]) - 1.0

    with pytest.raises(TrainingBaselineError, match="invalid_ohlc_relationship"):
        run_fixture_training_baseline(_fixture(bars={"rows": bars}))


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (
            lambda bars: bars[-1].update(
                {"open": 1e308, "high": 1e308, "low": 1e308, "close": 1e308}
            ),
            "nonfinite_pretrade_math",
        ),
        (lambda bars: bars[-1].update({"volume": -1}), "nonnegative_number_required"),
    ],
)
def test_extreme_math_and_invalid_volume_fail_closed(
    mutate: object, reason: str
) -> None:
    bars = _bars()
    assert callable(mutate)
    mutate(bars)

    with pytest.raises(TrainingBaselineError, match=reason):
        run_fixture_training_baseline(_fixture(bars={"rows": bars}))
