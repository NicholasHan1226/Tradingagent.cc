"""Synthetic local archive checks; no runtime roots or market data are accessed."""
import json
import os
from pathlib import Path

import pytest

from Crypto.delayed_paper_round_trip_epoch import _InvocationArchiveVerification
from Crypto.fixture_auto_sim import run_fixture_auto_sim
from Crypto.fixture_sim.ledger import CryptoCapitalLedger, CryptoLedgerError


def _ledger(tmp_path):
    fixture = Path(__file__).parents[1] / "Crypto/fixtures/auto_sim_spot_cycle_v1.json"
    run_fixture_auto_sim(json.loads(fixture.read_text()), output_root=tmp_path)
    return CryptoCapitalLedger(tmp_path / "capital")


def test_invocation_reuses_replay_only_with_full_content_rechecks(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    original = ledger.head
    calls = []
    def count():
        calls.append(True)
        return original()
    monkeypatch.setattr(ledger, "head", count)
    before = {p: p.read_bytes() for p in ledger.root.iterdir() if p.is_file()}
    verification = _InvocationArchiveVerification()
    heads = [verification.head(ledger) for _ in range(4)]
    assert len(set(heads)) == 1
    assert len(calls) == 1
    assert {p: p.read_bytes() for p in before} == before
    assert _InvocationArchiveVerification().head(ledger) == heads[0]
    assert len(calls) == 2  # A separate invocation cannot inherit the result.


@pytest.mark.parametrize("change", ["events", "head", "replace", "symlink", "missing_lock"])
def test_invocation_rejects_archive_changes_after_replay(tmp_path, change):
    ledger = _ledger(tmp_path)
    verification = _InvocationArchiveVerification()
    verification.head(ledger)
    path = ledger.head_path if change == "head" else ledger.events_path
    if change in {"events", "head"}:
        stamp = path.stat()
        data = path.read_bytes()
        path.write_bytes(data.replace(b'10000', b'10001', 1) if change == "events" else data.replace(b'checksum', b'checksUm', 1))
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    elif change == "replace":
        replacement = path.with_suffix('.replacement')
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
    elif change == "symlink":
        replacement = path.with_suffix('.original')
        path.rename(replacement)
        path.symlink_to(replacement)
    else:
        ledger.lock_path.unlink()
    with pytest.raises((CryptoLedgerError, OSError)):
        verification.head(ledger)


def test_invocation_detects_change_during_initial_replay(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    original = ledger.head
    def replay_then_change():
        result = original()
        ledger.events_path.write_bytes(ledger.events_path.read_bytes() + b'\n')
        return result
    monkeypatch.setattr(ledger, "head", replay_then_change)
    with pytest.raises(CryptoLedgerError, match="snapshot_changed"):
        _InvocationArchiveVerification().head(ledger)


def test_invocation_does_not_hide_initial_invalid_ledger(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.events_path.write_bytes(ledger.events_path.read_bytes() + b'\n')
    with pytest.raises(CryptoLedgerError):
        _InvocationArchiveVerification().head(ledger)


def test_invocation_detects_concurrent_change_before_first_result(tmp_path, monkeypatch):
    from threading import Event, Thread
    ledger = _ledger(tmp_path)
    replayed, changed = Event(), Event()
    original = ledger.head
    def mutate():
        assert replayed.wait(3)
        ledger.head_path.write_bytes(ledger.head_path.read_bytes() + b' ')
        changed.set()
    def replay_while_writer_changes():
        result = original()
        replayed.set()
        assert changed.wait(3)
        return result
    monkeypatch.setattr(ledger, "head", replay_while_writer_changes)
    writer = Thread(target=mutate)
    writer.start()
    try:
        with pytest.raises(CryptoLedgerError, match="snapshot_changed"):
            _InvocationArchiveVerification().head(ledger)
    finally:
        writer.join(3)
    assert not writer.is_alive()


@pytest.mark.parametrize("subclass", [False, True])
def test_prepare_rejects_duck_typed_or_subclass_archive_verifier(subclass):
    from Crypto.delayed_paper_round_trip_epoch import (
        CryptoRoundTripEpochError, prepare_round_trip_epoch_candidate,
    )
    parent = _InvocationArchiveVerification if subclass else object
    class Fake(parent):
        def head(self, ledger):
            raise AssertionError("untrusted verifier must never be called")
    with pytest.raises(CryptoRoundTripEpochError, match="archive_verification_invalid"):
        prepare_round_trip_epoch_candidate(None, _archive_verification=Fake())


@pytest.mark.parametrize("changed_path", ["events", "lock", "root"])
def test_cached_hit_rechecks_all_paths_after_cross_file_reads(tmp_path, monkeypatch, changed_path):
    ledger = _ledger(tmp_path)
    verification = _InvocationArchiveVerification()
    verification.head(ledger)
    original_open = Path.open
    mutated = []
    def open_then_mutate(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path == ledger.head_path and args == ("rb",) and not mutated:
            mutated.append(True)
            if changed_path == "events":
                with original_open(ledger.events_path, "ab") as writer:
                    writer.write(b'\n')
            elif changed_path == "lock":
                with original_open(ledger.lock_path, "ab") as writer:
                    writer.write(b'changed')
            else:
                # A directory entry mutation changes root metadata while leaving
                # event/head content unchanged, so the whole set must be checked.
                (ledger.root / "concurrent-marker").touch()
        return stream
    monkeypatch.setattr(Path, "open", open_then_mutate)
    with pytest.raises(CryptoLedgerError, match="snapshot_changed"):
        verification.head(ledger)
    assert mutated
