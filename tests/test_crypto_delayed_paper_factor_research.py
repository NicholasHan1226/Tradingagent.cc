from __future__ import annotations

import copy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import Crypto.delayed_paper_factor_research as research
from Crypto.delayed_paper_factor_research import (
    CryptoFactorProjectionError,
    factor_projection_exit_code,
    run_crypto_delayed_paper_factor_research_full_scrub,
    run_crypto_delayed_paper_factor_research_incremental,
)
from Crypto.delayed_paper_round_trip import run_crypto_delayed_paper_round_trip_once
from Crypto.delayed_paper_factor_research_worker import _validated_manifest_path
from Crypto.five_minute_data import TradingDatasCryptoFiveMinuteDataPort
from tests.test_crypto_5m_support import (
    FixtureTradingDatasTransport,
    client,
    profile,
    window_request,
)


def _complete(root: Path) -> None:
    transport = FixtureTradingDatasTransport()
    tradingdatas_client = client(transport)
    result = run_crypto_delayed_paper_round_trip_once(
        port=TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client),
        profile=profile(tradingdatas_client),
        request=window_request(),
        output_root=root,
    )
    assert result["status"] == "completed"


def _core_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "evolution/factor_research" not in path.as_posix()
    }


def test_factor_research_projects_a_complete_segment_before_24h(
    tmp_path: Path,
) -> None:
    _complete(tmp_path)
    before = _core_bytes(tmp_path)

    result = run_crypto_delayed_paper_factor_research_full_scrub(output_root=tmp_path)

    assert result["status"] == "recovered"
    assert result["latest_continuous_completion_count"] == 1
    assert result["operational_maturity"] is False
    assert result["segmented_learning_policy"]["gap_crossing_allowed"] is False
    assert (
        result["segmented_learning_policy"]["minimum_slots_source"]
        == "preregistered_feature_and_label_profile"
    )
    assert result["segmented_learning_profile"]["consumer_profile_id"] == (
        "crypto-5m-ohlcv-13bar-forward-labels-v1"
    )
    assert result["segmented_learning_profile"]["required_label_horizon_minutes"] == 60
    assert result["label_learning_eligible_sample_count"] == 0
    assert result["execution_authority"] is False
    assert factor_projection_exit_code(result) == 0
    assert _core_bytes(tmp_path) == before
    assert (tmp_path / "evolution" / "factor_research").is_dir()


def test_segmented_learning_rejects_an_unregistered_minimum_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = research.load_evidence_readiness_contract()
    market_policies = copy.deepcopy(contract.market_policies)
    market_policies["crypto"]["segmented_learning"]["minimum_slots_source"] = (
        "runtime_completion_count"
    )
    monkeypatch.setattr(
        research,
        "load_evidence_readiness_contract",
        lambda: SimpleNamespace(
            contract_id=contract.contract_id,
            safety=contract.safety,
            market_policies=market_policies,
        ),
    )

    with pytest.raises(
        CryptoFactorProjectionError,
        match="factor_projection_readiness_contract_invalid",
    ):
        research._segmented_learning_policy()


def test_full_scrub_is_idempotent_and_does_not_mutate_core(
    tmp_path: Path,
) -> None:
    _complete(tmp_path)
    before = _core_bytes(tmp_path)

    first = run_crypto_delayed_paper_factor_research_full_scrub(output_root=tmp_path)
    root = tmp_path / "evolution" / "factor_research"
    after_first = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    second = run_crypto_delayed_paper_factor_research_full_scrub(output_root=tmp_path)
    after_second = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }

    assert first["status"] == "recovered"
    assert second["status"] == "scrubbed"
    assert first["label_count"] == 0
    assert after_first == after_second
    assert _core_bytes(tmp_path) == before
    assert len(list((root / "records").glob("*.json"))) == 1
    assert len(list((root / "receipts").glob("*.json"))) == 1


