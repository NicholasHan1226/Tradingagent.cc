from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import Crypto.delayed_paper_epoch as epoch_module
import Crypto.delayed_paper_exit_shadow_worker as worker_module
from Crypto.delayed_paper_epoch import (
    load_crypto_delayed_paper_epoch_manifest,
    prepare_crypto_delayed_paper_epoch,
)
from Crypto.delayed_paper_exit_shadow import (
    CryptoDelayedPaperExitShadowError,
    project_crypto_delayed_paper_exit_shadow,
)
from Crypto.delayed_paper_exit_shadow_worker import (
    exit_shadow_worker_exit_code,
    run_exit_shadow_worker_once,
)
from Crypto.delayed_paper_runner import run_crypto_delayed_paper_once
from Crypto.five_minute_data import (
    CryptoFiveMinuteWindowRequest,
    TradingDatasCryptoFiveMinuteDataPort,
)
from tests.test_crypto_5m_support import (
    BAR_DATASETS,
    RULE_DATASETS,
    SYMBOLS,
    WINDOW_END,
    FixtureTradingDatasTransport,
    bar_rows,
    client,
    iso,
    metadata,
    profile,
    window_request,
)


def _completed_result(root: Path) -> dict[str, Any]:
    transport = FixtureTradingDatasTransport()
    tradingdatas_client = client(transport)
    return run_crypto_delayed_paper_once(
        port=TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client),
        profile=profile(tradingdatas_client),
        request=window_request(),
        output_root=root,
    )


def _shifted_completed_result(root: Path, *, minutes: int) -> dict[str, Any]:
    delta = timedelta(minutes=minutes)
    shifted = bar_rows()
    for row in shifted:
        for field_name in ("open_time", "close_time"):
            parsed = datetime.fromisoformat(str(row[field_name]).replace("Z", "+00:00"))
            row[field_name] = iso(parsed + delta)
    shifted_end = WINDOW_END + delta
    metadata_by_dataset: dict[str, dict[str, Any]] = {}
    for symbol in SYMBOLS:
        metadata_by_dataset[BAR_DATASETS[symbol]] = metadata(
            dataset_id=BAR_DATASETS[symbol],
            data_through=shifted_end - timedelta(milliseconds=1),
            observed_at=shifted_end + timedelta(seconds=20),
        )
        metadata_by_dataset[RULE_DATASETS[symbol]] = metadata(
            dataset_id=RULE_DATASETS[symbol],
            data_through=shifted_end + timedelta(seconds=5),
            observed_at=shifted_end + timedelta(seconds=10),
        )
    transport = FixtureTradingDatasTransport(
        bars=shifted,
        metadata_by_dataset=metadata_by_dataset,
    )
    tradingdatas_client = client(transport)
    return run_crypto_delayed_paper_once(
        port=TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client),
        profile=profile(tradingdatas_client),
        request=CryptoFiveMinuteWindowRequest(
            window_end=shifted_end,
            observation_cutoff=shifted_end + timedelta(seconds=30),
        ),
        output_root=root,
    )


def _capital_bytes(root: Path) -> dict[str, bytes]:
    capital = root / "capital"
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(capital.rglob("*"))
        if path.is_file()
    }


