from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from Crypto.five_minute_data import (
    CryptoFiveMinuteDataError,
    CryptoFiveMinuteSnapshot,
    CryptoFiveMinuteWindowRequest,
    TradingDatasCryptoFiveMinuteDataPort,
    _sha256,
)
from shared.data.sharedsignals_v1 import CatalogEnvelope
from tests.test_crypto_5m_support import (
    BAR_DATASETS,
    BAR_FIELDS,
    OBSERVATION_CUTOFF,
    RULE_DATASETS,
    WINDOW_END,
    FixtureTradingDatasTransport,
    bar_rows,
    catalog_payload,
    client,
    iso,
    metadata,
    offset_iso,
    profile,
    window_request,
)


ROOT = Path(__file__).resolve().parents[1]


def _rehashed_snapshot(
    snapshot: CryptoFiveMinuteSnapshot,
    *,
    bars: tuple[Any, ...] | None = None,
    source_proofs: tuple[Any, ...] | None = None,
) -> CryptoFiveMinuteSnapshot:
    effective_bars = snapshot.bars if bars is None else bars
    effective_proofs = (
        snapshot.source_proofs if source_proofs is None else source_proofs
    )
    market_content_sha256 = _sha256(
        {
            "bars": list(effective_bars),
            "instrument_rules": list(snapshot.instrument_rules),
        }
    )
    observation_sha256 = _sha256(
        {
            "profile_sha256": snapshot.profile_sha256,
            "request": snapshot.request.to_payload(),
            "market_content_sha256": market_content_sha256,
            "source_proofs": [proof.to_payload() for proof in effective_proofs],
            "same_observation": True,
            "execution_eligible": False,
            "execution_authority": False,
            "production_eligible": False,
        }
    )
    return replace(
        snapshot,
        bars=effective_bars,
        source_proofs=effective_proofs,
        market_content_sha256=market_content_sha256,
        observation_sha256=observation_sha256,
    )


def test_checked_in_crypto_5m_binding_is_fixture_only_and_unconfigured() -> None:
    config = yaml.safe_load(
        (ROOT / "Crypto" / "config.yaml").read_text(encoding="utf-8")
    )

    assert config["data"]["reader"] == "tradingdatas_v1_catalog_query"
    assert config["data"]["binding_scope"] == "fixture_only"
    assert set(config["data"]) == {
        "reader",
        "binding_scope",
    }
    assert config["risk"]["max_positions"] == 2
    serialized = str(config).lower()
    for forbidden in (
        "crypto.spot.binance",
        "base_url",
        "token",
        "sqlite",
        "testnet_url",
        "live_url",
    ):
        assert forbidden not in serialized
    assert config["safety"]["real_money_enabled"] is False
    assert config["safety"]["live_broker_enabled"] is False


