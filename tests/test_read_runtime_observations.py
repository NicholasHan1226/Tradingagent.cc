from datetime import datetime, timedelta
from dataclasses import replace
import json
from pathlib import Path

import pytest

from Ashare.minute_loop import MinuteFixtureClosedLoop
from Ashare.minute_paper_runner import load_minute_research_universe
from tests.test_ashare_minute_paper_runner import _snapshot, _write_inputs
from tests.test_crypto_delayed_paper_round_trip_health import _completed_round_trip, _tree_bytes
from tests.test_crypto_5m_support import WINDOW_END
from Crypto.delayed_paper_round_trip_health import build_crypto_delayed_paper_round_trip_health
from tools import read_runtime_observations as reader

NOW = datetime.fromisoformat("2026-07-28T09:41:00+08:00")


def _ashare(tmp_path: Path) -> Path:
    _, _, universe = _write_inputs(tmp_path)
    loop = MinuteFixtureClosedLoop(universe=load_minute_research_universe(universe))
    loop.process_snapshot(snapshot=_snapshot("2026-07-28T09:35:00+08:00", 10.0), manifest_sha256="2" * 64)
    root = tmp_path / "runs"
    path = root / "20260728" / "state-bundle.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema": "tradingagent.ashare.delayed_minute_paper_bundle.v1",
        "authority_tier": "non_production_fixture",
        "real_trading_enabled": False,
        "loop_state": loop.export_state(),
        # These unsealed values must never be used to inflate coverage.
        "last_receipt": {"accepted_count": 999999, "decision_time": "2099-01-01"},
    }))
    return root


def test_missing_roots_stay_missing_and_markets_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    before = _tree_bytes(tmp_path)
    result = reader.build_snapshot(now=NOW, ashare_root=tmp_path / "missing", crypto_manifest=tmp_path / "missing.json")
    assert [item["status"] for item in result["entries"]] == ["unavailable", "unavailable"]
    assert all(item["canonicalAccountConnected"] is False for item in result["entries"])
    assert _tree_bytes(tmp_path) == before


def test_ashare_coverage_comes_from_hash_verified_state_not_receipt(tmp_path):
    root = _ashare(tmp_path)
    before = _tree_bytes(root)
    result = reader.read_ashare(root, NOW)
    assert result["status"] == "ready"
    assert result["coverage"] == {"universe": 2, "accepted": 2, "missing": 0}
    assert result["observedAt"] == "2026-07-28T01:40:06Z"
    assert "simulation" not in result
    assert len(result["sourceSha256"]) == 64
    assert _tree_bytes(root) == before


def test_ashare_previous_run_keeps_original_time_and_dated_state(tmp_path):
    root = _ashare(tmp_path)
    (root / "20260729").mkdir()
    result = reader.read_ashare(root, NOW + timedelta(days=1))
    assert result["status"] == "dated"
    assert result["observedAt"] == "2026-07-28T01:40:06Z"


def test_one_stock_missing_does_not_inflate_or_block_latest_coverage(tmp_path):
    root = _ashare(tmp_path)
    path = root / "20260728" / "state-bundle.json"
    raw = json.loads(path.read_text())
    loop = MinuteFixtureClosedLoop.restore(raw["loop_state"])
    full = _snapshot("2026-07-28T09:40:00+08:00", 10.1)
    partial = replace(full, bars=full.bars[:1], row_count=1)
    loop.process_snapshot(snapshot=partial, manifest_sha256="2" * 64)
    raw["loop_state"] = loop.export_state()
    path.write_text(json.dumps(raw))
    result = reader.read_ashare(root, NOW + timedelta(minutes=5))
    assert result["status"] == "ready"
    assert result["coverage"] == {"universe": 2, "accepted": 1, "missing": 1}


def test_latest_invalid_bundle_does_not_fallback_to_old_success(tmp_path):
    root = _ashare(tmp_path)
    new = root / "20260729" / "state-bundle.json"
    new.parent.mkdir()
    new.write_text("{}")
    assert reader.read_ashare(root, NOW + timedelta(days=1))["status"] == "invalid"


