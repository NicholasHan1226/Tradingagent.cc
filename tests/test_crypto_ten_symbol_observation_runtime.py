from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable
import urllib.error

import pytest

import Crypto.ten_symbol_observation_runtime as runtime_module
from Crypto.market_observation import OBSERVATION_SYMBOLS
from Crypto.ten_symbol_observation_profile import (
    CryptoTenSymbolObservationProfile,
)
from Crypto.ten_symbol_observation_runtime import (
    CRYPTO_TEN_SYMBOL_RUNTIME_CONTRACT,
    CryptoTenSymbolObservationRuntimeError,
    crypto_ten_symbol_observation_exit_code,
    load_crypto_ten_symbol_observation_runtime_manifest,
    run_crypto_ten_symbol_observation_once,
)
from Crypto.ten_symbol_observation_store import (
    TEN_SYMBOL_DATA_GAP_CONTRACT,
    CryptoTenSymbolObservationStore,
)
from shared.data.sharedsignals_v1 import HTTPResponse, parse_catalog_envelope
from tests.test_crypto_ten_symbol_support import (
    CATALOG_VERSION,
    CUTOFF,
    WINDOW_END,
    TenSymbolFixtureTransport,
    catalog_payload,
    iso,
)


def _profile() -> CryptoTenSymbolObservationProfile:
    return CryptoTenSymbolObservationProfile.from_catalog(
        parse_catalog_envelope(catalog_payload()),
        expected_catalog_version=CATALOG_VERSION,
    )


def _manifest_payload(
    output_root: Path,
    *,
    catalog_version: str = CATALOG_VERSION,
) -> dict[str, Any]:
    profile = _profile()
    payload = profile.to_payload()
    if catalog_version != CATALOG_VERSION:
        payload["catalog_version"] = catalog_version
    return {
        "schema": "tradingagent.crypto.ten_symbol_observation_runtime_manifest.v1",
        "base_url": "http://127.0.0.1:18083",
        "catalog_version": catalog_version,
        "access_policy_id": "tradingagent-crypto-read-v1",
        "output_root": str(output_root),
        "profile_sha256": profile.profile_sha256,
        "profile": payload,
        "safety": {
            "real_trading_enabled": False,
            "production_eligible": False,
            "execution_authority": False,
            "testnet_enabled": False,
            "live_broker_enabled": False,
            "model_network_enabled": False,
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
        },
    }


def _write_manifest(
    tmp_path: Path,
    *,
    payload: dict[str, Any],
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "crypto-ten-symbol-observation.runtime.json"
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
    path.chmod(0o600)
    return path


def _runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path]:
    token_file = tmp_path / "tradingdatas-crypto-read.token"
    output_root = tmp_path / "crypto-ten-symbol-observation"
    monkeypatch.setattr(runtime_module, "RUNTIME_TOKEN_FILE", token_file)
    monkeypatch.setattr(runtime_module, "RUNTIME_OUTPUT_ROOT", output_root)
    # Keep the tests hermetic.  GitHub happens to provide this globally, but a
    # normal clean developer shell must exercise the same simulation-only
    # contract without inheriting CI-only ambient state.
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    return token_file, output_root


def _factory(
    transport: Callable[..., HTTPResponse],
    *,
    calls: list[tuple[str, Path, str]] | None = None,
) -> Callable[..., Callable[..., HTTPResponse]]:
    def build(
        transport_id: str,
        *,
        token_file: Path,
        base_url: str,
    ) -> Callable[..., HTTPResponse]:
        if calls is not None:
            calls.append((transport_id, token_file, base_url))
        return transport

    return build


def _forbidden_factory(*_: Any, **__: Any) -> Callable[..., HTTPResponse]:
    raise AssertionError("transport must not be constructed in this path")


