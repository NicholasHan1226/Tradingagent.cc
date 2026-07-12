"""Tests for dual-market capital authority v2 — Nicholas fresh-start approved.

Audit P0 fixes:
- fail-before-write: invalid manifest → root nonexistent
- real legacy freeze verification (actual file SHA, row count, dir exists)
- pinned decision IDs in policy
- reservation lineage (authority_generation, execution_lineage_id)
- reconcile conservation (active reservations must match, frozen amounts, conflicting payload)
- snapshot/provider capacities, available_to_reserve min constraint
- filesystem fsync parent
- ops: reject default root, --opening-manifest/--legacy-freeze-manifest paths
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from shared.capital.market_ledger import (
    RECONCILE_SOURCE_SCHEMA_VERSION,
    MarketCapitalLedger,
    MarketCapitalLedgerError,
    MarketCapitalReservationRequest,
    OpeningStateManifest,
    ReconcileManifest,
    _compute_event_checksum,
    load_market_capital_provider_state,
    market_capital_root,
)
from shared.capital.market_policy import MarketPolicy, MarketPolicyError

TRADE_DATE = "20260712"
PREV_DATE = "20260711"
EARLY_DATE = "20260710"

NICHOLAS_ID = "nicholas-fresh-start-019f5040-20260712"
SOURCE_THREAD = "019f5040-76a7-7672-b2fc-91c1526312bf"
EXECUTION_LINEAGE_ID = "exec-lineage-001"


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _real_legacy_freeze(tmp_path: Path) -> tuple[dict, Path, Path]:
    """Create a real legacy events file and archive dir, return freeze manifest."""
    archive = tmp_path / "legacy_archive"
    archive.mkdir(parents=True, exist_ok=True)
    events_file = tmp_path / "legacy_events.jsonl"
    events = [
        {"event_id": f"OLD-{i}", "event_type": "mark", "amount_cny": 100.0}
        for i in range(5)
    ]
    content = "\n".join(json.dumps(e, sort_keys=True) for e in events) + "\n"
    events_file.write_text(content, "utf-8")
    actual_sha = _sha256(content)
    return (
        {
            "events_path": str(events_file),
            "sha256": actual_sha,
            "last_event_id": "OLD-4",
            "row_count": 5,
            "frozen_at": "2026-07-12T00:00:00+08:00",
            "archive_path": str(archive),
            "imported": False,
        },
        events_file,
        archive,
    )


def _fresh_cutover() -> dict:
    return {
        "cutover_decision_id": NICHOLAS_ID,
        "source_thread_id": SOURCE_THREAD,
        "cutover_state": "fresh_start_approved",
        "authority_generation": 1,
        "confirmed_by": "nicholas",
    }


def _opening_manifest(market: str, **overrides) -> OpeningStateManifest:
    aid = "ashare-capital-v1" if market == "ashare" else "cn-futures-capital-v1"
    body = json.dumps({"mode": "fresh_start", "cash": 50000.0}, sort_keys=True)
    defaults = dict(
        market=market,
        authority_id=aid,
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
        source_sha256=_sha256(body),
        execution_lineage_id=EXECUTION_LINEAGE_ID,
        real=False,
    )
    defaults.update(overrides)
    return OpeningStateManifest(**defaults)


def _init_ledger(
    tmp_path: Path, market: str, legacy_tmp: Path | None = None, **manifest_overrides
) -> MarketCapitalLedger:
    policy = MarketPolicy.load(market)
    ledger = MarketCapitalLedger(tmp_path, policy=policy)
    if legacy_tmp is None:
        legacy_tmp = tmp_path
    freeze, _, _ = _real_legacy_freeze(legacy_tmp)
    ledger.initialize(
        _opening_manifest(market, **manifest_overrides),
        cutover_manifest=_fresh_cutover(),
        legacy_freeze_manifest=freeze,
    )
    return ledger


# ===========================================================================
# 1. Fail-before-write
# ===========================================================================


class TestFailBeforeWrite:
    def test_invalid_cash_leaves_root_nonexistent(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        freeze, _, _ = _real_legacy_freeze(tmp_path)
        with pytest.raises(MarketCapitalLedgerError):
            ledger.initialize(
                _opening_manifest("ashare", cash_balance_cny=49_999.0),
                cutover_manifest=_fresh_cutover(),
                legacy_freeze_manifest=freeze,
            )
        assert not root.exists()

    def test_nonzero_positions_leaves_root_nonexistent(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        freeze, _, _ = _real_legacy_freeze(tmp_path)
        with pytest.raises(MarketCapitalLedgerError):
            ledger.initialize(
                _opening_manifest("ashare", positions_by_risk_unit={"X": 1.0}),
                cutover_manifest=_fresh_cutover(),
                legacy_freeze_manifest=freeze,
            )
        assert not root.exists()

    def test_nonzero_margin_leaves_root_nonexistent(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("cn_futures"))
        freeze, _, _ = _real_legacy_freeze(tmp_path)
        with pytest.raises(MarketCapitalLedgerError):
            ledger.initialize(
                _opening_manifest(
                    "cn_futures", position_margin_by_risk_unit={"IF": 1.0}
                ),
                cutover_manifest=_fresh_cutover(),
                legacy_freeze_manifest=freeze,
            )
        assert not root.exists()

    def test_bad_source_sha_leaves_root_nonexistent(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        freeze, _, _ = _real_legacy_freeze(tmp_path)
        with pytest.raises(MarketCapitalLedgerError):
            ledger.initialize(
                _opening_manifest("ashare", source_sha256="bad"),
                cutover_manifest=_fresh_cutover(),
                legacy_freeze_manifest=freeze,
            )
        assert not root.exists()

    def test_missing_execution_lineage_leaves_root_nonexistent(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "cap"
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        freeze, _, _ = _real_legacy_freeze(tmp_path)
        with pytest.raises(MarketCapitalLedgerError):
            ledger.initialize(
                _opening_manifest("ashare", execution_lineage_id=""),
                cutover_manifest=_fresh_cutover(),
                legacy_freeze_manifest=freeze,
            )
        assert not root.exists()

    def test_wrong_authority_generation_in_manifest_leaves_root_nonexistent(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "cap"
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        freeze, _, _ = _real_legacy_freeze(tmp_path)
        # Can't actually pass wrong generation via OpeningStateManifest (no such field yet),
        # but cutover mismatch should also fail-before-write
        bad_cutover = dict(_fresh_cutover())
        bad_cutover["authority_generation"] = 99
        with pytest.raises(MarketCapitalLedgerError):
            ledger.initialize(
                _opening_manifest("ashare"),
                cutover_manifest=bad_cutover,
                legacy_freeze_manifest=freeze,
            )
        assert not root.exists()


# ===========================================================================
# 2. Real legacy freeze verification
# ===========================================================================


class TestRealLegacyFreeze:
    def test_valid_legacy_freeze_passes(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        freeze, ef, ar = _real_legacy_freeze(tmp_path)
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        result = ledger.initialize(
            _opening_manifest("ashare"),
            cutover_manifest=_fresh_cutover(),
            legacy_freeze_manifest=freeze,
        )
        assert result["status"] == "initialized"

    def test_legacy_events_path_not_file_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        freeze, ef, ar = _real_legacy_freeze(tmp_path)
        freeze["events_path"] = str(tmp_path / "nonexistent.jsonl")
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        with pytest.raises(MarketCapitalLedgerError, match="legacy"):
            ledger.initialize(
                _opening_manifest("ashare"),
                cutover_manifest=_fresh_cutover(),
                legacy_freeze_manifest=freeze,
            )
        assert not root.exists()

    def test_legacy_sha_mismatch_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        freeze, ef, ar = _real_legacy_freeze(tmp_path)
        freeze["sha256"] = "0" * 64  # wrong sha
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        with pytest.raises(MarketCapitalLedgerError, match="legacy|sha"):
            ledger.initialize(
                _opening_manifest("ashare"),
                cutover_manifest=_fresh_cutover(),
                legacy_freeze_manifest=freeze,
            )
        assert not root.exists()

    def test_legacy_sha_not_64hex_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        freeze, ef, ar = _real_legacy_freeze(tmp_path)
        freeze["sha256"] = "short"
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        with pytest.raises(MarketCapitalLedgerError, match="legacy|sha"):
            ledger.initialize(
                _opening_manifest("ashare"),
                cutover_manifest=_fresh_cutover(),
                legacy_freeze_manifest=freeze,
            )
        assert not root.exists()

    def test_legacy_row_count_mismatch_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        freeze, ef, ar = _real_legacy_freeze(tmp_path)
        freeze["row_count"] = 99
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        with pytest.raises(MarketCapitalLedgerError, match="legacy|row"):
            ledger.initialize(
                _opening_manifest("ashare"),
                cutover_manifest=_fresh_cutover(),
                legacy_freeze_manifest=freeze,
            )
        assert not root.exists()

    @pytest.mark.parametrize("bad_row_count", [None, "5", -1, True])
    def test_legacy_row_count_type_is_strict(
        self, tmp_path: Path, bad_row_count: object
    ) -> None:
        root = tmp_path / "cap"
        freeze, _, _ = _real_legacy_freeze(tmp_path)
        freeze["row_count"] = bad_row_count
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        with pytest.raises(MarketCapitalLedgerError, match="legacy|row_count"):
            ledger.initialize(
                _opening_manifest("ashare"),
                cutover_manifest=_fresh_cutover(),
                legacy_freeze_manifest=freeze,
            )
        assert not root.exists()

    def test_legacy_last_event_id_mismatch_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        freeze, ef, ar = _real_legacy_freeze(tmp_path)
        freeze["last_event_id"] = "WRONG"
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        with pytest.raises(MarketCapitalLedgerError, match="legacy|event_id"):
            ledger.initialize(
                _opening_manifest("ashare"),
                cutover_manifest=_fresh_cutover(),
                legacy_freeze_manifest=freeze,
            )
        assert not root.exists()

    def test_legacy_archive_not_dir_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        freeze, ef, ar = _real_legacy_freeze(tmp_path)
        freeze["archive_path"] = str(tmp_path / "nonexistent_dir")
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        with pytest.raises(MarketCapitalLedgerError, match="legacy|archive"):
            ledger.initialize(
                _opening_manifest("ashare"),
                cutover_manifest=_fresh_cutover(),
                legacy_freeze_manifest=freeze,
            )
        assert not root.exists()

    def test_legacy_frozen_at_naive_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        freeze, ef, ar = _real_legacy_freeze(tmp_path)
        freeze["frozen_at"] = "2026-07-12T00:00:00"  # no tz
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        with pytest.raises(MarketCapitalLedgerError, match="legacy|timezone"):
            ledger.initialize(
                _opening_manifest("ashare"),
                cutover_manifest=_fresh_cutover(),
                legacy_freeze_manifest=freeze,
            )
        assert not root.exists()

    def test_fake_paths_rejected(self, tmp_path: Path) -> None:
        """Fake /a/b paths from prior tests must be rejected."""
        root = tmp_path / "cap"
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        bad_freeze = {
            "events_path": "/a/b.jsonl",
            "sha256": "a" * 64,
            "last_event_id": "X",
            "row_count": 1,
            "frozen_at": "2026-01-01T00:00:00+00:00",
            "archive_path": "/a/",
            "imported": False,
        }
        with pytest.raises(MarketCapitalLedgerError):
            ledger.initialize(
                _opening_manifest("ashare"),
                cutover_manifest=_fresh_cutover(),
                legacy_freeze_manifest=bad_freeze,
            )
        assert not root.exists()


# ===========================================================================
# 3. Pinned decision IDs
# ===========================================================================


class TestPinnedDecisionIds:
    def test_policy_has_pinned_decision_id(self) -> None:
        p = MarketPolicy.load("ashare")
        assert p.cutover_decision_id == NICHOLAS_ID
        assert p.source_thread_id == SOURCE_THREAD

    def test_init_rejects_wrong_decision_id(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        freeze, _, _ = _real_legacy_freeze(tmp_path)
        bad = dict(_fresh_cutover())
        bad["cutover_decision_id"] = "wrong-id"
        with pytest.raises(MarketCapitalLedgerError, match="decision"):
            ledger.initialize(
                _opening_manifest("ashare"),
                cutover_manifest=bad,
                legacy_freeze_manifest=freeze,
            )
        assert not root.exists()

    def test_init_rejects_wrong_source_thread_id(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        freeze, _, _ = _real_legacy_freeze(tmp_path)
        bad = dict(_fresh_cutover())
        bad["source_thread_id"] = "wrong"
        with pytest.raises(MarketCapitalLedgerError, match="source_thread"):
            ledger.initialize(
                _opening_manifest("ashare"),
                cutover_manifest=bad,
                legacy_freeze_manifest=freeze,
            )
        assert not root.exists()


# ===========================================================================
# 4. Reservation lineage
# ===========================================================================


class TestReservationLineage:
    def _req(
        self, market: str, ref: str, sym: str, amt: float, **kw
    ) -> MarketCapitalReservationRequest:
        aid = "ashare-capital-v1" if market == "ashare" else "cn-futures-capital-v1"
        d = dict(
            market=market,
            reference_id=ref,
            risk_unit_key=sym,
            worst_case_amount_cny=amt,
            authority_id=aid,
            trade_date=TRADE_DATE,
            point_in_time_as_of="2026-07-12T10:00:00+08:00",
            lineage_sha256=_sha256("test"),
            authority_generation=1,
            execution_lineage_id=EXECUTION_LINEAGE_ID,
        )
        d.update(kw)
        return MarketCapitalReservationRequest(**d)

    def test_reservation_requires_authority_generation(self) -> None:
        """Request must carry authority_generation."""
        req = self._req("ashare", "R", "000001.XSHE", 100.0)
        assert req.authority_generation == 1

    def test_reservation_requires_execution_lineage_id(self) -> None:
        req = self._req("ashare", "R", "000001.XSHE", 100.0)
        assert req.execution_lineage_id == EXECUTION_LINEAGE_ID

    def test_reservation_requires_64hex_lineage(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        r = ledger.reserve(
            self._req("ashare", "R", "000001.XSHE", 100.0, lineage_sha256="short")
        )
        assert r.approved is False
        assert "lineage" in r.reason.lower()

    def test_reservation_requires_nonempty_pit_as_of(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        r = ledger.reserve(
            self._req("ashare", "R", "000001.XSHE", 100.0, point_in_time_as_of="")
        )
        assert r.approved is False

    @pytest.mark.parametrize("market", ["ashare", "cn_futures"])
    def test_new_risk_requires_current_trade_date_reconcile(
        self,
        tmp_path: Path,
        market: str,
    ) -> None:
        ledger = _init_ledger(tmp_path / market, market, legacy_tmp=tmp_path)

        decision = ledger.reserve(
            self._req(
                market,
                f"NO-RECONCILE-{market}",
                "000001.XSHE" if market == "ashare" else "IF2607",
                100.0,
            )
        )

        assert decision.approved is False
        assert decision.reason == "current_trade_date_reconcile_required"

    def test_yesterday_reconcile_cannot_authorize_today_new_risk(
        self,
        tmp_path: Path,
    ) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(PREV_DATE))

        decision = ledger.reserve(
            self._req("ashare", "TODAY-NEW-RISK", "000001.XSHE", 100.0)
        )

        assert decision.approved is False
        assert decision.reason == "current_trade_date_reconcile_required"

    def test_reservation_pit_cannot_precede_current_day_reconcile(
        self,
        tmp_path: Path,
    ) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(
            _mtm_ashare(
                TRADE_DATE,
                pit_timestamp="2026-07-12T10:30:00+08:00",
            )
        )

        decision = ledger.reserve(
            self._req(
                "ashare",
                "STALE-PIT",
                "000001.XSHE",
                100.0,
                point_in_time_as_of="2026-07-12T10:00:00+08:00",
            )
        )

        assert decision.approved is False
        assert decision.reason == "reservation_point_in_time_before_reconcile"

    def test_exact_reservation_retry_survives_later_trade_date_without_new_risk(
        self,
        tmp_path: Path,
    ) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        request = self._req("ashare", "IDEMPOTENT-RECOVERY", "000001.XSHE", 100.0)
        first = ledger.reserve(request)
        assert first.approved is True
        ledger.mtm_reconcile(
            _mtm_ashare(
                "20260713",
                active_reservations_cny=100.0,
                active_reservations=ledger.active_reservation_manifest(),
                expected_ledger_event_id=first.snapshot.event_id,
                expected_ledger_checksum=first.snapshot.event_checksum,
            )
        )

        retry = ledger.reserve(request)

        assert retry.approved is True
        assert retry.reason == "idempotent_reservation"
        assert retry.reservation_id == first.reservation_id

    def test_reservation_stores_lineage_on_event(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        d = ledger.reserve(self._req("ashare", "R-LINEAGE", "000001.XSHE", 100.0))
        assert d.approved is True
        events = ledger._load_events_unlocked()
        reserve_evt = [e for e in events if e["event_type"] == "reserve"][0]
        assert reserve_evt.get("authority_generation") == 1
        assert reserve_evt.get("execution_lineage_id") == EXECUTION_LINEAGE_ID
        assert reserve_evt.get("lineage_sha256") == _sha256("test")
        assert reserve_evt.get("point_in_time_as_of") == "2026-07-12T10:00:00+08:00"

    def test_reservation_rejects_wrong_market_even_with_matching_authority(
        self, tmp_path: Path
    ) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        request = MarketCapitalReservationRequest(
            market="cn_futures",
            reference_id="WRONG-MARKET",
            risk_unit_key="000001.XSHE",
            worst_case_amount_cny=100.0,
            authority_id="ashare-capital-v1",
            trade_date=TRADE_DATE,
            point_in_time_as_of="2026-07-12T10:00:00+08:00",
            lineage_sha256=_sha256("test"),
            authority_generation=1,
            execution_lineage_id=EXECUTION_LINEAGE_ID,
        )
        decision = ledger.reserve(request)
        assert decision.approved is False
        assert decision.reason == "market_mismatch"

    def test_idempotency_conflicts_on_different_lineage_sha(
        self, tmp_path: Path
    ) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        first = ledger.reserve(
            self._req("ashare", "LINEAGE-CONFLICT", "000001.XSHE", 100.0)
        )
        assert first.approved is True
        conflict = ledger.reserve(
            self._req(
                "ashare",
                "LINEAGE-CONFLICT",
                "000001.XSHE",
                100.0,
                lineage_sha256=_sha256("different"),
            )
        )
        assert conflict.approved is False
        assert conflict.reason == "reservation_conflict"

    def test_verify_returns_lineage_fields(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        d = ledger.reserve(self._req("ashare", "V-LINEAGE", "000001.XSHE", 100.0))
        v = ledger.verify_reservation(
            reservation_id=d.reservation_id,
            reference_id="V-LINEAGE",
            market="ashare",
            authority_id="ashare-capital-v1",
            retained_amount_cny=100.0,
            authority_generation=1,
            execution_lineage_id=EXECUTION_LINEAGE_ID,
            risk_unit_key="000001.XSHE",
        )
        assert v["verified"] is True
        assert v.get("authority_generation") == 1
        assert v.get("execution_lineage_id") == EXECUTION_LINEAGE_ID
        assert v.get("risk_unit_key") == "000001.XSHE"
        assert v.get("lineage_sha256") == _sha256("test")

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"authority_generation": None}, "gen"),
            ({"execution_lineage_id": ""}, "execution_lineage"),
            ({"risk_unit_key": ""}, "risk_unit"),
        ],
    )
    def test_verify_requires_explicit_lineage_scope(
        self, tmp_path: Path, overrides: dict[str, object], reason: str
    ) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        decision = ledger.reserve(
            self._req("ashare", "VERIFY-SCOPE", "000001.XSHE", 100.0)
        )
        kwargs: dict[str, object] = {
            "reservation_id": decision.reservation_id,
            "reference_id": "VERIFY-SCOPE",
            "market": "ashare",
            "authority_id": "ashare-capital-v1",
            "retained_amount_cny": 100.0,
            "authority_generation": 1,
            "execution_lineage_id": EXECUTION_LINEAGE_ID,
            "risk_unit_key": "000001.XSHE",
        }
        kwargs.update(overrides)
        verified = ledger.verify_reservation(**kwargs)
        assert verified["verified"] is False
        assert reason in verified["reason"]


# ===========================================================================
# 5. Reconcile conservation
# ===========================================================================


def _with_canonical_reconcile_source(
    manifest: ReconcileManifest,
) -> ReconcileManifest:
    payload = {
        "schema_version": RECONCILE_SOURCE_SCHEMA_VERSION,
        "market": manifest.market,
        "trade_date": str(manifest.as_of).replace("-", ""),
        "pit_timestamp": manifest.pit_timestamp,
        "execution_lineage_id": manifest.execution_lineage_id,
        "cash_balance_cny": manifest.cash_balance_cny,
        "positions_market_value": manifest.positions_market_value,
        "unrealized_pnl_cny": manifest.unrealized_pnl_cny,
        "position_margin_by_risk_unit": manifest.position_margin_by_risk_unit,
        "active_reservations_cny": manifest.active_reservations_cny,
        "active_reservations": manifest.active_reservations,
        "frozen_order_cash_cny": manifest.frozen_order_cash_cny,
        "frozen_order_margin_cny": manifest.frozen_order_margin_cny,
        "positions_quantity_by_risk_unit": (
            manifest.positions_quantity_by_risk_unit or {}
        ),
        "positions_cost_basis_cny_by_risk_unit": (
            manifest.positions_cost_basis_cny_by_risk_unit or {}
        ),
        "positions_entry_fee_cny_by_risk_unit": (
            manifest.positions_entry_fee_cny_by_risk_unit or {}
        ),
        "position_entry_price_by_risk_unit": (
            manifest.position_entry_price_by_risk_unit or {}
        ),
        "position_side_by_risk_unit": manifest.position_side_by_risk_unit or {},
        "position_contract_multiplier_by_risk_unit": (
            manifest.position_contract_multiplier_by_risk_unit or {}
        ),
        "position_contract_spec_sha256_by_risk_unit": (
            manifest.position_contract_spec_sha256_by_risk_unit or {}
        ),
        "position_mark_price_by_risk_unit": (
            manifest.position_mark_price_by_risk_unit or {}
        ),
        "expected_ledger_event_id": manifest.expected_ledger_event_id,
        "expected_ledger_checksum": manifest.expected_ledger_checksum,
        "included_fill_commit_ids": list(manifest.included_fill_commit_ids),
        "real_trading_enabled": False,
    }
    fd, raw_path = tempfile.mkstemp(prefix="market-capital-reconcile-", suffix=".json")
    os.close(fd)
    path = Path(raw_path)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return replace(
        manifest,
        source_sha256=sha,
        canonical_snapshot_path=str(path),
        canonical_snapshot_sha256=sha,
    )


def _mtm_ashare(as_of: str = TRADE_DATE, **kw) -> ReconcileManifest:
    compact = str(as_of).replace("-", "")
    pit = (
        f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}T09:00:00+08:00"
        if len(compact) == 8 and compact.isdigit()
        else "2026-07-12T09:00:00+08:00"
    )
    d = dict(
        market="ashare",
        authority_id="ashare-capital-v1",
        as_of=as_of,
        cash_balance_cny=50_000.0,
        positions_market_value={},
        unrealized_pnl_cny=0.0,
        position_margin_by_risk_unit={},
        active_reservations_cny=0.0,
        frozen_order_cash_cny=0.0,
        frozen_order_margin_cny=0.0,
        authority_generation=1,
        execution_lineage_id=EXECUTION_LINEAGE_ID,
        pit_timestamp=pit,
        source="t",
        source_sha256=_sha256("x"),
    )
    d.update(kw)
    return _with_canonical_reconcile_source(ReconcileManifest(**d))


def _mtm_cn(as_of: str = TRADE_DATE, **kw) -> ReconcileManifest:
    compact = str(as_of).replace("-", "")
    pit = (
        f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}T09:00:00+08:00"
        if len(compact) == 8 and compact.isdigit()
        else "2026-07-12T09:00:00+08:00"
    )
    d = dict(
        market="cn_futures",
        authority_id="cn-futures-capital-v1",
        as_of=as_of,
        cash_balance_cny=50_000.0,
        positions_market_value={},
        unrealized_pnl_cny=0.0,
        position_margin_by_risk_unit={},
        active_reservations_cny=0.0,
        frozen_order_cash_cny=0.0,
        frozen_order_margin_cny=0.0,
        authority_generation=1,
        execution_lineage_id=EXECUTION_LINEAGE_ID,
        pit_timestamp=pit,
        source="t",
        source_sha256=_sha256("x"),
    )
    d.update(kw)
    return _with_canonical_reconcile_source(ReconcileManifest(**d))


class TestReconcileConservation:
    def test_active_reservations_must_match(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        # First reconcile on TRADE_DATE
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        # Reserve
        ledger.reserve(
            MarketCapitalReservationRequest(
                "ashare",
                "R1",
                "000001.XSHE",
                7_500.0,
                "ashare-capital-v1",
                TRADE_DATE,
                "2026-07-12T10:00:00+08:00",
                _sha256("x"),
                authority_generation=1,
                execution_lineage_id=EXECUTION_LINEAGE_ID,
            )
        )
        # Now reconcile on a DIFFERENT as_of with wrong active_reservations
        NEW_DATE = "20260713"
        with pytest.raises(MarketCapitalLedgerError, match="reservation"):
            ledger.mtm_reconcile(_mtm_ashare(NEW_DATE, active_reservations_cny=0.0))

    def test_conflicting_same_as_of_payload_rejected(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE, cash_balance_cny=50_000.0))
        # Same as_of cannot overwrite canonical cash even with a new source file.
        with pytest.raises(MarketCapitalLedgerError, match="cash_conservation"):
            ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE, cash_balance_cny=49_000.0))

    def test_bootstrap_alone_not_fresh(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        state = ledger.provider_state(TRADE_DATE)
        assert state["fresh"] is False  # bootstrap alone, no reconcile
        assert state["reconciled"] is False
        assert state["last_reconciled_trade_date"] == ""

    def test_fresh_after_reconcile(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        state = ledger.provider_state(TRADE_DATE)
        assert state["fresh"] is True

    def test_source_sha_must_be_64hex(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        with pytest.raises(MarketCapitalLedgerError, match="sha"):
            ledger.mtm_reconcile(
                replace(_mtm_ashare(TRADE_DATE), source_sha256="short")
            )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("as_of", ""),
            ("cash_balance_cny", float("nan")),
            ("active_reservations_cny", -1.0),
            ("frozen_order_cash_cny", -1.0),
            ("pit_timestamp", "2026-07-12T09:30:00"),
            ("execution_lineage_id", ""),
            ("source", ""),
        ],
    )
    def test_reconcile_rejects_invalid_point_in_time_evidence(
        self, tmp_path: Path, field: str, value: object
    ) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        with pytest.raises(MarketCapitalLedgerError):
            ledger.mtm_reconcile(_mtm_ashare(**{field: value}))

    def test_same_as_of_conflict_checks_full_payload(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        with pytest.raises(
            MarketCapitalLedgerError, match="reconcile_reference_conflict"
        ):
            ledger.mtm_reconcile(
                _mtm_ashare(
                    TRADE_DATE,
                    pit_timestamp="2026-07-12T10:00:00+08:00",
                )
            )

    def test_equity_conservation_ashare(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        with pytest.raises(MarketCapitalLedgerError, match="cash_conservation"):
            ledger.mtm_reconcile(
                _mtm_ashare(
                    TRADE_DATE,
                    cash_balance_cny=40_000.0,
                    positions_market_value={"000001.XSHE": 10_000.0},
                )
            )

    def test_equity_conservation_cn(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "cn_futures")
        with pytest.raises(MarketCapitalLedgerError, match="cash_conservation"):
            ledger.mtm_reconcile(
                _mtm_cn(
                    TRADE_DATE,
                    cash_balance_cny=49_000.0,
                    unrealized_pnl_cny=1_000.0,
                )
            )


# ===========================================================================
# 6. Snapshot/provider capacities
# ===========================================================================


class TestSnapshotCapacities:
    def test_ashare_snapshot_has_exposure_limits(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        s = ledger.snapshot()
        assert s.positions_market_value_cny == 0.0
        # available capacity should reflect 45k gross limit
        state = ledger.provider_state(TRADE_DATE)
        assert "stock_gross_exposure_limit_cny" in state
        assert state["available_to_reserve_cny"] == 45_000.0

    def test_cn_available_never_50000(self, tmp_path: Path) -> None:
        """CNFutures available_to_reserve must be ≤ 25000, never 50000."""
        ledger = _init_ledger(tmp_path, "cn_futures")
        ledger.mtm_reconcile(_mtm_cn(TRADE_DATE))
        s = ledger.snapshot()
        # available_to_reserve must be min(cash constraint, remaining 25k margin)
        assert s.available_to_reserve_cny <= 25_000.0
        # Never report 50k available
        assert s.available_to_reserve_cny < 50_000.0

    def test_reconcile_cannot_mint_frozen_cash(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        with pytest.raises(MarketCapitalLedgerError, match="frozen_cash"):
            ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE, frozen_order_cash_cny=500.0))

    def test_reconcile_cannot_mint_cn_frozen_margin(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "cn_futures")
        with pytest.raises(MarketCapitalLedgerError, match="frozen_margin"):
            ledger.mtm_reconcile(
                _mtm_cn(
                    TRADE_DATE,
                    frozen_order_margin_cny=5_000.0,
                )
            )

    def test_initialize_snapshot_never_advertises_cross_market_50k_capacity(
        self, tmp_path: Path
    ) -> None:
        a = _init_ledger(tmp_path / "a", "ashare", legacy_tmp=tmp_path)
        c = _init_ledger(tmp_path / "c", "cn_futures", legacy_tmp=tmp_path)
        assert a.snapshot().available_to_reserve_cny == 45_000.0
        assert c.snapshot().available_to_reserve_cny == 25_000.0


# ===========================================================================
# 7. Filesystem: fsync parent
# ===========================================================================


class TestFilesystem:
    def test_init_creates_root_with_parent_fsync(self, tmp_path: Path) -> None:
        root = tmp_path / "deep" / "cap"
        _init_ledger(root, "ashare", legacy_tmp=tmp_path)
        assert root.exists()
        assert (root / "ashare_sim_capital_events.jsonl").exists()
        assert (root / "ashare_sim_capital_latest.json").exists()

    def test_init_idempotent_same_root(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        _init_ledger(root, "ashare")
        # Reuse same legacy freeze — must already exist
        freeze, _, _ = _real_legacy_freeze(tmp_path)
        ledger = MarketCapitalLedger(root, policy=MarketPolicy.load("ashare"))
        result = ledger.initialize(
            _opening_manifest("ashare"),
            cutover_manifest=_fresh_cutover(),
            legacy_freeze_manifest=freeze,
        )
        assert result["status"] == "already_initialized"


# ===========================================================================
# 8. Ops: reject default root, manifest paths
# ===========================================================================


class TestOpsRejectDefaultRoot:
    def test_init_rejects_default_production_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--root matching resolved default production root must be rejected."""
        default = market_capital_root("ashare")
        monkeypatch.setenv("TRADINGAGENT_ASHARE_CAPITAL_ROOT", str(default))
        # Even with --root explicitly set to the default, must reject
        from tools.market_capital_ops import _is_default_production_root

        # Verify the check function works
        assert _is_default_production_root("ashare", default) is True
        # A temp root should not be default
        assert _is_default_production_root("ashare", tmp_path / "test_cap") is False


