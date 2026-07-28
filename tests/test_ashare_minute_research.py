from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from Ashare.minute_data import (
    MinuteBarEvidence,
    MinuteBarSnapshot,
    MinuteDatasetProfile,
    MinuteTimestampSemantics,
)
from Ashare.minute_research import (
    MinuteContextObservation,
    MinuteResearchContractError,
    MinuteResearchUniverse,
    MinuteRollingFeatureEngine,
    MinuteUniverseInstrument,
    rank_minute_candidates,
)


def _sha(character: str) -> str:
    return character * 64


def _profile() -> MinuteDatasetProfile:
    fields = (
        "ts_code",
        "bar_time",
        "freq",
        "open",
        "high",
        "low",
        "close",
        "vol",
        "amount",
        "pre_close",
        "suspended",
    )
    return MinuteDatasetProfile(
        catalog_version="fixture-minute-catalog-v1",
        dataset_id="fixture.cn.equity.five_minute",
        schema_major=1,
        default_fields=fields,
        default_order=("ts_code:asc", "bar_time:asc"),
        filter_operators=tuple((field, ("eq",)) for field in fields),
        catalog_contract_sha256=_sha("a"),
        identity_fields=("ts_code", "bar_time"),
        symbol_field="ts_code",
        timestamp_field="bar_time",
        open_field="open",
        high_field="high",
        low_field="low",
        close_field="close",
        volume_field="vol",
        amount_field="amount",
        previous_close_field="pre_close",
        suspension_field="suspended",
        frequency_field="freq",
        frequency_value="5min",
        timestamp_format="%Y%m%d %H:%M:%S",
        timestamp_semantics=MinuteTimestampSemantics.BAR_END,
        volume_multiplier_to_shares=1.0,
        amount_multiplier_to_cny=1.0,
        price_adjustment="raw_unadjusted",
        max_pages=2,
        max_rows=10,
        page_limit=10,
    )


def _bar(
    end: str,
    *,
    symbol: str = "600000.SH",
    close: float = 10.0,
    volume: int = 100_000,
) -> MinuteBarEvidence:
    bar_end = datetime.fromisoformat(end)
    return MinuteBarEvidence(
        symbol=symbol,
        bar_start=bar_end - timedelta(minutes=5),
        bar_end=bar_end,
        open_cny=close - 0.05,
        high_cny=close + 0.1,
        low_cny=close - 0.1,
        close_cny=close,
        volume_shares=volume,
        amount_cny=volume * close,
        previous_close_cny=9.8,
        suspended=False,
        market_session=(
            "continuous_auction_am" if bar_end.hour < 12 else "continuous_auction_pm"
        ),
        dataset_id="fixture.cn.equity.five_minute",
        catalog_version="fixture-minute-catalog-v1",
        receipt_id=f"receipt-{symbol}-{end}",
        data_through=bar_end,
        observed_at=bar_end + timedelta(seconds=15),
        available_at=bar_end + timedelta(seconds=15),
        decision_time=bar_end + timedelta(seconds=20),
        source_lineage_sha256=_sha("b"),
        envelope_proof_sha256=_sha("c"),
        source_row_sha256=_sha("d"),
        reference_evidence_sha256=_sha("e"),
    )


def _snapshot(end: str, *, close: float, volume: int) -> MinuteBarSnapshot:
    bars = (
        _bar(end, symbol="600000.SH", close=close, volume=volume),
        _bar(end, symbol="000001.SZ", close=close * 1.01, volume=volume + 5_000),
    )
    return MinuteBarSnapshot(
        profile=_profile(),
        bars=bars,
        page_count=1,
        row_count=len(bars),
        pagination_trace_sha256=_sha("e"),
        first_semantic_sha256=_sha("f"),
        replay_semantic_sha256=_sha("f"),
        same_observation=True,
    )


def _universe() -> MinuteResearchUniverse:
    return MinuteResearchUniverse(
        instruments=(
            MinuteUniverseInstrument(
                symbol="600000.SH",
                name="AI infrastructure fixture",
                industry="electronics",
                research_theme="ai_semiconductor_infrastructure",
                list_date=date(1999, 11, 10),
            ),
            MinuteUniverseInstrument(
                symbol="000001.SZ",
                name="Robotics fixture",
                industry="automation",
                research_theme="robotics_industrial_automation",
                list_date=date(1991, 4, 3),
            ),
            MinuteUniverseInstrument(
                symbol="399006.SZ",
                name="ChiNext context",
                industry="broad market",
                research_theme="broad_market_control",
                list_date=None,
                context_only=True,
            ),
        )
    )