def test_profile_and_port_match_four_dataset_candidate_contract() -> None:
    transport = FixtureTradingDatasTransport()
    tradingdatas_client = client(transport)
    frozen = profile(tradingdatas_client)
    snapshot = TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client).load_snapshot(
        profile=frozen, request=window_request()
    )

    assert snapshot.same_observation is True
    assert len(snapshot.bars) == 26
    assert len(snapshot.source_proofs) == 4
    assert [bar.symbol for bar in snapshot.bars[:13]] == ["BTCUSDT"] * 13
    assert [bar.symbol for bar in snapshot.bars[13:]] == ["ETHUSDT"] * 13
    assert snapshot.bars[0].open_time == WINDOW_END - timedelta(minutes=65)
    assert snapshot.bars[-1].close_time == WINDOW_END
    assert snapshot.bars[-1].source_close_time == (
        WINDOW_END - timedelta(milliseconds=1)
    )
    assert snapshot.rules_for("BTCUSDT").price_tick.as_tuple().exponent == -2
    assert str(snapshot.rules_for("ETHUSDT").quantity_step) == "0.0001"
    assert (
        snapshot.proof_for(BAR_DATASETS["BTCUSDT"]).receipt_id
        == f"fixture-receipt-{BAR_DATASETS['BTCUSDT']}"
    )
    assert len(snapshot.market_content_sha256) == 64
    assert len(snapshot.observation_sha256) == 64

    calls = transport.calls
    assert {call["method"] for call in calls} == {"GET", "POST"}
    assert all(call["url"].endswith(("/v1/catalog", "/v1/query")) for call in calls)
    first_btc_query = next(
        call["json_body"]
        for call in calls
        if call["method"] == "POST"
        and call["json_body"]["dataset_id"] == BAR_DATASETS["BTCUSDT"]
        and "cursor" not in call["json_body"]
    )
    assert tuple(first_btc_query["fields"]) == BAR_FIELDS
    assert first_btc_query["order"] == ["symbol:asc", "open_time:desc"]
    assert first_btc_query["filters"] == {
        "open_time": {
            "between": [
                offset_iso(WINDOW_END - timedelta(minutes=65)),
                offset_iso(WINDOW_END - timedelta(minutes=5)),
            ]
        },
        "symbol": {"eq": "BTCUSDT"},
    }
    assert first_btc_query["as_of"] == offset_iso(OBSERVATION_CUTOFF)

    first_btc_rules = next(
        call["json_body"]
        for call in calls
        if call["method"] == "POST"
        and call["json_body"]["dataset_id"] == RULE_DATASETS["BTCUSDT"]
        and "cursor" not in call["json_body"]
    )
    assert first_btc_rules["filters"] == {
        "status": {"eq": "TRADING"},
        "symbol": {"eq": "BTCUSDT"},
    }
    assert "as_of" not in first_btc_rules


def test_snapshot_rejects_bar_proof_binding_and_digest_tampering() -> None:
    transport = FixtureTradingDatasTransport()
    tradingdatas_client = client(transport)
    snapshot = TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client).load_snapshot(
        profile=profile(tradingdatas_client),
        request=window_request(),
    )

    tampered_bar = replace(
        snapshot.bars[0],
        source_receipt_id="fixture-receipt-other",
    )
    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="crypto_5m_bar_source_proof_mismatch",
    ):
        CryptoFiveMinuteSnapshot(
            profile_sha256=snapshot.profile_sha256,
            request=snapshot.request,
            bars=(tampered_bar, *snapshot.bars[1:]),
            instrument_rules=snapshot.instrument_rules,
            source_proofs=snapshot.source_proofs,
            market_content_sha256=snapshot.market_content_sha256,
            observation_sha256=snapshot.observation_sha256,
        )

    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="crypto_5m_observation_digest_mismatch",
    ):
        replace(snapshot, observation_sha256="0" * 64)


def test_snapshot_requires_exact_four_unique_proof_bindings() -> None:
    transport = FixtureTradingDatasTransport()
    tradingdatas_client = client(transport)
    snapshot = TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client).load_snapshot(
        profile=profile(tradingdatas_client),
        request=window_request(),
    )
    duplicated_dataset = replace(
        snapshot.source_proofs[1],
        dataset_id=snapshot.source_proofs[0].dataset_id,
    )

    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="crypto_5m_source_proof_binding_invalid",
    ):
        CryptoFiveMinuteSnapshot(
            profile_sha256=snapshot.profile_sha256,
            request=snapshot.request,
            bars=snapshot.bars,
            instrument_rules=snapshot.instrument_rules,
            source_proofs=(
                snapshot.source_proofs[0],
                duplicated_dataset,
                *snapshot.source_proofs[2:],
            ),
            market_content_sha256=snapshot.market_content_sha256,
            observation_sha256=snapshot.observation_sha256,
        )