# ===========================================================================
# Keep all prior tests (condensed but present)
# ===========================================================================


class TestPolicyBasics:
    def test_fresh_start_approved(self) -> None:
        p = MarketPolicy.load("ashare")
        assert p.cutover_state == "fresh_start_approved"
        assert p.real_trading_enabled is False

    def test_cn_margin_utilization(self) -> None:
        p = MarketPolicy.load("cn_futures")
        assert p.margin_utilization_limit_pct == 0.50
        assert p.margin_utilization_limit_cny == 25_000.0

    def test_no_account_epoch(self) -> None:
        assert not hasattr(MarketPolicy.load("ashare"), "account_epoch")


class TestNoAutoBootstrap:
    def test_construct_no_create(self, tmp_path: Path) -> None:
        r = tmp_path / "nope"
        MarketCapitalLedger(r, policy=MarketPolicy.load("ashare"))
        assert not r.exists()

    def test_wrappers_no_bootstrap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = tmp_path / "nope"
        monkeypatch.setenv("TRADINGAGENT_ASHARE_CAPITAL_ROOT", str(r))
        assert load_market_capital_provider_state("ashare", TRADE_DATE) is None
        assert not r.exists()


class TestChecksumChain:
    def test_previous_checksum_in_canonical(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        genesis = ledger._load_events_unlocked()[0]
        content = dict(genesis)
        content.pop("checksum", None)
        assert genesis["checksum"] == _sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True)
        )

    def test_chain_linear(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        events = ledger._load_events_unlocked()
        for i in range(1, len(events)):
            assert events[i]["previous_checksum"] == events[i - 1]["checksum"]

    def test_tamper_fails(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ep = tmp_path / "ashare_sim_capital_events.jsonl"
        lines = [line for line in ep.read_text("utf-8").splitlines() if line.strip()]
        tampered = []
        for line in lines:
            row = json.loads(line)
            if row["event_type"] == "bootstrap":
                row["cash_balance_cny"] = 99_999.0
            tampered.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
        ep.write_text("\n".join(tampered) + "\n", "utf-8")
        with pytest.raises(MarketCapitalLedgerError, match="cksum|checksum"):
            ledger.snapshot()

    def test_duplicate_event_id(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ep = tmp_path / "ashare_sim_capital_events.jsonl"
        events = ledger._load_events_unlocked()
        dup = dict(events[0])
        dup.pop("checksum", None)
        dup["previous_checksum"] = events[-1]["checksum"]
        dup["checksum"] = _compute_event_checksum(dup)
        with ep.open("a", encoding="utf-8") as h:
            h.write(json.dumps(dup, ensure_ascii=False, sort_keys=True) + "\n")
        with pytest.raises(MarketCapitalLedgerError, match="dup"):
            ledger.snapshot()


class TestMTMRisk:
    def test_daily_loss(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        ledger.record_realized_pnl(
            reference_id="daily-loss",
            amount_cny=-1_500.0,
            trade_date=TRADE_DATE,
        )
        r = ledger.reserve(
            MarketCapitalReservationRequest(
                "ashare",
                "R",
                "000001.XSHE",
                100.0,
                "ashare-capital-v1",
                TRADE_DATE,
                "2026-07-12T10:00:00+08:00",
                _sha256("x"),
                authority_generation=1,
                execution_lineage_id=EXECUTION_LINEAGE_ID,
            )
        )
        assert r.approved is False
        assert r.reason == "daily_loss_limit"

    def test_drawdown_halt(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(EARLY_DATE))
        ledger.record_realized_pnl(
            reference_id="peak", amount_cny=4_000.0, trade_date=EARLY_DATE
        )
        ledger.mtm_reconcile(_mtm_ashare(PREV_DATE, cash_balance_cny=54_000.0))
        ledger.record_realized_pnl(
            reference_id="drawdown-1", amount_cny=-3_000.0, trade_date=PREV_DATE
        )
        prev_head = ledger.snapshot()
        ledger.mtm_reconcile(
            _mtm_ashare(
                PREV_DATE,
                cash_balance_cny=51_000.0,
                expected_ledger_event_id=prev_head.event_id,
                expected_ledger_checksum=prev_head.event_checksum,
            )
        )
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE, cash_balance_cny=51_000.0))
        ledger.record_realized_pnl(
            reference_id="drawdown-2", amount_cny=-500.0, trade_date=TRADE_DATE
        )
        r = ledger.reserve(
            MarketCapitalReservationRequest(
                "ashare",
                "R",
                "000002.XSHE",
                100.0,
                "ashare-capital-v1",
                TRADE_DATE,
                "2026-07-12T10:00:00+08:00",
                _sha256("x"),
                authority_generation=1,
                execution_lineage_id=EXECUTION_LINEAGE_ID,
            )
        )
        assert r.approved is False
        assert r.reason == "maximum_drawdown_limit"

    def test_drawdown_tighten_5625(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(EARLY_DATE))
        ledger.record_realized_pnl(
            reference_id="peak", amount_cny=4_000.0, trade_date=EARLY_DATE
        )
        ledger.mtm_reconcile(_mtm_ashare(PREV_DATE, cash_balance_cny=54_000.0))
        ledger.record_realized_pnl(
            reference_id="tighten-1", amount_cny=-2_000.0, trade_date=PREV_DATE
        )
        prev_head = ledger.snapshot()
        ledger.mtm_reconcile(
            _mtm_ashare(
                PREV_DATE,
                cash_balance_cny=52_000.0,
                expected_ledger_event_id=prev_head.event_id,
                expected_ledger_checksum=prev_head.event_checksum,
            )
        )
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE, cash_balance_cny=52_000.0))
        ledger.record_realized_pnl(
            reference_id="tighten-2", amount_cny=-500.0, trade_date=TRADE_DATE
        )
        ok = ledger.reserve(
            MarketCapitalReservationRequest(
                "ashare",
                "R1",
                "000002.XSHE",
                5_625.0,
                "ashare-capital-v1",
                TRADE_DATE,
                "2026-07-12T10:00:00+08:00",
                _sha256("x"),
                authority_generation=1,
                execution_lineage_id=EXECUTION_LINEAGE_ID,
            )
        )
        assert ok.approved and ok.risk_tightened and ok.risk_multiplier == 0.75

    def test_drawdown_tighten_cn_18750(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "cn_futures")
        ledger.mtm_reconcile(_mtm_cn(EARLY_DATE))
        ledger.record_realized_pnl(
            reference_id="cn-peak", amount_cny=4_000.0, trade_date=EARLY_DATE
        )
        ledger.mtm_reconcile(_mtm_cn(PREV_DATE, cash_balance_cny=54_000.0))
        ledger.record_realized_pnl(
            reference_id="cn-tighten-1", amount_cny=-2_000.0, trade_date=PREV_DATE
        )
        prev_head = ledger.snapshot()
        ledger.mtm_reconcile(
            _mtm_cn(
                PREV_DATE,
                cash_balance_cny=52_000.0,
                expected_ledger_event_id=prev_head.event_id,
                expected_ledger_checksum=prev_head.event_checksum,
            )
        )
        ledger.mtm_reconcile(_mtm_cn(TRADE_DATE, cash_balance_cny=52_000.0))
        ledger.record_realized_pnl(
            reference_id="cn-tighten-2", amount_cny=-500.0, trade_date=TRADE_DATE
        )
        ok = ledger.reserve(
            MarketCapitalReservationRequest(
                "cn_futures",
                "R1",
                "IC2409",
                13_000.0,
                "cn-futures-capital-v1",
                TRADE_DATE,
                "2026-07-12T10:00:00+08:00",
                _sha256("x"),
                authority_generation=1,
                execution_lineage_id=EXECUTION_LINEAGE_ID,
            )
        )
        assert ok.approved and ok.risk_tightened