def _run(
    tmp_path: Path,
    token_file: Path,
    output_root: Path,
    *,
    now: datetime,
    transport_factory: Callable[..., Any],
    invocation_budget_seconds: float | None = None,
    retry_sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if retry_sleep is not None:
        kwargs["retry_sleep"] = retry_sleep
    return run_crypto_ten_symbol_observation_once(
        runtime_manifest=_write_manifest(
            tmp_path, payload=_manifest_payload(output_root)
        ),
        token_file=token_file,
        output_root=output_root,
        now=now,
        transport_factory=transport_factory,
        invocation_budget_seconds=invocation_budget_seconds,
        **kwargs,
    )


def _assert_recursive_non_authority(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {
                "production_eligible",
                "execution_eligible",
                "capital_write_eligible",
                "model_authority",
                "real_trading_enabled",
            }:
                assert item is False
            if key == "authority":
                assert item == "none"
            _assert_recursive_non_authority(item)
    elif isinstance(value, list):
        for item in value:
            _assert_recursive_non_authority(item)


def test_manifest_loads_and_binds_frozen_profile(tmp_path: Path) -> None:
    output_root = tmp_path / "root"
    monkeypatch_target = runtime_module.RUNTIME_OUTPUT_ROOT
    manifest_path = _write_manifest(
        tmp_path, payload=_manifest_payload(monkeypatch_target)
    )

    manifest = load_crypto_ten_symbol_observation_runtime_manifest(manifest_path)

    assert manifest.base_url == "http://127.0.0.1:18083"
    assert manifest.catalog_version == CATALOG_VERSION
    assert manifest.output_root == monkeypatch_target
    assert manifest.profile.profile_sha256 == manifest.profile_sha256
    assert len(manifest.dataset_ids) == 10
    assert len(manifest.sha256) == 64
    serialized = manifest_path.read_text(encoding="utf-8").lower()
    for forbidden in (
        "authorization",
        "bearer",
        "api_key",
        "sqlite://",
        "/tushare",
        "/source_status",
        "api.binance",
    ):
        assert forbidden not in serialized
    del output_root


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda payload: payload.update({"base_url": "http://10.0.0.5:18083"}),
            "runtime_base_url_must_be_loopback",
        ),
        (
            lambda payload: payload["safety"].update({"real_trading_enabled": True}),
            "runtime_safety_contract_invalid",
        ),
        (
            lambda payload: payload.update({"profile_sha256": "0" * 64}),
            "runtime_profile_sha256_mismatch",
        ),
        (
            lambda payload: payload.update({"token": "secret"}),
            "runtime_manifest_keys_invalid",
        ),
        (
            lambda payload: payload.update({"output_root": "relative/root"}),
            "runtime_output_root_invalid",
        ),
        (
            lambda payload: payload.update({"output_root": "/var/lib/tradingagent/other"}),
            "runtime_output_root_invalid",
        ),
        (
            lambda payload: payload.update({"catalog_version": "other-version"}),
            "runtime_catalog_profile_mismatch",
        ),
    ],
)
def test_manifest_fails_closed_on_authority_or_contract_expansion(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    reason: str,
) -> None:
    payload = _manifest_payload(runtime_module.RUNTIME_OUTPUT_ROOT)
    mutate(payload)
    path = _write_manifest(tmp_path, payload=payload)

    with pytest.raises(CryptoTenSymbolObservationRuntimeError, match=reason):
        load_crypto_ten_symbol_observation_runtime_manifest(path)


def test_manifest_rejects_duplicate_keys_symlink_hardlink_and_group_writable(
    tmp_path: Path,
) -> None:
    manifest_path = _write_manifest(
        tmp_path, payload=_manifest_payload(runtime_module.RUNTIME_OUTPUT_ROOT)
    )
    duplicate = tmp_path / "duplicate.runtime.json"
    duplicate.write_text('{"schema":"first","schema":"second"}\n', encoding="utf-8")
    duplicate.chmod(0o600)
    with pytest.raises(
        CryptoTenSymbolObservationRuntimeError,
        match="runtime_manifest_duplicate_key",
    ):
        load_crypto_ten_symbol_observation_runtime_manifest(duplicate)

    symlink = tmp_path / "symlink.runtime.json"
    symlink.symlink_to(manifest_path)
    with pytest.raises(
        CryptoTenSymbolObservationRuntimeError,
        match="runtime_manifest_file_untrusted",
    ):
        load_crypto_ten_symbol_observation_runtime_manifest(symlink)

    hardlink = tmp_path / "hardlink.runtime.json"
    os.link(manifest_path, hardlink)
    with pytest.raises(
        CryptoTenSymbolObservationRuntimeError,
        match="runtime_manifest_file_untrusted",
    ):
        load_crypto_ten_symbol_observation_runtime_manifest(hardlink)

    group_writable = tmp_path / "group.runtime.json"
    group_writable.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
    group_writable.chmod(0o660)
    with pytest.raises(
        CryptoTenSymbolObservationRuntimeError,
        match="runtime_manifest_file_untrusted",
    ):
        load_crypto_ten_symbol_observation_runtime_manifest(group_writable)


def test_completed_cycle_records_observation_event_without_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    transport = TenSymbolFixtureTransport()
    factory_calls: list[tuple[str, Path, str]] = []

    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(transport, calls=factory_calls),
    )

    assert receipt["contract"] == CRYPTO_TEN_SYMBOL_RUNTIME_CONTRACT
    assert receipt["status"] == "completed"
    assert receipt["requested_window_end"] == iso(WINDOW_END)
    assert receipt["requested_observation_cutoff"] == iso(CUTOFF)
    assert receipt["requested_window_consumed"] is True
    assert receipt["processed_cycle_count"] == 1
    assert receipt["fresh_cycle_count"] == 1
    assert receipt["market_data_network_used"] is True
    assert receipt["market_data_access_attempt_count"] == 1
    assert receipt["transport_factory_attempt_count"] == 1
    assert receipt["backlog_remaining"] is False
    assert crypto_ten_symbol_observation_exit_code(receipt) == 0
    _assert_recursive_non_authority(receipt)
    assert factory_calls == [("http-json-v1", token_file, "http://127.0.0.1:18083")]
    assert {call["method"] for call in transport.calls} == {"GET", "POST"}
    assert all(
        call["url"].endswith(("/v1/catalog", "/v1/query"))
        for call in transport.calls
    )
    assert sum(call["method"] == "GET" for call in transport.calls) == 2
    assert sum(call["method"] == "POST" for call in transport.calls) == 20

    store = CryptoTenSymbolObservationStore(output_root)
    checkpoint = store.checkpoint()
    assert checkpoint["event_count"] == 1
    assert checkpoint["observation_count"] == 1
    assert checkpoint["latest_terminal_slot"] == iso(WINDOW_END)
    event = store.events()[0]
    assert event["event_type"] == "observation"
    assert event["profile_sha256"] == receipt["fresh_query_profile_sha256"]


