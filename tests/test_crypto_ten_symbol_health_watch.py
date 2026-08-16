from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Callable

import pytest

import Crypto.ten_symbol_health_watch as health_module
import Crypto.ten_symbol_observation_runtime as runtime_module
from Crypto.market_observation import (
    OBSERVATION_SYMBOLS,
    build_spread_event_block,
)
from Crypto.ten_symbol_health_watch import (
    CryptoTenSymbolHealthWatchError,
    build_ten_symbol_health_report,
    health_watch_exit_code,
    main as health_main,
    run_health_watch_once,
)
from Crypto.ten_symbol_observation_profile import (
    CryptoTenSymbolObservationProfile,
)
from Crypto.ten_symbol_observation_store import (
    CryptoTenSymbolObservationStore,
)
from shared.data.sharedsignals_v1 import HTTPResponse, parse_catalog_envelope
from tests.test_crypto_ten_symbol_support import (
    CATALOG_VERSION,
    TenSymbolFixtureTransport,
    book_ticker_catalog_row,
    catalog_payload,
    catalog_row,
    collect_fixture_spreads_sidecar,
    iso,
    query_metadata,
)


NOW = datetime(2026, 8, 10, 2, 0, 0, tzinfo=timezone.utc)
LATEST_SLOT = datetime(2026, 8, 10, 1, 55, tzinfo=timezone.utc)
OI_DATASET = "crypto.perp.binance.btcusdt.open_interest"
PROFILE_SHA = "1" * 64


def _slots(count: int, *, latest: datetime = LATEST_SLOT) -> list[datetime]:
    return [latest - timedelta(minutes=5 * offset) for offset in range(count - 1, -1, -1)]


def _observation_event(
    slot: datetime,
    *,
    seed: str,
    spread_block: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "contract": "tradingagent.crypto.ten_symbol_observation_event.v1",
        "event_id": f"crypto-ten-observation-{seed * 12}",
        "event_type": "observation",
        "market": "crypto",
        "market_session": "24x7",
        "window_end": iso(slot),
        "observation_cutoff": iso(slot + timedelta(seconds=55)),
        "catalog_version": CATALOG_VERSION,
        "profile_sha256": PROFILE_SHA,
        "observation": {
            "contract": "tradingagent.crypto.market_observation.v1",
            "window_end": iso(slot),
            "observation_sha256": seed[0] * 64,
        },
        "authority": "none",
        "execution_eligible": False,
        "capital_write_eligible": False,
        "model_authority": False,
    }
    if spread_block is not None:
        event["spread"] = spread_block
    return event


def _reject_event(slot: datetime, *, tag: str) -> dict[str, Any]:
    return {
        "contract": "tradingagent.crypto.ten_symbol_observation_event.v1",
        "event_id": f"crypto-ten-data-reject-{tag}",
        "event_type": "data_reject",
        "market": "crypto",
        "market_session": "24x7",
        "window_end": iso(slot),
        "observation_cutoff": iso(slot + timedelta(seconds=55)),
        "catalog_version": CATALOG_VERSION,
        "profile_sha256": PROFILE_SHA,
        "reason_code": "crypto_observation_watermark_invalid",
        "authority": "none",
        "execution_eligible": False,
        "capital_write_eligible": False,
        "model_authority": False,
    }


def _completed_spread_block(store: CryptoTenSymbolObservationStore, slot: datetime) -> dict[str, Any]:
    sidecar = collect_fixture_spreads_sidecar(
        slot,
        profile_sha256=PROFILE_SHA,
        observed_at=slot + timedelta(seconds=20),
    )
    store.write_spreads_sidecar(sidecar)
    return build_spread_event_block(
        entries=sidecar["entries"],
        catalog_version=CATALOG_VERSION,
        spread_sha256=sidecar["spread_sha256"],
    )


def _build_store(
    root: Path,
    slots: list[datetime],
    *,
    spreads: bool = True,
    skip_last_sidecar: bool = False,
) -> CryptoTenSymbolObservationStore:
    store = CryptoTenSymbolObservationStore(root)
    for index, slot in enumerate(slots):
        block = None
        if spreads:
            if skip_last_sidecar and index == len(slots) - 1:
                sidecar = collect_fixture_spreads_sidecar(
                    slot,
                    profile_sha256=PROFILE_SHA,
                    observed_at=slot + timedelta(seconds=20),
                )
                block = build_spread_event_block(
                    entries=sidecar["entries"],
                    catalog_version=CATALOG_VERSION,
                    spread_sha256=sidecar["spread_sha256"],
                )
            else:
                block = _completed_spread_block(store, slot)
        store.append_event(
            _observation_event(slot, seed=f"{index:02x}", spread_block=block)
        )
    return store


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _profile() -> CryptoTenSymbolObservationProfile:
    return CryptoTenSymbolObservationProfile.from_catalog(
        parse_catalog_envelope(catalog_payload()),
        expected_catalog_version=CATALOG_VERSION,
    )


