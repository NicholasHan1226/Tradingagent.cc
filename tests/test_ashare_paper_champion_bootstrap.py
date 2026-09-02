from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from Ashare.capital_backed_paper_runner import (
    make_observation_window,
)
from Ashare.paper_champion import (
    PAPER_CHAMPION_MODEL_ID,
    PAPER_CHAMPION_MODEL_VERSION,
    PAPER_CHAMPION_SELECTION_ID,
    frozen_paper_champion_spec,
    paper_champion_designation_sha256,
)
from Ashare.paper_champion_bootstrap import (
    PaperChampionBootstrapError,
    bootstrap_paper_champion,
)
from shared.models.champion_registry import (
    ChampionRegistryError,
    ChampionSelectionRegistry,
)
from shared.review.decision_ledger import ExposureDisposition
from tests.test_ashare_capital_backed_paper_runner import _run_session


PAPER_SYMBOLS = ("000063.SZ", "600276.SH", "000001.SZ")
ROOT = Path(__file__).resolve().parents[1]


def _observation_windows() -> dict[str, object]:
    return {
        symbol: make_observation_window(
            symbol,
            prior_close_cny=10.0,
            quote_clocks_ok=True,
        )
        for symbol in PAPER_SYMBOLS
    }


def test_frozen_paper_champion_spec_is_stable_and_simulation_only() -> None:
    first = frozen_paper_champion_spec()
    second = frozen_paper_champion_spec()
    assert first.manifest_sha256 == second.manifest_sha256
    assert len(first.manifest_sha256) == 64
    assert first.champion_id == PAPER_CHAMPION_MODEL_ID
    assert first.version == PAPER_CHAMPION_MODEL_VERSION
    assert first.score_semantics == "uncalibrated_deterministic_rank_score"
    assert first.automatic_promotion_enabled is False
    assert first.risk_expansion_enabled is False
    assert paper_champion_designation_sha256() == paper_champion_designation_sha256()


def test_empty_registry_still_rejects_as_champion_current_unavailable(
    tmp_path: Path,
) -> None:
    registry = ChampionSelectionRegistry(tmp_path / "empty-registry")
    with pytest.raises(ChampionRegistryError, match="current_pointer_missing"):
        registry.load_current()
    assert not (tmp_path / "empty-registry" / "current.json").exists()
    assert list((tmp_path / "empty-registry" / "receipts").glob("*.json")) == []

    result = _run_session(
        tmp_path,
        windows=_observation_windows(),
        champion_registry=registry,
        drift_ok=True,
    )
    assert result.fill_count == 0
    for symbol in PAPER_SYMBOLS:
        row = result.disposition_for(symbol)
        assert row.disposition is ExposureDisposition.REJECTED
        assert row.rejection_reason == "champion_current_unavailable"
        assert row.reason_code == "champion_current_unavailable"


