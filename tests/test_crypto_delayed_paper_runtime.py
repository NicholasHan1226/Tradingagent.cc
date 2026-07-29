from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
from typing import Any, Callable

import pytest

import Crypto.delayed_paper_epoch as epoch_module
import Crypto.delayed_paper_runtime as runtime_module
import Crypto.delayed_paper_runner as runner_module
from Crypto.delayed_paper_epoch import (
    load_crypto_delayed_paper_epoch_manifest,
    prepare_crypto_delayed_paper_epoch,
)
from Crypto.delayed_paper_ledger import (
    CryptoDelayedPaperLedgerError,
    CryptoDelayedPaperObservationStore,
)
from Crypto.delayed_paper_runtime import (
    CRYPTO_RUNTIME_CONTRACT,
    CryptoDelayedPaperRuntimeError,
    load_crypto_delayed_paper_runtime_manifest,
    run_crypto_delayed_paper_server_once as _core_run_crypto_delayed_paper_server_once,
)
from Crypto.five_minute_data import _sha256 as crypto_profile_sha256
from shared.data.sharedsignals_v1 import HTTPResponse
from shared.data.tradingdatas_transport import RuntimeGateConfigurationError
from tests.test_crypto_5m_support import (
    BAR_DATASETS,
    CATALOG_VERSION,
    RULE_DATASETS,
    WINDOW_END,
    FixtureTradingDatasTransport,
    bar_rows,
    client,
    metadata,
    profile,
)

_TEST_EPOCH_ID = "crypto-delayed-paper-epoch-g2-runtime-test"


