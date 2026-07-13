"""Tests for per-market capital ops CLI v2 — Nicholas fresh-start approved.

Audit P0 fixes:
- reject default production root for init
- --opening-manifest and --legacy-freeze-manifest paths (not synthesized JSON)
- real legacy freeze file verification
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

OPS_SCRIPT = str(
    Path(__file__).resolve().parents[1] / "tools" / "market_capital_ops.py"
)
TRADE_DATE = "20260712"
NICHOLAS_ID = "nicholas-fresh-start-019f5040-20260712"


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _run(*args: str) -> dict:
    result = subprocess.run(
        [sys.executable, OPS_SCRIPT, *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return {
            "raw_stdout": result.stdout,
            "raw_stderr": result.stderr,
            "returncode": result.returncode,
        }


def _make_opening_manifest(tmp_path: Path, market: str) -> Path:
    aid = "ashare-capital-v1" if market == "ashare" else "cn-futures-capital-v1"
    body = json.dumps({"mode": "fresh_start", "cash": 50000.0}, sort_keys=True)
    manifest = {
        "market": market,
        "authority_id": aid,
        "cutover_decision_id": NICHOLAS_ID,
        "mode": "fresh_start",
        "as_of": TRADE_DATE,
        "cash_balance_cny": 50000.0,
        "opening_equity_cny": 50000.0,
        "active_reservations_cny": 0.0,
        "consecutive_losses": 0,
        "inherited_high_water_equity_cny": 0.0,
        "positions_by_risk_unit": {},
        "position_margin_by_risk_unit": {},
        "frozen_order_cash_cny": 0.0,
        "realized_pnl_cny": 0.0,
        "unrealized_pnl_cny": 0.0,
        "source": "cli_test",
        "source_sha256": _sha256(body),
        "execution_lineage_id": "exec-lineage-test",
        "real": False,
    }
    p = tmp_path / "opening_manifest.json"
    p.write_text(json.dumps(manifest), "utf-8")
    return p


def _make_legacy_freeze_manifest(tmp_path: Path) -> Path:
    """Create a real legacy events file and archive, then the freeze manifest."""
    archive = tmp_path / "legacy_archive"
    archive.mkdir()
    events_file = tmp_path / "legacy_events.jsonl"
    events = [
        {"event_id": f"OLD-{i}", "event_type": "mark", "amount_cny": 100.0}
        for i in range(3)
    ]
    content = "\n".join(json.dumps(e, sort_keys=True) for e in events) + "\n"
    events_file.write_text(content, "utf-8")
    actual_sha = _sha256(content)
    manifest = {
        "events_path": str(events_file),
        "sha256": actual_sha,
        "last_event_id": "OLD-2",
        "row_count": 3,
        "frozen_at": "2026-07-12T00:00:00+08:00",
        "archive_path": str(archive),
        "imported": False,
    }
    p = tmp_path / "legacy_freeze.json"
    p.write_text(json.dumps(manifest), "utf-8")
    return p


# ===========================================================================
# Init — reject default root, require manifest paths
# ===========================================================================


class TestInit:
    def test_rejects_missing_root(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                OPS_SCRIPT,
                "init",
                "--market",
                "ashare",
                "--confirm-fresh-start",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0

    def test_rejects_missing_opening_manifest(self, tmp_path: Path) -> None:
        output = _run(
            "init",
            "--market",
            "ashare",
            "--confirm-fresh-start",
            "--root",
            str(tmp_path / "cap"),
        )
        assert output["status"] == "blocked"
        assert "opening" in str(output).lower()

    def test_rejects_missing_legacy_freeze(self, tmp_path: Path) -> None:
        om = _make_opening_manifest(tmp_path, "ashare")
        output = _run(
            "init",
            "--market",
            "ashare",
            "--confirm-fresh-start",
            "--root",
            str(tmp_path / "cap"),
            "--opening-manifest",
            str(om),
        )
        assert output["status"] == "blocked"
        assert "legacy" in str(output).lower()

    def test_successful_init(self, tmp_path: Path) -> None:
        om = _make_opening_manifest(tmp_path, "ashare")
        lm = _make_legacy_freeze_manifest(tmp_path)
        output = _run(
            "init",
            "--market",
            "ashare",
            "--confirm-fresh-start",
            "--root",
            str(tmp_path / "cap"),
            "--opening-manifest",
            str(om),
            "--legacy-freeze-manifest",
            str(lm),
        )
        assert output["status"] == "initialized"
        assert output["mode"] == "fresh_start"

    @pytest.mark.parametrize(
        ("field", "value", "expected_blocker"),
        [
            ("market", "cn_futures", "market_mismatch"),
            ("authority_id", "wrong-authority", "authority_id_mismatch"),
            ("mode", "inherit", "only_fresh_start_allowed"),
            ("cash_balance_cny", 49_999.0, "fresh_start_cash_50000"),
            (
                "positions_by_risk_unit",
                {"600000.SH": 100.0},
                "fresh_start_positions_zero",
            ),
            ("realized_pnl_cny", 1.0, "fresh_start_realized_pnl_zero"),
            ("active_reservations_cny", 1.0, "fresh_start_reservations_zero"),
            ("execution_lineage_id", "", "execution_lineage_id_required"),
            ("source_sha256", "not-a-sha", "source_sha256_not_64hex"),
            ("real", True, "real_must_be_false"),
        ],
    )
    def test_init_validates_user_opening_manifest_without_synthesizing_values(
        self,
        tmp_path: Path,
        field: str,
        value: object,
        expected_blocker: str,
    ) -> None:
        opening = _make_opening_manifest(tmp_path, "ashare")
        payload = json.loads(opening.read_text("utf-8"))
        payload[field] = value
        opening.write_text(json.dumps(payload), "utf-8")
        legacy = _make_legacy_freeze_manifest(tmp_path)
        root = tmp_path / "cap"

        output = _run(
            "init",
            "--market",
            "ashare",
            "--confirm-fresh-start",
            "--root",
            str(root),
            "--opening-manifest",
            str(opening),
            "--legacy-freeze-manifest",
            str(legacy),
        )

        assert output["status"] == "blocked"
        assert any(expected_blocker in blocker for blocker in output["blockers"])
        assert not root.exists()

    def test_init_rejects_missing_lineage_instead_of_defaulting_cli_lineage(
        self,
        tmp_path: Path,
    ) -> None:
        opening = _make_opening_manifest(tmp_path, "ashare")
        payload = json.loads(opening.read_text("utf-8"))
        payload.pop("execution_lineage_id")
        opening.write_text(json.dumps(payload), "utf-8")
        legacy = _make_legacy_freeze_manifest(tmp_path)
        root = tmp_path / "cap"

        output = _run(
            "init",
            "--market",
            "ashare",
            "--confirm-fresh-start",
            "--root",
            str(root),
            "--opening-manifest",
            str(opening),
            "--legacy-freeze-manifest",
            str(legacy),
        )

        assert output["status"] == "blocked"
        assert "opening_manifest_fields_invalid" in output["blockers"]
        assert not root.exists()

    def test_init_persists_user_supplied_opening_source_and_lineage(
        self,
        tmp_path: Path,
    ) -> None:
        opening = _make_opening_manifest(tmp_path, "ashare")
        payload = json.loads(opening.read_text("utf-8"))
        payload["source"] = "operator_verified_opening_snapshot"
        payload["execution_lineage_id"] = "operator-lineage-20260712"
        opening.write_text(json.dumps(payload), "utf-8")
        legacy = _make_legacy_freeze_manifest(tmp_path)
        root = tmp_path / "cap"

        output = _run(
            "init",
            "--market",
            "ashare",
            "--confirm-fresh-start",
            "--root",
            str(root),
            "--opening-manifest",
            str(opening),
            "--legacy-freeze-manifest",
            str(legacy),
        )

        assert output["status"] == "initialized"
        event = json.loads(
            (root / "ashare_sim_capital_events.jsonl")
            .read_text("utf-8")
            .splitlines()[0]
        )
        assert event["source"] == "operator_verified_opening_snapshot"
        assert event["source_sha256"] == payload["source_sha256"]
        assert event["execution_lineage_id"] == "operator-lineage-20260712"

    def test_init_does_not_create_files_before_validation(self, tmp_path: Path) -> None:
        """Missing opening manifest → no root created."""
        root = tmp_path / "cap"
        _run(
            "init",
            "--market",
            "ashare",
            "--confirm-fresh-start",
            "--root",
            str(root),
        )
        assert not root.exists()

    def test_default_root_untouched_test(self, tmp_path: Path) -> None:
        """Verify that a test init with explicit --root does not touch production default root."""
        from shared.capital.market_ledger import market_capital_root

        default = market_capital_root("ashare")
        default_existed_before = default.exists()
        # Do a successful test init to a temp root
        om = _make_opening_manifest(tmp_path, "ashare")
        lm = _make_legacy_freeze_manifest(tmp_path)
        _run(
            "init",
            "--market",
            "ashare",
            "--confirm-fresh-start",
            "--root",
            str(tmp_path / "test_cap"),
            "--opening-manifest",
            str(om),
            "--legacy-freeze-manifest",
            str(lm),
        )
        # Default root should be unchanged
        assert default.exists() == default_existed_before

    def test_rejects_real_trading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
        om = _make_opening_manifest(tmp_path, "ashare")
        lm = _make_legacy_freeze_manifest(tmp_path)
        output = _run(
            "init",
            "--market",
            "ashare",
            "--confirm-fresh-start",
            "--root",
            str(tmp_path / "cap"),
            "--opening-manifest",
            str(om),
            "--legacy-freeze-manifest",
            str(lm),
        )
        assert output["status"] == "blocked"
        assert "environment_real_trading_requested" in output["blockers"]


# ===========================================================================
# Status
# ===========================================================================


class TestStatus:
    def test_requires_market(self) -> None:
        result = subprocess.run(
            [sys.executable, OPS_SCRIPT, "status"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0

    def test_unavailable(self, tmp_path: Path) -> None:
        output = _run("status", "--market", "ashare", "--root", str(tmp_path / "x"))
        assert output["status"] == "market_capital_unavailable"

    def test_does_not_rewrite_latest_projection(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        _setup_ledger(root, "ashare", tmp_path)
        latest = root / "ashare_sim_capital_latest.json"
        sentinel = '{"sentinel":true}\n'
        latest.write_text(sentinel, encoding="utf-8")

        output = _run(
            "status",
            "--market",
            "ashare",
            "--trade-date",
            TRADE_DATE,
            "--root",
            str(root),
        )

        assert output["status"] == "market_capital_available"
        assert latest.read_text(encoding="utf-8") == sentinel


# ===========================================================================
# Verify
# ===========================================================================


class TestVerify:
    def test_requires_market(self) -> None:
        result = subprocess.run(
            [sys.executable, OPS_SCRIPT, "verify"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0

    def test_unavailable(self, tmp_path: Path) -> None:
        output = _run("verify", "--market", "ashare", "--root", str(tmp_path / "x"))
        assert output["status"] == "market_capital_unavailable"

    def test_valid_chain(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        _setup_ledger(root, "ashare", tmp_path)
        output = _run("verify", "--market", "ashare", "--root", str(root))
        assert output["status"] == "valid"

    def test_detect_tamper(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        _setup_ledger(root, "ashare", tmp_path)
        ep = root / "ashare_sim_capital_events.jsonl"
        lines = ep.read_text("utf-8").splitlines()
        tampered = []
        for line in lines:
            row = json.loads(line)
            if row["event_type"] == "bootstrap":
                row["cash_balance_cny"] = 99_999.0
            tampered.append(json.dumps(row, sort_keys=True))
        ep.write_text("\n".join(tampered) + "\n", "utf-8")
        output = _run("verify", "--market", "ashare", "--root", str(root))
        assert output["status"] == "invalid"


# ===========================================================================
# Reconcile dry-run
# ===========================================================================


class TestReconcileDryRun:
    def test_requires_market(self) -> None:
        result = subprocess.run(
            [sys.executable, OPS_SCRIPT, "reconcile-dry-run"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0

    def test_unavailable(self, tmp_path: Path) -> None:
        output = _run(
            "reconcile-dry-run", "--market", "ashare", "--root", str(tmp_path / "x")
        )
        assert output["status"] == "market_capital_unavailable"

    def test_readonly(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        _setup_ledger(root, "ashare", tmp_path)
        ep = root / "ashare_sim_capital_events.jsonl"
        latest = root / "ashare_sim_capital_latest.json"
        sentinel = '{"sentinel":true}\n'
        latest.write_text(sentinel, encoding="utf-8")
        before = len(ep.read_text("utf-8").splitlines())
        output = _run(
            "reconcile-dry-run",
            "--market",
            "ashare",
            "--trade-date",
            TRADE_DATE,
            "--root",
            str(root),
        )
        assert output["status"] == "dry_run_ok"
        assert len(ep.read_text("utf-8").splitlines()) == before
        assert latest.read_text(encoding="utf-8") == sentinel


# ===========================================================================
# Cutover audit
# ===========================================================================


class TestCutoverAudit:
    def test_requires_market(self) -> None:
        result = subprocess.run(
            [sys.executable, OPS_SCRIPT, "cutover-audit"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0

    def test_audit_without_authority(self, tmp_path: Path) -> None:
        output = _run(
            "cutover-audit", "--market", "ashare", "--root", str(tmp_path / "x")
        )
        assert output["status"] == "cutover_audit"
        assert output["cutover_state"] == "fresh_start_approved"
        assert output["authority_initialized"] is False

    def test_audit_with_authority(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        _setup_ledger(root, "ashare", tmp_path)
        latest = root / "ashare_sim_capital_latest.json"
        sentinel = '{"sentinel":true}\n'
        latest.write_text(sentinel, encoding="utf-8")
        output = _run("cutover-audit", "--market", "ashare", "--root", str(root))
        assert output["status"] == "cutover_audit"
        assert output["authority_initialized"] is True
        assert latest.read_text(encoding="utf-8") == sentinel


# ===========================================================================
# Dual status
# ===========================================================================


class TestDualStatus:
    def test_never_sums(self, tmp_path: Path) -> None:
        ar = tmp_path / "a"
        cr = tmp_path / "c"
        _setup_ledger(ar, "ashare", tmp_path)
        _setup_ledger(cr, "cn_futures", tmp_path)
        a_latest = ar / "ashare_sim_capital_latest.json"
        c_latest = cr / "cn_futures_sim_capital_latest.json"
        sentinel = '{"sentinel":true}\n'
        a_latest.write_text(sentinel, encoding="utf-8")
        c_latest.write_text(sentinel, encoding="utf-8")
        env = os.environ.copy()
        env["TRADINGAGENT_ASHARE_CAPITAL_ROOT"] = str(ar)
        env["TRADINGAGENT_CN_FUTURES_CAPITAL_ROOT"] = str(cr)
        result = subprocess.run(
            [sys.executable, OPS_SCRIPT, "dual-status", "--trade-date", TRADE_DATE],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        output = json.loads(result.stdout.strip())
        if output["status"] == "dual_market_capital":
            assert output["markets"]["ashare"]["initial_equity_cny"] == 50_000.0
            assert output["markets"]["cn_futures"]["initial_equity_cny"] == 50_000.0
        assert "100000" not in result.stdout
        assert a_latest.read_text(encoding="utf-8") == sentinel
        assert c_latest.read_text(encoding="utf-8") == sentinel


# ===========================================================================
# Migration plan
# ===========================================================================


class TestMigrationPlan:
    def test_readonly(self) -> None:
        output = _run("migration-plan")
        assert output["status"] == "migration_plan"


# ===========================================================================
# Retired master entry removed
# ===========================================================================


class TestMasterEntryRemoved:
    def test_retired_entry_no_longer_exists(self) -> None:
        master = Path(__file__).resolve().parents[1] / "tools" / "master_capital_ops.py"
        assert not master.exists()


# ===========================================================================
# Helpers
# ===========================================================================


def _setup_ledger(root: Path, market: str, work_tmp: Path) -> None:
    from shared.capital.market_ledger import MarketCapitalLedger, OpeningStateManifest
    from shared.capital.market_policy import MarketPolicy
    import hashlib

    root.mkdir(parents=True, exist_ok=True)
    policy = MarketPolicy.load(market)
    ledger = MarketCapitalLedger(root, policy=policy)

    # Real legacy freeze
    archive = work_tmp / f"archive_{market}"
    archive.mkdir(exist_ok=True)
    events_file = work_tmp / f"legacy_{market}.jsonl"
    events = [
        {"event_id": f"OLD-{i}", "event_type": "mark", "amount_cny": 100.0}
        for i in range(3)
    ]
    content = "\n".join(json.dumps(e, sort_keys=True) for e in events) + "\n"
    events_file.write_text(content, "utf-8")
    actual_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

    body = json.dumps({"mode": "fresh_start", "cash": 50000.0}, sort_keys=True)
    manifest = OpeningStateManifest(
        market=market,
        authority_id="ashare-capital-v1"
        if market == "ashare"
        else "cn-futures-capital-v1",
        cutover_decision_id=NICHOLAS_ID,
        mode="fresh_start",
        as_of=TRADE_DATE,
        cash_balance_cny=50_000.0,
        opening_equity_cny=50_000.0,
        active_reservations_cny=0.0,
        consecutive_losses=0,
        inherited_high_water_equity_cny=0.0,
        positions_by_risk_unit={},
        position_margin_by_risk_unit={},
        frozen_order_cash_cny=0.0,
        realized_pnl_cny=0.0,
        unrealized_pnl_cny=0.0,
        source="test",
        source_sha256=hashlib.sha256(body.encode()).hexdigest(),
        execution_lineage_id="exec-lineage-test",
        real=False,
    )
    ledger.initialize(
        manifest,
        cutover_manifest={
            "cutover_decision_id": NICHOLAS_ID,
            "source_thread_id": "019f5040-76a7-7672-b2fc-91c1526312bf",
            "cutover_state": "fresh_start_approved",
            "authority_generation": 1,
            "confirmed_by": "nicholas",
        },
        legacy_freeze_manifest={
            "events_path": str(events_file),
            "sha256": actual_sha,
            "last_event_id": "OLD-2",
            "row_count": 3,
            "frozen_at": "2026-07-12T00:00:00+08:00",
            "archive_path": str(archive),
            "imported": False,
        },
    )
