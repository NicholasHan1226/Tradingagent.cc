"""Read-only, pre-registered Crypto factor research primitives.

This module deliberately has no runtime, transport, capital, order, Champion,
or promotion dependency.  It turns a validated 13-bar, 5-minute OHLCV window
into a versioned feature snapshot, later binds an observed forward outcome, and
compares three fixed research hypotheses.  It cannot decide or place a trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Mapping, Sequence


FACTOR_RESEARCH_CONTRACT = "tradingagent.crypto.factor_research.v1"
FACTOR_SET_ID = "crypto-5m-ohlcv-factor-research-v1"
FACTOR_SET_VERSION = 1
SUPPORTED_SYMBOLS = ("BTCUSDT", "ETHUSDT")
TEN_SYMBOL_FACTOR_SET_ID = "crypto-5m-ohlcv-factor-research-v2"
TEN_SYMBOL_FACTOR_SET_VERSION = 2
FORTY_SYMBOL_FACTOR_SET_ID = "crypto-5m-ohlcv-factor-research-v3"
FORTY_SYMBOL_FACTOR_SET_VERSION = 3
WINDOW_BARS = 13
BAR_INTERVAL = timedelta(minutes=5)
MINIMUM_SCREENING_LABELS = 50
FEE_RATE = Decimal("0.001")


class CryptoFactorResearchError(RuntimeError):
    """Stable fail-closed error for factor-research inputs or labels."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CryptoFactorResearchError("factor_research_payload_invalid") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _decimal(value: Any, *, field: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise CryptoFactorResearchError(f"factor_research_{field}_invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CryptoFactorResearchError(f"factor_research_{field}_invalid") from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise CryptoFactorResearchError(f"factor_research_{field}_invalid")
    return parsed