def test_snapshot_profile_verification_binds_each_dataset_to_its_symbol_and_kind() -> (
    None
):
    transport = FixtureTradingDatasTransport()
    tradingdatas_client = client(transport)
    frozen = profile(tradingdatas_client)
    snapshot = TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client).load_snapshot(
        profile=frozen,
        request=window_request(),
    )
    btc, eth = frozen.symbols
    swapped_profile = replace(
        frozen,
        symbols=(
            replace(btc, bars=eth.bars),
            replace(eth, bars=btc.bars),
        ),
    )
    observation_payload = {
        "profile_sha256": swapped_profile.sha256,
        "request": snapshot.request.to_payload(),
        "market_content_sha256": snapshot.market_content_sha256,
        "source_proofs": [proof.to_payload() for proof in snapshot.source_proofs],
        "same_observation": True,
        "execution_eligible": False,
        "execution_authority": False,
        "production_eligible": False,
    }
    forged = replace(
        snapshot,
        profile_sha256=swapped_profile.sha256,
        observation_sha256=_sha256(observation_payload),
    )

    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="crypto_5m_snapshot_profile_binding_mismatch",
    ):
        forged.verify_profile(swapped_profile)


def test_snapshot_request_verification_rejects_rehashed_shifted_market_window() -> None:
    transport = FixtureTradingDatasTransport()
    tradingdatas_client = client(transport)
    frozen = profile(tradingdatas_client)
    request = window_request()
    snapshot = TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client).load_snapshot(
        profile=frozen,
        request=request,
    )
    shift = timedelta(minutes=5)
    shifted_bars = tuple(
        replace(
            bar,
            open_time=bar.open_time + shift,
            close_time=bar.close_time + shift,
            source_close_time=bar.source_close_time + shift,
            data_through=bar.data_through + shift,
            observed_at=bar.observed_at + shift,
        )
        for bar in snapshot.bars
    )
    shifted_proofs = tuple(
        (
            replace(
                proof,
                data_through=proof.data_through + shift,
                observed_at=proof.observed_at + shift,
            )
            if proof.dataset_kind == "closed_5m_bars"
            else proof
        )
        for proof in snapshot.source_proofs
    )
    forged = _rehashed_snapshot(
        snapshot,
        bars=shifted_bars,
        source_proofs=shifted_proofs,
    )

    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="crypto_5m_snapshot_window_binding_mismatch",
    ):
        forged.verify_against(profile=frozen, request=request)


def test_snapshot_request_verification_rejects_proof_observed_after_cutoff() -> None:
    transport = FixtureTradingDatasTransport()
    tradingdatas_client = client(transport)
    frozen = profile(tradingdatas_client)
    request = window_request()
    snapshot = TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client).load_snapshot(
        profile=frozen,
        request=request,
    )
    future_rule_proof = replace(
        snapshot.source_proofs[1],
        observed_at=request.observation_cutoff + timedelta(microseconds=1),
    )
    forged = _rehashed_snapshot(
        snapshot,
        source_proofs=(
            snapshot.source_proofs[0],
            future_rule_proof,
            *snapshot.source_proofs[2:],
        ),
    )

    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="crypto_5m_snapshot_source_budget_or_cutoff_invalid",
    ):
        forged.verify_against(profile=frozen, request=request)