def _epoch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path]:
    epoch_id = "crypto-delayed-paper-epoch-g2-exit-shadow"
    archived_root = tmp_path / "archive"
    epoch_parent = tmp_path / "epochs"
    output_root = epoch_parent / epoch_id
    manifest_path = tmp_path / "crypto-delayed-paper.epoch.json"
    archived_root.mkdir(mode=0o700)
    epoch_parent.mkdir(mode=0o700)
    monkeypatch.setattr(epoch_module, "LEGACY_ARCHIVE_ROOT", archived_root)
    monkeypatch.setattr(epoch_module, "EPOCH_ROOT_PARENT", epoch_parent)
    monkeypatch.setattr(epoch_module, "EPOCH_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(worker_module, "PRODUCTION_EPOCH_MANIFEST", manifest_path)
    payload = {
        "schema": "tradingagent.crypto.delayed_paper_epoch_manifest.v1",
        "epoch_id": epoch_id,
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
    manifest_path.write_text(
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
    manifest_path.chmod(0o600)
    context = load_crypto_delayed_paper_epoch_manifest(manifest_path)
    prepare_crypto_delayed_paper_epoch(context)
    return manifest_path, output_root


def test_exit_shadow_is_idempotent_and_never_mutates_capital(tmp_path: Path) -> None:
    _completed_result(tmp_path)
    capital_before = _capital_bytes(tmp_path)

    first = project_crypto_delayed_paper_exit_shadow(output_root=tmp_path)
    second = project_crypto_delayed_paper_exit_shadow(output_root=tmp_path)

    assert first == second
    assert first["status"] == "projected"
    assert first["shadow_exit_count"] == 0
    assert set(first["symbols"]) == {"BTCUSDT", "ETHUSDT"}
    assert _capital_bytes(tmp_path) == capital_before
    for item in first["symbols"].values():
        assert item["action"] == "hold"
        assert item["reason_code"] == "exit_threshold_not_met"
        assert item["counterfactual_only"] is True
        assert item["execution_authority"] is False
        assert item["capital_commit_id"] is None
        assert item["outbox_id"] is None
        assert item["model_network_used"] is False


def test_exit_shadow_builds_round_trip_after_max_holding_period(
    tmp_path: Path,
) -> None:
    _completed_result(tmp_path)
    _shifted_completed_result(tmp_path, minutes=(24 * 60 + 5))
    capital_before = _capital_bytes(tmp_path)

    result = project_crypto_delayed_paper_exit_shadow(output_root=tmp_path)

    assert result["status"] == "projected"
    assert result["shadow_exit_count"] == 2
    assert _capital_bytes(tmp_path) == capital_before
    for item in result["symbols"].values():
        assert item["action"] == "shadow_exit"
        assert item["reason_code"] == "max_holding_period_reached"
        assert item["holding_seconds"] >= 24 * 60 * 60
        assert item["entry_notional"] is not None
        assert item["entry_fee"] is not None
        assert item["shadow_exit_notional"] is not None
        assert item["shadow_exit_fee"] is not None
        assert item["shadow_net_pnl"] is not None
        assert item["shadow_return_on_entry_cost"] is not None
        assert item["source_entry_receipt_id"].startswith("crypto-paper-receipt-")


def test_exit_shadow_fails_closed_on_source_bundle_tamper(tmp_path: Path) -> None:
    result = _completed_result(tmp_path)
    run_id = result["symbols"]["BTCUSDT"]["bundle"]["run_id"]
    path = tmp_path / "runs" / f"{run_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["decision"]["reason"] = "tampered"
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    capital_before = _capital_bytes(tmp_path)

    with pytest.raises(
        CryptoDelayedPaperExitShadowError,
        match="exit_shadow_source_invalid",
    ):
        project_crypto_delayed_paper_exit_shadow(output_root=tmp_path)

    assert _capital_bytes(tmp_path) == capital_before
    assert not (tmp_path / "evolution" / "exit_shadow").exists()


def test_exit_shadow_worker_is_epoch_bound_and_non_authoritative(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path, output_root = _epoch(monkeypatch, tmp_path)
    _completed_result(output_root)

    result = run_exit_shadow_worker_once(epoch_manifest=manifest_path)

    assert exit_shadow_worker_exit_code(result) == 0
    assert result["status"] == "projected"
    assert result["epoch_generation"] == 2
    assert result["epoch_output_root"] == str(output_root)
    assert result["execution_authority"] is False
    assert result["real_trading_enabled"] is False
    assert result["automatic_promotion_enabled"] is False
    assert result["automatic_risk_expansion_enabled"] is False