def test_unrelated_catalog_increment_is_bound_to_queries_without_profile_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    observed_version = "fixture-unrelated-catalog-v2"
    delegate = TenSymbolFixtureTransport()

    def incremented_catalog_transport(**kwargs: Any) -> HTTPResponse:
        response = delegate(**kwargs)
        payload = copy.deepcopy(dict(response.json_body))
        payload["catalog_version"] = observed_version
        return HTTPResponse(response.status_code, payload)

    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(incremented_catalog_transport),
    )

    assert receipt["status"] == "completed"
    assert receipt["fresh_query_catalog_version"] == observed_version
    event = CryptoTenSymbolObservationStore(output_root).events()[0]
    assert event["catalog_version"] == observed_version
    assert event["observation"]["catalog_version"] == observed_version
    assert event["observation"]["sources"][0]["symbol"] == OBSERVATION_SYMBOLS[0]
    assert (
        event["observation"]["observation_sha256"]
        == receipt["core_result"]["observation_sha256"]
    )
    _assert_recursive_non_authority(event)


def test_same_slot_replay_is_noop_without_transport(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    first = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(TenSymbolFixtureTransport()),
    )
    assert first["status"] == "completed"

    replay = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=59),
        transport_factory=_forbidden_factory,
    )

    assert replay["status"] == "noop"
    assert replay["processed_cycle_count"] == 0
    assert replay["market_data_network_used"] is False
    assert replay["transport_factory_attempt_count"] == 0
    assert crypto_ten_symbol_observation_exit_code(replay) == 0
    assert CryptoTenSymbolObservationStore(output_root).checkpoint()["event_count"] == 1


def test_slot_cutoff_is_fixed_at_bar_close_plus_55_seconds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    before_settle = runtime_module.crypto_ten_symbol_observation_window(
        WINDOW_END + timedelta(seconds=54)
    )
    assert before_settle.window_end == WINDOW_END - timedelta(minutes=5)
    at_settle = runtime_module.crypto_ten_symbol_observation_window(
        WINDOW_END + timedelta(seconds=55)
    )
    assert at_settle.window_end == WINDOW_END
    assert at_settle.observation_cutoff == CUTOFF
    jittered = runtime_module.crypto_ten_symbol_observation_window(
        WINDOW_END + timedelta(seconds=58)
    )
    assert jittered.window_end == WINDOW_END
    assert jittered.observation_cutoff == CUTOFF

    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=58),
        transport_factory=_factory(TenSymbolFixtureTransport()),
    )
    assert receipt["requested_window_end"] == iso(WINDOW_END)
    assert receipt["requested_observation_cutoff"] == iso(CUTOFF)


def test_incomplete_window_writes_one_idempotent_data_reject(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    short = TenSymbolFixtureTransport(row_count=12)

    first = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(short),
    )
    second = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(short),
    )

    assert first["status"] == second["status"] == "data_reject"
    assert first["data_incomplete"] is True
    assert second["data_incomplete"] is True
    assert (
        first["core_result"]["reason_code"]
        == "crypto_observation_query_shape_invalid"
    )
    assert first["warmup_eligible"] is True
    assert second["warmup_eligible"] is False
    assert crypto_ten_symbol_observation_exit_code(first) == 0
    assert crypto_ten_symbol_observation_exit_code(second) == 0
    store = CryptoTenSymbolObservationStore(output_root)
    assert len(store.data_reject_events()) == 1
    assert "observed_at" not in store.data_reject_events()[0]
    assert store.checkpoint()["latest_terminal_slot"] is None


def test_after_cutoff_reject_records_failed_observed_at(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    failed_observed_at = CUTOFF + timedelta(seconds=1)
    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(
            TenSymbolFixtureTransport(observed_at=failed_observed_at)
        ),
    )

    assert receipt["status"] == "data_reject"
    assert (
        receipt["core_result"]["reason_code"]
        == "crypto_observation_observed_at_after_cutoff"
    )
    assert receipt["core_result"]["observed_at"] == iso(failed_observed_at)
    store = CryptoTenSymbolObservationStore(output_root)
    rejects = store.data_reject_events()
    assert len(rejects) == 1
    assert rejects[0]["reason_code"] == "crypto_observation_observed_at_after_cutoff"
    assert rejects[0]["observed_at"] == iso(failed_observed_at)


def test_401_is_not_retried_and_has_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    calls: list[dict[str, Any]] = []

    def rejected_transport(**kwargs: Any) -> HTTPResponse:
        calls.append(copy.deepcopy(kwargs))
        return HTTPResponse(401, {})

    with pytest.raises(
        runtime_module.CryptoTenSymbolObservationRuntimeError,
        match="runtime_cycle_failed",
    ):
        _run(
            tmp_path,
            token_file,
            output_root,
            now=WINDOW_END + timedelta(seconds=55),
            transport_factory=_factory(rejected_transport),
        )
    assert len(calls) == 1
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"].endswith("/v1/catalog")
    assert all(
        forbidden not in str(calls).lower()
        for forbidden in ("/tushare", "/source_status", "api.binance", "sqlite")
    )


