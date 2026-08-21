from __future__ import annotations

import ast
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from Ashare.minute_data import SHANGHAI
from Ashare.minute_scale500_runtime import (
    EXPECTED_UNIVERSE_COUNT,
    MinuteScale500RuntimeError,
    build_scale500_partial_shadow_receipt,
    canonical_universe_sha256,
    initialize_scale500_session,
    main,
    run_scale500_once,
)
from Ashare.minute_scale500_runtime import (
    _rolling_effective_universe,
    _validate_scale500_reference_fragment,
)
from shared.data.tradingdatas_transport import TradingDatasAuthenticationError


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def mock_os_access_for_root():
    """Mock os.access to return False for W_OK on read-only files when running as root."""
    original_access = os.access
    def patched_access(path, mode):
        if mode == os.W_OK and isinstance(path, (str, Path)):
            try:
                stat_result = os.stat(path)
                if not (stat_result.st_mode & 0o222):
                    return False
            except (OSError, TypeError):
                pass
        return original_access(path, mode)
    with patch("os.access", side_effect=patched_access):
        yield


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=SHANGHAI)


def _universe(path: Path, *, count: int = EXPECTED_UNIVERSE_COUNT) -> str:
    rows = []
    for index in range(count):
        symbol = f"{index + 1:06d}.SZ"
        rows.append(
            {
                "symbol": symbol,
                "name": f"主板样本{index:03d}",
                "industry": "主板扫描",
                "research_theme": "mainboard_opportunity_scan",
                "list_date": "2020-01-01",
                "risk_warning": False,
                "delisting_risk": False,
                "context_only": False,
            }
        )
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o440)
    return canonical_universe_sha256(path)


def _published_session(
    *,
    state_root: Path,
    trading_date: str,
    universe_source: Path,
    universe_sha256: str,
) -> None:
    day_root = state_root / trading_date.replace("-", "")
    day_root.mkdir(parents=True)
    universe = json.loads(universe_source.read_text(encoding="utf-8"))
    (day_root / "universe.json").write_text(
        json.dumps(universe, ensure_ascii=False),
        encoding="utf-8",
    )
    (day_root / "minute-manifest.json").write_text(
        json.dumps(
            {
                "base_url": "http://127.0.0.1:18082",
                "dataset_id": "cn.dataset.rt_min",
                "universe_sha256": universe_sha256,
                "profile": {
                    "max_rows": len(universe),
                    "page_limit": len(universe),
                    "max_pages": len(universe),
                },
            }
        ),
        encoding="utf-8",
    )
    (day_root / "reference-facts.json").write_text(
        json.dumps(
            [
                {
                    "symbol": row["symbol"],
                    "trade_date": trading_date,
                    "previous_close_cny": 10.0,
                    "suspended": False,
                    "evidence_sha256": hashlib.sha256(
                        row["symbol"].encode("utf-8")
                    ).hexdigest(),
                }
                for row in universe
            ]
        ),
        encoding="utf-8",
    )


def _paths(tmp_path: Path) -> tuple[Path, Path, Path, Path, str]:
    scale_root = tmp_path / "scale500"
    rollback_root = tmp_path / "rollback30"
    rollback_root.mkdir()
    token_file = tmp_path / "token"
    token_file.write_text("not-read-by-test", encoding="utf-8")
    token_file.chmod(0o600)
    universe_source = tmp_path / "universe.json"
    digest = _universe(universe_source)
    return scale_root, rollback_root, token_file, universe_source, digest


def test_rolling_effective_universe_quarantines_only_recent_listing(
    tmp_path: Path,
) -> None:
    universe_source = tmp_path / "universe.json"
    _universe(universe_source)
    rows = json.loads(universe_source.read_text(encoding="utf-8"))
    rows[0]["list_date"] = "2026-07-01"
    universe_source.chmod(0o600)
    universe_source.write_text(json.dumps(rows), encoding="utf-8")
    universe_source.chmod(0o440)

    effective, effective_sha256 = _rolling_effective_universe(
        universe_source,
        trade_date="2026-07-29",
    )

    assert len(effective) == EXPECTED_UNIVERSE_COUNT - 1
    assert "000001.SZ" not in {row["symbol"] for row in effective}
    assert len(effective_sha256) == 64


