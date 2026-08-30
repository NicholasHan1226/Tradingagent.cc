"""Tests for the detached ten-symbol factor strategy evaluation (v2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from Crypto.fixture_sim.contracts import CryptoSafetyError
from Crypto.market_observation import FIVE_MINUTES, OBSERVATION_SYMBOLS
from Crypto.round_trip_capital import (
    SLIPPAGE_BPS,
    TAKER_FEE_RATE,
    run_round_trip_fixture_cycle,
)
from Crypto.ten_symbol_factor_strategy_evaluation import (
    CHECKPOINT_FILENAME,
    CHAMPION_PROMOTION_DIRNAME,
    COST_ATTRIBUTION_DIRNAME,
    COST_ATTRIBUTION_CONTRACT,
    COST_POLICY_ID,
    EVALUATION_BUNDLE_CONTRACT,
    STRATEGY_EVALUATION_DIRNAME,
    CryptoTenSymbolFactorStrategyEvaluationError,
    run_ten_symbol_factor_strategy_evaluation,
    run_ten_symbol_factor_strategy_evaluation_fast,
)
from Crypto.ten_symbol_factor_research import (
    run_crypto_ten_symbol_factor_research_full_scrub,
)
from Crypto.ten_symbol_hypothesis_generator import (
    run_ten_symbol_hypothesis_generation_once,
)
from Crypto.ten_symbol_research_loop import run_ten_symbol_research_loop_once
from Crypto.ten_symbol_spread_projection import (
    CHECKPOINT_FILENAME as SPREAD_CHECKPOINT_FILENAME,
    run_crypto_ten_symbol_spread_projection,
)
from tests.test_crypto_ten_symbol_observation_runtime import (
    _factory,
    _run,
    _runtime_paths,
)
from tests.test_crypto_ten_symbol_observation_sidecar import (
    _assert_recursive_non_authority,
)
from tests.test_crypto_ten_symbol_support import (
    CATALOG_VERSION,
    WINDOW_END,
    TenSymbolFixtureTransport,
    generated_book_ticker_row,
    iso,
    query_metadata,
)
from shared.data.sharedsignals_v1 import HTTPResponse


TREND_EPOCH = WINDOW_END - timedelta(minutes=130)
FEE = TAKER_FEE_RATE
SLIP = SLIPPAGE_BPS / Decimal("10000")


class TrendingFixtureTransport(TenSymbolFixtureTransport):
    """Fixture variant with continuously rising closes across windows."""

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        if kwargs["method"] == "GET" or self.status_code != 200:
            return super().__call__(**kwargs)
        body = kwargs["json_body"]
        assert isinstance(body, dict)
        dataset_id = body["dataset_id"]
        between = body["filters"]["open_time"]["between"]
        first_open = datetime.fromisoformat(str(between[0]).replace("Z", "+00:00"))
        last_open = datetime.fromisoformat(str(between[1]).replace("Z", "+00:00"))
        symbol = str(body["filters"]["symbol"]["eq"])
        base = Decimal("100") + Decimal(OBSERVATION_SYMBOLS.index(symbol) * 10)
        rows: list[dict[str, Any]] = []
        for index in range(self.row_count):
            open_time = first_open + index * FIVE_MINUTES
            step = int((open_time - TREND_EPOCH).total_seconds() // 300)
            price = base + step
            rows.append(
                {
                    "symbol": symbol,
                    "open_time": iso(open_time),
                    "close_time": iso(
                        open_time + FIVE_MINUTES - timedelta(milliseconds=1)
                    ),
                    "open": format(price, "f"),
                    "high": format(price + 2, "f"),
                    "low": format(price - 1, "f"),
                    "close": format(price + 1, "f"),
                    "volume": "10",
                    "quote_volume": "1000",
                    "trade_count": 10 + index,
                }
            )
        window_end = last_open + FIVE_MINUTES
        observed_at = (
            self.observed_at
            if self.observed_at is not None
            else window_end + timedelta(seconds=20)
        )
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": CATALOG_VERSION,
                "request_id": f"fixture-query-{dataset_id}",
                "dataset_id": dataset_id,
                "data": rows[: int(body["limit"])],
                "next_cursor": None,
                "metadata": query_metadata(
                    dataset_id,
                    data_through=window_end - timedelta(milliseconds=1),
                    observed_at=observed_at,
                ),
            },
        )


def _accumulate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    count: int,
    *,
    transport_factory: Any = None,
) -> Path:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    for index in range(count):
        end = WINDOW_END + index * timedelta(minutes=5)
        transport = (
            transport_factory() if transport_factory is not None
            else TenSymbolFixtureTransport()
        )
        receipt = _run(
            tmp_path,
            token_file,
            output_root,
            now=end + timedelta(seconds=55),
            transport_factory=_factory(transport),
        )
        assert receipt["status"] == "completed"
    return output_root


def _scrub(root: Path) -> dict[str, Any]:
    result = run_crypto_ten_symbol_factor_research_full_scrub(output_root=root)
    assert result["status"] in {"recovered", "scrubbed"}
    return result


def _cost_adjusted(entry: Decimal, exit_: Decimal) -> Decimal:
    net = exit_ * (Decimal("1") - FEE) / (entry * (Decimal("1") + FEE)) - Decimal("1")
    return (Decimal("1") + net) * (Decimal("1") - SLIP) ** 2 - Decimal("1")


def _label_returns(root: Path) -> dict[str, list[Decimal]]:
    """Independently recompute per-source-slot cost-adjusted returns."""

    by_slot: dict[str, list[Decimal]] = {}
    labels_dir = root / "evolution" / "ten_symbol_factor_research" / "labels"
    for path in sorted(labels_dir.glob("*.json")):
        label = json.loads(path.read_text(encoding="utf-8"))
        if label["horizon_minutes"] != 60:
            continue
        value = _cost_adjusted(
            Decimal(label["entry_price"]), Decimal(label["exit_price"])
        )
        by_slot.setdefault(label["future_market_slot"], []).append(value)
    return by_slot


def _expected_mean(root: Path) -> Decimal:
    values = [value for values in _label_returns(root).values() for value in values]
    return sum(values, Decimal("0")) / Decimal(len(values))


def _artifact_dir(root: Path) -> Path:
    return (
        root
        / "evolution"
        / "ten_symbol_factor_research"
        / STRATEGY_EVALUATION_DIRNAME
    )


def test_evaluation_metrics_baselines_and_recommendation_branches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(
        monkeypatch, tmp_path, 14, transport_factory=TrendingFixtureTransport
    )
    _scrub(output_root)

    artifact = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)

    assert artifact["contract"] == EVALUATION_BUNDLE_CONTRACT
    assert artifact["status"] == "shadow_evaluated"
    assert artifact["resolved_count"] == 20
    assert len(artifact["last_evaluated_outcome_sha256"]) == 64
    _assert_recursive_non_authority(artifact)
    artifact_files = list(_artifact_dir(output_root).glob("*.json"))
    assert len(artifact_files) == 1
    on_disk = json.loads(artifact_files[0].read_text(encoding="utf-8"))
    assert on_disk["artifact_sha256"] == artifact["artifact_sha256"]
    checkpoint = json.loads(
        (
            output_root
            / "evolution"
            / "ten_symbol_factor_research"
            / CHECKPOINT_FILENAME
        ).read_text(encoding="utf-8")
    )
    assert checkpoint["last_evaluated_outcome_sha256"] == (
        artifact["last_evaluated_outcome_sha256"]
    )
    assert checkpoint["artifact_sha256"] == artifact["artifact_sha256"]
    _assert_recursive_non_authority(checkpoint)

    expected_mean = _expected_mean(output_root)
    assert artifact["horizon_status"] == {
        "60": "evaluated",
        "240": "insufficient_resolved_samples",
        "720": "insufficient_resolved_samples",
        "1440": "insufficient_resolved_samples",
    }
    assert artifact["resolved_count_by_horizon"] == {
        "60": 20,
        "240": 0,
        "720": 0,
        "1440": 0,
    }
    momentum = artifact["evaluations"]["60"]["momentum"]
    assert momentum["contract"] == (
        "tradingagent.crypto.ten_symbol_factor_strategy_evaluation.v2"
    )
    assert momentum["factor_hypothesis_id"] == "time_series_momentum_v1"
    assert momentum["feature_set_id"] == "crypto-5m-ohlcv-factor-research-v2"
    assert momentum["cost_policy_id"] == COST_POLICY_ID
    assert momentum["evaluated_status"] == "exploratory_insufficient_edge"
    assert momentum["recommendation"] == {
        "shadow_only_action": "retain_for_more_evidence",
        "parameter_suggestion": "no_automatic_parameter_change",
    }
    metrics = momentum["metrics"]
    assert metrics["resolved_count"] == 20
    assert metrics["signal_count"] == 20
    assert metrics["abstention_count"] == 0
    assert metrics["coverage"] == "1"
    assert metrics["hit_rate"] == "1"
    assert metrics["turnover"] == "1"
    assert metrics["round_trip_leg_rate"] == "2"
    assert metrics["drawdown"] == "0"
    assert Decimal(metrics["cost_adjusted_net_return"]) == expected_mean
    assert Decimal(metrics["baseline_delta"]) == 0
    assert Decimal(metrics["cash_baseline_delta"]) == expected_mean
    assert "overlap" in metrics["metric_basis"]
    assert "1/12" in metrics["metric_basis"]
    _assert_recursive_non_authority(momentum)

    baseline = momentum["baseline"]
    assert baseline["signal_count"] == 20
    assert Decimal(baseline["cost_adjusted_net_return"]) == expected_mean
    cash = momentum["cash_baseline"]
    assert cash["cost_adjusted_net_return"] == "0"
    assert cash["turnover"] == "0"
    assert cash["metric_basis"] == "cash_no_position"

    for name in ("trend", "volatility"):
        other = artifact["evaluations"]["60"][name]
        assert other["recommendation"]["shadow_only_action"] == "disable"
        assert other["metrics"]["signal_count"] == 0
        assert other["metrics"]["coverage"] == "0"
        assert other["metrics"]["cost_adjusted_net_return"] is None
        assert other["metrics"]["hit_rate"] is None
        assert other["metrics"]["drawdown"] == "0"
    assert artifact["recommendation"] == {
        "momentum": "retain_for_more_evidence",
        "trend": "disable",
        "volatility": "disable",
    }

    champion = artifact["champion"]
    assert champion is not None
    assert champion["champion_id"] == "momentum"
    assert champion["strategy_name"] == "momentum"
    assert champion["factor_hypothesis_id"] == "time_series_momentum_v1"
    assert champion["simulated_capital_authority_id"] == (
        "crypto-round-trip-capital-v1"
    )
    assert champion["simulated_capital_allocated"] is True
    assert champion["selection_rule"] == (
        "highest_positive_cost_adjusted_net_return"
    )
    assert len(champion["champion_sha256"]) == 64
    assert champion["automatic_champion_replacement"] is True
    assert champion["promotion_authorized"] is True
    assert champion["automatic_promotion_enabled"] is True
    assert champion["automatic_risk_expansion_enabled"] is False
    assert champion["real_trading_enabled"] is False
    # Top-level projection safety fields stay simulation-only.
    assert artifact["automatic_champion_replacement"] is False
    assert artifact["promotion_authorized"] is False
    assert len(artifact["champion_promotion_receipt_sha256"]) == 64
    receipt_path = (
        output_root
        / "evolution"
        / "ten_symbol_factor_research"
        / CHAMPION_PROMOTION_DIRNAME
        / f"{artifact['champion_promotion_receipt_sha256']}.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["contract"] == (
        "tradingagent.crypto.ten_symbol_champion_promotion_receipt.v1"
    )
    assert receipt["automatic_champion_replacement"] is True
    assert receipt["promotion_authorized"] is True
    assert receipt["automatic_risk_expansion_enabled"] is False
    assert receipt["real_trading_enabled"] is False
    assert receipt["authority"] == "none"
    assert receipt["champion"]["champion_id"] == "momentum"
    assert receipt["champion"]["simulated_capital_allocated"] is True
    _assert_recursive_non_authority(receipt)


def test_evaluation_downweights_non_positive_mean_with_exact_drawdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The default fixture repeats identical windows, so gross return is zero
    # and fees plus slippage push every cost-adjusted return below zero.
    output_root = _accumulate(monkeypatch, tmp_path, 14)
    _scrub(output_root)

    artifact = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)

    momentum = artifact["evaluations"]["60"]["momentum"]
    assert momentum["recommendation"]["shadow_only_action"] == "downweight"
    metrics = momentum["metrics"]
    assert metrics["signal_count"] == 20
    expected = _cost_adjusted(Decimal("1"), Decimal("1"))
    assert expected < 0
    assert Decimal(metrics["cost_adjusted_net_return"]) == expected
    equity = (Decimal("1") + expected) ** 2
    assert Decimal(metrics["drawdown"]) == Decimal("1") - equity
    assert metrics["hit_rate"] == "0"
    assert Decimal(metrics["cash_baseline_delta"]) == expected
    for name in ("trend", "volatility"):
        assert artifact["evaluations"]["60"][name]["recommendation"][
            "shadow_only_action"
        ] == "disable"


def test_evaluation_is_idempotent_per_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 14)
    _scrub(output_root)
    first = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)
    artifact_bytes = next(_artifact_dir(output_root).glob("*.json")).read_bytes()

    second = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)

    assert second["status"] == "no_new_outcome"
    assert second["last_evaluated_outcome_sha256"] == (
        first["last_evaluated_outcome_sha256"]
    )
    assert second["artifact_sha256"] == first["artifact_sha256"]
    assert next(_artifact_dir(output_root).glob("*.json")).read_bytes() == (
        artifact_bytes
    )


def test_evaluation_v2_preserves_legacy_v1_checkpoint_and_starts_own_lineage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 14)
    _scrub(output_root)
    evolution = output_root / "evolution" / "ten_symbol_factor_research"
    legacy_checkpoint = evolution / "strategy_evaluation_checkpoint.json"
    _write_canonical(
        legacy_checkpoint,
        {
            "contract": "tradingagent.crypto.ten_symbol_factor_strategy_evaluation_checkpoint.v1",
            "last_evaluated_completion_sha256": "0" * 64,
            "last_evaluated_outcome_sha256": "1" * 64,
            "artifact_sha256": "2" * 64,
        },
    )
    legacy_bytes = legacy_checkpoint.read_bytes()
    legacy_artifacts = {
        "strategy_evaluations": {"legacy": "strategy"},
        "strategy_evaluation_cost_attributions": {"legacy": "cost"},
        "champion_promotions": {"legacy": "promotion"},
    }
    legacy_artifact_bytes: dict[Path, bytes] = {}
    for directory_name, payload in legacy_artifacts.items():
        path = evolution / directory_name / ("0" * 64 + ".json")
        path.parent.mkdir(mode=0o700)
        _write_canonical(path, payload)
        legacy_artifact_bytes[path] = path.read_bytes()

    artifact = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)
    fast = run_ten_symbol_factor_strategy_evaluation_fast(store_root=output_root)

    assert artifact["status"] == "shadow_evaluated"
    assert legacy_checkpoint.read_bytes() == legacy_bytes
    assert {
        path: path.read_bytes() for path in legacy_artifact_bytes
    } == legacy_artifact_bytes
    assert (evolution / STRATEGY_EVALUATION_DIRNAME).is_dir()
    assert (evolution / COST_ATTRIBUTION_DIRNAME).is_dir()
    assert (evolution / CHAMPION_PROMOTION_DIRNAME).is_dir()
    checkpoint = json.loads((evolution / CHECKPOINT_FILENAME).read_text("utf-8"))
    assert checkpoint["contract"].endswith(".v2")
    assert fast["status"] == "no_new_outcome"
    assert fast["artifact_sha256"] == artifact["artifact_sha256"]


def test_evaluation_tracks_new_outcomes_after_each_scrub(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    for index in range(13):
        end = WINDOW_END + index * timedelta(minutes=5)
        receipt = _run(
            tmp_path,
            token_file,
            output_root,
            now=end + timedelta(seconds=55),
            transport_factory=_factory(TenSymbolFixtureTransport()),
        )
        assert receipt["status"] == "completed"
    _scrub(output_root)
    first = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)
    assert first["status"] == "shadow_evaluated"
    assert first["resolved_count"] == 10

    end = WINDOW_END + timedelta(minutes=65)
    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=end + timedelta(seconds=55),
        transport_factory=_factory(TenSymbolFixtureTransport()),
    )
    assert receipt["status"] == "completed"
    _scrub(output_root)
    second = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)
    assert second["status"] == "shadow_evaluated"
    assert second["resolved_count"] == 20
    assert second["last_evaluated_outcome_sha256"] != (
        first["last_evaluated_outcome_sha256"]
    )
    assert len(list(_artifact_dir(output_root).glob("*.json"))) == 2

    third = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)
    assert third["status"] == "no_new_outcome"


def test_evaluation_reports_insufficient_resolved_samples(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 2)
    _scrub(output_root)

    result = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)

    assert result["status"] == "insufficient_resolved_samples"
    assert result["resolved_count"] == 0
    assert not _artifact_dir(output_root).exists()

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.undo()
    token_file, fresh_root = _runtime_paths(monkeypatch, fresh)
    receipt = _run(
        fresh,
        token_file,
        fresh_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(TenSymbolFixtureTransport()),
    )
    assert receipt["status"] == "completed"
    early = run_ten_symbol_factor_strategy_evaluation(store_root=fresh_root)
    assert early["status"] == "insufficient_resolved_samples"


def test_evaluation_rejects_labels_beyond_evaluation_as_of(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 14)
    _scrub(output_root)

    with pytest.raises(
        CryptoTenSymbolFactorStrategyEvaluationError,
        match="evaluation_future_after_as_of",
    ):
        run_ten_symbol_factor_strategy_evaluation(
            store_root=output_root,
            evaluation_as_of=iso(WINDOW_END),
        )


@pytest.mark.parametrize(
    "tamper",
    ["label", "checkpoint", "record_sidecar_sha", "sidecar_file", "receipt"],
)
def test_evaluation_fails_closed_on_proof_tamper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tamper: str,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 14)
    _scrub(output_root)
    evolution = output_root / "evolution" / "ten_symbol_factor_research"

    if tamper == "label":
        target = next((evolution / "labels").glob("*.json"))
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["net_return"] = "999"
        target.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    elif tamper == "checkpoint":
        target = sorted((evolution / "checkpoints").glob("*.json"))[0]
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["observation_id"] = "tampered"
        target.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    elif tamper == "record_sidecar_sha":
        target = next((evolution / "records").glob("*.json"))
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["source_bars_sidecar_sha256"] = "0" * 64
        target.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    elif tamper == "sidecar_file":
        from Crypto.ten_symbol_observation_store import (
            CryptoTenSymbolObservationStore,
        )

        store = CryptoTenSymbolObservationStore(output_root)
        target = store.bars_sidecar_path(iso(WINDOW_END))
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["sources"][0]["rows"][0]["close"] = "999999"
        target.write_text(
            json.dumps(
                payload,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    elif tamper == "receipt":
        target = next((evolution / "receipts").glob("*.json"))
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["factor_projection_sha256"] = "0" * 64
        target.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(CryptoTenSymbolFactorStrategyEvaluationError):
        run_ten_symbol_factor_strategy_evaluation(store_root=output_root)


def test_fast_path_skips_before_first_scrub_then_reports_no_new_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 14)

    early = run_ten_symbol_factor_strategy_evaluation_fast(store_root=output_root)
    assert early["status"] == "no_evaluation_checkpoint"
    assert early["reason"] == "evaluation_checkpoint_missing_pre_first_scrub"
    _assert_recursive_non_authority(early)

    _scrub(output_root)
    # After a scrub but before the first evaluation there is still no
    # compact checkpoint; the fast path keeps skipping explicitly.
    pre = run_ten_symbol_factor_strategy_evaluation_fast(store_root=output_root)
    assert pre["status"] == "no_evaluation_checkpoint"

    evaluated = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)
    fast = run_ten_symbol_factor_strategy_evaluation_fast(store_root=output_root)
    assert fast["status"] == "no_new_outcome"
    assert fast["last_evaluated_outcome_sha256"] == (
        evaluated["last_evaluated_outcome_sha256"]
    )
    assert fast["artifact_sha256"] == evaluated["artifact_sha256"]
    _assert_recursive_non_authority(fast)


def test_evaluation_result_is_zero_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 14)
    _scrub(output_root)

    artifact = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)

    assert artifact["authority"] == "none"
    assert artifact["execution_authority"] is False
    assert artifact["real_trading_enabled"] is False
    assert artifact["promotion_authorized"] is False
    for horizon, evaluations in artifact["evaluations"].items():
        for name, item in evaluations.items():
            assert item["authority"] == "none"
            assert item["evaluation_sha256"]
            assert item["horizon_minutes"] == int(horizon)
            _assert_recursive_non_authority(item)


def test_aux_horizons_evaluated_with_exact_values_and_attribution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # 73 slots give 61 required-horizon and 25 4h-horizon source slots; the
    # flat fixture makes every cost-adjusted return the same known constant.
    output_root = _accumulate(monkeypatch, tmp_path, 73)
    _scrub(output_root)

    artifact = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)

    assert artifact["status"] == "shadow_evaluated"
    assert artifact["horizon_status"] == {
        "60": "evaluated",
        "240": "evaluated",
        "720": "insufficient_resolved_samples",
        "1440": "insufficient_resolved_samples",
    }
    assert artifact["resolved_count_by_horizon"] == {
        "60": 610,
        "240": 250,
        "720": 0,
        "1440": 0,
    }
    assert set(artifact["evaluations"]) == {"60", "240"}
    expected = _cost_adjusted(Decimal("1"), Decimal("1"))
    required = artifact["evaluations"]["60"]["momentum"]
    aux = artifact["evaluations"]["240"]["momentum"]
    assert required["research_attribution"] is False
    assert aux["research_attribution"] is True
    assert aux["horizon_minutes"] == 240
    assert aux["evaluated_status"] == "exploratory_insufficient_edge"
    assert Decimal(aux["metrics"]["cost_adjusted_net_return"]) == expected
    assert aux["metrics"]["signal_count"] == 250
    assert aux["metrics"]["hit_rate"] == "0"
    assert Decimal(aux["metrics"]["baseline_delta"]) == 0
    assert Decimal(aux["metrics"]["cash_baseline_delta"]) == expected
    assert aux["metrics"]["turnover"] == "1"
    assert aux["metrics"]["round_trip_leg_rate"] == "2"
    assert "47/48" in aux["metrics"]["metric_basis"]
    assert "1/48" in aux["metrics"]["metric_basis"]
    assert "11/12" in required["metrics"]["metric_basis"]
    assert "1/12" in required["metrics"]["metric_basis"]
    assert aux["baseline"]["metric_basis"] == aux["metrics"]["metric_basis"]
    _assert_recursive_non_authority(aux)
    # Recommendation stays scoped to the required 60min evaluations only.
    assert artifact["recommendation"] == {
        name: value["recommendation"]["shadow_only_action"]
        for name, value in artifact["evaluations"]["60"].items()
    }
    assert artifact["recommendation"]["momentum"] == "downweight"


def test_aux_samples_never_cross_a_segment_cut(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 73)
    # The unit at slot index 30 loses its sidecar and cuts the segment.
    from Crypto.ten_symbol_observation_store import (
        CryptoTenSymbolObservationStore,
    )

    store = CryptoTenSymbolObservationStore(output_root)
    store.bars_sidecar_path(iso(WINDOW_END + timedelta(minutes=150))).unlink()
    _scrub(output_root)

    artifact = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)

    # 60min: 18 same-segment slots before the cut plus 30 after it; 4h: every
    # source slot with a +48 target sits in the earlier segment while its
    # target sits in the later one, so no auxiliary sample resolves at all.
    assert artifact["resolved_count_by_horizon"] == {
        "60": 480,
        "240": 0,
        "720": 0,
        "1440": 0,
    }
    assert artifact["horizon_status"]["60"] == "evaluated"
    assert artifact["horizon_status"]["240"] == "insufficient_resolved_samples"
    assert artifact["recommendation"]["momentum"] == "downweight"


def test_all_four_horizons_evaluated_and_outcome_tracks_aux_growth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root_73 = _accumulate(monkeypatch, tmp_path / "a", 73)
    _scrub(root_73)
    first = run_ten_symbol_factor_strategy_evaluation(store_root=root_73)
    assert first["status"] == "shadow_evaluated"

    root_300 = _accumulate(monkeypatch, tmp_path / "b", 300)
    _scrub(root_300)

    artifact = run_ten_symbol_factor_strategy_evaluation(store_root=root_300)

    assert artifact["status"] == "shadow_evaluated"
    assert artifact["last_evaluated_outcome_sha256"] != (
        first["last_evaluated_outcome_sha256"]
    )
    assert artifact["horizon_status"] == {
        "60": "evaluated",
        "240": "evaluated",
        "720": "evaluated",
        "1440": "evaluated",
    }
    assert artifact["resolved_count_by_horizon"] == {
        "60": 2880,
        "240": 2520,
        "720": 1560,
        "1440": 120,
    }
    expected = _cost_adjusted(Decimal("1"), Decimal("1"))
    tolerance = Decimal("1e-24")
    for horizon, bars in (("720", 144), ("1440", 288)):
        aux = artifact["evaluations"][horizon]["momentum"]
        assert aux["research_attribution"] is True
        assert (
            abs(
                Decimal(aux["metrics"]["cost_adjusted_net_return"]) - expected
            )
            < tolerance
        )
        assert f"{bars - 1}/{bars}" in aux["metrics"]["metric_basis"]
        assert f"1/{bars}" in aux["metrics"]["metric_basis"]


# ---------------------------------------------------------------------------
# Measured-spread cost model consumption
# ---------------------------------------------------------------------------


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_canonical(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _accumulate_with_spreads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    count: int,
    *,
    book_ticker_metadata_mutator: Any = None,
) -> Path:
    # Pin every leg's observed_at inside each slot's watermark window so the
    # spread leg is sampled (not rejected) at every accumulated slot.
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    for index in range(count):
        end = WINDOW_END + index * timedelta(minutes=5)
        receipt = _run(
            tmp_path,
            token_file,
            output_root,
            now=end + timedelta(seconds=55),
            transport_factory=_factory(
                TenSymbolFixtureTransport(
                    observed_at=end + timedelta(seconds=20),
                    book_ticker_metadata_mutator=book_ticker_metadata_mutator,
                )
            ),
        )
        assert receipt["status"] == "completed"
    return output_root


def _expected_measured_half_spread_bps(symbol: str) -> Decimal:
    row = generated_book_ticker_row(symbol)
    bid = Decimal(row["bid_price"])
    ask = Decimal(row["ask_price"])
    spread_bps = (
        (ask - bid) / ((ask + bid) / Decimal(2)) * Decimal(10000)
    ).quantize(Decimal("0.00000001"))
    return spread_bps / Decimal(2)


def _measured_cost_adjusted(symbol: str) -> Decimal:
    # Flat fixture: entry == exit, so the label net is fee-only; the
    # evaluation then applies the measured half-spread per side.
    net = (Decimal("1") - FEE) / (Decimal("1") + FEE) - Decimal("1")
    slip = _expected_measured_half_spread_bps(symbol) / Decimal("10000")
    return (Decimal("1") + net) * (Decimal("1") - slip) ** 2 - Decimal("1")


def _attribution_dir(root: Path) -> Path:
    return (
        root
        / "evolution"
        / "ten_symbol_factor_research"
        / COST_ATTRIBUTION_DIRNAME
    )


def _attribution(root: Path, outcome: str) -> dict[str, Any]:
    return json.loads(
        (_attribution_dir(root) / f"{outcome}.json").read_text(encoding="utf-8")
    )


def test_evaluation_substitutes_measured_half_spread_costs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate_with_spreads(monkeypatch, tmp_path, 14)
    _scrub(output_root)
    spread = run_crypto_ten_symbol_spread_projection(output_root=output_root)
    assert spread["status"] == "projected"

    artifact = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)

    assert artifact["status"] == "shadow_evaluated"
    cost_model = artifact["cost_model"]
    assert cost_model["cost_policy_id"] == COST_POLICY_ID
    assert cost_model["fee_rate"] == "0.001"
    assert cost_model["assumed_slippage_bps_each_side"] == "2"
    assert cost_model["spread_quantile"] == "p75_bps"
    assert cost_model["min_spread_samples_per_bucket"] == 12
    assert cost_model["spread_artifact"] == {
        "outcome_sha256": spread["outcome_sha256"],
        "artifact_sha256": spread["artifact_sha256"],
        "projected_through_slot": spread["projected_through_slot"],
    }
    assert len(artifact["cost_attribution_sha256"]) == 64
    _assert_recursive_non_authority(artifact)

    momentum = artifact["evaluations"]["60"]["momentum"]
    assert momentum["cost_model"]["spread_artifact"]["outcome_sha256"] == (
        spread["outcome_sha256"]
    )
    assert momentum["metrics"]["cost_source_counts"] == {
        "measured": 20,
        "assumed": 0,
    }
    assert momentum["baseline"]["cost_source_counts"] == {
        "measured": 20,
        "assumed": 0,
    }
    expected_mean = sum(
        (_measured_cost_adjusted(symbol) for symbol in OBSERVATION_SYMBOLS),
        Decimal("0"),
    ) / Decimal(len(OBSERVATION_SYMBOLS))
    assert (
        abs(
            Decimal(momentum["metrics"]["cost_adjusted_net_return"])
            - expected_mean
        )
        < Decimal("1e-24")
    )
    # The fixture's measured half-spread (~5-9 bps/side) exceeds the assumed
    # 2 bps, so measured-cost evaluation is strictly more conservative.
    assert expected_mean < _cost_adjusted(Decimal("1"), Decimal("1"))

    attribution = _attribution(
        output_root, artifact["last_evaluated_outcome_sha256"]
    )
    assert attribution["contract"] == COST_ATTRIBUTION_CONTRACT
    assert attribution["unit_count"] == 20
    assert attribution["measured_unit_count"] == 20
    assert attribution["assumed_unit_count"] == 0
    material = dict(attribution)
    claimed = material.pop("cost_attribution_sha256")
    assert claimed == artifact["cost_attribution_sha256"]
    assert claimed == _canonical_sha256(material)
    _assert_recursive_non_authority(attribution)
    day = WINDOW_END.date().isoformat()
    assert [unit["market_slot"] for unit in attribution["units"]] == sorted(
        unit["market_slot"] for unit in attribution["units"]
    )
    for unit in attribution["units"]:
        assert unit["horizon_minutes"] == [60]
        assert unit["cost_source"] == "measured"
        assert unit["spread_bucket_day"] == day
        assert unit["spread_sample_count"] == 14
        assert unit["slippage_bps_each_side"] == format(
            _expected_measured_half_spread_bps(unit["symbol"]), "f"
        )


def test_evaluation_marks_insufficient_spread_units_assumed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def go_stale(dataset_id: str, metadata: dict[str, Any]) -> None:
        if dataset_id.endswith("ethusdt.book_ticker"):
            metadata["freshness"] = {"state": "stale", "stale": True}

    output_root = _accumulate_with_spreads(
        monkeypatch, tmp_path, 14, book_ticker_metadata_mutator=go_stale
    )
    _scrub(output_root)
    spread = run_crypto_ten_symbol_spread_projection(output_root=output_root)
    assert spread["status"] == "projected"

    artifact = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)

    momentum = artifact["evaluations"]["60"]["momentum"]
    assert momentum["metrics"]["cost_source_counts"] == {
        "measured": 18,
        "assumed": 2,
    }
    attribution = _attribution(
        output_root, artifact["last_evaluated_outcome_sha256"]
    )
    assert attribution["measured_unit_count"] == 18
    assert attribution["assumed_unit_count"] == 2
    eth_units = [
        unit for unit in attribution["units"] if unit["symbol"] == "ETHUSDT"
    ]
    assert len(eth_units) == 2
    for unit in eth_units:
        assert unit["cost_source"] == "assumed"
        assert unit["slippage_bps_each_side"] == "2"
        assert unit["spread_bucket_day"] is None
        assert unit["spread_sample_count"] is None
    for unit in attribution["units"]:
        if unit["symbol"] != "ETHUSDT":
            assert unit["cost_source"] == "measured"


def test_evaluation_never_extrapolates_thin_day_buckets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Only the first three slots sample spreads, so every symbol's day bucket
    # has 3 samples (< 12): the artifact loads, but every unit keeps the
    # assumed cost instead of extrapolating from a thin bucket.
    cutoff = iso(WINDOW_END + timedelta(minutes=15))

    def stale_after_cutoff(dataset_id: str, metadata: dict[str, Any]) -> None:
        if metadata["observed_at"] >= cutoff:
            metadata["freshness"] = {"state": "stale", "stale": True}

    output_root = _accumulate_with_spreads(
        monkeypatch, tmp_path, 14, book_ticker_metadata_mutator=stale_after_cutoff
    )
    _scrub(output_root)
    spread = run_crypto_ten_symbol_spread_projection(output_root=output_root)
    assert spread["status"] == "projected"
    assert spread["sampled_entry_count"] == 30

    artifact = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)

    assert artifact["cost_model"]["spread_artifact"] is not None
    momentum = artifact["evaluations"]["60"]["momentum"]
    assert momentum["metrics"]["cost_source_counts"] == {
        "measured": 0,
        "assumed": 20,
    }
    assert Decimal(momentum["metrics"]["cost_adjusted_net_return"]) == (
        _cost_adjusted(Decimal("1"), Decimal("1"))
    )
    attribution = _attribution(
        output_root, artifact["last_evaluated_outcome_sha256"]
    )
    assert attribution["assumed_unit_count"] == 20
    assert all(
        unit["cost_source"] == "assumed" and unit["spread_bucket_day"] is None
        for unit in attribution["units"]
    )


def test_evaluation_without_spread_artifact_marks_all_units_assumed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate_with_spreads(monkeypatch, tmp_path, 14)
    _scrub(output_root)

    artifact = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)

    assert artifact["cost_model"]["spread_artifact"] is None
    momentum = artifact["evaluations"]["60"]["momentum"]
    assert momentum["metrics"]["cost_source_counts"] == {
        "measured": 0,
        "assumed": 20,
    }
    assert Decimal(momentum["metrics"]["cost_adjusted_net_return"]) == (
        _cost_adjusted(Decimal("1"), Decimal("1"))
    )
    attribution = _attribution(
        output_root, artifact["last_evaluated_outcome_sha256"]
    )
    assert attribution["measured_unit_count"] == 0
    assert attribution["assumed_unit_count"] == 20
    for unit in attribution["units"]:
        assert unit["cost_source"] == "assumed"
        assert unit["slippage_bps_each_side"] == "2"
        assert unit["spread_bucket_day"] is None


def test_evaluation_with_empty_spread_namespace_stays_assumed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate_with_spreads(monkeypatch, tmp_path, 14)
    _scrub(output_root)
    # The projection namespace exists but never produced a checkpoint.
    (output_root / "evolution" / "ten_symbol_spread_projection").mkdir()

    artifact = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)

    assert artifact["status"] == "shadow_evaluated"
    assert artifact["cost_model"]["spread_artifact"] is None
    assert artifact["evaluations"]["60"]["momentum"]["metrics"][
        "cost_source_counts"
    ] == {"measured": 0, "assumed": 20}


@pytest.mark.parametrize(
    "tamper",
    ["checkpoint", "artifact_corrupt", "artifact_missing", "metric_drift"],
)
def test_evaluation_fails_closed_on_spread_artifact_tamper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tamper: str,
) -> None:
    output_root = _accumulate_with_spreads(monkeypatch, tmp_path, 14)
    _scrub(output_root)
    spread = run_crypto_ten_symbol_spread_projection(output_root=output_root)
    assert spread["status"] == "projected"
    evolution = output_root / "evolution" / "ten_symbol_spread_projection"
    checkpoint_path = evolution / SPREAD_CHECKPOINT_FILENAME
    artifact_path = evolution / "artifacts" / f"{spread['outcome_sha256']}.json"

    if tamper == "checkpoint":
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        payload["artifact_sha256"] = "0" * 64
        _write_canonical(checkpoint_path, payload)
    elif tamper == "artifact_corrupt":
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        payload["totals"]["sample_count"] = 999
        artifact_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    elif tamper == "artifact_missing":
        artifact_path.unlink()
    elif tamper == "metric_drift":
        # A self-consistent forgery: both digests are recomputed, so only
        # the consumer-side metric-contract check can reject it.
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact["spread_metric"]["quantile_method"] = "nearest_rank"
        material = dict(artifact)
        artifact["artifact_sha256"] = _canonical_sha256(material)
        _write_canonical(artifact_path, artifact)
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint["artifact_sha256"] = artifact["artifact_sha256"]
        material = dict(checkpoint)
        claimed = material.pop("checkpoint_sha256")
        assert claimed != _canonical_sha256(material)
        checkpoint["checkpoint_sha256"] = _canonical_sha256(material)
        _write_canonical(checkpoint_path, checkpoint)

    with pytest.raises(
        CryptoTenSymbolFactorStrategyEvaluationError,
        match="evaluation_spread_artifact_invalid",
    ):
        run_ten_symbol_factor_strategy_evaluation(store_root=output_root)
    assert not _attribution_dir(output_root).exists()


def test_evaluation_outcome_tracks_spread_artifact_updates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate_with_spreads(monkeypatch, tmp_path, 14)
    _scrub(output_root)
    run_crypto_ten_symbol_spread_projection(output_root=output_root)
    first = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)
    assert first["status"] == "shadow_evaluated"
    outcome = first["last_evaluated_outcome_sha256"]
    bundle_bytes = (_artifact_dir(output_root) / f"{outcome}.json").read_bytes()
    attribution_bytes = (
        _attribution_dir(output_root) / f"{outcome}.json"
    ).read_bytes()

    rerun = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)
    assert rerun["status"] == "no_new_outcome"
    assert (_artifact_dir(output_root) / f"{outcome}.json").read_bytes() == (
        bundle_bytes
    )
    assert (_attribution_dir(output_root) / f"{outcome}.json").read_bytes() == (
        attribution_bytes
    )

    # One more observation slot: the resolved sample inventory is unchanged
    # (no scrub), but new measured spread evidence must re-evaluate.
    token_file = tmp_path / "tradingdatas-crypto-read.token"
    end = WINDOW_END + timedelta(minutes=70)
    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=end + timedelta(seconds=55),
        transport_factory=_factory(
            TenSymbolFixtureTransport(observed_at=end + timedelta(seconds=20))
        ),
    )
    assert receipt["status"] == "completed"
    second_spread = run_crypto_ten_symbol_spread_projection(output_root=output_root)
    assert second_spread["status"] == "projected"

    second = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)

    assert second["status"] == "shadow_evaluated"
    assert second["last_evaluated_outcome_sha256"] != outcome
    assert second["cost_model"]["spread_artifact"]["outcome_sha256"] == (
        second_spread["outcome_sha256"]
    )
    second_attribution = _attribution(
        output_root, second["last_evaluated_outcome_sha256"]
    )
    assert second_attribution["measured_unit_count"] == 20
    assert all(
        unit["spread_sample_count"] == 15
        for unit in second_attribution["units"]
    )

    third = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)
    assert third["status"] == "no_new_outcome"
    assert third["artifact_sha256"] == second["artifact_sha256"]


def test_auto_selected_champion_allocates_simulated_capital(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(
        monkeypatch, tmp_path, 14, transport_factory=TrendingFixtureTransport
    )
    _scrub(output_root)

    artifact = run_ten_symbol_factor_strategy_evaluation(store_root=output_root)
    champion = artifact["champion"]
    assert champion is not None

    payload = {
        "fixture_id": "auto-champion-fixture",
        "symbol": "BTCUSDT",
        "execution_slot": iso(WINDOW_END),
        "decision": {
            "action": "buy",
            "regime_return": "0.01",
            "decision_return": "0.01",
            "decision_id": "auto-champion-decision",
        },
        "quote": {"bid": "99999.99", "ask": "100000.01"},
        "instrument": {
            "price_tick": "0.01",
            "quantity_step": "0.00001",
            "min_quantity": "0.00001",
            "min_notional": "5",
        },
        "evidence_receipt_id": "auto-champion-receipt",
        "market_evidence_sha256": "a" * 64,
        "champion_id": champion["champion_id"],
        "champion_sha256": champion["champion_sha256"],
    }

    result = run_round_trip_fixture_cycle(payload, output_root=tmp_path)

    assert result["order"]["side"] == "buy"
    assert result["order"]["champion_id"] == champion["champion_id"]
    assert result["order"]["champion_sha256"] == champion["champion_sha256"]
    assert result["receipt"]["status"] == "fixture_simulated"
    assert "BTCUSDT" in result["capital"]["positions"]
    assert result["capital"]["real_trading_enabled"] is False
    assert result["capital"]["execution_authority"] is False


@pytest.mark.parametrize("runner", ["generator", "research_loop", "evaluation"])
def test_automatic_promotion_fails_closed_when_real_trading_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner: str,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 14)
    _scrub(output_root)
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")

    with pytest.raises(CryptoSafetyError, match="real_trading_enabled_must_be_false"):
        if runner == "generator":
            run_ten_symbol_hypothesis_generation_once(store_root=output_root)
        elif runner == "research_loop":
            run_ten_symbol_research_loop_once(store_root=output_root)
        else:
            run_ten_symbol_factor_strategy_evaluation(store_root=output_root)