def _manifest(root: Path) -> Any:
    profile = _profile()
    return health_module.CryptoTenSymbolObservationRuntimeManifest(
        base_url="http://127.0.0.1:18083",
        catalog_version=CATALOG_VERSION,
        access_policy_id="fixture-ten-symbol-observation",
        output_root=root,
        profile=profile,
        profile_sha256=profile.profile_sha256,
        sha256="0" * 64,
    )


def _oi_catalog_row() -> dict[str, Any]:
    return {
        "dataset_id": OI_DATASET,
        "schema_major": 1,
        "default_fields": ["symbol", "open_interest"],
        "default_order": ["symbol:asc"],
        "identity_fields": ["symbol"],
        "fields": [],
        "filter_operators": {"symbol": ["eq", "in"]},
        "limits": {"max_page_size": 500, "max_lookback_days": 365},
        "availability": {
            "entitlement_states": ["active"],
            "activation_states": ["active"],
        },
        "queryability": {"queryable": True, "reasons": []},
    }


def _catalog_rows(*, include_open_interest: bool = True) -> list[dict[str, Any]]:
    rows = [catalog_row(symbol) for symbol in OBSERVATION_SYMBOLS]
    rows += [book_ticker_catalog_row(symbol) for symbol in OBSERVATION_SYMBOLS]
    if include_open_interest:
        rows.append(_oi_catalog_row())
    return rows