def test_scale500_initializer_can_open_rolling_partition(
    tmp_path: Path,
) -> None:
    scale_root, rollback_root, token_file, universe_source, _ = _paths(tmp_path)
    rows = json.loads(universe_source.read_text(encoding="utf-8"))
    rows[0]["list_date"] = "2026-07-01"
    universe_source.chmod(0o600)
    universe_source.write_text(json.dumps(rows), encoding="utf-8")
    universe_source.chmod(0o440)
    source_digest = canonical_universe_sha256(universe_source)

    def rolling_initializer(**kwargs: object) -> dict[str, object]:
        state_root = Path(str(kwargs["state_root"]))
        now = kwargs["now"]
        assert isinstance(now, datetime)
        active = rows[2:]
        effective_payload = json.dumps(
            active,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        effective_digest = hashlib.sha256(effective_payload).hexdigest()
        day_root = state_root / now.strftime("%Y%m%d")
        day_root.mkdir(parents=True)
        (day_root / "universe.json").write_text(
            json.dumps(active, ensure_ascii=False), encoding="utf-8"
        )
        (day_root / "minute-manifest.json").write_text(
            json.dumps(
                {
                    "base_url": "http://127.0.0.1:18082",
                    "dataset_id": "cn.dataset.rt_min",
                    "universe_sha256": effective_digest,
                    "profile": {
                        "max_rows": len(active),
                        "page_limit": len(active),
                        "max_pages": 1,
                    },
                }
            ),
            encoding="utf-8",
        )
        (day_root / "reference-facts.json").write_text(
            json.dumps(
                [
                    {
                        "symbol": row["symbol"],
                        "trade_date": now.date().isoformat(),
                        "previous_close_cny": 10.0,
                        "suspended": False,
                    }
                    for row in active
                ]
            ),
            encoding="utf-8",
        )
        return {
            "status": "pass",
            "authority_tier": "non_production_fixture",
            "trading_date": now.date().isoformat(),
            "symbol_count": len(active),
            "universe_sha256": effective_digest,
            "source_universe_sha256": source_digest,
            "rolling_eligible": True,
            "pending_listings": [
                {
                    "symbol": "000001.SZ",
                    "reason": "listed_less_than_30_days",
                    "listed_on": "2026-07-01",
                    "eligible_after": "2026-07-31",
                }
            ],
            "daily_data_excluded": [
                {
                    "symbol": rows[1]["symbol"],
                    "reason": "previous_close_missing",
                    "trade_date": "20260728",
                }
            ],
            "state_bundle_created": False,
            "capital_authority": False,
            "execution_authority": False,
            "real_trading_enabled": False,
        }

    result = initialize_scale500_session(
        scale_state_root=scale_root,
        rollback30_state_root=rollback_root,
        token_file=token_file,
        universe_source=universe_source,
        expected_universe_sha256=source_digest,
        now=_at("2026-07-29T09:18:00"),
        rolling_eligible=True,
        initializer=rolling_initializer,
    )

    assert result["selected_mode"] == "rolling_eligible"
    assert result["symbol_count"] == EXPECTED_UNIVERSE_COUNT - 2
    assert result["pending_listings"][0]["symbol"] == "000001.SZ"


def _initializer(
    *,
    state_root: Path,
    now: datetime,
    universe_source: Path,
    **_: object,
) -> dict[str, object]:
    digest = canonical_universe_sha256(universe_source)
    _published_session(
        state_root=state_root,
        trading_date=now.date().isoformat(),
        universe_source=universe_source,
        universe_sha256=digest,
    )
    return {
        "status": "pass",
        "authority_tier": "non_production_fixture",
        "trading_date": now.date().isoformat(),
        "symbol_count": EXPECTED_UNIVERSE_COUNT,
        "universe_sha256": digest,
        "source_universe_sha256": digest,
        "rolling_eligible": False,
        "pending_listings": [],
        "state_bundle_created": False,
        "capital_authority": False,
        "execution_authority": False,
        "real_trading_enabled": False,
    }


def _receipt(bar_end: str) -> dict[str, object]:
    return {
        "status": "pass",
        "bar_end": bar_end,
        "row_count": EXPECTED_UNIVERSE_COUNT,
        "audit_rejections": 0,
        "authority_tier": "non_production_fixture",
        "capital_authority": False,
        "durable_capital": False,
        "execution_authority": False,
        "real_trading_enabled": False,
    }


def _partial_runtime_receipt(
    bar_end: str, *, replacement: bool = False
) -> dict[str, object]:
    expected = list(_symbols())
    accepted = expected[:-1]
    if replacement:
        accepted[-1] = "300001.SZ"
    accepted_set = set(accepted)
    missing = sorted(set(expected) - accepted_set)
    return {
        "status": "partial_observation",
        "bar_end": bar_end,
        "decision_time": "2026-07-31T09:40:30+08:00",
        "observed_at": "2026-07-31T09:40:20+08:00",
        "requested_count": EXPECTED_UNIVERSE_COUNT,
        "accepted_count": len(accepted),
        "missing_count": len(missing),
        "accepted_symbols": accepted,
        "missing_symbols": missing,
        "same_observation": True,
        "lineage_complete": True,
        "proof_complete": True,
        "audit_rejections": 0,
        "per_row_evidence": [
            {
                "symbol": symbol,
                "bar_end": "2026-07-31T09:35:00+08:00",
                "receipt_id": f"receipt-{index}",
                "data_through": "2026-07-31T09:40:10+08:00",
                "observed_at": "2026-07-31T09:40:20+08:00",
                "source_lineage_sha256": "a" * 64,
                "envelope_proof_sha256": "b" * 64,
                "source_row_sha256": "c" * 64,
            }
            for index, symbol in enumerate(accepted)
        ],
        "capital_authority": False,
        "execution_authority": False,
        "execution_eligible": False,
        "training_eligible": False,
        "promotion_authorized": False,
        "real_trading_enabled": False,
    }


def _late_start_receipt(bar_end: str) -> dict[str, object]:
    receipt = _receipt(bar_end)
    receipt.update(
        {
            "late_start": True,
            "late_start_reason": "incident_recovery_no_historical_pit",
            "gap_recovery": True,
            "gap_recovery_reason": "incident_recovery_no_historical_pit",
            "skipped_session_slots": 25,
            "full_session_complete": False,
            "learning_eligible": False,
        }
    )
    return receipt


def _canary_receipt(path: Path, *, universe_source: Path, bar_end: str) -> Path:
    symbols = [
        item["symbol"]
        for item in json.loads(universe_source.read_text(encoding="utf-8"))
    ]
    path.write_text(
        json.dumps(
            {
                "status": "pass",
                "authority_tier": "observation_only",
                "evidence_use": "delayed_paper",
                "execution_latency_eligible": False,
                "real_trading_enabled": False,
                "trading_date": "2026-07-31",
                "decision_time": "2026-07-31T13:54:00+08:00",
                "row_count": EXPECTED_UNIVERSE_COUNT,
                "same_observation": True,
                "lineage_complete": True,
                "audit_rejections": 0,
                "dataset_contract_fingerprint": "a" * 64,
                "consumer_profile_sha256": "c" * 64,
                "snapshot_sha256": "b" * 64,
                "bars": [
                    {
                        "symbol": symbol,
                        "bar_end": _at(bar_end).isoformat(),
                        "receipt_id": f"receipt-{index}",
                        "observed_at": "2026-07-31T13:50:00+08:00",
                    }
                    for index, symbol in enumerate(symbols)
                ],
            }
        ),
        encoding="utf-8",
    )
    return path.resolve()


def _initialize(tmp_path: Path) -> tuple[Path, Path, Path, Path, str]:
    paths = _paths(tmp_path)
    scale_root, rollback_root, token_file, universe_source, digest = paths
    result = initialize_scale500_session(
        scale_state_root=scale_root,
        rollback30_state_root=rollback_root,
        token_file=token_file,
        universe_source=universe_source,
        expected_universe_sha256=digest,
        now=_at("2026-07-31T09:18:00"),
        initializer=_initializer,
    )
    assert result["status"] == "pass"
    return paths


def test_initialize_accepts_honest_fresh_state_bundle_flag(
    tmp_path: Path,
) -> None:
    """A freshly created day reports state_bundle_created=True and passes."""

    scale_root, rollback_root, token_file, universe_source, digest = _paths(
        tmp_path
    )

    def fresh_initializer(**kwargs: object) -> dict[str, object]:
        return {**_initializer(**kwargs), "state_bundle_created": True}

    # Mock os.access to return False for W_OK (simulates non-root user)
    original_access = os.access
    def mock_access(path: str, mode: int) -> bool:
        if mode == os.W_OK:
            return False
        return original_access(path, mode)
    
    with patch.object(os, 'access', side_effect=mock_access):
        result = initialize_scale500_session(
            scale_state_root=scale_root,
            rollback30_state_root=rollback_root,
            token_file=token_file,
            universe_source=universe_source,
            expected_universe_sha256=digest,
            now=_at("2026-07-31T09:18:00"),
            initializer=fresh_initializer,
        )

    assert result["status"] == "pass"
    assert result["scale500_acceptance_status"] == "pending_two_live_snapshots"


def _symbols(count: int = EXPECTED_UNIVERSE_COUNT) -> tuple[str, ...]:
    return tuple(f"{index + 1:06d}.SZ" for index in range(count))


def test_99_percent_partial_cohort_is_zero_notional_shadow_only() -> None:
    receipt = build_scale500_partial_shadow_receipt(
        expected_symbols=_symbols(),
        observed_symbols=_symbols()[:-5],
        trading_date="2026-07-31",
        bar_end="2026-07-31 13:40:00",
        observed_at=_at("2026-07-31T13:45:20"),
        decision_time=_at("2026-07-31T13:45:30"),
    )
    assert receipt["observed_cohort_size"] == EXPECTED_UNIVERSE_COUNT - 5
    assert receipt["missing_identity_count"] == 5
    assert receipt["simulated_notional_cny"] == 0
    assert receipt["simulation_timing"] == "next_bar_only"
    assert receipt["capital_layer"] == "simulated"
    assert receipt["account_type"] == "simulated"
    assert receipt["capital_commit_id"] is None
    assert receipt["outbox_id"] is None
    assert receipt["delayed_paper_eligible"] is False
    assert all(
        receipt[key] is False
        for key in (
            "candidate_authority",
            "capital_authority",
            "execution_authority",
            "execution_latency_eligible",
            "training_eligible",
            "promotion_authorized",
        )
    )
    assert (
        build_scale500_partial_shadow_receipt(
            expected_symbols=_symbols(),
            observed_symbols=_symbols()[:-5],
            trading_date="2026-07-31",
            bar_end="2026-07-31 13:40:00",
            observed_at=_at("2026-07-31T13:45:20"),
            decision_time=_at("2026-07-31T13:45:30"),
        )
        == receipt
    )
    assert receipt["missing_identity_set"] == sorted(_symbols()[-5:])


@pytest.mark.parametrize(
    ("observed", "reason"),
    [
        (_symbols()[:-32], "coverage_insufficient"),
        (_symbols()[:-5] + ("300001.SZ",), "silent_identity_replacement"),
        (_symbols()[:-6] + (_symbols()[0],), "observed_identity_invalid"),
        (_symbols(), "requires_partial_cohort"),
    ],
)
def test_partial_shadow_rejects_missing_duplicate_replacement_or_full_cohort(
    observed: tuple[str, ...], reason: str
) -> None:
    with pytest.raises(MinuteScale500RuntimeError, match=reason):
        build_scale500_partial_shadow_receipt(
            expected_symbols=_symbols(),
            observed_symbols=observed,
            trading_date="2026-07-31",
            bar_end="2026-07-31 13:40:00",
            observed_at=_at("2026-07-31T13:45:20"),
            decision_time=_at("2026-07-31T13:45:30"),
        )


@pytest.mark.parametrize(
    ("observed_at", "decision_time", "reason"),
    [
        ("2026-07-31T13:40:00", "2026-07-31T13:45:00", "shadow_time_invalid"),
        ("2026-07-31T13:47:01", "2026-07-31T13:47:01", "shadow_time_invalid"),
        ("2026-07-31T13:46:50", "2026-07-31T13:47:01", "shadow_time_invalid"),
    ],
)
def test_partial_shadow_rejects_same_bar_or_overdue_observation(
    observed_at: str, decision_time: str, reason: str
) -> None:
    with pytest.raises(MinuteScale500RuntimeError, match=reason):
        build_scale500_partial_shadow_receipt(
            expected_symbols=_symbols(),
            observed_symbols=_symbols()[:-5],
            trading_date="2026-07-31",
            bar_end="2026-07-31 13:40:00",
            observed_at=_at(observed_at),
            decision_time=_at(decision_time),
        )


def test_initializer_publishes_pending_gate_without_state_bundle(
    tmp_path: Path,
) -> None:
    scale_root, rollback_root, _, _, digest = _initialize(tmp_path)

    gate = json.loads(
        (scale_root / ".scale500-gates" / "20260731.json").read_text(encoding="utf-8")
    )
    assert gate == {
        "account_type": "simulated",
        "capital_layer": "simulated",
        "capital_authority": False,
        "execution_authority": False,
        "execution_eligible": False,
        "expected_universe_count": EXPECTED_UNIVERSE_COUNT,
        "failure_reason": None,
        "late_start": False,
        "late_start_bar_end": None,
        "real_trading_enabled": False,
        "partial_session": False,
        "rollback30_state_root": str(rollback_root),
        "schema": "tradingagent.ashare.scale500-acceptance.v1",
        "selected_mode": "scale500",
        "status": "pending_two_live_snapshots",
        "training_eligible": False,
        "trading_date": "2026-07-31",
        "universe_sha256": digest,
        "validated_bar_ends": [],
        "promotion_authorized": False,
    }
    assert not (scale_root / "20260731" / "state-bundle.json").exists()


def test_first_two_exact_rounds_activate_and_never_write_rollback_root(
    tmp_path: Path,
) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _initialize(
        tmp_path
    )
    sentinel = rollback_root / "historical-state"
    sentinel.write_text("immutable-30", encoding="utf-8")
    before = hashlib.sha256(sentinel.read_bytes()).hexdigest()

    first = run_scale500_once(
        scale_state_root=scale_root,
        rollback30_state_root=rollback_root,
        token_file=token_file,
        universe_source=universe_source,
        expected_universe_sha256=digest,
        now=_at("2026-07-31T09:42:00"),
        runner=lambda **_: _receipt("2026-07-31 09:35:00"),
    )
    (scale_root / "20260731" / "state-bundle.json").write_text(
        "{}",
        encoding="utf-8",
    )
    second = run_scale500_once(
        scale_state_root=scale_root,
        rollback30_state_root=rollback_root,
        token_file=token_file,
        universe_source=universe_source,
        expected_universe_sha256=digest,
        now=_at("2026-07-31T09:47:00"),
        runner=lambda **_: _receipt("2026-07-31 09:40:00"),
    )

    assert first["scale500_acceptance_status"] == "pending_two_live_snapshots"
    assert second["scale500_acceptance_status"] == "active"
    assert second["execution_eligible"] is False
    assert second["training_eligible"] is False
    assert second["promotion_authorized"] is False
    assert second["validated_bar_ends"] == [
        "2026-07-31 09:35:00",
        "2026-07-31 09:40:00",
    ]
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == before
    assert list(rollback_root.iterdir()) == [sentinel]


def test_rolling_runtime_uses_published_partition_after_initializer_exclusion(
    tmp_path: Path,
) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _initialize(
        tmp_path
    )
    source_rows = json.loads(universe_source.read_text(encoding="utf-8"))
    published_source = tmp_path / "published-universe.json"
    published_source.write_text(
        json.dumps(source_rows[:-1], ensure_ascii=False), encoding="utf-8"
    )
    published_source.chmod(0o440)
    published_digest = canonical_universe_sha256(published_source)
    day_root = scale_root / "20260731"
    for child in day_root.iterdir():
        child.unlink()
    day_root.rmdir()
    _published_session(
        state_root=scale_root,
        trading_date="2026-07-31",
        universe_source=published_source,
        universe_sha256=published_digest,
    )
    gate_path = scale_root / ".scale500-gates" / "20260731.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    gate.update(
        {
            "selected_mode": "rolling_eligible",
            "expected_universe_count": EXPECTED_UNIVERSE_COUNT - 1,
            "universe_sha256": published_digest,
        }
    )
    gate_path.write_text(json.dumps(gate), encoding="utf-8")

    result = run_scale500_once(
        scale_state_root=scale_root,
        rollback30_state_root=rollback_root,
        token_file=token_file,
        universe_source=universe_source,
        expected_universe_sha256=digest,
        now=_at("2026-07-31T09:42:00"),
        rolling_eligible=True,
        runner=lambda **_: {
            **_receipt("2026-07-31 09:35:00"),
            "row_count": EXPECTED_UNIVERSE_COUNT - 1,
        },
    )

    assert result["status"] == "pass"
    assert result["scale500_acceptance_status"] == "pending_two_live_snapshots"
    assert result["selected_mode"] == "rolling_eligible"
    persisted_gate = json.loads(gate_path.read_text(encoding="utf-8"))
    assert persisted_gate["status"] == "pending_two_live_snapshots"
    assert persisted_gate["selected_mode"] == "rolling_eligible"


