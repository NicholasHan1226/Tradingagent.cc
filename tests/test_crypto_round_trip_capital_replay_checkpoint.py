"""Replay-only order proof reuse: synthetic ledger, no runtime data access."""
import copy
from datetime import datetime, timedelta, timezone

import pytest

import Crypto.round_trip_capital as module
from tests.test_crypto_round_trip_capital import _direct_payload


def _legacy_checkpoint(self, state, **_ignored):
    snapshot = self._snapshot(state)
    orders = snapshot.pop('orders')
    snapshot['order_count'] = len(orders)
    snapshot['orders_sha256'] = module._sha256(orders)
    return snapshot


def _rows(tmp_path, monkeypatch, cycles=12):
    # Build evidence with the PRE-optimization algorithm, not with the code
    # being checked. Alternate entry, unchanged hold, exit, and flat observe.
    with monkeypatch.context() as old:
        old.setattr(module.RoundTripCapitalLedger, '_capital_checkpoint', _legacy_checkpoint)
        for i in range(cycles):
            phase = i % 4
            payload = _direct_payload(
                fixture_id=f'checkpoint-{i}',
                slot=(datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5*i)).isoformat(),
                action='buy' if phase == 0 else 'observe',
                regime_return='-0.01' if phase == 2 else '0.01',
                decision_return='-0.01' if phase == 2 else '0.01',
            )
            module.run_round_trip_fixture_cycle(payload, output_root=tmp_path)
    ledger = module.RoundTripCapitalLedger(tmp_path / 'round_trip_capital')
    return ledger, ledger._read_rows(require_existing_lock=True)


def test_replay_checkpoints_equal_legacy_for_orders_and_holds(tmp_path, monkeypatch):
    ledger, rows = _rows(tmp_path, monkeypatch)
    before = {p: p.read_bytes() for p in ledger.root.iterdir() if p.is_file()}
    legacy = []
    def expected(self, state, **kwargs):
        value = _legacy_checkpoint(self, state)
        legacy.append(module._canonical_json(value))
        return value
    with monkeypatch.context() as old:
        old.setattr(module.RoundTripCapitalLedger, '_capital_checkpoint', expected)
        old_state, old_checksum = ledger._replay(rows)
    actual = []
    optimized = module.RoundTripCapitalLedger._capital_checkpoint
    def observe(self, state, **kwargs):
        value = optimized(self, state, **kwargs)
        actual.append(module._canonical_json(value))
        return value
    with monkeypatch.context() as new:
        new.setattr(module.RoundTripCapitalLedger, '_capital_checkpoint', observe)
        state, checksum = ledger._replay(rows)
    assert actual == legacy
    assert len(actual) == 24
    assert module._canonical_json(state) == module._canonical_json(old_state)
    assert checksum == old_checksum
    assert any(row['payload'].get('order') for row in rows)
    assert any(row['event_type'] == 'cycle' and row['payload']['order'] is None for row in rows)
    assert {p: p.read_bytes() for p in before} == before


def test_replay_rehashes_only_after_order_insertion_and_never_across_calls(tmp_path, monkeypatch):
    ledger, rows = _rows(tmp_path, monkeypatch)
    calls = []
    original = module._sha256
    def counted(value):
        if isinstance(value, dict) and (not value or all(str(k).startswith('crypto-round-trip-intent-') for k in value)):
            calls.append(len(value))
        return original(value)
    monkeypatch.setattr(module, '_sha256', counted)
    ledger._replay(rows)
    one = list(calls)
    order_count = sum(bool(row['payload'].get('order')) for row in rows)
    assert one == list(range(order_count + 1))
    ledger._replay(rows)
    assert calls == one + one


@pytest.mark.parametrize('field', ['before', 'after', 'order', 'checksum'])
def test_replay_still_rejects_tampered_history(tmp_path, monkeypatch, field):
    ledger, rows = _rows(tmp_path, monkeypatch)
    changed = copy.deepcopy(rows)
    row = next(r for r in changed if r['payload'].get('order'))
    if field in {'before', 'after'}:
        row['payload'][field]['orders_sha256'] = 'f' * 64
    elif field == 'order':
        row['payload']['order']['quantity'] = '99'
    else:
        row['checksum'] = 'f' * 64
    if field != 'checksum':
        # Repair the envelope so semantic rejection cannot be masked by a
        # trivial broken event checksum or chain.
        previous = ''
        for item in changed:
            item['previous_checksum'] = previous
            item['event_id'] = 'crypto-round-trip-event-' + module._sha256({
                'event_type': item['event_type'], 'reference_id': item['reference_id'],
                'payload': item['payload'],
            })[:24]
            item['checksum'] = module._sha256({k:v for k,v in item.items() if k != 'checksum'})
            previous = item['checksum']
    with pytest.raises(module.CryptoRoundTripError):
        ledger._replay(changed)