def test_candidate_contract_has_no_invented_closed_frequency_or_active_fields() -> None:
    transport = FixtureTradingDatasTransport()
    frozen = profile(client(transport))
    payload = str(frozen.to_payload())

    assert "frequency" not in payload
    assert "'closed'" not in payload
    assert "'active'" not in payload
    assert "inclusive_last_millisecond" in payload
    assert "dataset_contract_discards_open_bars" in payload


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (
            lambda rows: rows.pop(4),
            "crypto_5m_window_incomplete",
        ),
        (
            lambda rows: rows.__setitem__(0, {**rows[0], "high": "1"}),
            "crypto_5m_ohlc_invalid",
        ),
        (
            lambda rows: rows.__setitem__(0, {**rows[0], "open": "50000.005"}),
            "crypto_5m_price_off_tick",
        ),
        (
            lambda rows: rows.__setitem__(
                0,
                {
                    **rows[0],
                    "open_time": datetime.fromisoformat(
                        str(rows[0]["open_time"]).replace("Z", "+00:00")
                    )
                    .astimezone(timezone(timedelta(hours=8)))
                    .isoformat(),
                },
            ),
            "crypto_5m_timestamp_must_be_utc",
        ),
        (
            lambda rows: rows.__setitem__(
                0,
                {
                    **rows[0],
                    "close_time": iso(WINDOW_END - timedelta(minutes=60)),
                },
            ),
            "crypto_5m_close_time_semantics_invalid",
        ),
        (
            lambda rows: rows.__setitem__(0, {**rows[0], "quote_volume": "-1"}),
            "crypto_5m_quote_volume_invalid",
        ),
        (
            lambda rows: rows.__setitem__(0, {**rows[0], "trade_count": True}),
            "crypto_5m_trade_count_invalid",
        ),
    ],
)
def test_bar_contract_failures_are_reason_coded(
    mutator: Any,
    reason: str,
) -> None:
    rows = bar_rows()
    mutator(rows)
    transport = FixtureTradingDatasTransport(
        bars=rows,
        page_size_override=10,
    )
    tradingdatas_client = client(transport)

    with pytest.raises(CryptoFiveMinuteDataError, match=reason):
        TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client).load_snapshot(
            profile=profile(tradingdatas_client),
            request=window_request(),
        )


@pytest.mark.parametrize(
    ("metadata_override", "reason"),
    [
        ({"state": "stale"}, "crypto_5m_metadata_not_ready"),
        ({"degraded": True}, "crypto_5m_metadata_not_ready"),
        (
            {"freshness": {"state": "stale", "stale": True}},
            "crypto_5m_metadata_not_fresh",
        ),
        (
            {"quality": {"state": "invalid"}},
            "crypto_5m_metadata_quality_invalid",
        ),
        (
            {"lineage": {"complete": False}},
            "crypto_5m_metadata_lineage_incomplete",
        ),
        (
            {"observed_at": WINDOW_END + timedelta(minutes=7)},
            "crypto_5m_observation_after_cutoff",
        ),
        (
            {
                "data_through": (
                    WINDOW_END - timedelta(minutes=5) - timedelta(milliseconds=1)
                )
            },
            "crypto_5m_data_through_mismatch",
        ),
    ],
)
def test_metadata_future_stale_degraded_and_lineage_fail_closed(
    metadata_override: dict[str, Any],
    reason: str,
) -> None:
    dataset_id = BAR_DATASETS["BTCUSDT"]
    kwargs: dict[str, Any] = {
        "dataset_id": dataset_id,
        "data_through": WINDOW_END - timedelta(milliseconds=1),
    }
    kwargs.update(metadata_override)
    transport = FixtureTradingDatasTransport(
        metadata_by_dataset={dataset_id: metadata(**kwargs)}
    )
    tradingdatas_client = client(transport)

    with pytest.raises(CryptoFiveMinuteDataError, match=reason):
        TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client).load_snapshot(
            profile=profile(tradingdatas_client),
            request=window_request(),
        )


def test_second_read_change_is_rejected_as_same_observation_mismatch() -> None:
    def mutate(dataset_id: str, rows: list[dict[str, Any]]) -> None:
        if dataset_id == BAR_DATASETS["BTCUSDT"]:
            rows[0]["close"] = "50099.99"
            rows[0]["high"] = "50100.00"

    transport = FixtureTradingDatasTransport(replay_mutator=mutate)
    tradingdatas_client = client(transport)

    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="crypto_5m_same_observation_mismatch",
    ):
        TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client).load_snapshot(
            profile=profile(tradingdatas_client),
            request=window_request(),
        )