def test_pending_recovery_of_recorded_slot_needs_no_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    first = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(TenSymbolFixtureTransport()),
    )
    assert first["status"] == "completed"
    # Simulate a crash between the event publish and the pending clear.
    store = CryptoTenSymbolObservationStore(output_root)
    store.set_pending(
        {
            "window_end": iso(WINDOW_END),
            "observation_cutoff": iso(CUTOFF),
            "profile_sha256": first["fresh_query_profile_sha256"],
            "catalog_version": CATALOG_VERSION,
        }
    )

    recovered = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_forbidden_factory,
    )

    assert recovered["status"] == "completed"
    assert recovered["recovered_cycle_count"] == 1
    assert recovered["fresh_cycle_count"] == 0
    assert recovered["market_data_network_used"] is False
    assert recovered["transport_factory_attempt_count"] == 0
    assert recovered["recovered_observations"] == [
        {
            "window_end": iso(WINDOW_END),
            "source_profile_sha256": first["fresh_query_profile_sha256"],
            "runtime_manifest_profile_used_for_recovery": False,
            "network_used": False,
        }
    ]
    assert store.pending_record() is None
    assert store.checkpoint()["event_count"] == 1


def test_outage_gap_recovers_latest_window_after_unrecoverable_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    first = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(TenSymbolFixtureTransport()),
    )
    assert first["status"] == "completed"
    current_end = WINDOW_END + timedelta(minutes=30)
    current_now = current_end + timedelta(seconds=55)
    transport = TenSymbolFixtureTransport(
        observed_at=current_end + timedelta(seconds=20)
    )

    recovered = _run(
        tmp_path,
        token_file,
        output_root,
        now=current_now,
        transport_factory=_factory(transport),
    )

    assert recovered["status"] == "completed"
    assert recovered["outage_gap_recovered"] is True
    assert recovered["requested_window_consumed"] is True
    assert [item["cycle_kind"] for item in recovered["cycle_results"]] == [
        "fresh_query",
        "outage_gap_recovery",
    ]
    reject_cycle = recovered["cycle_results"][0]
    assert reject_cycle["target_window_end"] == iso(WINDOW_END + timedelta(minutes=5))
    assert reject_cycle["result"]["status"] == "data_reject"
    assert (
        reject_cycle["result"]["reason_code"]
        == "crypto_observation_observed_at_after_cutoff"
    )
    gap_result = recovered["cycle_results"][1]["result"]
    assert gap_result["status"] == "completed"
    assert gap_result["skipped_from"] == iso(WINDOW_END + timedelta(minutes=5))
    assert gap_result["skipped_to"] == iso(WINDOW_END + timedelta(minutes=25))
    assert gap_result["recovery_market_slot"] == iso(current_end)
    _assert_recursive_non_authority(recovered)

    store = CryptoTenSymbolObservationStore(output_root)
    gaps = store.data_gap_events()
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap["gap_contract"] == TEN_SYMBOL_DATA_GAP_CONTRACT
    assert gap["prior_market_slot"] == iso(WINDOW_END)
    assert gap["reason_code"] == "crypto_observation_observed_at_after_cutoff"
    assert len(gap["recovery_observation"]["sources"]) == 10
    assert store.checkpoint()["latest_terminal_slot"] == iso(current_end)
    _assert_recursive_non_authority(gap)

    replay = _run(
        tmp_path,
        token_file,
        output_root,
        now=current_now,
        transport_factory=_forbidden_factory,
    )
    assert replay["status"] == "noop"
    assert replay["outage_gap_recovered"] is False
    assert len(store.data_gap_events()) == 1

    adjacent_end = current_end + timedelta(minutes=5)
    adjacent = _run(
        tmp_path,
        token_file,
        output_root,
        now=adjacent_end + timedelta(seconds=55),
        transport_factory=_factory(
            TenSymbolFixtureTransport(observed_at=adjacent_end + timedelta(seconds=20))
        ),
    )
    assert adjacent["status"] == "completed"
    assert adjacent["outage_gap_recovered"] is False
    assert adjacent["cycle_results"][0]["cycle_kind"] == "fresh_query"
    assert adjacent["cycle_results"][0]["target_window_end"] == iso(adjacent_end)
    assert store.checkpoint()["observation_count"] == 2


def test_outage_gap_recovers_after_permanently_missing_source_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class MissingRowsTransport(TenSymbolFixtureTransport):
        """The historical window permanently lacks two source bars."""

        def __call__(self, **kwargs: Any) -> HTTPResponse:
            response = super().__call__(**kwargs)
            body = kwargs.get("json_body")
            if (
                kwargs.get("method") != "GET"
                and isinstance(body, dict)
                and str(body.get("dataset_id", "")).endswith(".5m")
            ):
                last_open = datetime.fromisoformat(
                    str(body["filters"]["open_time"]["between"][1]).replace(
                        "Z", "+00:00"
                    )
                )
                if last_open < current_end - timedelta(minutes=5):
                    payload = copy.deepcopy(dict(response.json_body))
                    payload["data"] = list(payload["data"])[:11]
                    return HTTPResponse(200, payload)
            return response

    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    first = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(TenSymbolFixtureTransport()),
    )
    assert first["status"] == "completed"
    current_end = WINDOW_END + timedelta(minutes=30)
    current_now = current_end + timedelta(seconds=55)
    transport = MissingRowsTransport(
        observed_at=current_end + timedelta(seconds=20)
    )

    recovered = _run(
        tmp_path,
        token_file,
        output_root,
        now=current_now,
        transport_factory=_factory(transport),
    )

    assert recovered["status"] == "completed"
    assert recovered["outage_gap_recovered"] is True
    assert [item["cycle_kind"] for item in recovered["cycle_results"]] == [
        "fresh_query",
        "outage_gap_recovery",
    ]
    reject_cycle = recovered["cycle_results"][0]
    assert reject_cycle["result"]["status"] == "data_reject"
    assert (
        reject_cycle["result"]["reason_code"]
        == "crypto_observation_query_shape_invalid"
    )
    gap_result = recovered["cycle_results"][1]["result"]
    assert gap_result["status"] == "completed"
    assert gap_result["skipped_from"] == iso(WINDOW_END + timedelta(minutes=5))
    assert gap_result["skipped_to"] == iso(WINDOW_END + timedelta(minutes=25))

    store = CryptoTenSymbolObservationStore(output_root)
    gaps = store.data_gap_events()
    assert len(gaps) == 1
    assert gaps[0]["reason_code"] == "crypto_observation_query_shape_invalid"
    assert store.checkpoint()["latest_terminal_slot"] == iso(current_end)
    _assert_recursive_non_authority(recovered)

    replay = _run(
        tmp_path,
        token_file,
        output_root,
        now=current_now,
        transport_factory=_forbidden_factory,
    )
    assert replay["status"] == "noop"


