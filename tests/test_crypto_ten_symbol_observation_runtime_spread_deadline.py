"""Offline deadline/orphan regressions using the real forty-symbol runtime."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

import Crypto.ten_symbol_observation_runtime as runtime
from Crypto.ten_symbol_observation_profile import CryptoTenSymbolObservationProfile
from Crypto.ten_symbol_observation_store import CryptoTenSymbolObservationStore
from shared.data.sharedsignals_v1 import HTTPResponse, parse_catalog_envelope
from tests.test_crypto_forty_symbol_universe import FortySymbolFixtureTransport
from tests.test_crypto_ten_symbol_observation_runtime import (
    _assert_recursive_non_authority,
    _factory,
    _forbidden_factory,
    _manifest_payload,
    _runtime_paths,
    _write_manifest,
)
from tests.test_crypto_ten_symbol_support import (
    WINDOW_END,
    book_ticker_catalog_row,
    iso,
)


@pytest.fixture
def subject(monkeypatch, tmp_path):
    token, _ = _runtime_paths(monkeypatch, tmp_path)
    root = tmp_path / "crypto-40-symbol-observation"
    config = replace(runtime.FORTY_SYMBOL_RUNTIME_CONFIG, output_root=root)
    catalog = parse_catalog_envelope(
        FortySymbolFixtureTransport()(method="GET").json_body
    )
    profile = CryptoTenSymbolObservationProfile.from_catalog(
        catalog,
        expected_catalog_version=catalog.catalog_version,
        symbols=config.symbols,
        profile_contract=config.profile_contract,
    )
    payload = _manifest_payload(root)
    payload.update(
        schema=config.manifest_contract,
        catalog_version=profile.catalog_version,
        profile=profile.to_payload(),
        profile_sha256=profile.profile_sha256,
    )
    manifest = _write_manifest(tmp_path, payload=payload)
    store = CryptoTenSymbolObservationStore(root, contracts=config.store_contracts)
    clock = [0.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])

    def run(end=WINDOW_END, *, transport=None):
        clock[0] = 0.0
        return runtime.run_crypto_ten_symbol_observation_once(
            runtime_manifest=manifest,
            token_file=token,
            output_root=root,
            now=end + timedelta(seconds=285),
            config=config,
            invocation_budget_seconds=300.0,
            transport_factory=(
                _forbidden_factory if transport is None else _factory(transport)
            ),
            retry_sleep=lambda _: pytest.fail("unexpected retry sleep"),
        )

    return SimpleNamespace(run=run, store=store, clock=clock, config=config)


def _spread_deadline_transport(subject, phase="catalog"):
    fixture = FortySymbolFixtureTransport(
        observed_at=WINDOW_END + timedelta(seconds=20)
    )
    current_bar_reads = 0

    def exhaust():
        # Check actual persisted rows/digests before the optional wire deadline.
        sidecar = subject.store.read_bars_sidecar(iso(WINDOW_END))
        subject.persisted_at_deadline = sidecar
        if sidecar is not None:
            observation, rows = runtime.observation_from_ten_symbol_bars_sidecar(
                sidecar,
                symbols=subject.config.symbols,
                bars_sidecar_contract=subject.config.bars_sidecar_contract,
            )
            assert len(observation.sources) == 40
            assert sum(map(len, rows.values())) == 520
        subject.clock[0] = 300.0
        raise TimeoutError("fixture optional wire deadline")

    def transport(**request):
        nonlocal current_bar_reads
        body = request.get("json_body")
        if request["method"] == "GET" and current_bar_reads == 40:
            if phase == "catalog":
                exhaust()
            response = fixture(**request)
            payload = dict(response.json_body)
            payload["data"] = list(payload["data"]) + [
                book_ticker_catalog_row(symbol) for symbol in subject.config.symbols
            ]
            return HTTPResponse(200, payload)
        if isinstance(body, dict) and body["dataset_id"].endswith(".book_ticker"):
            exhaust()
        response = fixture(**request)
        if isinstance(body, dict):
            last_open = body["filters"]["open_time"]["between"][1]
            if last_open != (WINDOW_END - timedelta(minutes=5)).isoformat():
                payload = dict(response.json_body)
                payload["data"] = payload["data"][:11]
                return HTTPResponse(200, payload)
            current_bar_reads += 1
        return response

    return transport


@pytest.mark.parametrize("phase", ["catalog", "book_ticker"])
@pytest.mark.parametrize("next_slot", [False, True], ids=["same-slot", "next-slot"])
@pytest.mark.parametrize("gap", [False, True], ids=["fresh", "gap"])
def test_spread_deadline_preserves_bars_and_recovers_without_refetch(
    subject, phase, next_slot, gap
):
    prior = WINDOW_END - timedelta(minutes=30)
    if gap:
        first = subject.run(prior, transport=FortySymbolFixtureTransport())
        assert first["status"] == "completed"
    before = subject.store.checkpoint()
    receipt = subject.run(transport=_spread_deadline_transport(subject, phase))
    assert receipt["status"] == "backlog_pending"
    assert receipt["budget_deferred"] is True
    assert receipt["requested_window_consumed"] is False
    assert receipt["processed_cycle_count"] == (1 if gap else 0)
    assert receipt["collect_attempts"] == (2 if gap else 1)
    assert subject.persisted_at_deadline is not None
    deferred = subject.store.checkpoint()
    assert deferred["observation_count"] == before["observation_count"]
    assert deferred["data_gap_count"] == before["data_gap_count"]
    assert deferred["data_reject_count"] == before["data_reject_count"] + int(gap)
    assert (
        subject.store.checkpoint()["latest_terminal_slot"]
        == before["latest_terminal_slot"]
    )
    assert subject.store.pending_record()["window_end"] == iso(WINDOW_END)
    sidecar_path = subject.store.bars_sidecar_path(iso(WINDOW_END))
    saved = sidecar_path.read_bytes()
    assert not subject.store.spreads_sidecar_path(iso(WINDOW_END)).exists()
    _assert_recursive_non_authority(receipt)

    recovery_end = WINDOW_END + timedelta(minutes=5 if next_slot else 0)
    new_queries = []
    fixture = FortySymbolFixtureTransport(
        observed_at=recovery_end + timedelta(seconds=20)
    )

    def only_new_slot(**request):
        body = request.get("json_body")
        if isinstance(body, dict):
            last_open = body["filters"]["open_time"]["between"][1]
            assert last_open == (recovery_end - timedelta(minutes=5)).isoformat()
            new_queries.append(body)
        return fixture(**request)

    recovered = subject.run(
        recovery_end, transport=only_new_slot if next_slot else None
    )
    assert recovered["status"] == "completed"
    assert recovered["budget_deferred"] is False
    assert recovered["requested_window_consumed"] is True
    assert recovered["collect_attempts"] == (1 if next_slot else 0)
    assert len(new_queries) == (40 if next_slot else 0)
    assert recovered["recovered_observations"][0]["network_used"] is False
    assert sidecar_path.read_bytes() == saved
    assert subject.store.pending_record() is None
    kind = "data_gap" if gap else "observation"
    event = subject.store.event_for_slot(kind, iso(WINDOW_END))
    assert event["observation_cutoff"] == iso(WINDOW_END + timedelta(seconds=270))
    assert event["spread"]["reason_code"] == "crypto_spread_sidecar_missing"
    if gap:
        assert event["prior_market_slot"] == iso(prior)
        assert event["skipped_from"] == iso(prior + timedelta(minutes=5))
        assert event["skipped_to"] == iso(WINDOW_END - timedelta(minutes=5))
        assert recovered["outage_gap_recovered"] is True
    after = subject.store.checkpoint()
    assert subject.run(recovery_end)["status"] == "noop"
    assert subject.store.checkpoint() == after


@pytest.mark.parametrize(
    "fault",
    ["wire-deadline", "validation-deadline", "sidecar-deadline", "late-last-bar"],
)
def test_bar_failure_never_persists_partial_or_late_bars(subject, monkeypatch, fault):
    fixture = FortySymbolFixtureTransport()
    if fault in {"validation-deadline", "sidecar-deadline"}:
        target = (
            "_collect_market_observation_rows_with_catalog"
            if fault == "validation-deadline"
            else "build_ten_symbol_bars_sidecar"
        )
        original = getattr(runtime, target)

        def expired_after_validation(*args, **kwargs):
            result = original(*args, **kwargs)
            subject.clock[0] = 300.0
            return result

        monkeypatch.setattr(runtime, target, expired_after_validation)

    def transport(**request):
        response = fixture(**request)
        if request["method"] != "GET":
            if fault == "wire-deadline":
                subject.clock[0] = 300.0
                raise TimeoutError("fixture bar wire deadline")
            if (
                fault == "late-last-bar"
                and request["json_body"]["filters"]["symbol"]["eq"]
                == subject.config.symbols[-1]
            ):
                payload = dict(response.json_body)
                payload["metadata"]["observed_at"] = iso(
                    WINDOW_END + timedelta(seconds=271)
                )
                return HTTPResponse(200, payload)
        return response

    receipt = subject.run(transport=transport)
    assert subject.store.read_bars_sidecar(iso(WINDOW_END)) is None
    assert subject.store.checkpoint()["latest_terminal_slot"] is None
    if fault == "late-last-bar":
        assert receipt["status"] == "data_reject"
        assert receipt["core_result"]["reason_code"] == (
            "crypto_observation_observed_at_after_cutoff"
        )
    else:
        assert receipt["status"] == "backlog_pending"
        assert receipt["budget_deferred"] is True
        assert subject.store.data_reject_events() == []


@pytest.mark.parametrize("next_slot", [False, True])
@pytest.mark.parametrize("gap", [False, True])
@pytest.mark.parametrize(
    "tamper", ["profile", "pending-profile", "digest", "rows", "family"]
)
def test_orphan_integrity_failure_never_refetches_or_publishes(
    subject, tamper, next_slot, gap
):
    if gap:
        subject.run(
            WINDOW_END - timedelta(minutes=30), transport=FortySymbolFixtureTransport()
        )
    subject.run(transport=_spread_deadline_transport(subject))
    path = subject.store.bars_sidecar_path(iso(WINDOW_END))
    payload = json.loads(path.read_text())
    if tamper == "pending-profile":
        pending = subject.store.pending_record()
        subject.store.clear_pending(iso(WINDOW_END))
        subject.store.set_pending({**pending, "profile_sha256": "0" * 64})
    elif tamper == "profile":
        payload["profile_sha256"] = "0" * 64
    elif tamper == "digest":
        payload["observation_sha256"] = "0" * 64
    elif tamper == "rows":
        payload["sources"][0]["rows"][0]["close"] = "999"
    else:
        payload["contract"] = runtime.TEN_SYMBOL_BARS_SIDECAR_CONTRACT
    path.write_text(json.dumps(payload))
    before = subject.store.checkpoint()
    with pytest.raises(runtime.CryptoTenSymbolObservationRuntimeError):
        subject.run(WINDOW_END + timedelta(minutes=5 if next_slot else 0))
    assert subject.store.checkpoint() == before
    assert subject.store.pending_record() is not None


@pytest.mark.parametrize("next_slot", [False, True])
@pytest.mark.parametrize("gap", [False, True])
def test_missing_orphan_does_not_become_a_completed_observation(subject, next_slot, gap):
    prior = WINDOW_END - timedelta(minutes=30)
    if gap:
        subject.run(prior, transport=FortySymbolFixtureTransport())
    subject.run(transport=_spread_deadline_transport(subject))
    # Remove only the test's temporary sidecar to model loss before restart.
    subject.store.bars_sidecar_path(iso(WINDOW_END)).unlink()
    recovery_end = WINDOW_END + timedelta(minutes=5 if next_slot else 0)
    fixture = FortySymbolFixtureTransport(
        observed_at=recovery_end + timedelta(seconds=20)
    )
    before = subject.store.checkpoint()
    receipt = subject.run(recovery_end, transport=fixture)
    if next_slot:
        assert receipt["cycle_results"][0]["result"]["status"] == (
            "cleared_unrecoverable_pending"
        )
        assert subject.store.event_for_slot("observation", iso(WINDOW_END)) is None
        assert subject.store.event_for_slot("data_gap", iso(WINDOW_END)) is None
        if gap:
            assert receipt["status"] == "backlog_pending"
            assert receipt["requested_window_consumed"] is False
            assert subject.store.checkpoint()["latest_terminal_slot"] == iso(prior)
            assert subject.store.checkpoint()["observation_count"] == before["observation_count"]
            receipt = subject.run(recovery_end, transport=fixture)
    assert receipt["status"] == "completed"
    assert receipt["collect_attempts"] > 0
    assert subject.store.checkpoint()["latest_terminal_slot"] == iso(recovery_end)


@pytest.mark.parametrize("next_slot", [False, True])
@pytest.mark.parametrize("gap", [False, True])
def test_deadline_immediately_after_bar_write_keeps_pending_recoverable(
    subject, monkeypatch, next_slot, gap
):
    if gap:
        subject.run(
            WINDOW_END - timedelta(minutes=30), transport=FortySymbolFixtureTransport()
        )
    original = CryptoTenSymbolObservationStore.write_bars_sidecar

    def write_then_expire(self, payload):
        result = original(self, payload)
        subject.clock[0] = 300.0
        return result

    monkeypatch.setattr(CryptoTenSymbolObservationStore, "write_bars_sidecar", write_then_expire)
    receipt = subject.run(transport=_spread_deadline_transport(subject))
    assert receipt["budget_deferred"] is True
    assert receipt["requested_window_consumed"] is False
    assert receipt["processed_cycle_count"] == int(gap)
    assert subject.store.pending_record()["window_end"] == iso(WINDOW_END)
    assert subject.store.read_bars_sidecar(iso(WINDOW_END)) is not None
    monkeypatch.setattr(CryptoTenSymbolObservationStore, "write_bars_sidecar", original)
    end = WINDOW_END + timedelta(minutes=5 if next_slot else 0)
    recovered = subject.run(
        end, transport=FortySymbolFixtureTransport() if next_slot else None
    )
    assert recovered["status"] == "completed"
    assert recovered["recovered_observations"][0]["network_used"] is False
    assert recovered["collect_attempts"] == int(next_slot)
    assert subject.store.pending_record() is None


@pytest.mark.parametrize("next_slot", [False, True])
@pytest.mark.parametrize("fault", ["missing", "reason", "profile", "cutoff"])
def test_gap_orphan_requires_original_rejection_proof(
    subject, monkeypatch, fault, next_slot
):
    subject.run(
        WINDOW_END - timedelta(minutes=30), transport=FortySymbolFixtureTransport()
    )
    subject.run(transport=_spread_deadline_transport(subject))
    original = CryptoTenSymbolObservationStore.event_for_slot

    def missing_reject(self, event_type, slot):
        event = original(self, event_type, slot)
        if event_type != "data_reject":
            return event
        if fault == "missing":
            return None
        event = dict(event)
        field, value = {
            "reason": ("reason_code", "crypto_observation_metadata_invalid"),
            "profile": ("profile_sha256", "0" * 64),
            "cutoff": ("observation_cutoff", iso(WINDOW_END)),
        }[fault]
        event[field] = value
        return event

    monkeypatch.setattr(CryptoTenSymbolObservationStore, "event_for_slot", missing_reject)
    with pytest.raises(
        runtime.CryptoTenSymbolObservationRuntimeError,
        match="runtime_outage_gap_reject_missing_or_invalid",
    ):
        subject.run(WINDOW_END + timedelta(minutes=5 if next_slot else 0))
    assert subject.store.data_gap_events() == []
    assert subject.store.pending_record() is not None


@pytest.mark.parametrize("next_slot", [False, True])
@pytest.mark.parametrize("crash_after_set", [False, True])
def test_gap_pending_reuse_crash_preserves_orphan_locator(
    subject, monkeypatch, next_slot, crash_after_set
):
    subject.run(
        WINDOW_END - timedelta(minutes=30), transport=FortySymbolFixtureTransport()
    )
    subject.run(transport=_spread_deadline_transport(subject))
    before = subject.store.checkpoint()
    pending_bytes = subject.store.pending_path.read_bytes()
    bars_bytes = subject.store.bars_sidecar_path(iso(WINDOW_END)).read_bytes()
    original = CryptoTenSymbolObservationStore.set_pending

    def interrupted_set(self, record):
        if crash_after_set:
            original(self, record)
        raise RuntimeError("fixture crash at pending boundary")

    monkeypatch.setattr(CryptoTenSymbolObservationStore, "set_pending", interrupted_set)
    end = WINDOW_END + timedelta(minutes=5 if next_slot else 0)
    with pytest.raises(runtime.CryptoTenSymbolObservationRuntimeError) as caught:
        subject.run(end)
    assert str(caught.value.__cause__) == "fixture crash at pending boundary"
    assert subject.store.checkpoint() == before
    assert subject.store.pending_path.read_bytes() == pending_bytes
    assert subject.store.bars_sidecar_path(iso(WINDOW_END)).read_bytes() == bars_bytes
    monkeypatch.setattr(CryptoTenSymbolObservationStore, "set_pending", original)
    recovered = subject.run(
        end, transport=FortySymbolFixtureTransport() if next_slot else None
    )
    assert recovered["status"] == "completed"
    assert recovered["recovered_observations"][0]["network_used"] is False
    assert recovered["collect_attempts"] == int(next_slot)
    assert subject.store.pending_record() is None


@pytest.mark.parametrize("crash_after_set", [False, True])
def test_initial_gap_pending_write_crash_cannot_publish_or_lose_bars(
    subject, monkeypatch, crash_after_set
):
    prior = WINDOW_END - timedelta(minutes=30)
    subject.run(prior, transport=FortySymbolFixtureTransport())
    original = CryptoTenSymbolObservationStore.set_pending

    def interrupted_set(self, record):
        if record["window_end"] != iso(WINDOW_END):
            return original(self, record)
        if crash_after_set:
            original(self, record)
        raise RuntimeError("fixture initial pending crash")

    monkeypatch.setattr(CryptoTenSymbolObservationStore, "set_pending", interrupted_set)
    with pytest.raises(runtime.CryptoTenSymbolObservationRuntimeError):
        subject.run(transport=_spread_deadline_transport(subject))
    assert subject.store.checkpoint()["latest_terminal_slot"] == iso(prior)
    assert subject.store.data_gap_events() == []
    assert len(subject.store.data_reject_events()) == 1
    assert subject.store.read_bars_sidecar(iso(WINDOW_END)) is None
    pending = subject.store.pending_record()
    assert (pending is not None) == crash_after_set
    if pending:
        assert pending["window_end"] == iso(WINDOW_END)
    monkeypatch.setattr(CryptoTenSymbolObservationStore, "set_pending", original)
    recovered = subject.run(transport=_spread_deadline_transport(subject))
    assert recovered["budget_deferred"] is True
    assert subject.store.pending_record()["window_end"] == iso(WINDOW_END)
    assert subject.run()["status"] == "completed"


@pytest.mark.parametrize(
    "field", ["window_end", "observation_cutoff", "profile_sha256", "catalog_version"]
)
def test_gap_recovery_rejects_nonmatching_pending(subject, field):
    prior = WINDOW_END - timedelta(minutes=30)
    subject.run(prior, transport=FortySymbolFixtureTransport())
    subject.run(transport=_spread_deadline_transport(subject))
    pending = subject.store.pending_record()
    before = subject.store.checkpoint()
    manifest = SimpleNamespace(
        profile=SimpleNamespace(profile_sha256=pending["profile_sha256"]),
        catalog_version=pending["catalog_version"],
    )
    changed = {
        "window_end": iso(WINDOW_END - timedelta(minutes=5)),
        "observation_cutoff": iso(WINDOW_END + timedelta(seconds=269)),
        "profile_sha256": "0" * 64,
        "catalog_version": "fixture-wrong-catalog",
    }
    subject.store.clear_pending(iso(WINDOW_END))
    subject.store.set_pending({**pending, field: changed[field]})
    pending_bytes = subject.store.pending_path.read_bytes()
    with pytest.raises(
        runtime.CryptoTenSymbolObservationRuntimeError,
        match="runtime_outage_gap_pending_forbidden",
    ):
        runtime._attempt_outage_gap_recovery(
            store=subject.store, lazy=None, manifest=manifest,
            prior_market_slot=prior,
            rejected_window=runtime._window_for_end(
                prior + timedelta(minutes=5), config=subject.config
            ),
            current_window=runtime._window_for_end(WINDOW_END, config=subject.config),
            reason_code="crypto_observation_query_shape_invalid",
            config=subject.config,
        )
    assert subject.store.checkpoint() == before
    assert subject.store.pending_path.read_bytes() == pending_bytes