def test_old_receipt_cannot_be_replayed_under_a_new_cutoff() -> None:
    transport = FixtureTradingDatasTransport()
    tradingdatas_client = client(transport)

    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="crypto_5m_observation_stale_by_cutoff",
    ):
        TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client).load_snapshot(
            profile=profile(tradingdatas_client),
            request=CryptoFiveMinuteWindowRequest(
                window_end=WINDOW_END,
                observation_cutoff=OBSERVATION_CUTOFF + timedelta(days=1),
            ),
        )


def test_rules_must_report_exact_trading_status() -> None:
    transport = FixtureTradingDatasTransport(
        rules=[
            {
                "symbol": "BTCUSDT",
                "status": "BREAK",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "price_filter_tick_size": "0.01",
                "lot_size_step_size": "0.000001",
                "lot_size_min_qty": "0.00001",
                "min_notional": "10",
            },
            {
                "symbol": "ETHUSDT",
                "status": "TRADING",
                "base_asset": "ETH",
                "quote_asset": "USDT",
                "price_filter_tick_size": "0.01",
                "lot_size_step_size": "0.0001",
                "lot_size_min_qty": "0.001",
                "min_notional": "10",
            },
        ]
    )
    tradingdatas_client = client(transport)

    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="crypto_5m_instrument_not_trading",
    ):
        TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client).load_snapshot(
            profile=profile(tradingdatas_client),
            request=window_request(),
        )


def test_duplicate_identity_pagination_failure_has_no_fallback() -> None:
    rows = bar_rows()
    rows[10] = copy.deepcopy(rows[9])
    transport = FixtureTradingDatasTransport(bars=rows)
    tradingdatas_client = client(transport)

    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="pagination_duplicate_row_identity",
    ):
        TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client).load_snapshot(
            profile=profile(tradingdatas_client),
            request=window_request(),
        )

    assert all(
        call["url"].endswith(("/v1/catalog", "/v1/query")) for call in transport.calls
    )


def test_bounded_time_filter_excludes_older_history_and_stays_terminal() -> None:
    rows = bar_rows()
    older_btc = copy.deepcopy(rows[0])
    older_open = WINDOW_END - timedelta(minutes=70)
    older_btc["open_time"] = iso(older_open)
    older_btc["close_time"] = iso(
        older_open + timedelta(minutes=5) - timedelta(milliseconds=1)
    )
    rows.insert(0, older_btc)
    transport = FixtureTradingDatasTransport(bars=rows)
    tradingdatas_client = client(transport)

    snapshot = TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client).load_snapshot(
        profile=profile(tradingdatas_client),
        request=window_request(),
    )

    proof = snapshot.proof_for(BAR_DATASETS["BTCUSDT"])
    assert proof.row_count == 13
    assert proof.page_count == 1


def test_nonterminal_cursor_is_fully_traversed_within_budget() -> None:
    transport = FixtureTradingDatasTransport(page_size_override=7)
    tradingdatas_client = client(transport)
    snapshot = TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client).load_snapshot(
        profile=profile(tradingdatas_client),
        request=window_request(),
    )

    for symbol in ("BTCUSDT", "ETHUSDT"):
        proof = snapshot.proof_for(BAR_DATASETS[symbol])
        assert proof.page_count == 2
        assert proof.row_count == 13
    assert any(
        call["method"] == "POST" and call["json_body"].get("cursor") is not None
        for call in transport.calls
    )


def test_cursor_cycle_fails_closed_without_fallback() -> None:
    transport = FixtureTradingDatasTransport(
        page_size_override=7,
        force_cursor_cycle=True,
    )
    tradingdatas_client = client(transport)

    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="pagination_cursor_cycle",
    ):
        TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client).load_snapshot(
            profile=profile(tradingdatas_client),
            request=window_request(),
        )