class HealthFixtureTransport(TenSymbolFixtureTransport):
    """Extend the ten-symbol fixture with one open_interest dataset."""

    def __init__(
        self,
        *,
        oi_observed_at: datetime | None = None,
        catalog_get_status: int = 200,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.oi_observed_at = oi_observed_at
        self.catalog_get_status = catalog_get_status

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        if kwargs["method"] == "GET":
            if self.catalog_get_status != 200:
                return HTTPResponse(
                    self.catalog_get_status, {"error": "fixture catalog failure"}
                )
            return HTTPResponse(200, catalog_payload(rows=self.catalog_rows))
        body = kwargs["json_body"]
        dataset_id = body["dataset_id"]
        if dataset_id.endswith(".open_interest"):
            observed_at = self.oi_observed_at or NOW
            return HTTPResponse(
                200,
                {
                    "api_version": "v1",
                    "catalog_version": CATALOG_VERSION,
                    "request_id": f"fixture-query-{dataset_id}",
                    "dataset_id": dataset_id,
                    "data": [{"symbol": "BTCUSDT", "open_interest": "12345.0"}][
                        : int(body["limit"])
                    ],
                    "next_cursor": None,
                    "metadata": query_metadata(
                        dataset_id,
                        data_through=observed_at,
                        observed_at=observed_at,
                    ),
                },
            )
        return super().__call__(**kwargs)


def _factory(transport: Callable[..., HTTPResponse]) -> Callable[..., Any]:
    def build(
        transport_id: str,
        *,
        token_file: Path,
        base_url: str,
    ) -> Callable[..., HTTPResponse]:
        return transport

    return build


def _td_transport() -> HealthFixtureTransport:
    return HealthFixtureTransport(
        catalog_rows=_catalog_rows(),
        book_ticker_observed_at=NOW - timedelta(seconds=60),
        oi_observed_at=NOW - timedelta(seconds=30),
    )


def test_health_report_all_green_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    root = tmp_path / "store"
    _build_store(root, _slots(12))
    before = _tree_bytes(root)

    report = build_ten_symbol_health_report(
        store_root=root,
        now=NOW,
        runtime_manifest=_manifest(root),
        token_file=tmp_path / "token",
        transport_factory=_factory(_td_transport()),
    )

    assert _tree_bytes(root) == before
    assert report["contract"] == "tradingagent.crypto.ten_symbol_health_watch.v1"
    assert report["status"] == "ok"
    assert report["checks"]["observation_chain_lag"]["status"] == "ok"
    assert report["checks"]["observation_chain_lag"]["evidence"]["lag_seconds"] == 300
    assert report["checks"]["reject_gap_rate"]["status"] == "ok"
    assert report["checks"]["spread_sampling"]["status"] == "ok"
    td = report["checks"]["tradingdatas"]
    assert td["status"] == "ok"
    assert td["evidence"]["catalog_version"] == CATALOG_VERSION
    assert td["evidence"]["families"]["bars"]["status"] == "ok"
    assert td["evidence"]["families"]["book_ticker"]["status"] == "ok"
    assert td["evidence"]["families"]["open_interest"]["status"] == "ok"
    assert td["evidence"]["families"]["open_interest"]["dataset_id"] == OI_DATASET
    assert report["network_used"] is True
    assert report["authority"] == "none"
    assert report["read_only"] is True
    assert report["real_trading_enabled"] is False
    assert report["store_write_eligible"] is False
    assert health_watch_exit_code(report) == 0


def test_health_detects_stalled_chain(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = _build_store(root, _slots(12))
    pending_slot = LATEST_SLOT + timedelta(minutes=5)
    store.set_pending(
        {
            "window_end": iso(pending_slot),
            "observation_cutoff": iso(pending_slot + timedelta(seconds=55)),
            "profile_sha256": PROFILE_SHA,
            "catalog_version": CATALOG_VERSION,
        }
    )

    report = build_ten_symbol_health_report(
        store_root=root,
        now=NOW + timedelta(minutes=20),
    )

    lag = report["checks"]["observation_chain_lag"]
    assert lag["status"] == "failed"
    assert lag["reason_code"] == "crypto_ten_symbol_health_chain_stalled"
    assert lag["evidence"]["lag_seconds"] == 1500
    assert lag["evidence"]["pending_window_end"] == iso(pending_slot)
    assert report["status"] == "failed"
    assert health_watch_exit_code(report) == 2


def test_health_flags_one_missed_cycle_as_degraded(tmp_path: Path) -> None:
    root = tmp_path / "store"
    _build_store(root, _slots(12))

    report = build_ten_symbol_health_report(
        store_root=root,
        now=NOW + timedelta(minutes=7),
    )

    lag = report["checks"]["observation_chain_lag"]
    assert lag["status"] == "degraded"
    assert lag["reason_code"] == "crypto_ten_symbol_health_chain_lagging"
    assert report["status"] == "degraded"
    assert health_watch_exit_code(report) == 1


def test_health_reject_gap_rate_over_threshold_fails(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = _build_store(root, _slots(12))
    for index in range(4):
        store.append_event(_reject_event(_slots(12)[index], tag=f"t{index}"))

    report = build_ten_symbol_health_report(store_root=root, now=NOW)

    rate = report["checks"]["reject_gap_rate"]
    assert rate["status"] == "failed"
    assert rate["reason_code"] == "crypto_ten_symbol_health_reject_gap_rate_exceeded"
    assert rate["evidence"]["data_reject_count"] == 4
    assert rate["evidence"]["reject_gap_ratio"] == pytest.approx(4 / 12)
    assert report["status"] == "failed"
    assert health_watch_exit_code(report) == 2


def test_health_single_reject_degrades(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = _build_store(root, _slots(12))
    store.append_event(_reject_event(LATEST_SLOT, tag="only"))

    report = build_ten_symbol_health_report(store_root=root, now=NOW)

    rate = report["checks"]["reject_gap_rate"]
    assert rate["status"] == "degraded"
    assert rate["reason_code"] == "crypto_ten_symbol_health_reject_gap_present"
    assert report["status"] == "degraded"
    assert health_watch_exit_code(report) == 1


def test_health_missing_spread_sidecar_degrades(tmp_path: Path) -> None:
    root = tmp_path / "store"
    _build_store(root, _slots(12), skip_last_sidecar=True)

    report = build_ten_symbol_health_report(store_root=root, now=NOW)

    spread = report["checks"]["spread_sampling"]
    assert spread["status"] == "degraded"
    assert spread["reason_code"] == "crypto_ten_symbol_health_spread_sampling_impaired"
    assert spread["evidence"]["missing_sidecar_slots"] == 1
    assert spread["evidence"]["completed_slots"] == 12
    assert report["status"] == "degraded"
    assert health_watch_exit_code(report) == 1


def test_health_slots_without_spread_feature_stay_ok(tmp_path: Path) -> None:
    root = tmp_path / "store"
    _build_store(root, _slots(12), spreads=False)

    report = build_ten_symbol_health_report(store_root=root, now=NOW)

    spread = report["checks"]["spread_sampling"]
    assert spread["status"] == "ok"
    assert (
        spread["reason_code"] == "crypto_ten_symbol_health_spread_feature_ineligible"
    )
    assert report["status"] == "ok"


def test_health_corrupt_store_fails_closed_without_writes(tmp_path: Path) -> None:
    root = tmp_path / "store"
    _build_store(root, _slots(12))
    events_path = root / "events.jsonl"
    events_path.write_bytes(events_path.read_bytes() + b"garbage\n")
    before = _tree_bytes(root)

    report = build_ten_symbol_health_report(store_root=root, now=NOW)

    assert _tree_bytes(root) == before
    assert report["status"] == "failed"
    for name in ("observation_chain_lag", "reject_gap_rate", "spread_sampling"):
        assert report["checks"][name]["status"] == "failed"
        assert "ten_symbol_observation" in str(
            report["checks"][name]["reason_code"]
        )
    assert "tradingdatas" not in report["checks"]
    assert health_watch_exit_code(report) == 2


def test_health_td_http_failure_marks_data_plane_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    root = tmp_path / "store"
    _build_store(root, _slots(12))
    transport = HealthFixtureTransport(
        catalog_rows=_catalog_rows(),
        catalog_get_status=500,
    )

    report = build_ten_symbol_health_report(
        store_root=root,
        now=NOW,
        runtime_manifest=_manifest(root),
        token_file=tmp_path / "token",
        transport_factory=_factory(transport),
    )

    td = report["checks"]["tradingdatas"]
    assert td["status"] == "failed"
    assert td["reason_code"] == "crypto_ten_symbol_health_td_http_unavailable"
    assert report["checks"]["observation_chain_lag"]["status"] == "ok"
    assert report["status"] == "failed"
    assert report["network_used"] is False
    assert health_watch_exit_code(report) == 2


def test_health_td_stale_bars_degrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    root = tmp_path / "store"
    _build_store(root, _slots(12))

    def stale_bars(
        dataset_id: str,
        window_end: datetime,
        metadata: dict[str, Any],
    ) -> None:
        stale = window_end - timedelta(hours=2)
        metadata["data_through"] = iso(stale)
        metadata["observed_at"] = iso(stale)

    transport = HealthFixtureTransport(
        catalog_rows=_catalog_rows(),
        metadata_mutator=stale_bars,
        book_ticker_observed_at=NOW - timedelta(seconds=60),
        oi_observed_at=NOW - timedelta(seconds=30),
    )

    report = build_ten_symbol_health_report(
        store_root=root,
        now=NOW,
        runtime_manifest=_manifest(root),
        token_file=tmp_path / "token",
        transport_factory=_factory(transport),
    )

    td = report["checks"]["tradingdatas"]
    assert td["status"] == "degraded"
    bars = td["evidence"]["families"]["bars"]
    assert bars["status"] == "degraded"
    assert bars["freshness_reason"] == "stale"
    assert report["status"] == "degraded"
    assert health_watch_exit_code(report) == 1


def test_health_td_open_interest_absent_degrades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    root = tmp_path / "store"
    _build_store(root, _slots(12))
    transport = HealthFixtureTransport(
        catalog_rows=_catalog_rows(include_open_interest=False),
        book_ticker_observed_at=NOW - timedelta(seconds=60),
    )

    report = build_ten_symbol_health_report(
        store_root=root,
        now=NOW,
        runtime_manifest=_manifest(root),
        token_file=tmp_path / "token",
        transport_factory=_factory(transport),
    )

    td = report["checks"]["tradingdatas"]
    assert td["status"] == "degraded"
    oi = td["evidence"]["families"]["open_interest"]
    assert oi["status"] == "degraded"
    assert oi["reason_code"] == "crypto_ten_symbol_health_td_dataset_absent"
    assert health_watch_exit_code(report) == 1


def test_health_td_requires_simulation_gate(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.delenv("REAL_TRADING_ENABLED", raising=False)
    root = tmp_path / "store"
    _build_store(root, _slots(12))

    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("transport must not be constructed without the gate")

    report = build_ten_symbol_health_report(
        store_root=root,
        now=NOW,
        runtime_manifest=_manifest(root),
        token_file=tmp_path / "token",
        transport_factory=forbidden,
    )

    assert report["checks"]["tradingdatas"]["status"] == "failed"
    assert (
        report["checks"]["tradingdatas"]["reason_code"]
        == "crypto_ten_symbol_health_real_trading_gate"
    )
    monkeypatch.undo()


def _write_manifest(tmp_path: Path, output_root: Path) -> Path:
    profile = _profile()
    payload = {
        "schema": "tradingagent.crypto.ten_symbol_observation_runtime_manifest.v1",
        "base_url": "http://127.0.0.1:18083",
        "catalog_version": CATALOG_VERSION,
        "access_policy_id": "tradingagent-crypto-read-v1",
        "output_root": str(output_root),
        "profile_sha256": profile.profile_sha256,
        "profile": profile.to_payload(),
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


def test_cli_happy_path_prints_machine_readable_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    root = tmp_path / "crypto-ten-symbol-observation"
    token_file = tmp_path / "tradingdatas-crypto-read.token"
    monkeypatch.setattr(runtime_module, "RUNTIME_OUTPUT_ROOT", root)
    monkeypatch.setattr(health_module, "DEFAULT_TOKEN_FILE", token_file)
    now = datetime.now(tz=timezone.utc)
    latest = (now - timedelta(seconds=55)).replace(second=0, microsecond=0)
    latest = latest.replace(minute=latest.minute - latest.minute % 5)
    slots = [latest - timedelta(minutes=5 * i) for i in range(11, -1, -1)]
    _build_store(root, slots)
    manifest_path = _write_manifest(tmp_path, root)
    transport = HealthFixtureTransport(
        catalog_rows=_catalog_rows(),
        book_ticker_observed_at=now,
        oi_observed_at=now,
    )
    monkeypatch.setattr(
        health_module,
        "build_runtime_transport",
        _factory(transport),
    )
    before = _tree_bytes(root)

    code = health_main(
        [
            "--store-root",
            str(root),
            "--runtime-manifest",
            str(manifest_path),
        ]
    )

    assert code == 0
    assert _tree_bytes(root) == before
    report = json.loads(capsys.readouterr().out)
    assert report["contract"] == "tradingagent.crypto.ten_symbol_health_watch.v1"
    assert report["status"] == "ok"
    assert report["authority"] == "none"
    assert set(report["checks"]) == {
        "observation_chain_lag",
        "reject_gap_rate",
        "spread_sampling",
        "tradingdatas",
    }


def test_cli_store_root_mismatch_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    root = tmp_path / "crypto-ten-symbol-observation"
    other = tmp_path / "other-root"
    monkeypatch.setattr(runtime_module, "RUNTIME_OUTPUT_ROOT", root)
    monkeypatch.setattr(health_module, "DEFAULT_TOKEN_FILE", tmp_path / "token")
    _build_store(other, _slots(3))
    manifest_path = _write_manifest(tmp_path, root)

    code = health_main(
        [
            "--store-root",
            str(other),
            "--runtime-manifest",
            str(manifest_path),
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "failed closed" in captured.err


def test_cli_relative_store_root_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    root = tmp_path / "crypto-ten-symbol-observation"
    monkeypatch.setattr(runtime_module, "RUNTIME_OUTPUT_ROOT", root)
    monkeypatch.setattr(health_module, "DEFAULT_TOKEN_FILE", tmp_path / "token")
    manifest_path = _write_manifest(tmp_path, root)

    code = health_main(
        [
            "--store-root",
            "relative/path",
            "--runtime-manifest",
            str(manifest_path),
        ]
    )

    assert code == 2
    assert capsys.readouterr().out == ""


def test_cli_missing_manifest_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    root = tmp_path / "crypto-ten-symbol-observation"
    monkeypatch.setattr(runtime_module, "RUNTIME_OUTPUT_ROOT", root)
    monkeypatch.setattr(health_module, "DEFAULT_TOKEN_FILE", tmp_path / "token")

    code = health_main(
        [
            "--store-root",
            str(root),
            "--runtime-manifest",
            str(tmp_path / "absent.json"),
        ]
    )

    assert code == 2
    assert capsys.readouterr().out == ""


def test_run_health_watch_once_rejects_foreign_token_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    root = tmp_path / "crypto-ten-symbol-observation"
    monkeypatch.setattr(runtime_module, "RUNTIME_OUTPUT_ROOT", root)
    monkeypatch.setattr(health_module, "DEFAULT_TOKEN_FILE", tmp_path / "token")
    manifest_path = _write_manifest(tmp_path, root)

    with pytest.raises(CryptoTenSymbolHealthWatchError):
        run_health_watch_once(
            store_root=root,
            runtime_manifest=manifest_path,
            token_file=tmp_path / "other.token",
            now=NOW,
        )


def test_exit_code_rejects_authority_drift(tmp_path: Path) -> None:
    root = tmp_path / "store"
    _build_store(root, _slots(12))
    report = build_ten_symbol_health_report(store_root=root, now=NOW)
    assert health_watch_exit_code(report) == 0
    tampered = {**report, "authority": "research"}
    assert health_watch_exit_code(tampered) == 2
    assert health_watch_exit_code({"status": "ok"}) == 2