def _utc(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CryptoFactorResearchError(f"factor_research_{field}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CryptoFactorResearchError(f"factor_research_{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise CryptoFactorResearchError(f"factor_research_{field}_invalid")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _non_authority_fields() -> dict[str, Any]:
    return {
        "authority": "none",
        "research_only": True,
        "execution_eligible": False,
        "execution_authority": False,
        "durable_execution_receipt": False,
        "production_eligible": False,
        "real_trading_enabled": False,
        "network_used": False,
        "model_network_used": False,
        "testnet_used": False,
        "live_broker_used": False,
        "promotion_authorized": False,
        "automatic_champion_replacement": False,
        "automatic_risk_expansion_enabled": False,
        "outbox_id": None,
        "capital_commit_id": None,
    }


def _feature_set(
    feature_set_id: str | None,
    feature_set_version: int | None,
) -> tuple[str, int]:
    """Resolve the frozen feature-set identity; defaults stay v1-frozen."""

    resolved_id = FACTOR_SET_ID if feature_set_id is None else feature_set_id
    resolved_version = (
        FACTOR_SET_VERSION if feature_set_version is None else feature_set_version
    )
    if (
        not isinstance(resolved_id, str)
        or not resolved_id
        or isinstance(resolved_version, bool)
        or not isinstance(resolved_version, int)
        or resolved_version <= 0
    ):
        raise CryptoFactorResearchError("factor_research_feature_set_invalid")
    return resolved_id, resolved_version


def _assert_snapshot_integrity(
    snapshot: Mapping[str, Any],
    *,
    feature_set_id: str | None = None,
    feature_set_version: int | None = None,
) -> None:
    set_id, set_version = _feature_set(feature_set_id, feature_set_version)
    if (
        snapshot.get("contract") != FACTOR_RESEARCH_CONTRACT
        or snapshot.get("event_type") != "factor_snapshot"
        or snapshot.get("feature_set_id") != set_id
        or snapshot.get("feature_set_version") != set_version
    ):
        raise CryptoFactorResearchError("factor_research_snapshot_invalid")
    expected = snapshot.get("factor_snapshot_sha256")
    material = dict(snapshot)
    material.pop("factor_snapshot_sha256", None)
    if (
        not isinstance(expected, str)
        or len(expected) != 64
        or _sha256(material) != expected
    ):
        raise CryptoFactorResearchError("factor_research_snapshot_invalid")


def _assert_label_integrity(
    label: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
    feature_set_id: str | None = None,
    feature_set_version: int | None = None,
) -> None:
    set_id, set_version = _feature_set(feature_set_id, feature_set_version)
    expected = label.get("forward_label_sha256")
    material = dict(label)
    material.pop("forward_label_sha256", None)
    if (
        label.get("contract") != FACTOR_RESEARCH_CONTRACT
        or label.get("event_type") != "forward_return_label"
        or label.get("feature_set_id") != set_id
        or label.get("feature_set_version") != set_version
        or label.get("source_factor_snapshot_sha256")
        != snapshot.get("factor_snapshot_sha256")
        or not isinstance(expected, str)
        or len(expected) != 64
        or _sha256(material) != expected
    ):
        raise CryptoFactorResearchError("factor_research_label_invalid")


@dataclass(frozen=True)
class _Bar:
    open_time: datetime
    open: Decimal
    close: Decimal
    high: Decimal
    low: Decimal
    base_volume: Decimal
    quote_volume: Decimal


def _bar(value: Mapping[str, Any], *, index: int) -> _Bar:
    open_time = _utc(value.get("open_time"), field=f"bar_{index}_open_time")
    high = _decimal(value.get("high"), field=f"bar_{index}_high", positive=True)
    low = _decimal(value.get("low"), field=f"bar_{index}_low", positive=True)
    open_ = _decimal(value.get("open"), field=f"bar_{index}_open", positive=True)
    close = _decimal(value.get("close"), field=f"bar_{index}_close", positive=True)
    volume = _decimal(
        value.get("base_volume"), field=f"bar_{index}_base_volume", positive=True
    )
    quote_volume = _decimal(
        value.get("quote_volume"), field=f"bar_{index}_quote_volume", positive=True
    )
    if high < low or open_ < low or open_ > high or close < low or close > high:
        raise CryptoFactorResearchError("factor_research_bar_ohlc_invalid")
    return _Bar(
        open_time=open_time,
        open=open_,
        close=close,
        high=high,
        low=low,
        base_volume=volume,
        quote_volume=quote_volume,
    )


def _validated_bars(bars: Sequence[Mapping[str, Any]]) -> tuple[_Bar, ...]:
    if len(bars) != WINDOW_BARS:
        raise CryptoFactorResearchError("factor_research_window_size_invalid")
    result = tuple(_bar(value, index=index) for index, value in enumerate(bars))
    for previous, current in zip(result, result[1:]):
        if current.open_time - previous.open_time != BAR_INTERVAL:
            raise CryptoFactorResearchError("factor_research_window_not_continuous")
    return result


def _rms(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise CryptoFactorResearchError("factor_research_empty_return_window")
    return (sum(value * value for value in values) / Decimal(len(values))).sqrt()


def _volume_zscore(last: Decimal, baseline: Sequence[Decimal]) -> Decimal:
    if not baseline:
        raise CryptoFactorResearchError("factor_research_empty_volume_window")
    mean = sum(baseline) / Decimal(len(baseline))
    variance = sum((value - mean) ** 2 for value in baseline) / Decimal(len(baseline))
    deviation = variance.sqrt()
    return Decimal("0") if deviation == 0 else (last - mean) / deviation


def build_factor_snapshot(
    *,
    observation_id: str,
    symbol: str,
    bars: Sequence[Mapping[str, Any]],
    evidence: Mapping[str, Any],
    supported_symbols: Sequence[str] | None = None,
    feature_set_id: str | None = None,
    feature_set_version: int | None = None,
) -> dict[str, Any]:
    """Build a read-only feature snapshot from one evidence-bound 13-bar window.

    The keyword-only universe/feature-set parameters default to the frozen v1
    behavior; v1 callers are byte-identical.  The ten-symbol projection passes
    its frozen ten-symbol universe and v2 feature-set identity explicitly.
    """

    set_id, set_version = _feature_set(feature_set_id, feature_set_version)
    symbols = SUPPORTED_SYMBOLS if supported_symbols is None else supported_symbols
    if (
        isinstance(symbols, (str, bytes))
        or not symbols
        or any(not isinstance(item, str) or not item for item in symbols)
    ):
        raise CryptoFactorResearchError("factor_research_symbol_invalid")
    if not isinstance(observation_id, str) or not observation_id:
        raise CryptoFactorResearchError("factor_research_observation_id_invalid")
    if symbol not in symbols:
        raise CryptoFactorResearchError("factor_research_symbol_invalid")
    receipt_id = evidence.get("receipt_id")
    lineage_sha256 = evidence.get("lineage_sha256")
    observed_at = _utc(evidence.get("observed_at"), field="observed_at")
    data_through = _utc(evidence.get("data_through"), field="data_through")
    if (
        not isinstance(receipt_id, str)
        or not receipt_id
        or not isinstance(lineage_sha256, str)
        or len(lineage_sha256) != 64
        or observed_at < data_through
    ):
        raise CryptoFactorResearchError("factor_research_evidence_invalid")
    parsed = _validated_bars(bars)
    last_closed_at = parsed[-1].open_time + BAR_INTERVAL - timedelta(milliseconds=1)
    if data_through < last_closed_at:
        raise CryptoFactorResearchError("factor_research_evidence_before_window_close")
    returns = tuple(
        current.close / previous.close - Decimal("1")
        for previous, current in zip(parsed, parsed[1:])
    )
    last = parsed[-1]
    features = {
        "return_5m": _decimal_text(returns[-1]),
        "return_15m": _decimal_text(last.close / parsed[-4].close - Decimal("1")),
        "return_1h": _decimal_text(last.close / parsed[0].close - Decimal("1")),
        "realized_volatility_15m": _decimal_text(_rms(returns[-3:])),
        "realized_volatility_1h": _decimal_text(_rms(returns)),
        "normalized_range_5m": _decimal_text((last.high - last.low) / last.close),
        "base_volume_zscore_1h": _decimal_text(
            _volume_zscore(last.base_volume, [bar.base_volume for bar in parsed[:-1]])
        ),
    }
    snapshot = {
        "contract": FACTOR_RESEARCH_CONTRACT,
        "event_type": "factor_snapshot",
        "feature_set_id": set_id,
        "feature_set_version": set_version,
        "observation_id": observation_id,
        "symbol": symbol,
        "market_slot": _iso(last.open_time),
        "window_open": _iso(parsed[0].open_time),
        "window_bars": WINDOW_BARS,
        "evidence_receipt_id": receipt_id,
        "evidence_lineage_sha256": lineage_sha256,
        "observed_at": _iso(observed_at),
        "data_through": _iso(data_through),
        "features": features,
        **_non_authority_fields(),
    }
    snapshot["factor_snapshot_sha256"] = _sha256(snapshot)
    return snapshot


def build_forward_label(
    *,
    snapshot: Mapping[str, Any],
    horizon_minutes: int,
    future_market_slot: str,
    entry_price: Decimal | str,
    exit_price: Decimal | str,
    future_evidence: Mapping[str, Any],
    fee_rate: Decimal | str = FEE_RATE,
    feature_set_id: str | None = None,
    feature_set_version: int | None = None,
) -> dict[str, Any]:
    """Bind a later, cost-aware return label without altering a snapshot."""

    set_id, set_version = _feature_set(feature_set_id, feature_set_version)
    _assert_snapshot_integrity(
        snapshot,
        feature_set_id=set_id,
        feature_set_version=set_version,
    )
    snapshot_sha256 = snapshot.get("factor_snapshot_sha256")
    if not isinstance(horizon_minutes, int) or horizon_minutes not in {
        60,
        240,
        720,
        1440,
    }:
        raise CryptoFactorResearchError("factor_research_horizon_invalid")
    market_slot = _utc(snapshot.get("market_slot"), field="snapshot_market_slot")
    future_slot = _utc(future_market_slot, field="future_market_slot")
    if future_slot - market_slot != timedelta(minutes=horizon_minutes):
        raise CryptoFactorResearchError("factor_research_label_not_causal")
    future_receipt_id = future_evidence.get("receipt_id")
    future_lineage_sha256 = future_evidence.get("lineage_sha256")
    future_observed_at = _utc(
        future_evidence.get("observed_at"), field="future_observed_at"
    )
    future_data_through = _utc(
        future_evidence.get("data_through"), field="future_data_through"
    )
    future_closed_at = future_slot + BAR_INTERVAL - timedelta(milliseconds=1)
    if (
        not isinstance(future_receipt_id, str)
        or not future_receipt_id
        or not isinstance(future_lineage_sha256, str)
        or len(future_lineage_sha256) != 64
        or future_data_through < future_closed_at
        or future_observed_at < future_data_through
    ):
        raise CryptoFactorResearchError("factor_research_future_evidence_invalid")
    entry = _decimal(entry_price, field="entry_price", positive=True)
    exit_ = _decimal(exit_price, field="exit_price", positive=True)
    fee = _decimal(fee_rate, field="fee_rate")
    if fee < 0 or fee >= Decimal("1"):
        raise CryptoFactorResearchError("factor_research_fee_rate_invalid")
    gross_return = exit_ / entry - Decimal("1")
    net_return = exit_ * (Decimal("1") - fee) / (
        entry * (Decimal("1") + fee)
    ) - Decimal("1")
    label = {
        "contract": FACTOR_RESEARCH_CONTRACT,
        "event_type": "forward_return_label",
        "feature_set_id": set_id,
        "feature_set_version": set_version,
        "observation_id": snapshot.get("observation_id"),
        "symbol": snapshot.get("symbol"),
        "source_factor_snapshot_sha256": snapshot_sha256,
        "horizon_minutes": horizon_minutes,
        "future_market_slot": _iso(future_slot),
        "future_evidence_receipt_id": future_receipt_id,
        "future_evidence_lineage_sha256": future_lineage_sha256,
        "future_observed_at": _iso(future_observed_at),
        "future_data_through": _iso(future_data_through),
        "entry_price": _decimal_text(entry),
        "exit_price": _decimal_text(exit_),
        "fee_rate": _decimal_text(fee),
        "gross_return": _decimal_text(gross_return),
        "net_return": _decimal_text(net_return),
        "label_status": "observed_future_outcome",
        **_non_authority_fields(),
    }
    label["forward_label_sha256"] = _sha256(label)
    return label


def build_cross_asset_features(
    *,
    snapshots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Derive BTC-vs-ETH relative-strength features from aligned snapshots.

    This is not a cross-sectional factor score: the current universe has only
    two assets.  It is a dated, evidence-bound diagnostic for later research.
    """

    if len(snapshots) != 2:
        raise CryptoFactorResearchError("factor_research_cross_asset_count_invalid")
    by_symbol = {snapshot.get("symbol"): snapshot for snapshot in snapshots}
    if set(by_symbol) != set(SUPPORTED_SYMBOLS):
        raise CryptoFactorResearchError("factor_research_cross_asset_symbol_invalid")
    btc = by_symbol["BTCUSDT"]
    eth = by_symbol["ETHUSDT"]
    for snapshot in (btc, eth):
        try:
            _assert_snapshot_integrity(snapshot)
        except CryptoFactorResearchError as exc:
            raise CryptoFactorResearchError(
                "factor_research_cross_asset_snapshot_invalid"
            ) from exc
    if btc.get("market_slot") != eth.get("market_slot") or btc.get(
        "window_open"
    ) != eth.get("window_open"):
        raise CryptoFactorResearchError("factor_research_cross_asset_window_invalid")
    features = {
        f"btc_minus_eth_{horizon}": _decimal_text(
            _feature_decimal(btc, f"return_{horizon}")
            - _feature_decimal(eth, f"return_{horizon}")
        )
        for horizon in ("5m", "15m", "1h")
    }
    result = {
        "contract": FACTOR_RESEARCH_CONTRACT,
        "event_type": "cross_asset_factor_snapshot",
        "feature_set_id": FACTOR_SET_ID,
        "feature_set_version": FACTOR_SET_VERSION,
        "market_slot": btc["market_slot"],
        "window_open": btc["window_open"],
        "source_factor_snapshot_sha256": {
            "BTCUSDT": btc["factor_snapshot_sha256"],
            "ETHUSDT": eth["factor_snapshot_sha256"],
        },
        "features": features,
        **_non_authority_fields(),
    }
    result["cross_asset_factor_snapshot_sha256"] = _sha256(result)
    return result


def _feature_decimal(snapshot: Mapping[str, Any], field: str) -> Decimal:
    features = snapshot.get("features")
    if not isinstance(features, Mapping):
        raise CryptoFactorResearchError("factor_research_snapshot_invalid")
    return _decimal(features.get(field), field=field)


def _signal(hypothesis_id: str, snapshot: Mapping[str, Any]) -> bool:
    return_15m = _feature_decimal(snapshot, "return_15m")
    return_1h = _feature_decimal(snapshot, "return_1h")
    volume_zscore = _feature_decimal(snapshot, "base_volume_zscore_1h")
    if hypothesis_id == "time_series_momentum_v1":
        return return_1h >= 0 and return_15m >= Decimal("0.001")
    if hypothesis_id == "trend_pullback_v1":
        return return_1h > 0 and return_15m < 0 and volume_zscore >= 0
    if hypothesis_id == "volume_breakout_v1":
        return return_1h >= 0 and return_15m >= Decimal("0.001") and volume_zscore >= 1
    raise CryptoFactorResearchError("factor_research_hypothesis_invalid")


def evaluate_factor_hypotheses(
    samples: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    *,
    feature_set_id: str | None = None,
    feature_set_version: int | None = None,
) -> dict[str, Any]:
    """Compare fixed hypotheses on already observed, cost-aware labels only."""

    set_id, set_version = _feature_set(feature_set_id, feature_set_version)
    hypotheses = (
        "time_series_momentum_v1",
        "trend_pullback_v1",
        "volume_breakout_v1",
    )
    results: list[dict[str, Any]] = []
    for hypothesis_id in hypotheses:
        returns: list[Decimal] = []
        for snapshot, label in samples:
            _assert_snapshot_integrity(
                snapshot,
                feature_set_id=set_id,
                feature_set_version=set_version,
            )
            try:
                _assert_label_integrity(
                    label,
                    snapshot=snapshot,
                    feature_set_id=set_id,
                    feature_set_version=set_version,
                )
            except CryptoFactorResearchError as exc:
                raise CryptoFactorResearchError(
                    "factor_research_label_binding_invalid"
                ) from exc
            if _signal(hypothesis_id, snapshot):
                returns.append(_decimal(label.get("net_return"), field="net_return"))
        signal_count = len(returns)
        mean_return = sum(returns) / Decimal(signal_count) if returns else None
        results.append(
            {
                "hypothesis_id": hypothesis_id,
                "signal_count": signal_count,
                "net_positive_rate": (
                    _decimal_text(
                        Decimal(sum(value > 0 for value in returns))
                        / Decimal(signal_count)
                    )
                    if returns
                    else None
                ),
                "mean_net_return": _decimal_text(mean_return)
                if mean_return is not None
                else None,
                "screening_sample_minimum": MINIMUM_SCREENING_LABELS,
                "screening_sample_met": signal_count >= MINIMUM_SCREENING_LABELS,
                "strategy_edge_established": False,
                "manual_review_required": True,
            }
        )
    return {
        "contract": FACTOR_RESEARCH_CONTRACT,
        "event_type": "factor_hypothesis_report",
        "feature_set_id": set_id,
        "feature_set_version": set_version,
        "sample_count": len(samples),
        "hypotheses": results,
        "selection_authority": "none",
        "automatic_champion_replacement": False,
        "strategy_edge_established": False,
        **_non_authority_fields(),
    }


__all__ = [
    "BAR_INTERVAL",
    "FACTOR_RESEARCH_CONTRACT",
    "FACTOR_SET_ID",
    "FACTOR_SET_VERSION",
    "FEE_RATE",
    "MINIMUM_SCREENING_LABELS",
    "FORTY_SYMBOL_FACTOR_SET_ID",
    "FORTY_SYMBOL_FACTOR_SET_VERSION",
    "TEN_SYMBOL_FACTOR_SET_ID",
    "TEN_SYMBOL_FACTOR_SET_VERSION",
    "CryptoFactorResearchError",
    "build_cross_asset_features",
    "build_factor_snapshot",
    "build_forward_label",
    "evaluate_factor_hypotheses",
]