def test_gap_recovery_requires_complete_current_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    first = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(TenSymbolFixtureTransport()),
    )
    assert first["status"] == "completed"
    current_end = WINDOW_END + timedelta(minutes=30)

    def degrade_current_window(
        dataset_id: str,
        window_end: datetime,
        metadata: dict[str, Any],
    ) -> None:
        if window_end == current_end and dataset_id.endswith("btcusdt.5m"):
            metadata["state"] = "partial"
            metadata["degraded"] = True

    transport = TenSymbolFixtureTransport(
        observed_at=current_end + timedelta(seconds=20),
        metadata_mutator=degrade_current_window,
    )

    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=current_end + timedelta(seconds=55),
        transport_factory=_factory(transport),
    )

    assert receipt["status"] == "data_reject"
    assert receipt["outage_gap_recovered"] is False
    assert crypto_ten_symbol_observation_exit_code(receipt) == 2
    store = CryptoTenSymbolObservationStore(output_root)
    assert store.data_gap_events() == []
    assert store.checkpoint()["latest_terminal_slot"] == iso(WINDOW_END)


def test_backlog_pending_keeps_order_and_never_skips_slots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    first = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(TenSymbolFixtureTransport()),
    )
    assert first["status"] == "completed"
    # Crash left a pending marker for a now-historical slot with no event.
    store = CryptoTenSymbolObservationStore(output_root)
    store.set_pending(
        {
            "window_end": iso(WINDOW_END + timedelta(minutes=5)),
            "observation_cutoff": iso(WINDOW_END + timedelta(minutes=5, seconds=55)),
            "profile_sha256": first["fresh_query_profile_sha256"],
            "catalog_version": CATALOG_VERSION,
        }
    )
    current_end = WINDOW_END + timedelta(minutes=30)
    transport = TenSymbolFixtureTransport(
        observed_at=current_end + timedelta(seconds=20)
    )

    backlog = _run(
        tmp_path,
        token_file,
        output_root,
        now=current_end + timedelta(seconds=55),
        transport_factory=_factory(transport),
    )

    assert backlog["status"] == "backlog_pending"
    assert backlog["backlog_remaining"] is True
    assert backlog["requested_window_consumed"] is False
    # Ordered lag is an observable data condition, not a state-integrity
    # failure; the next invocation continues from the earliest missing slot.
    assert crypto_ten_symbol_observation_exit_code(backlog) == 0
    assert [item["cycle_kind"] for item in backlog["cycle_results"]] == [
        "pending_recovery",
        "fresh_query",
    ]
    assert backlog["cycle_results"][0]["result"]["status"] == (
        "cleared_unrecoverable_pending"
    )
    assert backlog["cycle_results"][1]["target_window_end"] == iso(
        WINDOW_END + timedelta(minutes=5)
    )
    assert store.pending_record() is None

    caught_up = _run(
        tmp_path,
        token_file,
        output_root,
        now=current_end + timedelta(seconds=55),
        transport_factory=_factory(transport),
    )
    assert caught_up["status"] == "completed"
    assert caught_up["outage_gap_recovered"] is True
    assert caught_up["cycle_results"][0]["target_window_end"] == iso(
        WINDOW_END + timedelta(minutes=5)
    )
    assert store.checkpoint()["latest_terminal_slot"] == iso(current_end)


def test_transient_transport_failure_retries_same_slot_and_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    delegate = TenSymbolFixtureTransport()
    sleeps: list[float] = []
    factory_instances: list[Any] = []

    def flaky_transport(**kwargs: Any) -> HTTPResponse:
        raise TimeoutError("catalog read timed out once")

    def fresh_factory(
        transport_id: str,
        *,
        token_file: Path,
        base_url: str,
    ) -> Callable[..., HTTPResponse]:
        if not factory_instances:
            transport = flaky_transport
        else:
            transport = delegate
        factory_instances.append(transport)
        return transport

    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=fresh_factory,
        retry_sleep=sleeps.append,
    )

    assert receipt["status"] == "completed"
    assert receipt["collect_attempts"] == 2
    assert receipt["market_data_access_attempt_count"] == 1
    assert receipt["transport_factory_attempt_count"] == 2
    assert len(factory_instances) == 2
    assert factory_instances[0] is not factory_instances[1]
    assert sleeps == [runtime_module.COLLECT_RETRY_DELAY_SECONDS]
    assert crypto_ten_symbol_observation_exit_code(receipt) == 0
    query_windows = {
        tuple(call["json_body"]["filters"]["open_time"]["between"])
        for call in delegate.calls
        if call["method"] == "POST" and "open_time" in call["json_body"]["filters"]
    }
    assert len(query_windows) == 1
    query_start, query_end = next(iter(query_windows))
    assert datetime.fromisoformat(query_end.replace("Z", "+00:00")) == (
        WINDOW_END - timedelta(minutes=5)
    )
    assert datetime.fromisoformat(query_end.replace("Z", "+00:00")) - datetime.fromisoformat(
        query_start.replace("Z", "+00:00")
    ) == timedelta(hours=1)
    store = CryptoTenSymbolObservationStore(output_root)
    assert store.checkpoint()["observation_count"] == 1
    assert store.data_reject_events() == []

    replay = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_forbidden_factory,
    )
    assert replay["status"] == "noop"
    assert replay["collect_attempts"] == 0
    assert store.checkpoint()["event_count"] == 1