def test_full_scrub_can_separate_immutable_input_and_private_output_roots(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    _complete(input_root)
    before = _core_bytes(input_root)

    first = run_crypto_delayed_paper_factor_research_full_scrub(
        input_root=input_root, output_root=output_root
    )
    second = run_crypto_delayed_paper_factor_research_full_scrub(
        input_root=input_root, output_root=output_root
    )

    assert first["status"] == "recovered"
    assert second["status"] == "scrubbed"
    assert first["completion_count"] == second["completion_count"] == 1
    assert first["label_count"] == second["label_count"] == 0
    assert _core_bytes(input_root) == before
    assert (output_root / "evolution" / "factor_research").is_dir()


def test_incremental_requires_a_full_scrub_then_is_idempotently_up_to_date(
    tmp_path: Path,
) -> None:
    _complete(tmp_path)
    before = _core_bytes(tmp_path)

    deferred = run_crypto_delayed_paper_factor_research_incremental(
        output_root=tmp_path
    )
    assert deferred["status"] == "full_scrub_required"
    assert deferred["label_count"] == 0
    assert _core_bytes(tmp_path) == before

    run_crypto_delayed_paper_factor_research_full_scrub(output_root=tmp_path)
    after_scrub = _core_bytes(tmp_path)
    result = run_crypto_delayed_paper_factor_research_incremental(output_root=tmp_path)

    assert result["status"] == "up_to_date"
    assert result["label_count"] == 0
    assert factor_projection_exit_code(result) == 0
    assert _core_bytes(tmp_path) == after_scrub


def test_full_scrub_fails_closed_for_tampered_factor_record(
    tmp_path: Path,
) -> None:
    _complete(tmp_path)
    run_crypto_delayed_paper_factor_research_full_scrub(output_root=tmp_path)
    record = next(
        (tmp_path / "evolution" / "factor_research" / "records").glob("*.json")
    )
    record.write_text("{}\n", encoding="utf-8")

    with pytest.raises(
        CryptoFactorProjectionError,
        match="factor_projection_record_invalid|factor_projection_not_derived",
    ):
        run_crypto_delayed_paper_factor_research_full_scrub(output_root=tmp_path)


def test_full_scrub_fails_closed_for_missing_claimed_receipt(
    tmp_path: Path,
) -> None:
    _complete(tmp_path)
    run_crypto_delayed_paper_factor_research_full_scrub(output_root=tmp_path)
    receipt = next(
        (tmp_path / "evolution" / "factor_research" / "receipts").glob("*.json")
    )
    receipt.unlink()

    with pytest.raises(
        CryptoFactorProjectionError, match="factor_projection_claimed_record_missing"
    ):
        run_crypto_delayed_paper_factor_research_full_scrub(output_root=tmp_path)


def test_worker_rejects_a_free_epoch_manifest_path(tmp_path: Path) -> None:
    with pytest.raises(
        CryptoFactorProjectionError, match="factor_projection_manifest_path_invalid"
    ):
        _validated_manifest_path(tmp_path / "g4.json")


def _shift_source(source: dict[str, object], minutes: int) -> dict[str, object]:
    shifted = copy.deepcopy(source)
    observation = shifted["observation"]
    assert isinstance(observation, dict)

    def move(value: str) -> str:
        return (
            (
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                + timedelta(minutes=minutes)
            )
            .isoformat()
            .replace("+00:00", "Z")
        )

    observation["market_slot"] = move(str(observation["market_slot"]))
    symbols = observation["symbols"]
    assert isinstance(symbols, dict)
    for symbol in ("BTCUSDT", "ETHUSDT"):
        item = symbols[symbol]
        assert isinstance(item, dict)
        bars = item["bars"]
        assert isinstance(bars, list)
        for bar in bars:
            assert isinstance(bar, dict)
            for field in (
                "open_time",
                "close_time",
                "source_close_time",
                "data_through",
                "observed_at",
            ):
                bar[field] = move(str(bar[field]))
    shifted["completion_sha256"] = "f" * 64
    return shifted


def test_labels_only_bind_to_the_exact_later_observed_window(tmp_path: Path) -> None:
    _complete(tmp_path)
    _, sources = research._sources(tmp_path)
    source = sources[0]
    record = research._record(source, segment_id="crypto-5m-segment-20260728T000000Z")
    future_source = _shift_source(source, 60)
    future_record = research._record(
        future_source, segment_id="crypto-5m-segment-20260728T000000Z"
    )
    research._ensure_root(tmp_path)

    label_count = research._labels(
        tmp_path,
        record,
        {
            future_record["snapshots"]["BTCUSDT"]["market_slot"]: {
                "record": future_record,
                "source": future_source,
            }
        },
    )

    labels = list(
        (tmp_path / "evolution" / "factor_research" / "labels").glob("*.json")
    )
    assert label_count == 2
    assert {path.name.rsplit("-", 1)[-1] for path in labels} == {"60.json"}


def test_labels_do_not_cross_a_completion_gap(tmp_path: Path) -> None:
    _complete(tmp_path)
    _, sources = research._sources(tmp_path)
    source = sources[0]
    record = research._record(source, segment_id="crypto-5m-segment-20260728T000000Z")
    future_source = _shift_source(source, 60)
    future_record = research._record(
        future_source, segment_id="crypto-5m-segment-20260728T010000Z"
    )
    research._ensure_root(tmp_path)

    label_count = research._labels(
        tmp_path,
        record,
        {
            future_record["snapshots"]["BTCUSDT"]["market_slot"]: {
                "record": future_record,
                "source": future_source,
            }
        },
    )

    assert label_count == 0
    assert not list(
        (tmp_path / "evolution" / "factor_research" / "labels").glob("*.json")
    )


def test_learning_eligibility_requires_60m_same_segment_label_only(
    tmp_path: Path,
) -> None:
    _complete(tmp_path)
    _, sources = research._sources(tmp_path)
    source = sources[0]
    segment_id = "crypto-5m-segment-20260728T000000Z"
    record = research._record(source, segment_id=segment_id)
    records: dict[str, dict[str, object]] = {
        record["snapshots"]["BTCUSDT"]["market_slot"]: {
            "record": record,
            "source": source,
        }
    }
    future_records: dict[int, dict[str, object]] = {}
    for horizon in (60, 240, 720, 1440):
        future_source = _shift_source(source, horizon)
        future_record = research._record(future_source, segment_id=segment_id)
        future_records[horizon] = {
            "record": future_record,
            "source": future_source,
        }
        records[future_record["snapshots"]["BTCUSDT"]["market_slot"]] = future_records[
            horizon
        ]
    research._ensure_root(tmp_path)
    research._labels(tmp_path, record, records)

    samples, observation_ids = research._learning_eligible_samples(
        tmp_path,
        records,
        consumer_profile=research._segmented_learning_consumer_profile(),
    )

    assert len(samples) == 2
    assert observation_ids == [record["observation_id"]]

    for horizon in (240, 720, 1440):
        for symbol in ("BTCUSDT", "ETHUSDT"):
            (
                tmp_path
                / "evolution"
                / "factor_research"
                / "labels"
                / f"{record['observation_id']}-{symbol.lower()}-{horizon}.json"
            ).unlink()
    samples, observation_ids = research._learning_eligible_samples(
        tmp_path,
        records,
        consumer_profile=research._segmented_learning_consumer_profile(),
    )
    assert len(samples) == 2
    assert observation_ids == [record["observation_id"]]

    future_records[60]["record"]["segment_id"] = "other-segment"
    samples, observation_ids = research._learning_eligible_samples(
        tmp_path,
        records,
        consumer_profile=research._segmented_learning_consumer_profile(),
    )
    assert samples == []
    assert observation_ids == []