def test_rolling_features_need_two_consecutive_completed_bars() -> None:
    engine = MinuteRollingFeatureEngine()
    first = _snapshot("2026-07-27T09:35:00+08:00", close=10.0, volume=100_000)
    assert engine.ingest(first) == ()
    second = _snapshot("2026-07-27T09:40:00+08:00", close=10.1, volume=120_000)
    features = engine.ingest(second)
    assert len(features) == 2
    assert features[0].score_semantics == "uncalibrated_deterministic_rank_score"
    assert features[0].raw_rank_score > 0

    with pytest.raises(MinuteResearchContractError, match="nonconsecutive"):
        engine.ingest(
            _snapshot("2026-07-27T09:50:00+08:00", close=10.2, volume=130_000)
        )


def test_rank_output_cannot_masquerade_as_probability_or_execution_authority() -> None:
    engine = MinuteRollingFeatureEngine()
    engine.ingest(_snapshot("2026-07-27T09:35:00+08:00", close=10, volume=100_000))
    features = engine.ingest(
        _snapshot("2026-07-27T09:40:00+08:00", close=10.1, volume=120_000)
    )
    candidates = rank_minute_candidates(
        universe=_universe(),
        features=features,
        trade_date=date(2026, 7, 27),
    )
    assert len(candidates) == 2
    assert all(item.instrument.symbol != "399006.SZ" for item in candidates)
    forecast = candidates[0].forecast
    assert forecast.calibrated_probability is None
    assert forecast.expected_return_bps is None
    assert forecast.probability_model_state == "not_calibrated"
    assert forecast.promotion_eligible is False
    assert forecast.execution_authority is False


def test_mainboard_universe_excludes_st_new_and_delisting_names() -> None:
    engine = MinuteRollingFeatureEngine()
    engine.ingest(_snapshot("2026-07-27T09:35:00+08:00", close=10, volume=100_000))
    features = engine.ingest(
        _snapshot("2026-07-27T09:40:00+08:00", close=10.1, volume=120_000)
    )
    original = _universe().instruments
    universe = MinuteResearchUniverse(
        instruments=(
            MinuteUniverseInstrument(
                **{
                    **original["600000.SH"].__dict__,
                    "risk_warning": True,
                }
            ),
            MinuteUniverseInstrument(
                **{
                    **original["000001.SZ"].__dict__,
                    "list_date": date(2026, 7, 10),
                }
            ),
        )
    )
    candidates = rank_minute_candidates(
        universe=universe,
        features=features,
        trade_date=date(2026, 7, 27),
    )
    reasons = {item.instrument.symbol: item.reason_code for item in candidates}
    assert reasons == {
        "000001.SZ": "listed_less_than_30_days",
        "600000.SH": "risk_warning_excluded",
    }


def test_universe_scales_from_500_scan_to_full_mainboard_capacity() -> None:
    instruments = tuple(
        MinuteUniverseInstrument(
            symbol=f"{600000 + index:06d}.SH",
            name=f"Mainboard {index}",
            industry="generic",
            research_theme="mainboard_opportunity_scan",
            list_date=date(2000, 1, 1),
        )
        for index in range(501)
    )
    with pytest.raises(
        MinuteResearchContractError,
        match="monitor_limit_exceeded",
    ):
        MinuteResearchUniverse(instruments=instruments)
    expanded = MinuteResearchUniverse(instruments=instruments, expanded=True)
    assert len(expanded.trade_symbols) == 501
    assert expanded.initial_monitor_limit == 500
    assert expanded.expanded_monitor_limit == 6_000


def test_context_is_bounded_pit_evidence_and_never_a_candidate() -> None:
    engine = MinuteRollingFeatureEngine()
    engine.ingest(_snapshot("2026-07-27T09:35:00+08:00", close=10, volume=100_000))
    context = MinuteContextObservation(
        context_id="chinext-breadth-context",
        event_time=datetime.fromisoformat("2026-07-27T09:40:00+08:00"),
        available_at=datetime.fromisoformat("2026-07-27T09:40:10+08:00"),
        decision_time=datetime.fromisoformat("2026-07-27T09:40:20+08:00"),
        expires_at=datetime.fromisoformat("2026-07-27T09:45:00+08:00"),
        normalized_value=0.2,
        evidence_sha256=_sha("9"),
    )
    features = engine.ingest(
        _snapshot("2026-07-27T09:40:00+08:00", close=10.1, volume=120_000),
        contexts=(context,),
    )
    assert all(item.context_adjustment == 0.2 for item in features)
    assert "399006.SZ" not in {item.symbol for item in features}


def test_feature_state_restart_is_integrity_checked() -> None:
    engine = MinuteRollingFeatureEngine()
    engine.ingest(_snapshot("2026-07-27T09:35:00+08:00", close=10, volume=100_000))
    state = engine.export_state()
    restored = MinuteRollingFeatureEngine.restore(state)
    features = restored.ingest(
        _snapshot("2026-07-27T09:40:00+08:00", close=10.1, volume=120_000)
    )
    assert len(features) == 2
    state["real_trading_enabled"] = True
    with pytest.raises(MinuteResearchContractError, match="integrity"):
        MinuteRollingFeatureEngine.restore(state)