def test_persistent_transport_failure_retries_bounded_then_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    sleeps: list[float] = []
    calls = 0

    def dead_transport(**kwargs: Any) -> HTTPResponse:
        nonlocal calls
        calls += 1
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    with pytest.raises(
        CryptoTenSymbolObservationRuntimeError,
        match="runtime_cycle_failed",
    ):
        _run(
            tmp_path,
            token_file,
            output_root,
            now=WINDOW_END + timedelta(seconds=55),
            transport_factory=_factory(dead_transport),
            retry_sleep=sleeps.append,
        )
    assert calls == runtime_module.MAX_COLLECT_ATTEMPTS
    assert sleeps == [runtime_module.COLLECT_RETRY_DELAY_SECONDS] * (
        runtime_module.MAX_COLLECT_ATTEMPTS - 1
    )
    store = CryptoTenSymbolObservationStore(output_root)
    # The original fail-closed shape is unchanged: no fabricated reject, and
    # the pending marker survives for the next invocation's ordered retry.
    assert store.data_reject_events() == []
    assert store.pending_record() is not None


def test_retry_sleep_counts_against_absolute_budget_before_next_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    clock = [0.0]
    sleeps: list[float] = []
    transport_calls = 0

    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock[0])

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    def timed_out_transport(**_: Any) -> HTTPResponse:
        nonlocal transport_calls
        transport_calls += 1
        raise TimeoutError("transient failure before retry budget expires")

    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(timed_out_transport),
        invocation_budget_seconds=10.0,
        retry_sleep=sleep,
    )

    assert receipt["status"] == "backlog_pending"
    assert receipt["budget_deferred"] is True
    assert receipt["collect_attempts"] == 1
    assert receipt["transport_factory_attempt_count"] == 1
    assert transport_calls == 1
    assert sleeps == [runtime_module.COLLECT_RETRY_DELAY_SECONDS]
    assert CryptoTenSymbolObservationStore(output_root).pending_record() is not None


def test_wrapped_http_error_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    sleeps: list[float] = []
    calls = 0

    def wrapped_http_failure(**_: Any) -> HTTPResponse:
        nonlocal calls
        calls += 1
        raise urllib.error.URLError(
            urllib.error.HTTPError(
                url="http://127.0.0.1:18083/v1/catalog",
                code=401,
                msg="unauthorized",
                hdrs=None,
                fp=None,
            )
        )

    with pytest.raises(
        CryptoTenSymbolObservationRuntimeError,
        match="runtime_cycle_failed",
    ):
        _run(
            tmp_path,
            token_file,
            output_root,
            now=WINDOW_END + timedelta(seconds=55),
            transport_factory=_factory(wrapped_http_failure),
            retry_sleep=sleeps.append,
        )

    assert calls == 1
    assert sleeps == []
    assert CryptoTenSymbolObservationStore(output_root).pending_record() is not None


def test_semantic_failures_are_never_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    sleeps: list[float] = []

    # A data-contract failure (incomplete window) is semantic: one attempt.
    short = TenSymbolFixtureTransport(row_count=12)
    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(short),
        retry_sleep=sleeps.append,
    )
    assert receipt["status"] == "data_reject"
    assert receipt["collect_attempts"] == 1
    assert sleeps == []

    # An HTTP status error (401) is never retried either.
    def rejected_transport(**kwargs: Any) -> HTTPResponse:
        return HTTPResponse(401, {})

    second_root = tmp_path / "second"
    second_root.mkdir()
    token_file_2, output_root_2 = _runtime_paths(monkeypatch, second_root)
    with pytest.raises(
        CryptoTenSymbolObservationRuntimeError,
        match="runtime_cycle_failed",
    ):
        _run(
            second_root,
            token_file_2,
            output_root_2,
            now=WINDOW_END + timedelta(seconds=55),
            transport_factory=_factory(rejected_transport),
            retry_sleep=sleeps.append,
        )
    assert sleeps == []


def test_real_trading_enabled_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")

    with pytest.raises(
        CryptoTenSymbolObservationRuntimeError,
        match="runtime_real_trading_must_be_disabled",
    ):
        _run(
            tmp_path,
            token_file,
            output_root,
            now=WINDOW_END + timedelta(seconds=55),
            transport_factory=_factory(TenSymbolFixtureTransport()),
        )