def test_lookback_is_bounded_without_deleting_older_history(tmp_path):
    root = _ashare(tmp_path)
    before = _tree_bytes(root)
    assert reader.read_ashare(root, NOW + timedelta(days=7))["status"] == "dated"
    assert reader.read_ashare(root, NOW + timedelta(days=8))["status"] == "unavailable"
    assert _tree_bytes(root) == before


def test_manifest_permission_failure_is_local(tmp_path, monkeypatch):
    root = _ashare(tmp_path)
    manifest = tmp_path / "unreadable.json"
    original = Path.exists
    def exists(path):
        if path == manifest:
            raise PermissionError("private path must not leak")
        return original(path)
    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    result = reader.build_snapshot(now=NOW, ashare_root=root, crypto_manifest=manifest)
    assert [item["status"] for item in result["entries"]] == ["ready", "invalid"]
    assert "private path" not in json.dumps(result)


@pytest.mark.parametrize("mutation", ["state", "real", "future", "alias", "hardlink", "oversize"])
def test_ashare_rejects_tampering_unsafe_and_unbounded_inputs(tmp_path, monkeypatch, mutation):
    root = _ashare(tmp_path)
    path = root / "20260728" / "state-bundle.json"
    raw = json.loads(path.read_text())
    if mutation == "state":
        raw["loop_state"]["minimum_raw_score"] = 999
        path.write_text(json.dumps(raw))
    elif mutation == "real":
        raw["real_trading_enabled"] = True
        path.write_text(json.dumps(raw))
    elif mutation == "alias":
        original = path.with_suffix(".source")
        path.rename(original)
        path.symlink_to(original)
    elif mutation == "hardlink":
        path.with_suffix(".alias").hardlink_to(path)
    elif mutation == "oversize":
        monkeypatch.setattr(reader, "MAX_BUNDLE_BYTES", 10)
    checked = NOW - timedelta(minutes=10) if mutation == "future" else NOW
    assert reader.read_ashare(root, checked)["status"] == "invalid"


def test_crypto_reuses_real_readonly_health_and_preserves_bytes(tmp_path, monkeypatch):
    root = tmp_path / "crypto"
    _completed_round_trip(root)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    monkeypatch.setattr(reader, "run_crypto_delayed_paper_round_trip_health_once", lambda **kw: build_crypto_delayed_paper_round_trip_health(output_root=root, now=kw["now"]))
    before = _tree_bytes(root)
    result = reader.read_crypto(manifest, WINDOW_END + timedelta(minutes=10))
    assert result["status"] == "ready"
    assert result["simulation"]["currency"] == "USDT"
    assert result["counts"]["completed"] == 1
    assert result["canonicalAccountConnected"] is False
    assert _tree_bytes(root) == before


def test_crypto_health_failure_does_not_remove_ashare_success(tmp_path, monkeypatch):
    root = _ashare(tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    def fail(**kwargs):
        raise RuntimeError("do not expose internal paths or secrets")
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    monkeypatch.setattr(reader, "run_crypto_delayed_paper_round_trip_health_once", fail)
    result = reader.build_snapshot(now=NOW, ashare_root=root, crypto_manifest=manifest)
    assert [item["status"] for item in result["entries"]] == ["ready", "invalid"]
    assert "secrets" not in json.dumps(result)


@pytest.mark.parametrize("enabled", [None, "true", "yes", "0", "unknown"])
def test_snapshot_requires_explicit_simulation_mode(tmp_path, monkeypatch, enabled):
    monkeypatch.delenv("REAL_TRADING_ENABLED", raising=False)
    if enabled is not None:
        monkeypatch.setenv("REAL_TRADING_ENABLED", enabled)
    with pytest.raises(ValueError, match="simulation_only_required"):
        reader.build_snapshot(now=NOW, ashare_root=tmp_path, crypto_manifest=tmp_path / "missing")