def test_cross_page_missing_bar_fails_closed() -> None:
    rows = bar_rows()
    rows.pop(8)
    transport = FixtureTradingDatasTransport(
        bars=rows,
        page_size_override=7,
    )
    tradingdatas_client = client(transport)

    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="crypto_5m_window_incomplete",
    ):
        TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client).load_snapshot(
            profile=profile(tradingdatas_client),
            request=window_request(),
        )


def test_profile_rejects_catalog_drift_and_inactive_dataset() -> None:
    payload = catalog_payload()
    rows = payload["data"]
    assert isinstance(rows, list)
    target = next(row for row in rows if row["dataset_id"] == BAR_DATASETS["BTCUSDT"])
    target["availability"] = {"activation_states": ["inactive"]}
    transport = FixtureTradingDatasTransport(catalog_rows=rows)
    tradingdatas_client = client(transport)

    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="crypto_5m_dataset_not_active",
    ):
        profile(tradingdatas_client)


def test_profile_requires_explicit_queryable_catalog_binding() -> None:
    payload = catalog_payload()
    rows = payload["data"]
    assert isinstance(rows, list)
    target = next(row for row in rows if row["dataset_id"] == BAR_DATASETS["BTCUSDT"])
    target.pop("queryability")
    transport = FixtureTradingDatasTransport(catalog_rows=rows)

    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="crypto_5m_dataset_not_active",
    ):
        profile(client(transport))


def test_profile_separates_canonical_contract_from_runtime_and_consumer_binding() -> (
    None
):
    transport = FixtureTradingDatasTransport()
    frozen = profile(client(transport))
    bars = frozen.binding_for("BTCUSDT").bars

    payload = catalog_payload()
    rows = payload["data"]
    assert isinstance(rows, list)
    runtime_changed = copy.deepcopy(rows)
    target = next(
        row for row in runtime_changed if row["dataset_id"] == BAR_DATASETS["BTCUSDT"]
    )
    target["availability"] = {
        "entitlement_states": ["temporarily_degraded"],
        "activation_states": ["active"],
    }
    target["queryability"] = {"queryable": True, "reasons": ["runtime-only"]}
    runtime_profile = profile(
        client(FixtureTradingDatasTransport(catalog_rows=runtime_changed))
    )
    runtime_bars = runtime_profile.binding_for("BTCUSDT").bars

    assert runtime_bars.catalog_contract_sha256 == bars.catalog_contract_sha256
    assert runtime_bars.consumer_profile_sha256 == bars.consumer_profile_sha256
    assert (
        replace(bars, page_limit=12).consumer_profile_sha256
        != bars.consumer_profile_sha256
    )


@pytest.mark.parametrize(
    "dataset_id",
    (*BAR_DATASETS.values(), *RULE_DATASETS.values()),
)
def test_profile_fails_closed_when_canonical_contract_field_drifts(
    dataset_id: str,
) -> None:
    transport = FixtureTradingDatasTransport()
    frozen = profile(client(transport))
    payload = catalog_payload()
    rows = payload["data"]
    assert isinstance(rows, list)
    changed = copy.deepcopy(rows)
    target = next(row for row in changed if row["dataset_id"] == dataset_id)
    target["limits"]["max_lookback_days"] = 36501
    drifting_catalog = client(
        FixtureTradingDatasTransport(catalog_rows=changed)
    ).get_catalog()

    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="crypto_5m_catalog_contract_drift",
    ):
        frozen.verify_catalog(drifting_catalog)


def test_profile_accepts_additive_in_limit_when_consumer_uses_only_eq() -> None:
    frozen = profile(client(FixtureTradingDatasTransport()))
    payload = catalog_payload()
    rows = payload["data"]
    assert isinstance(rows, list)
    changed = copy.deepcopy(rows)
    for row in changed:
        row["limits"]["max_in_values"] = 100
    observed = client(FixtureTradingDatasTransport(catalog_rows=changed)).get_catalog()

    evidence = frozen.verify_catalog(observed)

    assert {
        item["catalog_contract_compatibility"]
        for item in evidence["dataset_contracts"]
    } == {"additive_max_in_values_unused"}