def test_cli_has_no_output_root_flag_and_redacts_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    manifest_path = _write_manifest(
        tmp_path, payload=_manifest_payload(output_root)
    )

    with pytest.raises(SystemExit):
        runtime_module.main(
            [
                "--runtime-manifest",
                str(manifest_path),
                "--token-file",
                str(token_file),
                "--output-root",
                str(output_root),
            ]
        )
    capsys.readouterr()

    def damaged_runtime(**_: Any) -> dict[str, Any]:
        raise CryptoTenSymbolObservationRuntimeError("sensitive/internal/path")

    monkeypatch.setattr(
        runtime_module,
        "run_crypto_ten_symbol_observation_once",
        damaged_runtime,
    )
    exit_code = runtime_module.main(
        [
            "--runtime-manifest",
            str(manifest_path),
            "--token-file",
            str(token_file),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err.strip() == (
        "crypto ten symbol observation runtime failed closed"
    )
    assert "traceback" not in captured.err.lower()
    assert "sensitive" not in captured.err.lower()


def test_cli_rejects_non_dedicated_token_leaf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, output_root = _runtime_paths(monkeypatch, tmp_path)
    manifest_path = _write_manifest(
        tmp_path, payload=_manifest_payload(output_root)
    )

    exit_code = runtime_module.main(
        [
            "--runtime-manifest",
            str(manifest_path),
            "--token-file",
            str(tmp_path / "wrong.token"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err.strip() == (
        "crypto ten symbol observation runtime failed closed"
    )


def test_loopback_absolute_budget_stays_below_systemd_stop_line() -> None:
    service = (
        Path(__file__).resolve().parents[1]
        / "Crypto"
        / "systemd"
        / "tradingagent-crypto-ten-symbol-observation.service"
    ).read_text(encoding="utf-8")

    # Per-wire timeouts are further clamped by one absolute invocation budget,
    # so a partial/provider slowdown cannot run past multiple 5-minute slots.
    assert runtime_module.RUNTIME_TIMEOUT_SECONDS == 60.0
    assert runtime_module.REQUESTS_PER_CYCLE == 22
    assert runtime_module.INVOCATION_BUDGET_SECONDS == 120.0
    assert "TimeoutStartSec=180" in service


def test_absolute_budget_defers_without_recording_timeout_as_data_reject(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    clock = iter((0.0, 0.0, 31.0))
    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: next(clock))

    def timed_out_transport(**_: Any) -> HTTPResponse:
        raise TimeoutError("wire timed out at invocation deadline")

    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(timed_out_transport),
        invocation_budget_seconds=30.0,
    )

    assert receipt["status"] == "backlog_pending"
    assert receipt["budget_deferred"] is True
    assert receipt["processed_cycle_count"] == 0
    assert receipt["backlog_remaining"] is True
    assert crypto_ten_symbol_observation_exit_code(receipt) == 2
    store = CryptoTenSymbolObservationStore(output_root)
    assert store.pending_record() is not None
    assert store.checkpoint()["data_reject_count"] == 0


def test_forty_family_settle_margin_and_shape_retry_pins() -> None:
    assert (
        runtime_module.FORTY_SYMBOL_RUNTIME_CONFIG.slot_settle_delay_seconds == 270
    )
    assert runtime_module.FORTY_SYMBOL_RUNTIME_CONFIG.bar_shape_retry_delays == (
        20.0,
        45.0,
    )
    # The frozen ten-symbol chain keeps its +55s boundary and its
    # single-attempt bar collection byte-for-byte.
    assert runtime_module.TEN_SYMBOL_RUNTIME_CONFIG.slot_settle_delay_seconds == 55
    assert runtime_module.TEN_SYMBOL_RUNTIME_CONFIG.bar_shape_retry_delays == ()
    for config in (
        runtime_module.TEN_SYMBOL_RUNTIME_CONFIG,
        runtime_module.FORTY_SYMBOL_RUNTIME_CONFIG,
    ):
        assert 0 <= config.slot_settle_delay_seconds < 5 * 60


@pytest.mark.parametrize(
    ("config", "slot_offset_minutes", "expected_delays"),
    [
        (runtime_module.FORTY_SYMBOL_RUNTIME_CONFIG, -5, ()),
        (runtime_module.FORTY_SYMBOL_RUNTIME_CONFIG, 0, (20.0, 45.0)),
        (runtime_module.TEN_SYMBOL_RUNTIME_CONFIG, -5, ()),
        (runtime_module.TEN_SYMBOL_RUNTIME_CONFIG, 0, ()),
    ],
    ids=["forty-historical", "forty-current", "ten-historical", "ten-current"],
)
def test_family_selects_shape_retry_from_frozen_invocation_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config: runtime_module.CryptoTenSymbolObservationRuntimeConfig,
    slot_offset_minutes: int,
    expected_delays: tuple[float, ...],
) -> None:
    captured: dict[str, Any] = {}

    def fake_collect(
        client: Any,
        *,
        catalog: Any,
        expected_catalog_version: str,
        window: Any,
        symbols: tuple[str, ...],
        shape_retry_delays: tuple[float, ...],
        retry_sleep: Callable[[float], None],
        budget_remaining: Callable[[], float] | None,
    ) -> tuple[Any, dict[str, list[dict[str, Any]]]]:
        captured["symbols"] = symbols
        captured["shape_retry_delays"] = shape_retry_delays
        captured["retry_sleep"] = retry_sleep
        captured["budget_remaining"] = budget_remaining
        raise RuntimeError("stop-before-persistence")

    monkeypatch.setattr(
        runtime_module,
        "_collect_market_observation_rows_with_catalog",
        fake_collect,
    )
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    manifest = load_crypto_ten_symbol_observation_runtime_manifest(
        _write_manifest(tmp_path, payload=_manifest_payload(output_root))
    )
    sleeps: list[float] = []
    port = runtime_module._LazyObservationPort(
        manifest=manifest,
        token_file=token_file,
        transport_factory=_factory(
            lambda **kwargs: HTTPResponse(200, catalog_payload())
        ),
        config=config,
        current_window_end=WINDOW_END,
        retry_sleep=sleeps.append,
    )
    window = runtime_module._window_for_end(
        WINDOW_END + timedelta(minutes=slot_offset_minutes), config=config
    )
    with pytest.raises(RuntimeError, match="stop-before-persistence"):
        port.collect(window)

    assert captured["symbols"] == config.symbols
    assert captured["shape_retry_delays"] == expected_delays
    assert callable(captured["retry_sleep"])
    assert callable(captured["budget_remaining"])


@pytest.mark.parametrize("late_receipt", [False, True], ids=["recover", "reject-late"])
def test_forty_historical_shape_failure_preserves_current_gap_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    late_receipt: bool,
) -> None:
    from tests.test_crypto_forty_symbol_universe import FortySymbolFixtureTransport

    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    config = replace(runtime_module.FORTY_SYMBOL_RUNTIME_CONFIG, output_root=output_root)
    catalog = parse_catalog_envelope(
        FortySymbolFixtureTransport()(method="GET").json_body
    )
    profile = CryptoTenSymbolObservationProfile.from_catalog(
        catalog,
        expected_catalog_version=catalog.catalog_version,
        symbols=config.symbols,
        profile_contract=config.profile_contract,
    )
    payload = _manifest_payload(output_root)
    payload.update(
        schema=config.manifest_contract,
        catalog_version=profile.catalog_version,
        profile=profile.to_payload(),
        profile_sha256=profile.profile_sha256,
    )
    manifest = _write_manifest(tmp_path, payload=payload)

    def run(
        end: datetime, transport: Any, sleep: Callable[[float], None]
    ) -> dict[str, Any]:
        return run_crypto_ten_symbol_observation_once(
            runtime_manifest=manifest,
            token_file=token_file,
            output_root=output_root,
            now=end + timedelta(seconds=285),
            config=config,
            transport_factory=_factory(transport),
            retry_sleep=sleep,
        )

    first = run(WINDOW_END, FortySymbolFixtureTransport(), lambda _: None)
    assert first["status"] == "completed"
    current_end = WINDOW_END + timedelta(minutes=30)
    elapsed = 285.0
    sleeps: list[float] = []
    old_queries = 0

    def sleep(seconds: float) -> None:
        nonlocal elapsed
        sleeps.append(seconds)
        elapsed += seconds

    class MissingHistoryTransport(FortySymbolFixtureTransport):
        def __call__(self, **kwargs: Any) -> HTTPResponse:
            nonlocal old_queries
            # Waiting 65 seconds on missing history exposes the next receipt.
            delay = 320 if elapsed >= 320 else (271 if late_receipt else 20)
            self.observed_at = current_end + timedelta(seconds=delay)
            response = super().__call__(**kwargs)
            body = kwargs.get("json_body")
            if isinstance(body, dict) and str(body["dataset_id"]).endswith(".5m"):
                last_open = datetime.fromisoformat(
                    body["filters"]["open_time"]["between"][1]
                )
                if last_open < current_end - timedelta(minutes=5):
                    old_queries += 1
                    incomplete = copy.deepcopy(dict(response.json_body))
                    incomplete["data"] = incomplete["data"][:11]
                    return HTTPResponse(200, incomplete)
            return response

    receipt = run(current_end, MissingHistoryTransport(), sleep)
    assert old_queries == 1
    assert sleeps == []
    assert [cycle["cycle_kind"] for cycle in receipt["cycle_results"]] == [
        "fresh_query",
        "outage_gap_recovery",
    ]
    assert receipt["cycle_results"][0]["result"]["reason_code"] == (
        "crypto_observation_query_shape_invalid"
    )
    _assert_recursive_non_authority(receipt)
    store = CryptoTenSymbolObservationStore(output_root, contracts=config.store_contracts)
    assert store.pending_record() is None
    if late_receipt:
        assert receipt["status"] == "data_reject"
        assert receipt["outage_gap_recovered"] is False
        assert receipt["cycle_results"][1]["result"]["reason_code"] == (
            "crypto_observation_observed_at_after_cutoff"
        )
        assert store.data_gap_events() == []
        assert store.checkpoint()["latest_terminal_slot"] == iso(WINDOW_END)
        assert store.read_bars_sidecar(iso(current_end)) is None
    else:
        assert receipt["status"] == "completed"
        assert receipt["outage_gap_recovered"] is True
        gap = store.data_gap_events()[0]
        assert gap["skipped_from"] == iso(WINDOW_END + timedelta(minutes=5))
        assert gap["skipped_to"] == iso(current_end - timedelta(minutes=5))
        assert len(gap["recovery_observation"]["sources"]) == 40
        assert store.checkpoint()["latest_terminal_slot"] == iso(current_end)
        sidecar = store.read_bars_sidecar(iso(current_end))
        assert sidecar is not None
        assert sidecar["observation_cutoff"] == iso(current_end + timedelta(seconds=270))
        replay = run(
            current_end, lambda **_: pytest.fail("replay queried transport"), sleep
        )
        assert replay["status"] == "noop"
        assert len(store.data_gap_events()) == 1
