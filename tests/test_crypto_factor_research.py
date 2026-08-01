from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from Crypto.factor_research import (
    CryptoFactorResearchError,
    build_cross_asset_features,
    build_factor_snapshot,
    build_forward_label,
    evaluate_factor_hypotheses,
)


START = datetime(2026, 8, 1, tzinfo=timezone.utc)
LINEAGE = "a" * 64


def _bars(
    *, start: datetime = START, multiplier: Decimal = Decimal("1")
) -> list[dict[str, str]]:
    result = []
    for index in range(13):
        close = (Decimal("100") + Decimal(index)) * multiplier
        result.append(
            {
                "open_time": (start + timedelta(minutes=5 * index))
                .isoformat()
                .replace("+00:00", "Z"),
                "open": str(close - Decimal("0.2")),
                "high": str(close + Decimal("0.5")),
                "low": str(close - Decimal("0.5")),
                "close": str(close),
                "base_volume": str(Decimal("10") + Decimal(index)),
                "quote_volume": str(close * (Decimal("10") + Decimal(index))),
            }
        )
    return result


def _evidence() -> dict[str, str]:
    return {
        "receipt_id": "receipt:factor-research-fixture",
        "lineage_sha256": LINEAGE,
        "data_through": "2026-08-01T01:04:59.999Z",
        "observed_at": "2026-08-01T01:05:01Z",
    }


def _snapshot() -> dict[str, object]:
    return build_factor_snapshot(
        observation_id="observation-factor-research-1",
        symbol="BTCUSDT",
        bars=_bars(),
        evidence=_evidence(),
    )


def _future_evidence() -> dict[str, str]:
    return {
        "receipt_id": "receipt:factor-research-future-fixture",
        "lineage_sha256": "b" * 64,
        "data_through": "2026-08-01T02:04:59.999Z",
        "observed_at": "2026-08-01T02:05:01Z",
    }


def test_snapshot_is_evidence_bound_and_non_authoritative() -> None:
    snapshot = _snapshot()

    assert snapshot["event_type"] == "factor_snapshot"
    assert snapshot["window_bars"] == 13
    assert snapshot["evidence_lineage_sha256"] == LINEAGE
    assert Decimal(snapshot["features"]["return_1h"]) > 0
    assert Decimal(snapshot["features"]["realized_volatility_1h"]) > 0
    assert snapshot["execution_authority"] is False
    assert snapshot["promotion_authorized"] is False
    assert snapshot["capital_commit_id"] is None


def test_snapshot_rejects_missing_bar_without_filling_the_gap() -> None:
    bars = _bars()
    bars.pop(5)

    with pytest.raises(
        CryptoFactorResearchError, match="factor_research_window_size_invalid"
    ):
        build_factor_snapshot(
            observation_id="observation-factor-research-gap",
            symbol="BTCUSDT",
            bars=bars,
            evidence=_evidence(),
        )


def test_snapshot_rejects_non_continuous_window() -> None:
    bars = _bars()
    bars[8]["open_time"] = "2026-08-01T00:45:00Z"

    with pytest.raises(
        CryptoFactorResearchError, match="factor_research_window_not_continuous"
    ):
        build_factor_snapshot(
            observation_id="observation-factor-research-gap",
            symbol="BTCUSDT",
            bars=bars,
            evidence=_evidence(),
        )


def test_snapshot_rejects_incomplete_ohlcv() -> None:
    bars = _bars()
    bars[-1].pop("quote_volume")

    with pytest.raises(
        CryptoFactorResearchError, match="factor_research_bar_12_quote_volume_invalid"
    ):
        build_factor_snapshot(
            observation_id="observation-factor-research-incomplete",
            symbol="BTCUSDT",
            bars=bars,
            evidence=_evidence(),
        )


def test_forward_label_requires_exact_future_horizon_and_costs_reduce_return() -> None:
    snapshot = _snapshot()
    future = "2026-08-01T02:00:00Z"
    label = build_forward_label(
        snapshot=snapshot,
        horizon_minutes=60,
        future_market_slot=future,
        entry_price="100",
        exit_price="101",
        future_evidence=_future_evidence(),
    )

    assert Decimal(label["gross_return"]) == Decimal("0.01")
    assert Decimal(label["net_return"]) < Decimal(label["gross_return"])
    assert label["execution_authority"] is False

    with pytest.raises(
        CryptoFactorResearchError, match="factor_research_label_not_causal"
    ):
        build_forward_label(
            snapshot=snapshot,
            horizon_minutes=60,
            future_market_slot="2026-08-01T00:55:00Z",
            entry_price="100",
            exit_price="101",
            future_evidence=_future_evidence(),
        )

    with pytest.raises(
        CryptoFactorResearchError, match="factor_research_future_evidence_invalid"
    ):
        build_forward_label(
            snapshot=snapshot,
            horizon_minutes=60,
            future_market_slot=future,
            entry_price="100",
            exit_price="101",
            future_evidence={
                **_future_evidence(),
                "observed_at": "2026-08-01T02:00:00Z",
            },
        )


