from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_unreferenced_legacy_ashare_modules_are_physically_retired() -> None:
    retired = (
        "Ashare/formal_close_refresh.py",
        "Ashare/forward_validation.py",
        "Ashare/sample_target_monitor.py",
        "Ashare/portfolio_evolution.py",
        "Ashare/sample_learning.py",
        "Ashare/epoch_review.py",
        "Ashare/tier_experiments.py",
        "tools/rebuild_current_epoch_reviews.py",
        "tools/migrate_sim_capital_epoch.py",
    )
    for relative_path in retired:
        assert not (ROOT / relative_path).exists(), relative_path


def test_unreferenced_legacy_ashare_wrappers_are_physically_retired() -> None:
    retired = (
        "shared/wrappers/job_ashare_sample_learning.sh",
        "shared/wrappers/job_ashare_formal_close_refresh.sh",
        "shared/wrappers/job_ashare_forward_validation.sh",
        "shared/wrappers/job_ashare_sample_target_monitor.sh",
    )
    for relative_path in retired:
        assert not (ROOT / relative_path).exists(), relative_path


def test_active_python_has_no_legacy_numeric_epoch_authority_imports() -> None:
    forbidden = (
        "from Ashare.epoch_review import",
        "from Ashare.sample_learning import",
        "from Ashare.tier_experiments import",
        "import Ashare.epoch_review",
        "import Ashare.sample_learning",
        "import Ashare.tier_experiments",
    )
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        if path == Path(__file__).resolve():
            continue
        if any(part in {".git", "__pycache__"} for part in path.parts):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if any(marker in source for marker in forbidden):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_evolution_controller_contains_no_automatic_expand_action() -> None:
    source = (ROOT / "Ashare" / "evolution_controller.py").read_text(encoding="utf-8")
    forbidden = "expand" + "_risk_candidate"
    assert forbidden not in source
    assert '"automatic_promotion_enabled": False' in source
    assert '"automatic_risk_expansion_enabled": False' in source


def test_sim_execution_does_not_refresh_legacy_portfolio_evolution() -> None:
    source = (ROOT / "shared/wrappers/job_ashare_sim_exec.sh").read_text(
        encoding="utf-8"
    )
    assert "refresh_portfolio_evolution" not in source
    assert "Ashare.portfolio_evolution" not in source


def test_unified_sample_ops_is_the_only_active_ashare_learning_job() -> None:
    schedule = (ROOT / "shared/crontab.txt").read_text(encoding="utf-8")
    assert "job_ashare_sample_ops.sh" in schedule
    for marker in (
        "job_ashare_sample_learning.sh",
        "job_ashare_formal_close_refresh.sh",
        "job_ashare_forward_validation.sh",
        "job_ashare_sample_target_monitor.sh",
    ):
        assert marker not in schedule


def test_sample_ops_uses_only_sample_journal_manual_decision_and_maturity() -> None:
    source = (ROOT / "shared/runtime_test/ashare_sample_ops.py").read_text(
        encoding="utf-8"
    )
    assert "SampleJournal" in source
    assert "build_evolution_decision" in source
    assert "publish_projection_generation" in source
    assert "assess_ashare_maturity" in source
    assert "portfolio_evolution" not in source
    assert "sample_learning" not in source
    assert '"automatic_promotion_enabled": False' in source
    assert '"automatic_risk_expansion_enabled": False' in source


def test_cn_futures_automatic_evolution_schedule_and_wrapper_are_retired() -> None:
    assert not (ROOT / "CNFutures/evolution.py").exists()
    assert not (ROOT / "tests/test_cn_futures_evolution.py").exists()
    assert not (ROOT / "shared/wrappers/job_cn_futures_evolution.sh").exists()
    for relative_path in ("shared/crontab.txt", "crontab.txt"):
        schedule = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "job_cn_futures_evolution.sh" not in schedule


def test_cn_futures_adapter_cannot_load_runtime_weight_or_variant_authority() -> None:
    source = (ROOT / "CNFutures/adapter.py").read_text(encoding="utf-8")
    assert 'generated_dir = review_root / MARKET / "generated_styles"' not in source
    assert 'weights_path = review_root / MARKET / "style_weights.json"' not in source


def test_cn_futures_opening_validation_cannot_read_retired_weight_authority() -> None:
    source = (ROOT / "CNFutures/opening_validator.py").read_text(encoding="utf-8")
    assert "DEFAULT_STYLE_WEIGHTS_PATH" not in source
    assert "_style_state_summary" not in source
    assert '"style_weights.json"' not in source


def test_ashare_generic_style_files_and_weight_budget_projection_are_retired() -> None:
    styles_dir = ROOT / "Ashare" / "styles"
    assert not list(styles_dir.glob("*.json"))
    source = (ROOT / "Ashare" / "research_evidence.py").read_text(encoding="utf-8")
    for marker in (
        "style_weights.json",
        "virtual_budget",
        "allocated_capital",
        "DEFAULT_STYLE_CAPITAL",
    ):
        assert marker not in source
    assert "SampleJournal" in source
    assert "single_shared_execution_account" in source


def test_generic_style_runner_and_evolution_fail_closed_for_single_account_markets() -> (
    None
):
    pipeline = (ROOT / "shared" / "execution" / "auto_pipeline.py").read_text(
        encoding="utf-8"
    )
    runner = (ROOT / "shared" / "markets" / "style_runner.py").read_text(
        encoding="utf-8"
    )
    evolution = (ROOT / "shared" / "markets" / "evolution_engine.py").read_text(
        encoding="utf-8"
    )
    assert 'ACTIVE_MARKETS = ("crypto", "us", "pm")' in pipeline
    for source in (pipeline, runner, evolution):
        assert 'frozenset({"ashare", "cn_futures"})' in source
        assert "retired" in source


def test_execution_router_cannot_auto_graduate_or_queue_real_signals() -> None:
    source = (ROOT / "shared" / "execution" / "execution_router.py").read_text(
        encoding="utf-8"
    )
    assert '"real": "disabled_real_transition"' in source
    assert "automatic_shadow_to_real_transition_disabled" in source
    assert "send_order(" not in source
    assert '"shadow_to_real"' not in source