def test_bootstrap_records_simulation_only_current_and_clears_champion_gate(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "champion-registry"
    recorded = bootstrap_paper_champion(registry_root=registry_root)

    assert recorded["status"] == "recorded"
    assert recorded["actor"] == "automation"
    assert recorded["selection_id"] == PAPER_CHAMPION_SELECTION_ID
    assert recorded["simulation_only"] is True
    assert recorded["real_trading_enabled"] is False
    assert recorded["live_transition_authorized"] is False
    assert recorded["automatic_risk_expansion_enabled"] is False
    assert recorded["capital_layer"] == "simulated"
    assert recorded["account_type"] == "simulated"
    assert recorded["selected_model_id"] == PAPER_CHAMPION_MODEL_ID
    assert recorded["frozen_champion_spec_manifest_sha256"] == (
        frozen_paper_champion_spec().manifest_sha256
    )
    assert recorded["promotion_evidence_reference"].startswith("promotion-evidence:")

    registry = ChampionSelectionRegistry(registry_root)
    current = registry.load_current()
    assert current.receipt_sha256 == recorded["receipt_sha256"]
    assert current.selected_manifest_sha256 == recorded["selected_manifest_sha256"]
    assert current.simulation_only is True
    assert current.real_trading_enabled is False
    assert current.live_transition_authorized is False
    assert current.automatic_risk_expansion_enabled is False
    assert current.capital_layer == "simulated"
    assert current.account_type == "simulated"
    assert current.automatic_promotion_enabled is True

    replay = bootstrap_paper_champion(registry_root=registry_root)
    assert replay["status"] == "already_recorded"
    assert replay["receipt_sha256"] == recorded["receipt_sha256"]
    assert registry.load_history() == (current,)

    result = _run_session(
        tmp_path,
        windows=_observation_windows(),
        champion_registry=registry,
        drift_ok=False,
    )
    assert result.fill_count == 0
    for symbol in PAPER_SYMBOLS:
        row = result.disposition_for(symbol)
        assert row.rejection_reason != "champion_current_unavailable"
        assert row.reason_code != "champion_current_unavailable"
        assert row.disposition is ExposureDisposition.REJECTED
        assert row.rejection_reason == "drift_constraint_blocks_new_risk"


def test_bootstrap_refuses_when_real_trading_enabled_is_not_false(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "champion-registry"
    with pytest.raises(
        PaperChampionBootstrapError,
        match="real_trading_enabled_must_be_native_false",
    ):
        bootstrap_paper_champion(
            registry_root=registry_root,
            real_trading_enabled=True,
        )
    previous = os.environ.get("REAL_TRADING_ENABLED")
    os.environ["REAL_TRADING_ENABLED"] = "true"
    try:
        with pytest.raises(
            PaperChampionBootstrapError,
            match="real_trading_must_remain_disabled",
        ):
            bootstrap_paper_champion(registry_root=registry_root)
    finally:
        if previous is None:
            os.environ.pop("REAL_TRADING_ENABLED", None)
        else:
            os.environ["REAL_TRADING_ENABLED"] = previous
    assert not (registry_root / "current.json").exists()
    receipts = registry_root / "receipts"
    assert not receipts.exists() or list(receipts.glob("*.json")) == []


def test_bootstrap_refuses_to_overwrite_a_different_current(
    tmp_path: Path,
) -> None:
    from tests.test_capital_runtime_composition import _manual_registry

    foreign, _manifest = _manual_registry(tmp_path)
    with pytest.raises(
        PaperChampionBootstrapError,
        match="champion_current_already_present",
    ):
        bootstrap_paper_champion(registry_root=foreign.root)
    assert ChampionSelectionRegistry(foreign.root).load_current().selection_id != (
        PAPER_CHAMPION_SELECTION_ID
    )


def test_oneshot_candidate_is_sim_only_and_has_no_timer() -> None:
    root = ROOT / "Ashare" / "systemd"
    service = (root / "tradingagent-ashare-paper-champion-bootstrap.service").read_text(
        encoding="utf-8"
    )
    assert "Type=oneshot" in service
    assert "REAL_TRADING_ENABLED=false" in service
    assert "-m Ashare.paper_champion_bootstrap" in service
    assert "--registry-root /var/lib/tradingagent/ashare-canonical/shared/review/ashare/champion_registry" in service
    assert "[Install]" not in service
    assert "WantedBy=" not in service
    assert "live" not in service.lower() or "REAL_TRADING_ENABLED=false" in service
    assert "同花顺" not in service
    assert "compose_capital_backed_paper_runtime" not in service
    assert not (root / "tradingagent-ashare-paper-champion-bootstrap.timer").exists()


def test_bootstrap_does_not_handwrite_current_or_fabricate_kpi() -> None:
    source = (
        ROOT / "Ashare" / "paper_champion_bootstrap.py"
    ).read_text(encoding="utf-8")
    assert "registry.record_selection(" in source
    assert "execute_automatic_promotion" not in source
    assert "completed_round_trips" not in source
    assert "sample_kpi" not in source
    assert 'open("current.json"' not in source
    assert "current_path.write" not in source
