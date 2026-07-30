from __future__ import annotations

import ast
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pytest

from Ashare.minute_data import SHANGHAI
from Ashare.minute_scale500_runtime import (
    EXPECTED_UNIVERSE_COUNT,
    MinuteScale500RuntimeError,
    canonical_universe_sha256,
    initialize_scale500_session,
    main,
    run_scale500_once,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=SHANGHAI)


def _universe(path: Path, *, count: int = EXPECTED_UNIVERSE_COUNT) -> str:
    rows = []
    for index in range(count):
        if index % 2:
            symbol = f"{600000 + index:06d}.SH"
        else:
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
                    "max_rows": EXPECTED_UNIVERSE_COUNT,
                    "page_limit": EXPECTED_UNIVERSE_COUNT,
                    "max_pages": EXPECTED_UNIVERSE_COUNT,
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
        "authority_tier": "non_production_fixture",
        "capital_authority": False,
        "durable_capital": False,
        "execution_authority": False,
        "real_trading_enabled": False,
    }


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
        "expected_universe_count": 500,
        "failure_reason": None,
        "real_trading_enabled": False,
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
        now=_at("2026-07-31T09:49:00"),
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
        now=_at("2026-07-31T09:54:00"),
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
            now=_at("2026-07-31T09:49:00"),
            runner=fail,
        )

    gate = json.loads(
        (scale_root / ".scale500-gates" / "20260731.json").read_text(encoding="utf-8")
    )
    assert gate["status"] == "fallback30_selected"
    assert gate["selected_mode"] == "rollback30"
    assert gate["failure_reason"] == reason
    assert sentinel.read_text(encoding="utf-8") == "immutable-30"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("row_count", 499, "minute_scale500_row_count_mismatch"),
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
            now=_at("2026-07-31T09:49:00"),
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
            now=_at("2026-07-31T09:54:00"),
            runner=lambda **_: _receipt("2026-07-31 09:40:00"),
        )


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
        assert "ReadOnlyPaths=/var/lib/tradingagent/ashare-minute-paper" in service
        assert (
            "ReadWritePaths=/var/lib/tradingagent/ashare-minute-paper-scale500"
            in service
        )
        assert (
            "OnFailure=tradingagent-ashare-minute-scale500-rollback.service" in service
        )
        assert "broker" not in service.lower()

    assert "09:18:00" in session_timer
    triggers = tuple(
        line for line in paper_timer.splitlines() if line.startswith("OnCalendar=")
    )
    assert triggers == (
        "OnCalendar=Mon..Fri *-*-* 09:49/5:00",
        "OnCalendar=Mon..Fri *-*-* 10:04/5:00",
        "OnCalendar=Mon..Fri *-*-* 11:04..44/5:00",
        "OnCalendar=Mon..Fri *-*-* 13:19/5:00",
        "OnCalendar=Mon..Fri *-*-* 14:04/5:00",
        "OnCalendar=Mon..Fri *-*-* 15:04:00",
        "OnCalendar=Mon..Fri *-*-* 15:09:00",
        "OnCalendar=Mon..Fri *-*-* 15:19:00",
    )
    assert "15:14:00" not in paper_timer
    assert "Persistent=false" in paper_timer
    assert "disable --now tradingagent-ashare-minute-scale500" in rollback_service
    assert "enable --now tradingagent-ashare-minute-session.timer" in rollback_service
    assert "enable --now tradingagent-ashare-minute-paper.timer" in rollback_service
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
    assert "rm " not in rollback_service


def test_final_delayed_timer_still_targets_the_1500_bar() -> None:
    from Ashare.minute_auto_runner import expected_available_bar_end

    target = expected_available_bar_end(_at("2026-07-31T15:19:00"))
    assert target is not None
    assert target.strftime("%Y-%m-%d %H:%M:%S") == "2026-07-31 15:00:00"


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