def test_cross_asset_features_require_aligned_btc_and_eth_snapshots() -> None:
    btc = _snapshot()
    eth = build_factor_snapshot(
        observation_id="observation-factor-research-eth",
        symbol="ETHUSDT",
        bars=_bars(multiplier=Decimal("2")),
        evidence=_evidence(),
    )
    cross = build_cross_asset_features(snapshots=[btc, eth])

    assert cross["event_type"] == "cross_asset_factor_snapshot"
    assert "btc_minus_eth_1h" in cross["features"]
    assert cross["execution_authority"] is False

    misaligned_eth = build_factor_snapshot(
        observation_id="observation-factor-research-eth-later",
        symbol="ETHUSDT",
        bars=_bars(start=START + timedelta(minutes=5)),
        evidence={
            **_evidence(),
            "data_through": "2026-08-01T01:09:59.999Z",
            "observed_at": "2026-08-01T01:10:01Z",
        },
    )
    with pytest.raises(
        CryptoFactorResearchError, match="factor_research_cross_asset_window_invalid"
    ):
        build_cross_asset_features(snapshots=[btc, misaligned_eth])


def test_hypotheses_are_fixed_comparable_and_never_establish_edge() -> None:
    momentum_snapshot = _snapshot()
    pullback_bars = _bars()
    pullback_bars[-1]["close"] = "109"
    pullback_bars[-1]["high"] = "109.5"
    pullback_bars[-1]["low"] = "108.5"
    pullback_bars[-1]["open"] = "108.8"
    pullback_bars[-1]["quote_volume"] = "2398"
    pullback_snapshot = build_factor_snapshot(
        observation_id="observation-factor-research-2",
        symbol="ETHUSDT",
        bars=pullback_bars,
        evidence=_evidence(),
    )
    report = evaluate_factor_hypotheses(
        [
            (
                momentum_snapshot,
                build_forward_label(
                    snapshot=momentum_snapshot,
                    horizon_minutes=60,
                    future_market_slot="2026-08-01T02:00:00Z",
                    entry_price="100",
                    exit_price="101",
                    future_evidence=_future_evidence(),
                ),
            ),
            (
                pullback_snapshot,
                build_forward_label(
                    snapshot=pullback_snapshot,
                    horizon_minutes=60,
                    future_market_slot="2026-08-01T02:00:00Z",
                    entry_price="100",
                    exit_price="99",
                    future_evidence=_future_evidence(),
                ),
            ),
        ]
    )

    assert report["sample_count"] == 2
    assert [item["hypothesis_id"] for item in report["hypotheses"]] == [
        "time_series_momentum_v1",
        "trend_pullback_v1",
        "volume_breakout_v1",
    ]
    assert all(
        item["strategy_edge_established"] is False for item in report["hypotheses"]
    )
    assert report["automatic_champion_replacement"] is False


def test_hypothesis_rejects_unbound_label() -> None:
    snapshot = _snapshot()
    label = build_forward_label(
        snapshot=snapshot,
        horizon_minutes=60,
        future_market_slot="2026-08-01T02:00:00Z",
        entry_price="100",
        exit_price="101",
        future_evidence=_future_evidence(),
    )
    label["source_factor_snapshot_sha256"] = "b" * 64

    with pytest.raises(
        CryptoFactorResearchError, match="factor_research_label_binding_invalid"
    ):
        evaluate_factor_hypotheses([(snapshot, label)])


def test_tampered_feature_or_label_hash_fails_closed() -> None:
    snapshot = _snapshot()
    snapshot["features"]["return_1h"] = "999"
    with pytest.raises(
        CryptoFactorResearchError, match="factor_research_snapshot_invalid"
    ):
        build_forward_label(
            snapshot=snapshot,
            horizon_minutes=60,
            future_market_slot="2026-08-01T02:00:00Z",
            entry_price="100",
            exit_price="101",
            future_evidence=_future_evidence(),
        )

    valid_snapshot = _snapshot()
    label = build_forward_label(
        snapshot=valid_snapshot,
        horizon_minutes=60,
        future_market_slot="2026-08-01T02:00:00Z",
        entry_price="100",
        exit_price="101",
        future_evidence=_future_evidence(),
    )
    label["net_return"] = "999"
    with pytest.raises(
        CryptoFactorResearchError, match="factor_research_label_binding_invalid"
    ):
        evaluate_factor_hypotheses([(valid_snapshot, label)])
