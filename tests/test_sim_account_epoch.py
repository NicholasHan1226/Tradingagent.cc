"""Tests for the simulated account epoch cutover mechanism.

The epoch mechanism now uses an authoritative-path cutover design:
- Epoch 1 (legacy 200k) and epoch 2 (current 50k) share the SAME
  ``shared/logs/local_sim/`` authoritative path.
- Missing epoch state always means legacy epoch 1, never epoch 2.
- Cutover preserves the authoritative directory and lock inode, archives the
  old ledger contents under that lock, then bootstraps a fresh 50k epoch 2.
- There is NEVER a ``local_sim_epoch2`` path.
- Dry-run reports exact actions and writes nothing.
- Apply is idempotent; archive target collisions fail closed.
- Bootstrap after cutover shows 50,000 cash and no positions/trades.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.execution.sim_account_epoch import (
    CURRENT_EPOCH_ID,
    EPOCHS,
    apply_cutover,
    dry_run_cutover,
    epoch_capital_cny,
    epoch_ledger_root,
    epoch_state_path,
    get_current_epoch,
    get_epoch,
    read_epoch_state,
)


class SimAccountEpochTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

    # ------------------------------------------------------------------
    # Epoch definitions
    # ------------------------------------------------------------------

    def test_epoch_definitions_have_correct_capital(self) -> None:
        self.assertEqual(EPOCHS[1]["id"], 1)
        self.assertEqual(EPOCHS[1]["label"], "legacy_200k")
        self.assertEqual(EPOCHS[1]["capital_cny"], 200_000.0)

        self.assertEqual(EPOCHS[2]["id"], 2)
        self.assertEqual(EPOCHS[2]["label"], "current_50k")
        self.assertEqual(EPOCHS[2]["capital_cny"], 50_000.0)

    def test_current_epoch_is_50k(self) -> None:
        self.assertEqual(CURRENT_EPOCH_ID, 2)

    def test_get_current_epoch_returns_epoch_2(self) -> None:
        epoch = get_current_epoch()
        self.assertEqual(epoch["id"], 2)
        self.assertEqual(epoch["capital_cny"], 50_000.0)

    def test_get_epoch_returns_correct_epoch(self) -> None:
        e1 = get_epoch(1)
        self.assertEqual(e1["capital_cny"], 200_000.0)
        e2 = get_epoch(2)
        self.assertEqual(e2["capital_cny"], 50_000.0)

    def test_get_epoch_raises_for_unknown_id(self) -> None:
        with self.assertRaises(KeyError):
            get_epoch(99)

    def test_epoch_capital_cny_returns_capital_for_epoch(self) -> None:
        self.assertEqual(epoch_capital_cny(1), 200_000.0)
        self.assertEqual(epoch_capital_cny(2), 50_000.0)

    def test_epoch_state_path_is_deterministic_json(self) -> None:
        path = epoch_state_path()
        self.assertTrue(str(path).endswith(".json"))

    # ------------------------------------------------------------------
    # Missing epoch state => epoch 1 (legacy), NEVER epoch 2
    # ------------------------------------------------------------------

    def test_missing_epoch_state_means_epoch_1_not_epoch_2(self) -> None:
        """Requirement (1): missing epoch state means legacy epoch 1."""
        state_file = self.tmp_path / "nonexistent_epoch_state.json"
        with patch("shared.execution.sim_account_epoch.epoch_state_path", return_value=state_file):
            state = read_epoch_state()
            self.assertEqual(state["current_epoch_id"], 1,
                             "Missing epoch state must default to epoch 1 (legacy)")
            self.assertIn("activated_at", state)
            self.assertEqual(state["source"], "no_state_file")

    def test_read_epoch_state_reads_persisted_state(self) -> None:
        state_file = self.tmp_path / "epoch_state.json"
        state_file.write_text(json.dumps({
            "current_epoch_id": 2,
            "activated_at": "2026-07-10T12:00:00+08:00",
            "previous_epoch_id": 1,
            "cutover_timestamp": "2026-07-10T11:00:00+08:00",
        }))

        with patch("shared.execution.sim_account_epoch.epoch_state_path", return_value=state_file):
            state = read_epoch_state()
            self.assertEqual(state["current_epoch_id"], 2)
            self.assertEqual(state["previous_epoch_id"], 1)

    # ------------------------------------------------------------------
    # No local_sim_epoch2 path anywhere
    # ------------------------------------------------------------------

    def test_epoch_ledger_root_both_epochs_use_authoritative_path(self) -> None:
        """Requirement (7): no local_sim_epoch2 path anywhere."""
        root1 = epoch_ledger_root(1)
        root2 = epoch_ledger_root(2)
        self.assertEqual(root1, root2,
                         "Both epochs must share the SAME authoritative local_sim path")
        self.assertIn("local_sim", str(root1))
        self.assertNotIn("epoch2", str(root1))
        self.assertNotIn("epoch2", str(root2))

    def test_epoch_ledger_root_never_uses_epoch2_suffix(self) -> None:
        for epoch_id in (1, 2):
            root = epoch_ledger_root(epoch_id)
            self.assertNotIn("local_sim_epoch", str(root),
                             f"epoch_ledger_root({epoch_id}) must not reference local_sim_epoch2")

    def test_ledger_root_raises_for_nonexistent_epoch(self) -> None:
        with self.assertRaises(KeyError):
            epoch_ledger_root(99)

    # ------------------------------------------------------------------
    # Dry-run: reports exact actions, writes nothing
    # ------------------------------------------------------------------

    def test_dry_run_reports_exact_actions_writes_nothing(self) -> None:
        """Requirement (2): dry-run reports exact actions and writes nothing."""
        ledger_dir = self.tmp_path / "local_sim"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "local_sim_trades.jsonl").write_text(
            '{"trade_id":"T1","ts_code":"600000.SH","side":"buy","quantity":100}\n')
        (ledger_dir / "local_sim_pnl.json").write_text(
            '{"acct":{"cash_available":198995.0}}')
        (ledger_dir / "local_sim_positions.json").write_text('{}')
        (ledger_dir / ".local_sim.lock").write_text("")

        positions_snapshot = self.tmp_path / "simulated_ashare_positions.json"
        positions_snapshot.write_text(json.dumps({"positions": [{"ts_code": "600000.SH"}]}))

        tiers_root = self.tmp_path / "local_sim_tiers"
        tiers_root.mkdir(parents=True)
        (tiers_root / "ashare_50000" / "local_sim_pnl.json").parent.mkdir(parents=True)
        (tiers_root / "ashare_50000" / "local_sim_pnl.json").write_text('{"test":"data"}')

        state_file = self.tmp_path / "epoch_state.json"

        with patch("shared.execution.sim_account_epoch.epoch_state_path", return_value=state_file):
            result = dry_run_cutover(
                ledger_path=ledger_dir,
                positions_snapshot_path=positions_snapshot,
                tiers_root=tiers_root,
            )

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["from_epoch"], 1)
        self.assertEqual(result["to_epoch"], 2)
        self.assertIn("actions", result)
        self.assertGreater(len(result["actions"]), 0)

        # Verify nothing was actually written
        self.assertTrue(ledger_dir.exists(), "Ledger must not be moved on dry-run")
        self.assertTrue(positions_snapshot.exists(), "Positions snapshot must not be moved on dry-run")
        self.assertTrue(list(ledger_dir.iterdir()), "Ledger files must remain on dry-run")
        self.assertFalse(state_file.exists(), "Epoch state must not be written on dry-run")

    # ------------------------------------------------------------------
    # Apply: atomic cutover
    # ------------------------------------------------------------------

    def test_apply_archives_and_recreates_authoritative_path(self) -> None:
        """Requirement (3): apply archives existing local_sim, recreates same path
        as fresh epoch 2 with metadata."""
        ledger_dir = self.tmp_path / "local_sim"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "local_sim_trades.jsonl").write_text(
            '{"trade_id":"T1","ts_code":"600000.SH","side":"buy","quantity":100}\n')
        (ledger_dir / "local_sim_pnl.json").write_text(
            '{"acct":{"cash_available":198995.0}}')
        (ledger_dir / "local_sim_positions.json").write_text('{}')
        (ledger_dir / ".local_sim.lock").write_text("")
        original_lock_inode = (ledger_dir / ".local_sim.lock").stat().st_ino

        positions_snapshot = self.tmp_path / "simulated_ashare_positions.json"
        positions_snapshot.write_text(json.dumps({"positions": [{"ts_code": "600000.SH"}]}))

        state_file = self.tmp_path / "epoch_state.json"
        archive_root = self.tmp_path / "epoch_archive"

        with patch("shared.execution.sim_account_epoch.epoch_state_path", return_value=state_file):
            result = apply_cutover(
                ledger_path=ledger_dir,
                positions_snapshot_path=positions_snapshot,
                archive_root=archive_root,
            )

        self.assertEqual(result["status"], "migrated")
        self.assertEqual(result["from_epoch"], 1)
        self.assertEqual(result["to_epoch"], 2)

        # Archive must exist with the old content
        archive_dir = archive_root / "epoch_1_legacy_200k"
        self.assertTrue(archive_dir.exists(), "Archive directory must be created")
        archived_trades = archive_dir / "local_sim_trades.jsonl"
        self.assertTrue(archived_trades.exists(), "Trades must be archived")
        self.assertIn("T1", archived_trades.read_text())

        # External positions snapshot must be archived as evidence
        archived_positions = archive_dir / "simulated_ashare_positions.json"
        self.assertTrue(archived_positions.exists(), "Positions snapshot must be archived as evidence")
        current_positions = json.loads(positions_snapshot.read_text())
        self.assertEqual(current_positions["cash_available"], 50_000.0)
        self.assertEqual(current_positions["positions"], [])

        # Archive manifest must exist
        manifest_path = archive_dir / "epoch_archive_manifest.json"
        self.assertTrue(manifest_path.exists(), "Archive manifest must be written")
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["epoch_id"], 1)

        # Authoritative local_sim path must be recreated as fresh epoch 2
        self.assertTrue(ledger_dir.exists(), "Authoritative path must be recreated")
        # The new directory should have empty bootstrap state (or minimal epoch metadata)
        epoch_meta = ledger_dir / ".epoch_metadata.json"
        self.assertTrue(epoch_meta.exists(), "Epoch metadata must be written")
        meta = json.loads(epoch_meta.read_text())
        self.assertEqual(meta["current_epoch_id"], 2)
        self.assertEqual(meta["capital_cny"], 50_000.0)

        # Epoch state must be persisted
        self.assertTrue(state_file.exists(), "Epoch state must be persisted")
        state = json.loads(state_file.read_text())
        self.assertEqual(state["current_epoch_id"], 2)
        self.assertEqual(state["previous_epoch_id"], 1)

        # Old trades must NOT be in the recreated local_sim
        old_trades_path = ledger_dir / "local_sim_trades.jsonl"
        self.assertFalse(old_trades_path.exists(),
                         "Old trades must not be in the recreated authoritative path")
        self.assertEqual(
            (ledger_dir / ".local_sim.lock").stat().st_ino,
            original_lock_inode,
            "Cutover must keep the authoritative lock inode in place so writers cannot bypass it",
        )
        self.assertFalse(
            (archive_dir / ".local_sim.lock").exists(),
            "The active lock belongs to the authoritative path, not the historical archive",
        )

    # ------------------------------------------------------------------
    # Archive target collision fails closed
    # ------------------------------------------------------------------

    def test_archive_target_collision_fails_closed(self) -> None:
        """Requirement (4): archive target collisions fail closed unless
        a prior completed migration is proven via manifest."""
        ledger_dir = self.tmp_path / "local_sim"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "local_sim_trades.jsonl").write_text('{"trade_id":"T1"}\n')

        archive_root = self.tmp_path / "epoch_archive"
        # Pre-create a collision without a valid manifest (simulating partial run)
        archive_dir = archive_root / "epoch_1_legacy_200k"
        archive_dir.mkdir(parents=True)
        (archive_dir / "stale_file.txt").write_text("leftover from partial run")

        state_file = self.tmp_path / "epoch_state.json"

        with patch("shared.execution.sim_account_epoch.epoch_state_path", return_value=state_file):
            result = apply_cutover(
                ledger_path=ledger_dir,
                archive_root=archive_root,
            )

        self.assertEqual(result["status"], "error")
        self.assertIn("collision", str(result.get("reason", "")).lower())

    # ------------------------------------------------------------------
    # Idempotent apply
    # ------------------------------------------------------------------

    def test_apply_idempotent_second_run_is_noop(self) -> None:
        """Requirement (5): apply is idempotent."""
        ledger_dir = self.tmp_path / "local_sim"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "local_sim_trades.jsonl").write_text('{"trade_id":"T1"}\n')

        state_file = self.tmp_path / "epoch_state.json"
        archive_root = self.tmp_path / "epoch_archive"

        with patch("shared.execution.sim_account_epoch.epoch_state_path", return_value=state_file):
            first = apply_cutover(
                ledger_path=ledger_dir,
                archive_root=archive_root,
            )
            second = apply_cutover(
                ledger_path=ledger_dir,
                archive_root=archive_root,
            )

        self.assertEqual(first["status"], "migrated")
        self.assertEqual(second["status"], "already_migrated",
                         "Second apply must be a no-op")
        self.assertEqual(second["current_epoch_id"], 2)

    def test_apply_rolls_back_when_bootstrap_write_fails(self) -> None:
        ledger_dir = self.tmp_path / "local_sim"
        ledger_dir.mkdir(parents=True)
        old_trade = ledger_dir / "local_sim_trades.jsonl"
        old_trade.write_text('{"trade_id":"T1"}\n')
        tiers_root = self.tmp_path / "local_sim_tiers"
        tiers_root.mkdir()
        (tiers_root / "legacy.json").write_text("{}")
        state_file = self.tmp_path / "epoch_state.json"

        with patch("shared.execution.sim_account_epoch.epoch_state_path", return_value=state_file), \
             patch("shared.execution.sim_account_epoch._write_current_epoch_bootstrap", side_effect=OSError("disk full")):
            result = apply_cutover(
                ledger_path=ledger_dir,
                tiers_root=tiers_root,
                archive_root=self.tmp_path / "epoch_archive",
            )

        self.assertEqual(result["status"], "error")
        self.assertTrue(old_trade.exists())
        self.assertTrue(tiers_root.exists())
        self.assertFalse(state_file.exists())

    def test_apply_fails_closed_when_state_is_corrupt(self) -> None:
        ledger_dir = self.tmp_path / "local_sim"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "local_sim_trades.jsonl").write_text('{"trade_id":"T1"}\n')
        state_file = self.tmp_path / "epoch_state.json"
        state_file.write_text("not-json")

        with patch("shared.execution.sim_account_epoch.epoch_state_path", return_value=state_file):
            result = apply_cutover(
                ledger_path=ledger_dir,
                archive_root=self.tmp_path / "epoch_archive",
            )

        self.assertEqual(result["status"], "error")
        self.assertIn("state", result["reason"].lower())
        self.assertTrue((ledger_dir / "local_sim_trades.jsonl").exists())

    def test_apply_fails_closed_when_epoch_state_and_metadata_disagree(self) -> None:
        ledger_dir = self.tmp_path / "local_sim"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / ".epoch_metadata.json").write_text(json.dumps({
            "current_epoch_id": 2,
            "capital_cny": 50_000.0,
        }))
        state_file = self.tmp_path / "epoch_state.json"
        state_file.write_text(json.dumps({"current_epoch_id": 1}))

        with patch("shared.execution.sim_account_epoch.epoch_state_path", return_value=state_file):
            result = apply_cutover(
                ledger_path=ledger_dir,
                archive_root=self.tmp_path / "epoch_archive",
            )

        self.assertEqual(result["status"], "error")
        self.assertIn("inconsistent", result["reason"].lower())
        self.assertFalse((self.tmp_path / "epoch_archive").exists())

    # ------------------------------------------------------------------
    # Bootstrap snapshot after cutover shows 50,000
    # ------------------------------------------------------------------

    def test_bootstrap_after_cutover_shows_50000_cash_no_positions(self) -> None:
        """Requirement (6): bootstrap current snapshot shows 50,000 cash
        and no positions/trades."""
        from shared.execution import local_sim_ledger

        ledger_dir = self.tmp_path / "local_sim"
        ledger_dir.mkdir(parents=True)
        # Simulate some old trades
        (ledger_dir / "local_sim_trades.jsonl").write_text(
            '{"trade_id":"OLD","ts_code":"600000.SH","side":"buy","quantity":100,"net_amount":1000,"status":"filled","account":"ashare_sim"}\n')

        positions_snapshot = self.tmp_path / "simulated_ashare_positions.json"
        positions_snapshot.write_text("{}")

        state_file = self.tmp_path / "epoch_state.json"
        archive_root = self.tmp_path / "epoch_archive"

        with patch("shared.execution.sim_account_epoch.epoch_state_path", return_value=state_file):
            apply_cutover(
                ledger_path=ledger_dir,
                positions_snapshot_path=positions_snapshot,
                archive_root=archive_root,
            )

        # Now patch the ledger globals to point at our recreated directory
        with patch.object(local_sim_ledger, "LOCAL_SIM_DIR", ledger_dir):
            with patch.object(local_sim_ledger, "LOCAL_SIM_TRADES", ledger_dir / "local_sim_trades.jsonl"):
                with patch.object(local_sim_ledger, "LOCAL_SIM_POSITIONS", ledger_dir / "local_sim_positions.json"):
                    with patch.object(local_sim_ledger, "LOCAL_SIM_PNL", ledger_dir / "local_sim_pnl.json"):
                        with patch.object(local_sim_ledger, "LOCAL_SIM_LOCK", ledger_dir / ".local_sim.lock"):
                            with patch.object(local_sim_ledger, "LOCAL_SIM_POSITIONS_SNAPSHOT",
                                              self.tmp_path / "snapshot_out.json"):
                                with patch.object(local_sim_ledger, "LOCAL_SIM_RECEIPTS",
                                                  self.tmp_path / "receipts_out.jsonl"):
                                    result = local_sim_ledger.ensure_local_sim_bootstrap_snapshot(
                                        starting_cash=50000)

        self.assertEqual(result["status"], "bootstrapped")
        self.assertEqual(result["cash_available"], 50000.0)

        # Verify snapshot content
        snapshot = json.loads((self.tmp_path / "snapshot_out.json").read_text())
        self.assertEqual(snapshot["bootstrap_state"], "no_trades_yet")
        self.assertEqual(snapshot["cash_available"], 50000.0)
        self.assertEqual(snapshot["positions"], [])

    # ------------------------------------------------------------------
    # Tier evidence preservation
    # ------------------------------------------------------------------

    def test_cutover_preserves_tier_evidence_when_supplied(self) -> None:
        """Tier experiments data is archived as evidence when supplied."""
        ledger_dir = self.tmp_path / "local_sim"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "local_sim_trades.jsonl").write_text('{"trade_id":"T1"}\n')

        tiers_root = self.tmp_path / "local_sim_tiers"
        (tiers_root / "ashare_50000").mkdir(parents=True)
        (tiers_root / "ashare_50000" / "local_sim_pnl.json").write_text('{"acct":{"total_pnl":500}}')
        (tiers_root / "ashare_100000").mkdir(parents=True)
        (tiers_root / "ashare_100000" / "local_sim_trades.jsonl").write_text('{"trade_id":"T100k"}\n')

        state_file = self.tmp_path / "epoch_state.json"
        archive_root = self.tmp_path / "epoch_archive"

        with patch("shared.execution.sim_account_epoch.epoch_state_path", return_value=state_file):
            result = apply_cutover(
                ledger_path=ledger_dir,
                archive_root=archive_root,
                tiers_root=tiers_root,
            )

        self.assertEqual(result["status"], "migrated")

        archive_dir = archive_root / "epoch_1_legacy_200k"
        tiers_archive = archive_dir / "local_sim_tiers"
        self.assertTrue(tiers_archive.exists(), "Tier data must be archived")
        self.assertTrue((tiers_archive / "ashare_50000" / "local_sim_pnl.json").exists())
        self.assertTrue((tiers_archive / "ashare_100000" / "local_sim_trades.jsonl").exists())
        self.assertFalse(tiers_root.exists(), "Historical tier books must not remain active after cutover")

    # ------------------------------------------------------------------
    # Dry-run and apply with positions snapshot optional
    # ------------------------------------------------------------------

    def test_dry_run_without_optional_evidence_still_succeeds(self) -> None:
        ledger_dir = self.tmp_path / "local_sim"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "local_sim_trades.jsonl").write_text('{"trade_id":"T1"}\n')

        state_file = self.tmp_path / "epoch_state.json"

        with patch("shared.execution.sim_account_epoch.epoch_state_path", return_value=state_file):
            result = dry_run_cutover(ledger_path=ledger_dir)

        self.assertEqual(result["status"], "dry_run")
        self.assertIn("actions", result)

    def test_apply_without_optional_evidence_still_migrates(self) -> None:
        ledger_dir = self.tmp_path / "local_sim"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "local_sim_trades.jsonl").write_text('{"trade_id":"T1"}\n')

        state_file = self.tmp_path / "epoch_state.json"
        archive_root = self.tmp_path / "epoch_archive"

        with patch("shared.execution.sim_account_epoch.epoch_state_path", return_value=state_file):
            result = apply_cutover(
                ledger_path=ledger_dir,
                archive_root=archive_root,
            )

        self.assertEqual(result["status"], "migrated")


if __name__ == "__main__":
    unittest.main()