def test_profile_rejects_additive_in_limit_when_consumer_uses_in() -> None:
    frozen = profile(client(FixtureTradingDatasTransport()))
    bars = frozen.binding_for("BTCUSDT").bars
    in_filter = replace(bars.filter_bindings[0], operator="in")
    in_profile = replace(
        bars,
        filter_bindings=(in_filter, *bars.filter_bindings[1:]),
    )
    payload = catalog_payload()
    rows = payload["data"]
    assert isinstance(rows, list)
    changed = copy.deepcopy(rows)
    target = next(row for row in changed if row["dataset_id"] == bars.dataset_id)
    target["limits"]["max_in_values"] = 100
    observed = client(FixtureTradingDatasTransport(catalog_rows=changed)).get_catalog()

    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="crypto_5m_catalog_contract_drift",
    ):
        in_profile.verify_catalog(observed)


def test_profile_rejects_additive_in_limit_with_any_other_limit_drift() -> None:
    frozen = profile(client(FixtureTradingDatasTransport()))
    payload = catalog_payload()
    rows = payload["data"]
    assert isinstance(rows, list)
    changed = copy.deepcopy(rows)
    target = next(
        row for row in changed if row["dataset_id"] == BAR_DATASETS["BTCUSDT"]
    )
    target["limits"]["max_in_values"] = 100
    target["limits"]["max_lookback_days"] = 36501
    observed = client(FixtureTradingDatasTransport(catalog_rows=changed)).get_catalog()

    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="crypto_5m_catalog_contract_drift",
    ):
        frozen.verify_catalog(observed)


@pytest.mark.parametrize("mutation", ("missing", "duplicate"))
def test_profile_fails_closed_for_missing_or_duplicate_target_catalog_row(
    mutation: str,
) -> None:
    frozen = profile(client(FixtureTradingDatasTransport()))
    binding = frozen.binding_for("BTCUSDT").bars
    payload = catalog_payload()
    rows = payload["data"]
    assert isinstance(rows, list)
    target = next(row for row in rows if row["dataset_id"] == BAR_DATASETS["BTCUSDT"])
    if mutation == "missing":
        rows.remove(target)
    else:
        rows.append(copy.deepcopy(target))

    raw_catalog = CatalogEnvelope(
        api_version="v1",
        catalog_version="fixture-target-row-mutation-v1",
        request_id="fixture-target-row-mutation",
        data=tuple(rows),
    )
    with pytest.raises(
        CryptoFiveMinuteDataError,
        match="crypto_5m_dataset_catalog_row_missing",
    ):
        type(binding).from_catalog(
            raw_catalog,
            expected_catalog_version=binding.catalog_version,
            dataset_id=binding.dataset_id,
            expected_schema_major=binding.schema_major,
            selected_fields=binding.selected_fields,
            query_order=binding.query_order,
            identity_fields=binding.identity_fields,
            filter_bindings=binding.filter_bindings,
            page_limit=binding.page_limit,
            max_pages=binding.max_pages,
            max_rows=binding.max_rows,
        )


def test_profile_records_catalog_version_drift_without_blocking_unchanged_targets() -> (
    None
):
    frozen = profile(client(FixtureTradingDatasTransport()))
    observed_catalog = client(FixtureTradingDatasTransport()).get_catalog()
    changed_version = CatalogEnvelope(
        api_version=observed_catalog.api_version,
        catalog_version="fixture-unrelated-dataset-addition-v2",
        request_id=observed_catalog.request_id,
        data=observed_catalog.data,
    )

    evidence = frozen.verify_catalog(changed_version)

    assert evidence["expected_catalog_version"] == frozen.catalog_version
    assert evidence["observed_catalog_version"] == changed_version.catalog_version
    assert evidence["catalog_version_drift"] is True