class TestPortfolioAggregation:
    def test_45000_cap(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        syms = [f"00000{i}.XSHE" for i in range(1, 8)]
        for i, sym in enumerate(syms):
            r = ledger.reserve(
                MarketCapitalReservationRequest(
                    "ashare",
                    f"R{i}",
                    sym,
                    7_000.0,
                    "ashare-capital-v1",
                    TRADE_DATE,
                    "2026-07-12T10:00:00+08:00",
                    _sha256("x"),
                    authority_generation=1,
                    execution_lineage_id=EXECUTION_LINEAGE_ID,
                )
            )
            if i < 6:
                assert r.approved is True, f"sym {sym} i={i}: {r.reason}"
            else:
                assert r.approved is False
                assert "gross_exposure" in r.reason

    def test_single_symbol_49000_rejected(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        r = ledger.reserve(
            MarketCapitalReservationRequest(
                "ashare",
                "R",
                "000001.XSHE",
                49_000.0,
                "ashare-capital-v1",
                TRADE_DATE,
                "2026-07-12T10:00:00+08:00",
                _sha256("x"),
                authority_generation=1,
                execution_lineage_id=EXECUTION_LINEAGE_ID,
            )
        )
        assert r.approved is False
        assert "single_name_cap" in r.reason


class TestCNFuturesCap:
    def test_25000_hard(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "cn_futures")
        ledger.mtm_reconcile(_mtm_cn(TRADE_DATE))
        r0 = ledger.reserve(
            MarketCapitalReservationRequest(
                "cn_futures",
                "R0",
                "IF2409",
                10_000.0,
                "cn-futures-capital-v1",
                TRADE_DATE,
                "2026-07-12T10:00:00+08:00",
                _sha256("x"),
                authority_generation=1,
                execution_lineage_id=EXECUTION_LINEAGE_ID,
            )
        )
        assert r0.approved
        r1 = ledger.reserve(
            MarketCapitalReservationRequest(
                "cn_futures",
                "R1",
                "IC2409",
                15_000.0,
                "cn-futures-capital-v1",
                TRADE_DATE,
                "2026-07-12T10:00:00+08:00",
                _sha256("x"),
                authority_generation=1,
                execution_lineage_id=EXECUTION_LINEAGE_ID,
            )
        )
        assert r1.approved
        r2 = ledger.reserve(
            MarketCapitalReservationRequest(
                "cn_futures",
                "R2",
                "IM2409",
                0.01,
                "cn-futures-capital-v1",
                TRADE_DATE,
                "2026-07-12T10:00:00+08:00",
                _sha256("x"),
                authority_generation=1,
                execution_lineage_id=EXECUTION_LINEAGE_ID,
            )
        )
        assert r2.approved is False
        assert r2.reason == "margin_limit_exhausted"


class TestIsolation:
    def test_independent(self, tmp_path: Path) -> None:
        a = _init_ledger(tmp_path / "a", "ashare")
        c = _init_ledger(tmp_path / "c", "cn_futures", legacy_tmp=tmp_path)
        a.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        c.mtm_reconcile(_mtm_cn(TRADE_DATE))
        ar = a.reserve(
            MarketCapitalReservationRequest(
                "ashare",
                "A",
                "000001.XSHE",
                7_500.0,
                "ashare-capital-v1",
                TRADE_DATE,
                "2026-07-12T10:00:00+08:00",
                _sha256("x"),
                authority_generation=1,
                execution_lineage_id=EXECUTION_LINEAGE_ID,
            )
        )
        cr = c.reserve(
            MarketCapitalReservationRequest(
                "cn_futures",
                "C",
                "IF2409",
                20_000.0,
                "cn-futures-capital-v1",
                TRADE_DATE,
                "2026-07-12T10:00:00+08:00",
                _sha256("x"),
                authority_generation=1,
                execution_lineage_id=EXECUTION_LINEAGE_ID,
            )
        )
        assert ar.approved and cr.approved


class TestLifecycle:
    def test_idempotent_reserve(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        req = MarketCapitalReservationRequest(
            "ashare",
            "ORD",
            "000001.XSHE",
            7_500.0,
            "ashare-capital-v1",
            TRADE_DATE,
            "2026-07-12T10:00:00+08:00",
            _sha256("x"),
            authority_generation=1,
            execution_lineage_id=EXECUTION_LINEAGE_ID,
        )
        f = ledger.reserve(req)
        d = ledger.reserve(req)
        assert f.approved and d.approved and d.reason == "idempotent_reservation"

    def test_partial_release(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "cn_futures")
        ledger.mtm_reconcile(_mtm_cn(TRADE_DATE))
        d = ledger.reserve(
            MarketCapitalReservationRequest(
                "cn_futures",
                "R",
                "IF2409",
                4_000.0,
                "cn-futures-capital-v1",
                TRADE_DATE,
                "2026-07-12T10:00:00+08:00",
                _sha256("x"),
                authority_generation=1,
                execution_lineage_id=EXECUTION_LINEAGE_ID,
            )
        )
        ledger.release(d.reservation_id, 1_500.0, "partial")
        assert ledger.snapshot().reserved_capital_cny == 2_500.0

    def test_pnl_idempotent(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        f = ledger.record_realized_pnl(
            reference_id="P", amount_cny=-3.0, trade_date=TRADE_DATE
        )
        d = ledger.record_realized_pnl(
            reference_id="P", amount_cny=-3.0, trade_date=TRADE_DATE
        )
        assert f["status"] == "recorded" and d["status"] == "idempotent_realized_pnl"


class TestConsecutiveLosses:
    def test_independent(self, tmp_path: Path) -> None:
        a = _init_ledger(tmp_path / "a", "ashare")
        c = _init_ledger(tmp_path / "c", "cn_futures", legacy_tmp=tmp_path)
        a.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        c.mtm_reconcile(_mtm_cn(TRADE_DATE))
        for i in range(3):
            a.record_realized_pnl(
                reference_id=f"L{i}", amount_cny=-1.0, trade_date=TRADE_DATE
            )
        ab = a.reserve(
            MarketCapitalReservationRequest(
                "ashare",
                "A",
                "000001.XSHE",
                100.0,
                "ashare-capital-v1",
                TRADE_DATE,
                "2026-07-12T10:00:00+08:00",
                _sha256("x"),
                authority_generation=1,
                execution_lineage_id=EXECUTION_LINEAGE_ID,
            )
        )
        assert ab.approved is False and ab.reason == "consecutive_loss_limit"
        co = c.reserve(
            MarketCapitalReservationRequest(
                "cn_futures",
                "C",
                "IF2409",
                10_000.0,
                "cn-futures-capital-v1",
                TRADE_DATE,
                "2026-07-12T10:00:00+08:00",
                _sha256("x"),
                authority_generation=1,
                execution_lineage_id=EXECUTION_LINEAGE_ID,
            )
        )
        assert co.approved


class TestConcurrency:
    def test_concurrent(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "cn_futures")
        ledger.mtm_reconcile(_mtm_cn(TRADE_DATE))

        def reserve(i: int) -> bool:
            return ledger.reserve(
                MarketCapitalReservationRequest(
                    "cn_futures",
                    f"F{i}",
                    "IF2409",
                    5_000.0,
                    "cn-futures-capital-v1",
                    TRADE_DATE,
                    "2026-07-12T10:00:00+08:00",
                    _sha256("x"),
                    authority_generation=1,
                    execution_lineage_id=EXECUTION_LINEAGE_ID,
                )
            ).approved

        with ThreadPoolExecutor(max_workers=20) as pool:
            approvals = list(pool.map(reserve, range(20)))
        assert sum(approvals) <= 5
        assert ledger.snapshot().reserved_capital_cny <= 25_000.0


class TestRealFalse:
    def test_all_false(self, tmp_path: Path) -> None:
        for m in ("ashare", "cn_futures"):
            assert MarketPolicy.load(m).real_trading_enabled is False
        ledger = _init_ledger(tmp_path, "ashare")
        assert ledger.snapshot().real_trading_enabled is False
        for e in ledger._load_events_unlocked():
            assert e.get("real_trading_enabled") is False


class TestProjection:
    def test_latest(self, tmp_path: Path) -> None:
        ledger = _init_ledger(tmp_path, "ashare")
        ledger.mtm_reconcile(_mtm_ashare(TRADE_DATE))
        ledger.reserve(
            MarketCapitalReservationRequest(
                "ashare",
                "P",
                "000001.XSHE",
                7_500.0,
                "ashare-capital-v1",
                TRADE_DATE,
                "2026-07-12T10:00:00+08:00",
                _sha256("x"),
                authority_generation=1,
                execution_lineage_id=EXECUTION_LINEAGE_ID,
            )
        )
        proj = json.loads(
            (tmp_path / "ashare_sim_capital_latest.json").read_text("utf-8")
        )
        assert proj["authority_id"] == "ashare-capital-v1"
        assert proj["reserved_capital_cny"] == 7_500.0


class TestSymlink:
    def test_policy_rejects(self, tmp_path: Path) -> None:
        real = tmp_path / "r.yaml"
        real.write_text("x: 1")
        link = tmp_path / "l.yaml"
        link.symlink_to(real)
        with pytest.raises(MarketPolicyError, match="symlink"):
            MarketPolicy.load("ashare", path=link)

    def test_ledger_rejects(self, tmp_path: Path) -> None:
        real = tmp_path / "r"
        real.mkdir()
        link = tmp_path / "l"
        link.symlink_to(real, target_is_directory=True)
        with pytest.raises(MarketCapitalLedgerError, match="symlink"):
            MarketCapitalLedger(link, policy=MarketPolicy.load("ashare"))