def _manifest_payload(
    *,
    catalog_version: str = CATALOG_VERSION,
) -> dict[str, Any]:
    profile_transport = FixtureTradingDatasTransport()
    handoff_profile = replace(
        profile(client(profile_transport)),
        mode="tradingdatas_handoff",
    )
    frozen_profile = handoff_profile.to_payload()
    frozen_profile["catalog_version"] = catalog_version
    for binding in frozen_profile["symbols"]:
        binding["bars"]["catalog_version"] = catalog_version
        binding["instrument_rules"]["catalog_version"] = catalog_version
    return {
        "schema": "tradingagent.crypto.delayed_paper_runtime_manifest.v1",
        "base_url": "http://127.0.0.1:18083",
        "catalog_version": catalog_version,
        "access_policy_id": "tradingagent-crypto-read-v1",
        "profile_sha256": crypto_profile_sha256(frozen_profile),
        "profile": frozen_profile,
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
    payload: dict[str, Any] | None = None,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "crypto-delayed-paper.runtime.json"
    path.write_text(
        json.dumps(
            payload or _manifest_payload(),
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
    archived_root = tmp_path / "crypto-delayed-paper-archive"
    epoch_parent = tmp_path / "crypto-delayed-paper-epochs"
    output_root = epoch_parent / _TEST_EPOCH_ID
    epoch_manifest = tmp_path / "crypto-delayed-paper.epoch.json"
    archived_root.mkdir(parents=True, mode=0o700)
    epoch_parent.mkdir(parents=True, mode=0o700)
    epoch_payload = {
        "schema": "tradingagent.crypto.delayed_paper_epoch_manifest.v1",
        "epoch_id": _TEST_EPOCH_ID,
        "epoch_generation": 2,
        "current_output_root": str(output_root),
        "archived_output_root": str(archived_root),
        "archived_epoch_policy": "read_only_archive_no_resume",
        "capital_baseline_policy_id": "crypto-capital-v1",
        "aggregate_with_archived_epoch": False,
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
    epoch_manifest.write_text(
        json.dumps(
            epoch_payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    epoch_manifest.chmod(0o600)
    monkeypatch.setattr(epoch_module, "LEGACY_ARCHIVE_ROOT", archived_root)
    monkeypatch.setattr(epoch_module, "EPOCH_ROOT_PARENT", epoch_parent)
    monkeypatch.setattr(epoch_module, "EPOCH_MANIFEST_PATH", epoch_manifest)
    monkeypatch.setattr(runtime_module, "RUNTIME_TOKEN_FILE", token_file)
    context = load_crypto_delayed_paper_epoch_manifest(epoch_manifest)
    prepare_crypto_delayed_paper_epoch(context)
    monkeypatch.setattr(
        runtime_module,
        "_TEST_EPOCH_CONTEXT",
        context,
        raising=False,
    )
    return token_file, output_root


def run_crypto_delayed_paper_server_once(**kwargs: Any) -> dict[str, Any]:
    return _core_run_crypto_delayed_paper_server_once(
        **kwargs,
        epoch_context=runtime_module._TEST_EPOCH_CONTEXT,
    )


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


def _capital_bytes(root: Path) -> dict[str, bytes]:
    capital = root / "capital"
    if not capital.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(capital.rglob("*"))
        if path.is_file()
    }


def _shifted_transport(minutes: int) -> FixtureTradingDatasTransport:
    delta = timedelta(minutes=minutes)
    shifted_rows = copy.deepcopy(bar_rows())
    for row in shifted_rows:
        for field in ("open_time", "close_time"):
            parsed = datetime.fromisoformat(str(row[field]).replace("Z", "+00:00"))
            row[field] = (
                (parsed + delta)
                .astimezone(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
    shifted_end = WINDOW_END + delta
    metadata_by_dataset: dict[str, dict[str, Any]] = {}
    for dataset_id in BAR_DATASETS.values():
        metadata_by_dataset[dataset_id] = metadata(
            dataset_id=dataset_id,
            data_through=shifted_end - timedelta(milliseconds=1),
            observed_at=shifted_end + timedelta(seconds=20),
        )
    for dataset_id in RULE_DATASETS.values():
        metadata_by_dataset[dataset_id] = metadata(
            dataset_id=dataset_id,
            data_through=shifted_end + timedelta(seconds=5),
            observed_at=shifted_end + timedelta(seconds=10),
        )
    return FixtureTradingDatasTransport(
        bars=shifted_rows,
        metadata_by_dataset=metadata_by_dataset,
    )


def _sequence_factory(
    transports: list[Callable[..., HTTPResponse]],
) -> Callable[..., Callable[..., HTTPResponse]]:
    queue = list(transports)

    def build(
        transport_id: str,
        *,
        token_file: Path,
        base_url: str,
    ) -> Callable[..., HTTPResponse]:
        del transport_id, token_file, base_url
        if not queue:
            raise AssertionError("unexpected extra transport construction")
        return queue.pop(0)

    return build


def _assert_recursive_non_authority(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {
                "production_eligible",
                "execution_authority",
                "real_trading_enabled",
                "testnet_enabled",
                "live_broker_enabled",
                "automatic_promotion_enabled",
                "automatic_risk_expansion_enabled",
            }:
                assert item is False
            if key in {"outbox_id", "capital_commit_id"}:
                assert item is None
            if key == "status":
                assert item != "filled"
            _assert_recursive_non_authority(item)
    elif isinstance(value, list):
        for item in value:
            _assert_recursive_non_authority(item)


def test_manifest_reconstructs_exact_handoff_profile_from_external_file(
    tmp_path: Path,
) -> None:
    manifest_path = _write_manifest(tmp_path)

    manifest = load_crypto_delayed_paper_runtime_manifest(manifest_path)

    assert manifest.base_url == "http://127.0.0.1:18083"
    assert manifest.catalog_version == CATALOG_VERSION
    assert manifest.profile.mode == "tradingdatas_handoff"
    assert manifest.profile.catalog_version == CATALOG_VERSION
    assert manifest.profile.sha256 == manifest.profile_sha256
    assert manifest.dataset_ids == frozenset(
        {
            BAR_DATASETS["BTCUSDT"],
            BAR_DATASETS["ETHUSDT"],
            RULE_DATASETS["BTCUSDT"],
            RULE_DATASETS["ETHUSDT"],
        }
    )
    assert len(manifest.sha256) == 64
    serialized = manifest_path.read_text(encoding="utf-8").lower()
    for forbidden in (
        "token",
        "authorization",
        "api_key",
        "sqlite://",
        "/tushare",
        "/source_status",
        "api.binance",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda payload: payload.update({"base_url": "http://10.0.0.5:18083"}),
            "runtime_base_url_must_be_loopback",
        ),
        (
            lambda payload: payload.update(
                {"base_url": "https://data.example.com:443"}
            ),
            "runtime_base_url_must_be_loopback",
        ),
        (
            lambda payload: payload["safety"].update({"real_trading_enabled": True}),
            "runtime_safety_contract_invalid",
        ),
        (
            lambda payload: payload["profile"].update({"mode": "fixture_mock"}),
            "runtime_profile_mode_invalid",
        ),
        (
            lambda payload: payload.update({"profile_sha256": "0" * 64}),
            "runtime_profile_sha256_mismatch",
        ),
        (
            lambda payload: payload.update({"token": "secret"}),
            "runtime_manifest_keys_invalid",
        ),
    ],
)
def test_manifest_fails_closed_on_authority_or_contract_expansion(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    reason: str,
) -> None:
    payload = _manifest_payload()
    mutate(payload)
    path = _write_manifest(tmp_path, payload=payload)

    with pytest.raises(CryptoDelayedPaperRuntimeError, match=reason):
        load_crypto_delayed_paper_runtime_manifest(path)


def test_complete_window_runs_loopback_only_core_without_learning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    manifest_path = _write_manifest(tmp_path)
    transport = FixtureTradingDatasTransport()
    factory_calls: list[tuple[str, Path, str]] = []

    receipt = run_crypto_delayed_paper_server_once(
        runtime_manifest=manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(transport, calls=factory_calls),
    )

    assert receipt["contract"] == CRYPTO_RUNTIME_CONTRACT
    assert receipt["status"] == "completed"
    assert receipt["requested_window_end"].endswith("01:05:00Z")
    assert receipt["requested_window_consumed"] is True
    assert receipt["fresh_query_catalog_version"] == CATALOG_VERSION
    assert receipt["market_data_transport"] == "loopback_tradingdatas_v1"
    assert receipt["market_data_network_used"] is True
    assert receipt["market_data_access_attempt_count"] == 1
    assert receipt["model_network_used"] is False
    assert receipt["execution_authority"] is False
    assert receipt["production_eligible"] is False
    assert receipt["core_result"]["status"] == "completed"
    assert receipt["learning_mode"] == "detached_offline_worker"
    assert receipt["learning_authority"] is False
    assert receipt["learning_invoked"] is False
    _assert_recursive_non_authority(receipt)
    assert runtime_module.crypto_runtime_receipt_exit_code(receipt) == 0
    assert factory_calls == [("http-json-v1", token_file, "http://127.0.0.1:18083")]
    assert {call["method"] for call in transport.calls} == {"GET", "POST"}
    assert all(
        call["url"].endswith(("/v1/catalog", "/v1/query")) for call in transport.calls
    )
    assert not (output_root / "evolution").exists()
    assert (output_root / "capital" / "events.jsonl").is_file()


def test_manifest_rejects_duplicate_keys_symlink_and_hardlink(
    tmp_path: Path,
) -> None:
    manifest_path = _write_manifest(tmp_path)
    duplicate = tmp_path / "duplicate.runtime.json"
    duplicate.write_text(
        '{"schema":"first","schema":"second"}\n',
        encoding="utf-8",
    )
    duplicate.chmod(0o600)
    with pytest.raises(
        CryptoDelayedPaperRuntimeError,
        match="runtime_manifest_duplicate_key",
    ):
        load_crypto_delayed_paper_runtime_manifest(duplicate)

    symlink = tmp_path / "symlink.runtime.json"
    symlink.symlink_to(manifest_path)
    with pytest.raises(
        CryptoDelayedPaperRuntimeError,
        match="runtime_manifest_file_untrusted",
    ):
        load_crypto_delayed_paper_runtime_manifest(symlink)

    hardlink = tmp_path / "hardlink.runtime.json"
    os.link(manifest_path, hardlink)
    with pytest.raises(
        CryptoDelayedPaperRuntimeError,
        match="runtime_manifest_file_untrusted",
    ):
        load_crypto_delayed_paper_runtime_manifest(hardlink)


def test_manifest_rejects_unbounded_query_page_budget(tmp_path: Path) -> None:
    payload = _manifest_payload()
    payload["profile"]["symbols"][0]["bars"]["max_pages"] = 1_000
    payload["profile_sha256"] = crypto_profile_sha256(payload["profile"])
    path = _write_manifest(tmp_path, payload=payload)

    with pytest.raises(
        CryptoDelayedPaperRuntimeError,
        match="runtime_profile_page_budget_exceeded",
    ):
        load_crypto_delayed_paper_runtime_manifest(path)


def test_first_short_window_writes_one_idempotent_data_reject_without_capital(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    manifest_path = _write_manifest(tmp_path)
    rows = bar_rows()
    rows.pop()

    first = run_crypto_delayed_paper_server_once(
        runtime_manifest=manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(minutes=4, seconds=59),
        transport_factory=_factory(FixtureTradingDatasTransport(bars=rows)),
    )
    second = run_crypto_delayed_paper_server_once(
        runtime_manifest=manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(FixtureTradingDatasTransport(bars=rows)),
    )

    assert first["status"] == second["status"] == "data_reject"
    assert first["core_result"]["reason_code"] == "crypto_5m_window_incomplete"
    assert second["core_result"]["reason_code"] == "crypto_5m_window_incomplete"
    assert first["requested_window_end"] == second["requested_window_end"]
    assert (
        first["requested_observation_cutoff"] == second["requested_observation_cutoff"]
    )
    assert first["warmup_eligible"] is True
    assert second["warmup_eligible"] is False
    assert not (output_root / "capital").exists()
    rows_written = (
        (output_root / "delayed_paper" / "decision_ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(rows_written) == 1
    assert json.loads(rows_written[0])["event_type"] == "data_reject"
    assert not (output_root / "evolution").exists()
    assert runtime_module.crypto_runtime_receipt_exit_code(first) == 0
    assert runtime_module.crypto_runtime_receipt_exit_code(second) == 2


def test_401_is_not_retried_and_has_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    manifest_path = _write_manifest(tmp_path)
    calls: list[dict[str, Any]] = []

    def rejected_transport(**kwargs: Any) -> HTTPResponse:
        calls.append(copy.deepcopy(kwargs))
        return HTTPResponse(401, {})

    receipt = run_crypto_delayed_paper_server_once(
        runtime_manifest=manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(rejected_transport),
    )

    assert receipt["status"] == "data_reject"
    assert runtime_module.crypto_runtime_receipt_exit_code(receipt) == 2
    assert len(calls) == 1
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"].endswith("/v1/catalog")
    assert all(
        forbidden not in str(calls).lower()
        for forbidden in (
            "/tushare",
            "/source_status",
            "api.binance",
            "sqlite",
        )
    )
    assert not (output_root / "capital").exists()


def test_degraded_metadata_and_catalog_drift_reject_without_capital(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    manifest_path = _write_manifest(tmp_path)
    degraded = FixtureTradingDatasTransport(
        metadata_by_dataset={
            RULE_DATASETS["BTCUSDT"]: metadata(
                dataset_id=RULE_DATASETS["BTCUSDT"],
                data_through=WINDOW_END,
                state="partial",
                degraded=True,
            )
        }
    )
    degraded_receipt = run_crypto_delayed_paper_server_once(
        runtime_manifest=manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(degraded),
    )
    assert degraded_receipt["status"] == "data_reject"
    assert runtime_module.crypto_runtime_receipt_exit_code(degraded_receipt) == 2
    assert not (output_root / "capital").exists()

    drift_token, drift_root = _runtime_paths(
        monkeypatch,
        tmp_path / "drift-runtime",
    )
    drift_manifest = _write_manifest(
        tmp_path / "drift",
        payload=_manifest_payload(catalog_version="formal-catalog-drift-v2"),
    )
    drift_receipt = run_crypto_delayed_paper_server_once(
        runtime_manifest=drift_manifest,
        token_file=drift_token,
        output_root=drift_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(FixtureTradingDatasTransport()),
    )
    assert drift_receipt["status"] == "data_reject"
    assert runtime_module.crypto_runtime_receipt_exit_code(drift_receipt) == 2
    assert not (drift_root / "capital").exists()


def test_missing_token_writes_one_reject_without_network_or_capital(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    manifest_path = _write_manifest(tmp_path)
    factory_calls = 0

    def missing_token(
        transport_id: str,
        *,
        token_file: Path,
        base_url: str,
    ) -> Callable[..., HTTPResponse]:
        nonlocal factory_calls
        del transport_id, token_file, base_url
        factory_calls += 1
        raise RuntimeGateConfigurationError("tradingdatas_token_missing")

    receipt = run_crypto_delayed_paper_server_once(
        runtime_manifest=manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=missing_token,
    )

    assert factory_calls == 1
    assert receipt["status"] == "data_reject"
    assert receipt["core_result"]["reason_code"] == ("tradingdatas_token_missing")
    assert not (output_root / "capital").exists()
    assert runtime_module.crypto_runtime_receipt_exit_code(receipt) == 2
    assert (
        len(
            (output_root / "delayed_paper" / "decision_ledger.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        == 1
    )


def test_learning_is_detached_and_same_slot_replay_preserves_capital(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    manifest_path = _write_manifest(tmp_path)
    source = Path(runtime_module.__file__).read_text(encoding="utf-8")
    signature = inspect.signature(run_crypto_delayed_paper_server_once)
    assert "learning_projector" not in signature.parameters
    assert "delayed_paper_learning" not in source
    assert '"evolution"' not in source
    assert "'evolution'" not in source
    assert not (
        Path(runtime_module.__file__).parent / "delayed_paper_learning.py"
    ).exists()

    first = run_crypto_delayed_paper_server_once(
        runtime_manifest=manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(FixtureTradingDatasTransport()),
    )
    assert first["status"] == "completed"
    assert first["learning_mode"] == "detached_offline_worker"
    assert first["learning_authority"] is False
    assert first["learning_invoked"] is False
    assert runtime_module.crypto_runtime_receipt_exit_code(first) == 0
    injected_detached_failure = {
        **first,
        "learning": {"status": "failed_closed"},
    }
    assert (
        runtime_module.crypto_runtime_receipt_exit_code(injected_detached_failure) == 0
    )
    capital_before = _capital_bytes(output_root)
    assert capital_before

    replay = run_crypto_delayed_paper_server_once(
        runtime_manifest=manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(seconds=59),
        transport_factory=_factory(FixtureTradingDatasTransport()),
    )
    assert replay["status"] == "noop"
    assert replay["learning_mode"] == "detached_offline_worker"
    assert replay["learning_authority"] is False
    assert replay["learning_invoked"] is False
    assert _capital_bytes(output_root) == capital_before
    assert not (output_root / "evolution").exists()


def test_rechecksummed_event_indexes_remain_anchored_to_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    receipt = run_crypto_delayed_paper_server_once(
        runtime_manifest=_write_manifest(tmp_path),
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(FixtureTradingDatasTransport()),
    )
    assert receipt["status"] == "completed"
    store = CryptoDelayedPaperObservationStore(output_root)
    observation_index = next(store.observation_event_index_dir.glob("*/*.json"))
    row = json.loads(observation_index.read_text(encoding="utf-8"))
    row["counterfactual"] = {
        "forged": True,
        "label_status": "mature",
        "realized_return": "999",
    }
    material = dict(row)
    material.pop("checksum")
    row["checksum"] = hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    encoded = (
        json.dumps(
            row,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    observation_index.write_text(encoded, encoding="utf-8")
    authoritative_index = store.event_index_dir / f"{row['event_id']}.json"
    authoritative_index.write_text(encoded, encoding="utf-8")

    with pytest.raises(
        CryptoDelayedPaperLedgerError,
        match="delayed_paper_decision_event_index_invalid",
    ):
        store.events_for_observation(row["observation_id"])


def test_pending_recovery_precedes_token_read_and_any_http(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    manifest_path = _write_manifest(tmp_path)
    manifest = load_crypto_delayed_paper_runtime_manifest(manifest_path)
    request = runtime_module.crypto_runtime_window_request(
        WINDOW_END + timedelta(seconds=55)
    )
    source_transport = FixtureTradingDatasTransport()
    source_client = client(source_transport)
    snapshot = runtime_module.TradingDatasCryptoFiveMinuteDataPort(
        source_client
    ).load_snapshot(
        profile=manifest.profile,
        request=request,
    )
    observation = runner_module._snapshot_to_observation(snapshot)
    CryptoDelayedPaperObservationStore(output_root).accept(observation)
    factory_calls = 0

    def forbidden_factory(*_: Any, **__: Any) -> Callable[..., HTTPResponse]:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("pending recovery must not construct transport")

    recovered = run_crypto_delayed_paper_server_once(
        runtime_manifest=manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=forbidden_factory,
    )

    assert recovered["status"] == "completed"
    assert recovered["core_result"]["recovered_pending"] is True
    assert factory_calls == 0
    assert recovered["market_data_network_used"] is False
    assert recovered["requested_window_consumed"] is True
    assert recovered["recovered_observations"] == [
        {
            "observation_id": observation["observation_id"],
            "market_slot": observation["market_slot"],
            "source_profile_sha256": observation["profile_sha256"],
            "source_catalog_version": None,
            "runtime_manifest_profile_used_for_recovery": False,
        }
    ]
    assert (output_root / "capital" / "events.jsonl").is_file()


def test_cross_slot_pending_recovery_then_catches_up_current_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    manifest_path = _write_manifest(tmp_path)
    manifest = load_crypto_delayed_paper_runtime_manifest(manifest_path)
    request = runtime_module.crypto_runtime_window_request(
        WINDOW_END + timedelta(seconds=55)
    )
    source_client = client(FixtureTradingDatasTransport())
    snapshot = runtime_module.TradingDatasCryptoFiveMinuteDataPort(
        source_client
    ).load_snapshot(
        profile=manifest.profile,
        request=request,
    )
    observation = runner_module._snapshot_to_observation(snapshot)
    CryptoDelayedPaperObservationStore(output_root).accept(observation)
    current_transport = _shifted_transport(5)

    receipt = run_crypto_delayed_paper_server_once(
        runtime_manifest=manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(minutes=5, seconds=55),
        transport_factory=_factory(current_transport),
    )

    assert receipt["status"] == "completed"
    assert receipt["requested_window_end"].endswith("01:10:00Z")
    assert receipt["requested_window_consumed"] is True
    assert receipt["processed_cycle_count"] == 2
    assert receipt["recovered_cycle_count"] == 1
    assert receipt["fresh_cycle_count"] == 1
    assert [item["cycle_kind"] for item in receipt["cycle_results"]] == [
        "pending_recovery",
        "fresh_query",
    ]
    assert receipt["market_data_access_attempt_count"] == 1
    assert receipt["market_data_network_used"] is True
    assert (
        len(list((output_root / "delayed_paper" / "completions").glob("*.json"))) == 2
    )


def test_pending_recovery_provenance_is_not_overwritten_by_new_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    original_manifest_path = _write_manifest(tmp_path / "original")
    original = load_crypto_delayed_paper_runtime_manifest(original_manifest_path)
    request = runtime_module.crypto_runtime_window_request(
        WINDOW_END + timedelta(seconds=55)
    )
    snapshot = runtime_module.TradingDatasCryptoFiveMinuteDataPort(
        client(FixtureTradingDatasTransport())
    ).load_snapshot(profile=original.profile, request=request)
    observation = runner_module._snapshot_to_observation(snapshot)
    CryptoDelayedPaperObservationStore(output_root).accept(observation)
    drift_manifest_path = _write_manifest(
        tmp_path / "drift",
        payload=_manifest_payload(catalog_version="formal-catalog-v2"),
    )
    drift = load_crypto_delayed_paper_runtime_manifest(drift_manifest_path)

    def forbidden_factory(*_: Any, **__: Any) -> Callable[..., HTTPResponse]:
        raise AssertionError("same-slot pending recovery must not use transport")

    receipt = run_crypto_delayed_paper_server_once(
        runtime_manifest=drift_manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=forbidden_factory,
    )

    assert receipt["status"] == "completed"
    assert receipt["market_data_network_used"] is False
    assert receipt["fresh_query_catalog_version"] == "formal-catalog-v2"
    assert receipt["fresh_query_profile_sha256"] == drift.profile.sha256
    recovered = receipt["recovered_observations"][0]
    assert recovered["source_profile_sha256"] == observation["profile_sha256"]
    assert recovered["source_profile_sha256"] != drift.profile.sha256
    assert recovered["source_catalog_version"] is None
    assert recovered["runtime_manifest_profile_used_for_recovery"] is False


def test_bounded_backlog_progresses_without_skipping_slots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    manifest_path = _write_manifest(tmp_path)
    first = run_crypto_delayed_paper_server_once(
        runtime_manifest=manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(FixtureTradingDatasTransport()),
    )
    assert first["status"] == "completed"

    backlog = run_crypto_delayed_paper_server_once(
        runtime_manifest=manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(minutes=15, seconds=55),
        transport_factory=_sequence_factory(
            [_shifted_transport(5), _shifted_transport(10)]
        ),
    )
    assert backlog["status"] == "backlog_pending"
    assert backlog["backlog_remaining"] is True
    assert backlog["requested_window_consumed"] is False
    assert [item["target_window_end"] for item in backlog["cycle_results"]] == [
        "2026-07-19T01:10:00Z",
        "2026-07-19T01:15:00Z",
    ]
    assert runtime_module.crypto_runtime_receipt_exit_code(backlog) == 2

    caught_up = run_crypto_delayed_paper_server_once(
        runtime_manifest=manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(minutes=15, seconds=55),
        transport_factory=_factory(_shifted_transport(15)),
    )
    assert caught_up["status"] == "completed"
    assert caught_up["requested_window_consumed"] is True
    assert caught_up["cycle_results"][0]["target_window_end"] == (
        "2026-07-19T01:20:00Z"
    )
    assert (
        len(list((output_root / "delayed_paper" / "completions").glob("*.json"))) == 4
    )


def test_completed_same_slot_is_noop_without_network_or_duplicate_capital(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    manifest_path = _write_manifest(tmp_path)
    first = run_crypto_delayed_paper_server_once(
        runtime_manifest=manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(FixtureTradingDatasTransport()),
    )
    capital_before = _capital_bytes(output_root)

    def forbidden_factory(*_: Any, **__: Any) -> Callable[..., HTTPResponse]:
        raise AssertionError("completed same slot must not query TradingDatas")

    replay = run_crypto_delayed_paper_server_once(
        runtime_manifest=manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(seconds=59),
        transport_factory=forbidden_factory,
    )

    assert first["status"] == "completed"
    assert replay["status"] == "noop"
    assert replay["processed_cycle_count"] == 0
    assert replay["market_data_network_used"] is False
    assert runtime_module.crypto_runtime_receipt_exit_code(replay) == 0
    assert _capital_bytes(output_root) == capital_before


def test_corrupt_capital_is_wrapped_and_cli_stderr_is_always_redacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    manifest_path = _write_manifest(tmp_path)
    run_crypto_delayed_paper_server_once(
        runtime_manifest=manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=WINDOW_END + timedelta(seconds=55),
        transport_factory=_factory(FixtureTradingDatasTransport()),
    )
    capital_path = output_root / "capital" / "events.jsonl"
    capital_path.write_text("{not-json}\n", encoding="utf-8")

    with pytest.raises(
        CryptoDelayedPaperRuntimeError,
        match="runtime_core_cycle_failed",
    ):
        run_crypto_delayed_paper_server_once(
            runtime_manifest=manifest_path,
            token_file=token_file,
            output_root=output_root,
            now=WINDOW_END + timedelta(minutes=5, seconds=55),
            transport_factory=_factory(_shifted_transport(5)),
        )

    def damaged_runtime(**_: Any) -> dict[str, Any]:
        raise CryptoDelayedPaperRuntimeError("sensitive/internal/path")

    monkeypatch.setattr(
        runtime_module,
        "run_crypto_delayed_paper_server_once",
        damaged_runtime,
    )
    exit_code = runtime_module.main(
        [
            "--runtime-manifest",
            str(manifest_path),
            "--token-file",
            str(token_file),
            "--output-root",
            str(output_root),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err.strip() == "crypto delayed paper runtime failed closed"
    assert "traceback" not in captured.err.lower()
    assert "sensitive" not in captured.err.lower()


def test_cli_requires_exact_service_paths_and_reports_failure_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = _write_manifest(tmp_path)

    exit_code = runtime_module.main(
        [
            "--runtime-manifest",
            str(manifest_path),
            "--token-file",
            str(tmp_path / "wrong.token"),
            "--output-root",
            str(tmp_path / "wrong-output"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err.strip() == "crypto delayed paper runtime failed closed"
    assert "token" not in captured.err.lower()


def test_runtime_timestamp_must_be_utc_and_uses_closed_bar_settle_delay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    manifest_path = _write_manifest(tmp_path)

    with pytest.raises(
        CryptoDelayedPaperRuntimeError,
        match="runtime_now_must_be_utc",
    ):
        run_crypto_delayed_paper_server_once(
            runtime_manifest=manifest_path,
            token_file=token_file,
            output_root=output_root,
            now=datetime(2026, 7, 19, 1, 5),
            transport_factory=_factory(FixtureTradingDatasTransport()),
        )

    request = runtime_module.crypto_runtime_window_request(
        datetime(2026, 7, 19, 1, 5, 54, tzinfo=timezone.utc)
    )
    assert request.window_end == datetime(
        2026,
        7,
        19,
        1,
        0,
        tzinfo=timezone.utc,
    )
    request_after_settle = runtime_module.crypto_runtime_window_request(
        datetime(2026, 7, 19, 1, 5, 55, tzinfo=timezone.utc)
    )
    assert request_after_settle.window_end == WINDOW_END