def test_499_partial_is_persisted_shadow_without_runner_or_rollback30(
    tmp_path: Path,
) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _initialize(
        tmp_path
    )
    calls: list[dict[str, object]] = []

    def partial_runner(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return _partial_runtime_receipt("2026-07-31 09:35:00")

    result = run_scale500_once(
        scale_state_root=scale_root,
        rollback30_state_root=rollback_root,
        token_file=token_file,
        universe_source=universe_source,
        expected_universe_sha256=digest,
        now=_at("2026-07-31T09:42:00"),
        runner=partial_runner,
    )

    assert result["status"] == "partial_cohort_shadow"
    assert result["quality_status"] == "usable_degraded"
    assert result["accepted_count"] == EXPECTED_UNIVERSE_COUNT - 1
    assert result["missing_count"] == 1
    assert result["selected_mode"] == "scale500"
    assert result["delayed_paper_eligible"] is False
    assert calls[0]["partial_observation_minimum"] == 3162
    persisted = json.loads(
        (
            scale_root
            / "20260731"
            / "partial-shadow-receipts"
            / "093500.json"
        ).read_text(encoding="utf-8")
    )
    assert persisted["receipt_id"] == result["receipt_id"]
    gate = json.loads(
        (scale_root / ".scale500-gates" / "20260731.json").read_text(
            encoding="utf-8"
        )
    )
    assert gate["status"] == "pending_two_live_snapshots"
    assert gate["selected_mode"] == "scale500"


def test_unsafe_partial_replacement_fails_only_the_cohort(tmp_path: Path) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _initialize(
        tmp_path
    )

    result = run_scale500_once(
        scale_state_root=scale_root,
        rollback30_state_root=rollback_root,
        token_file=token_file,
        universe_source=universe_source,
        expected_universe_sha256=digest,
        now=_at("2026-07-31T09:42:00"),
        runner=lambda **_: _partial_runtime_receipt(
            "2026-07-31 09:35:00", replacement=True
        ),
    )

    assert result["status"] == "failed_closed"
    assert result["cohort_failed"] is True
    assert result["selected_mode"] == "scale500"
    gate = json.loads(
        (scale_root / ".scale500-gates" / "20260731.json").read_text(
            encoding="utf-8"
        )
    )
    assert gate["status"] == "pending_two_live_snapshots"
    assert gate["selected_mode"] == "scale500"


def test_partial_canary_cannot_satisfy_scale500_claim_gate(tmp_path: Path) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _initialize(
        tmp_path
    )
    canary_path = _canary_receipt(
        tmp_path / "partial-canary.json",
        universe_source=universe_source,
        bar_end="2026-07-31T09:35:00+08:00",
    )
    raw = json.loads(canary_path.read_text(encoding="utf-8"))
    raw["row_count"] = EXPECTED_UNIVERSE_COUNT - 1
    raw["bars"] = raw["bars"][:-1]
    canary_path.write_text(json.dumps(raw), encoding="utf-8")
    runner_calls = 0

    def runner(**_: object) -> dict[str, object]:
        nonlocal runner_calls
        runner_calls += 1
        return _receipt("2026-07-31 09:35:00")

    with pytest.raises(
        MinuteScale500RuntimeError,
        match="minute_scale500_late_start_canary_invalid",
    ):
        run_scale500_once(
            scale_state_root=scale_root,
            rollback30_state_root=rollback_root,
            token_file=token_file,
            universe_source=universe_source,
            expected_universe_sha256=digest,
            now=_at("2026-07-31T09:42:00"),
            allow_late_start=True,
            canary_receipt=canary_path,
            runner=runner,
        )

    assert runner_calls == 0
    gate = json.loads(
        (scale_root / ".scale500-gates" / "20260731.json").read_text(
            encoding="utf-8"
        )
    )
    assert gate["status"] == "fallback30_selected"
    assert gate["failure_reason"] == "minute_scale500_late_start_canary_invalid"


def test_legacy_pending_gate_defaults_to_non_late_start(tmp_path: Path) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _initialize(
        tmp_path
    )
    gate_path = scale_root / ".scale500-gates" / "20260731.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    for field in ("partial_session", "late_start", "late_start_bar_end"):
        del gate[field]
    gate_path.write_text(json.dumps(gate), encoding="utf-8")

    result = run_scale500_once(
        scale_state_root=scale_root,
        rollback30_state_root=rollback_root,
        token_file=token_file,
        universe_source=universe_source,
        expected_universe_sha256=digest,
        now=_at("2026-07-31T09:42:00"),
        runner=lambda **_: _receipt("2026-07-31 09:35:00"),
    )

    assert result["validated_bar_ends"] == ["2026-07-31 09:35:00"]
    upgraded = json.loads(gate_path.read_text(encoding="utf-8"))
    assert upgraded["partial_session"] is False
    assert upgraded["late_start"] is False
    assert upgraded["late_start_bar_end"] is None


@pytest.mark.parametrize(
    "reason",
    [
        "minute_snapshot_universe_incomplete",
        "minute_same_observation_mismatch",
        "minute_metadata_state_not_ready",
        "minute_lineage_missing",
        "minute_query_identity_mismatch",
        "minute_fanout_bar_end_mismatch",
    ],
)
def test_any_data_authority_failure_selects_rollback_with_exact_reason(
    tmp_path: Path,
    reason: str,
) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _initialize(
        tmp_path
    )
    sentinel = rollback_root / "historical-state"
    sentinel.write_text("immutable-30", encoding="utf-8")

    def fail(**_: object) -> dict[str, object]:
        raise ValueError(reason)

    with pytest.raises(MinuteScale500RuntimeError, match=reason):
        run_scale500_once(
            scale_state_root=scale_root,
            rollback30_state_root=rollback_root,
            token_file=token_file,
            universe_source=universe_source,
            expected_universe_sha256=digest,
            now=_at("2026-07-31T09:42:00"),
            runner=fail,
        )

    gate = json.loads(
        (scale_root / ".scale500-gates" / "20260731.json").read_text(encoding="utf-8")
    )
    assert gate["status"] == "fallback30_selected"
    assert gate["selected_mode"] == "rollback30"
    assert gate["failure_reason"] == reason
    assert sentinel.read_text(encoding="utf-8") == "immutable-30"


def test_authentication_failure_projects_redacted_reason_and_preserves_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _initialize(
        tmp_path
    )
    sentinel = rollback_root / "historical-state"
    sentinel.write_text("immutable-30", encoding="utf-8")
    expected_reason = "minute_scale500_tradingdatas_authentication_rejected"
    sensitive_exception_body = "HTTP 403 bearer=must-not-leak"
    runner_calls = 0

    def reject_runner(**_: object) -> dict[str, object]:
        nonlocal runner_calls
        runner_calls += 1
        raise TradingDatasAuthenticationError(sensitive_exception_body)

    with pytest.raises(MinuteScale500RuntimeError) as raised:
        run_scale500_once(
            scale_state_root=scale_root,
            rollback30_state_root=rollback_root,
            token_file=token_file,
            universe_source=universe_source,
            expected_universe_sha256=digest,
            now=_at("2026-07-31T09:42:00"),
            runner=reject_runner,
        )

    assert runner_calls == 1
    assert str(raised.value) == expected_reason
    assert sensitive_exception_body not in str(raised.value)
    gate = json.loads(
        (scale_root / ".scale500-gates" / "20260731.json").read_text(
            encoding="utf-8"
        )
    )
    assert gate["status"] == "fallback30_selected"
    assert gate["selected_mode"] == "rollback30"
    assert gate["failure_reason"] == expected_reason
    assert sentinel.read_text(encoding="utf-8") == "immutable-30"

    cli_calls = 0

    def reject_cli(**_: object) -> dict[str, object]:
        nonlocal cli_calls
        cli_calls += 1
        raise TradingDatasAuthenticationError(sensitive_exception_body)

    monkeypatch.setattr(
        "Ashare.minute_scale500_runtime.run_scale500_once",
        reject_cli,
    )
    code = main(
        [
            "run",
            "--scale-state-root",
            str(scale_root),
            "--rollback30-state-root",
            str(rollback_root),
            "--token-file",
            str(token_file),
            "--universe-source",
            str(universe_source),
            "--expected-universe-sha256",
            digest,
            "--now",
            "2026-07-31T09:42:00+08:00",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert code == 2
    assert cli_calls == 1
    assert captured.out == ""
    assert payload["status"] == "failed_closed"
    assert payload["reason_code"] == expected_reason
    assert payload["selected_mode"] == "rollback30"
    assert payload["capital_authority"] is False
    assert payload["execution_authority"] is False
    assert payload["training_eligible"] is False
    assert payload["real_trading_enabled"] is False
    assert sensitive_exception_body not in captured.err
    assert "403" not in captured.err
    assert "bearer" not in captured.err
    assert "must-not-leak" not in captured.err
    assert "minute_scale500_unclassified" not in captured.err

    systemd_root = REPO_ROOT / "Ashare" / "systemd"
    paper_service = (
        systemd_root / "tradingagent-ashare-minute-scale500-paper.service"
    ).read_text(encoding="utf-8")
    rollback_service = (
        systemd_root / "tradingagent-ashare-minute-scale500-rollback.service"
    ).read_text(encoding="utf-8")
    assert "OnFailure=tradingagent-ashare-minute-scale500-rollback.service" not in (
        paper_service
    )
    assert (
        "disable --now tradingagent-ashare-minute-scale500-session.timer "
        "tradingagent-ashare-minute-scale500-paper.timer"
    ) in rollback_service


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("row_count", EXPECTED_UNIVERSE_COUNT - 1, "minute_scale500_row_count_mismatch"),
        ("execution_authority", True, "minute_scale500_authority_violation"),
        ("capital_authority", True, "minute_scale500_authority_violation"),
        ("real_trading_enabled", True, "minute_scale500_authority_violation"),
        ("durable_capital", True, "minute_scale500_authority_violation"),
    ],
)
def test_partial_or_authoritative_receipt_fails_closed(
    tmp_path: Path,
    field: str,
    value: object,
    reason: str,
) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _initialize(
        tmp_path
    )
    receipt = _receipt("2026-07-31 09:35:00")
    receipt[field] = value

    with pytest.raises(MinuteScale500RuntimeError, match=reason):
        run_scale500_once(
            scale_state_root=scale_root,
            rollback30_state_root=rollback_root,
            token_file=token_file,
            universe_source=universe_source,
            expected_universe_sha256=digest,
            now=_at("2026-07-31T09:42:00"),
            runner=lambda **_: receipt,
        )


def test_first_accepted_round_must_be_opening_bar_not_late_start(
    tmp_path: Path,
) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _initialize(
        tmp_path
    )
    with pytest.raises(
        MinuteScale500RuntimeError,
        match="minute_scale500_first_bar_mismatch",
    ):
        run_scale500_once(
            scale_state_root=scale_root,
            rollback30_state_root=rollback_root,
            token_file=token_file,
            universe_source=universe_source,
            expected_universe_sha256=digest,
            now=_at("2026-07-31T09:47:00"),
            runner=lambda **_: _receipt("2026-07-31 09:40:00"),
        )


def test_late_start_requires_explicit_flag_and_never_accepts_a_prior_bar(
    tmp_path: Path,
) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _initialize(
        tmp_path
    )
    runner_flags: list[bool] = []

    def late_runner(*, allow_late_start: bool, **_: object) -> dict[str, object]:
        runner_flags.append(allow_late_start)
        return _late_start_receipt("2026-07-31 13:40:00")

    with pytest.raises(
        MinuteScale500RuntimeError,
        match="minute_scale500_late_start_not_authorized",
    ):
        run_scale500_once(
            scale_state_root=scale_root,
            rollback30_state_root=rollback_root,
            token_file=token_file,
            universe_source=universe_source,
            expected_universe_sha256=digest,
            now=_at("2026-07-31T13:47:00"),
            runner=late_runner,
        )

    assert runner_flags == [False]
    gate = json.loads(
        (scale_root / ".scale500-gates" / "20260731.json").read_text(encoding="utf-8")
    )
    assert gate["status"] == "fallback30_selected"
    assert gate["failure_reason"] == "minute_scale500_late_start_not_authorized"


def test_explicit_late_start_uses_only_current_complete_bar_and_stays_partial(
    tmp_path: Path,
) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _initialize(
        tmp_path
    )
    runner_flags: list[bool] = []

    def late_runner(*, allow_late_start: bool, **_: object) -> dict[str, object]:
        runner_flags.append(allow_late_start)
        return _late_start_receipt("2026-07-31 13:40:00")

    result = run_scale500_once(
        scale_state_root=scale_root,
        rollback30_state_root=rollback_root,
        token_file=token_file,
        universe_source=universe_source,
        expected_universe_sha256=digest,
        now=_at("2026-07-31T13:47:00"),
        allow_late_start=True,
        canary_receipt=_canary_receipt(
            tmp_path / "canary.json",
            universe_source=universe_source,
            bar_end="2026-07-31 13:40:00",
        ),
        runner=late_runner,
    )

    assert runner_flags == [True]
    assert result["bar_end"] == "2026-07-31 13:40:00"
    assert result["scale500_acceptance_status"] == "active"
    assert result["partial_session"] is True
    assert result["late_start"] is True
    assert result["learning_eligible"] is False
    assert result["full_session_complete"] is False
    gate = json.loads(
        (scale_root / ".scale500-gates" / "20260731.json").read_text(encoding="utf-8")
    )
    assert gate["validated_bar_ends"] == ["2026-07-31 13:40:00"]
    assert gate["partial_session"] is True
    assert gate["late_start"] is True
    assert gate["late_start_bar_end"] == "2026-07-31 13:40:00"


def test_late_start_requires_an_exact_delayed_canary_before_runner(
    tmp_path: Path,
) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _initialize(
        tmp_path
    )
    calls: list[object] = []

    with pytest.raises(
        MinuteScale500RuntimeError,
        match="minute_scale500_late_start_canary_missing",
    ):
        run_scale500_once(
            scale_state_root=scale_root,
            rollback30_state_root=rollback_root,
            token_file=token_file,
            universe_source=universe_source,
            expected_universe_sha256=digest,
            now=_at("2026-07-31T13:47:00"),
            allow_late_start=True,
            runner=lambda **_: (
                calls.append("runner") or _late_start_receipt("2026-07-31 13:40:00")
            ),
        )

    assert calls == []


def test_late_start_replay_is_idempotent_and_cannot_restore_learning(
    tmp_path: Path,
) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _initialize(
        tmp_path
    )
    run_scale500_once(
        scale_state_root=scale_root,
        rollback30_state_root=rollback_root,
        token_file=token_file,
        universe_source=universe_source,
        expected_universe_sha256=digest,
        now=_at("2026-07-31T13:47:00"),
        allow_late_start=True,
        canary_receipt=_canary_receipt(
            tmp_path / "canary.json",
            universe_source=universe_source,
            bar_end="2026-07-31 13:40:00",
        ),
        runner=lambda **_: _late_start_receipt("2026-07-31 13:40:00"),
    )
    replay_flags: list[bool] = []

    def replay_runner(*, allow_late_start: bool, **_: object) -> dict[str, object]:
        replay_flags.append(allow_late_start)
        return {"status": "noop", "reason": "bar_already_processed"}

    replay = run_scale500_once(
        scale_state_root=scale_root,
        rollback30_state_root=rollback_root,
        token_file=token_file,
        universe_source=universe_source,
        expected_universe_sha256=digest,
        now=_at("2026-07-31T13:47:00"),
        runner=replay_runner,
    )

    assert replay_flags == [False]
    assert replay["reason"] == "bar_already_processed"
    assert replay["partial_session"] is True
    assert replay["learning_eligible"] is False
    assert replay["full_session_complete"] is False


def test_late_start_cannot_reopen_an_active_scale_session(tmp_path: Path) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _initialize(
        tmp_path
    )
    run_scale500_once(
        scale_state_root=scale_root,
        rollback30_state_root=rollback_root,
        token_file=token_file,
        universe_source=universe_source,
        expected_universe_sha256=digest,
        now=_at("2026-07-31T13:47:00"),
        allow_late_start=True,
        canary_receipt=_canary_receipt(
            tmp_path / "canary.json",
            universe_source=universe_source,
            bar_end="2026-07-31 13:40:00",
        ),
        runner=lambda **_: _late_start_receipt("2026-07-31 13:40:00"),
    )

    with pytest.raises(
        MinuteScale500RuntimeError,
        match="minute_scale500_late_start_gate_not_pending",
    ):
        run_scale500_once(
            scale_state_root=scale_root,
            rollback30_state_root=rollback_root,
            token_file=token_file,
            universe_source=universe_source,
            expected_universe_sha256=digest,
            now=_at("2026-07-31T13:52:00"),
            allow_late_start=True,
            canary_receipt=_canary_receipt(
                tmp_path / "second-canary.json",
                universe_source=universe_source,
                bar_end="2026-07-31 13:45:00",
            ),
            runner=lambda **_: _late_start_receipt("2026-07-31 13:45:00"),
        )

    gate = json.loads(
        (scale_root / ".scale500-gates" / "20260731.json").read_text(encoding="utf-8")
    )
    assert gate["status"] == "fallback30_selected"
    assert gate["failure_reason"] == "minute_scale500_late_start_gate_not_pending"


def test_late_start_does_not_relax_data_rejects(tmp_path: Path) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _initialize(
        tmp_path
    )
    run_scale500_once(
        scale_state_root=scale_root,
        rollback30_state_root=rollback_root,
        token_file=token_file,
        universe_source=universe_source,
        expected_universe_sha256=digest,
        now=_at("2026-07-31T13:47:00"),
        allow_late_start=True,
        canary_receipt=_canary_receipt(
            tmp_path / "canary.json",
            universe_source=universe_source,
            bar_end="2026-07-31 13:40:00",
        ),
        runner=lambda **_: _late_start_receipt("2026-07-31 13:40:00"),
    )
    runner_flags: list[bool] = []

    def reject_runner(*, allow_late_start: bool, **_: object) -> dict[str, object]:
        runner_flags.append(allow_late_start)
        raise ValueError("minute_snapshot_universe_incomplete")

    with pytest.raises(
        MinuteScale500RuntimeError,
        match="minute_snapshot_universe_incomplete",
    ):
        run_scale500_once(
            scale_state_root=scale_root,
            rollback30_state_root=rollback_root,
            token_file=token_file,
            universe_source=universe_source,
            expected_universe_sha256=digest,
            now=_at("2026-07-31T13:52:00"),
            allow_late_start=True,
            canary_receipt=_canary_receipt(
                tmp_path / "second-canary.json",
                universe_source=universe_source,
                bar_end="2026-07-31 13:45:00",
            ),
            runner=reject_runner,
        )

    assert runner_flags == [True]
    gate = json.loads(
        (scale_root / ".scale500-gates" / "20260731.json").read_text(encoding="utf-8")
    )
    assert gate["status"] == "fallback30_selected"
    assert gate["failure_reason"] == "minute_snapshot_universe_incomplete"
    assert gate["partial_session"] is True
    assert gate["late_start"] is True


def test_artifact_tamper_and_non_independent_roots_fail_before_runtime(
    tmp_path: Path,
) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _paths(tmp_path)
    rows = json.loads(universe_source.read_text(encoding="utf-8"))
    rows[0]["name"] = "tampered"
    universe_source.chmod(0o640)
    universe_source.write_text(json.dumps(rows), encoding="utf-8")
    universe_source.chmod(0o440)
    with pytest.raises(
        MinuteScale500RuntimeError,
        match="minute_scale500_universe_digest_mismatch",
    ):
        initialize_scale500_session(
            scale_state_root=scale_root,
            rollback30_state_root=rollback_root,
            token_file=token_file,
            universe_source=universe_source,
            expected_universe_sha256=digest,
            now=_at("2026-07-31T09:18:00"),
            initializer=_initializer,
        )

    valid_source = tmp_path / "valid-universe.json"
    valid_digest = _universe(valid_source)
    with pytest.raises(
        MinuteScale500RuntimeError,
        match="minute_scale500_state_roots_not_independent",
    ):
        initialize_scale500_session(
            scale_state_root=rollback_root / "scale500",
            rollback30_state_root=rollback_root,
            token_file=token_file,
            universe_source=valid_source,
            expected_universe_sha256=valid_digest,
            now=_at("2026-07-31T09:18:00"),
            initializer=_initializer,
        )


def test_scale500_systemd_candidate_is_sim_only_rollback_capable_and_exactly_scheduled() -> (
    None
):
    systemd_root = REPO_ROOT / "Ashare" / "systemd"
    session_service = (
        systemd_root / "tradingagent-ashare-minute-scale500-session.service"
    ).read_text(encoding="utf-8")
    session_timer = (
        systemd_root / "tradingagent-ashare-minute-scale500-session.timer"
    ).read_text(encoding="utf-8")
    paper_service = (
        systemd_root / "tradingagent-ashare-minute-scale500-paper.service"
    ).read_text(encoding="utf-8")
    paper_timer = (
        systemd_root / "tradingagent-ashare-minute-scale500-paper.timer"
    ).read_text(encoding="utf-8")
    late_start_service = (
        systemd_root / "tradingagent-ashare-minute-scale500-late-start.service"
    ).read_text(encoding="utf-8")
    rollback_service = (
        systemd_root / "tradingagent-ashare-minute-scale500-rollback.service"
    ).read_text(encoding="utf-8")
    environment = (
        systemd_root / "tradingagent-ashare-minute-scale500.env.example"
    ).read_text(encoding="utf-8")

    for service in (session_service, paper_service):
        assert "Environment=REAL_TRADING_ENABLED=false" in service
        assert "User=tradingagent" in service
        assert "Ashare.minute_scale500_runtime" in service
        assert (
            "ConditionPathExists=/etc/tradingagent/ashare-minute-scale500.env"
            in service
        )
        assert (
            "ConditionPathExists=/run/secrets/tradingagent/"
            "tradingdatas-read.token"
        ) in service
        assert (
            "ConditionPathExists=/opt/investment/tools/venvs/"
            "tradingagent-observation-py312-pyyaml603-v1/bin/python3"
        ) in service
        assert (
            "ConditionPathExists=/opt/investment/releases/tradingagent/current/"
            not in service
        )
        assert "ReadOnlyPaths=/var/lib/tradingagent/ashare-minute-paper" in service
        assert (
            "ReadWritePaths=/var/lib/tradingagent/ashare-minute-paper-scale500"
            in service
        )
        assert (
            "OnFailure=tradingagent-ashare-minute-scale500-rollback.service"
            not in service
        )
        assert "broker" not in service.lower()

    session_triggers = tuple(
        line for line in session_timer.splitlines() if line.startswith("OnCalendar=")
    )
    assert session_triggers == (
        "OnCalendar=Mon..Fri *-*-* 09:18:00",
        "OnCalendar=Mon..Fri *-*-* 09:24:00",
        "OnCalendar=Mon..Fri *-*-* 09:30:00",
        "OnCalendar=Mon..Fri *-*-* 09:36:00",
    )
    triggers = tuple(
        line for line in paper_timer.splitlines() if line.startswith("OnCalendar=")
    )
    from Ashare.minute_auto_runner import (
        expected_available_bar_end,
        session_bar_ends,
    )

    slots = session_bar_ends(_at("2026-07-28T10:00:00").date())
    expected_calendar = tuple(
        "OnCalendar=Mon..Fri *-*-* "
        f"{(slot + timedelta(minutes=7)).strftime('%H:%M:%S')}"
        for slot in slots
    )
    assert triggers == expected_calendar
    assert len(triggers) == 48
    assert "09..11" not in paper_timer
    assert "13..15" not in paper_timer
    assert "15:07:00" in paper_timer
    assert "15:19" not in paper_timer
    assert "Persistent=false" in paper_timer
    assert "Unit=tradingagent-ashare-minute-scale500-paper.service" in paper_timer
    rollback_commands = tuple(
        line.removeprefix("ExecStart=")
        for line in rollback_service.splitlines()
        if line.startswith("ExecStart=")
    )
    assert rollback_commands == (
        "/usr/bin/systemctl disable --now "
        "tradingagent-ashare-minute-scale500-session.timer "
        "tradingagent-ashare-minute-scale500-paper.timer",
        "/usr/bin/systemctl stop "
        "tradingagent-ashare-minute-scale500-session.service "
        "tradingagent-ashare-minute-scale500-paper.service",
    )
    trigger_times = tuple(
        slot + timedelta(minutes=7) for slot in slots
    )
    assert len(trigger_times) == 48
    for trigger, slot in zip(trigger_times, slots, strict=True):
        assert expected_available_bar_end(trigger) == slot
        assert expected_available_bar_end(trigger + timedelta(seconds=10)) == slot
    assert "/opt/investment/current" not in rollback_service
    for unit in (
        "tradingagent-ashare-minute-session.timer",
        "tradingagent-ashare-minute-paper.timer",
        "tradingagent-ashare-minute-session.service",
        "tradingagent-ashare-minute-paper.service",
    ):
        assert unit not in rollback_service
    assert "REAL_TRADING_ENABLED=false" in environment
    assert (
        "ASHARE_MINUTE_SCALE500_STATE_ROOT="
        "/var/lib/tradingagent/ashare-minute-paper-scale500"
    ) in environment
    assert (
        "ASHARE_MINUTE_ROLLBACK30_STATE_ROOT=/var/lib/tradingagent/ashare-minute-paper"
    ) in environment
    assert "ASHARE_MINUTE_SCALE500_UNIVERSE_SHA256=" in environment
    forbidden = ("TOKEN=", "BROKER", "REAL_TRADING_ENABLED=true")
    assert not any(value in environment for value in forbidden)
    assert "--allow-late-start" not in session_service + paper_service
    assert "--allow-late-start" in late_start_service
    assert (
        "--canary-receipt ${ASHARE_MINUTE_SCALE500_CANARY_RECEIPT}"
        in late_start_service
    )
    assert (
        "ConditionPathExists=/var/lib/tradingagent/ashare-minute-paper-scale500/current-canary-receipt.json"
        in late_start_service
    )
    assert "ASHARE_MINUTE_SCALE500_CANARY_RECEIPT=" in environment
    assert (
        "OnFailure=tradingagent-ashare-minute-scale500-rollback.service"
        in late_start_service
    )
    assert "[Install]" not in late_start_service
    assert "OnCalendar=" not in late_start_service
    assert "Environment=REAL_TRADING_ENABLED=false" in late_start_service
    assert (
        "ReadOnlyPaths=/var/lib/tradingagent/ashare-minute-paper" in late_start_service
    )
    assert (
        "ReadWritePaths=/var/lib/tradingagent/ashare-minute-paper-scale500"
        in late_start_service
    )
    assert "broker" not in late_start_service.lower()
    assert "rm " not in rollback_service


def test_cli_late_start_flag_is_run_only_and_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scale_root, rollback_root, token_file, universe_source, digest = _paths(tmp_path)
    captured: dict[str, object] = {}

    def fake_runner(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "noop", "reason": "outside_delayed_session_window"}

    monkeypatch.setattr(
        "Ashare.minute_scale500_runtime.run_scale500_once",
        fake_runner,
    )
    code = main(
        [
            "run",
            "--scale-state-root",
            str(scale_root),
            "--rollback30-state-root",
            str(rollback_root),
            "--token-file",
            str(token_file),
            "--universe-source",
            str(universe_source),
            "--expected-universe-sha256",
            digest,
            "--allow-late-start",
            "--now",
            "2026-07-31T13:54:00+08:00",
        ]
    )

    assert code == 0
    assert captured["allow_late_start"] is True

    code = main(
        [
            "initialize",
            "--scale-state-root",
            str(scale_root),
            "--rollback30-state-root",
            str(rollback_root),
            "--token-file",
            str(token_file),
            "--universe-source",
            str(universe_source),
            "--expected-universe-sha256",
            digest,
            "--allow-late-start",
            "--now",
            "2026-07-31T13:54:00+08:00",
        ]
    )
    assert code == 2


def test_final_delayed_timer_does_not_reuse_a_stale_1500_bar() -> None:
    from Ashare.minute_auto_runner import expected_available_bar_end

    target = expected_available_bar_end(_at("2026-07-31T15:19:00"))
    assert target is None


def test_cli_prints_exact_secret_free_reason_instead_of_silent_exit2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scale_root, rollback_root, token_file, universe_source, _ = _paths(tmp_path)
    code = main(
        [
            "initialize",
            "--scale-state-root",
            str(scale_root),
            "--rollback30-state-root",
            str(rollback_root),
            "--token-file",
            str(token_file),
            "--universe-source",
            str(universe_source),
            "--expected-universe-sha256",
            "0" * 64,
            "--now",
            "2026-07-31T09:18:00+08:00",
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    failure = json.loads(captured.err)
    assert failure["status"] == "failed_closed"
    assert failure["reason_code"] == "minute_scale500_universe_digest_mismatch"
    assert failure["selected_mode"] == "rollback30"
    assert failure["execution_authority"] is False
    assert failure["real_trading_enabled"] is False


def test_scale500_module_has_no_duplicate_literal_dict_keys() -> None:
    source = (REPO_ROOT / "Ashare" / "minute_scale500_runtime.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [
            key.value
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        ]
        assert len(keys) == len(set(keys))


def test_scale500_reference_fragment_rejects_non_500_budget() -> None:
    fragment = {
        "target_bar_end": "2026-07-31 13:10:00",
        "universe_sha256": "a" * 64,
        "max_rows": EXPECTED_UNIVERSE_COUNT - 1,
        "row_count": EXPECTED_UNIVERSE_COUNT,
        "cohort_count": 31,
        "cohort_size": 103,
        "cohorts": [],
    }
    with pytest.raises(
        MinuteScale500RuntimeError,
        match="minute_scale500_reference_bundle_invalid",
    ):
        _validate_scale500_reference_fragment(
            fragment,
            universe_symbols=frozenset(),
            universe_sha256="a" * 64,
            trading_date="2026-07-31",
            expected_bar_end="2026-07-31 13:10:00",
        )
