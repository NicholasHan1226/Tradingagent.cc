from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_hk_is_not_in_default_evolution_markets() -> None:
    evolution = _read("cron/evolution.sh")
    engine = _read("shared/markets/evolution_engine.py")
    guard = _read("shared/markets/evolution_guard.py")

    assert "TRADINGAGENT_EVOLUTION_MARKETS:-crypto,pm,us}" in evolution
    assert '"TRADINGAGENT_EVOLUTION_MARKETS", "crypto,pm,us"' in evolution
    assert 'MARKETS = ("crypto", "pm", "us")' in engine
    assert 'MARKETS = ("crypto", "pm", "us")' in guard


def test_hk_sim_wrapper_is_permanently_retired() -> None:
    wrapper = _read("shared/wrappers/job_hk_sim.sh")

    assert 'block_retired_legacy_runtime "job_hk_sim"' in wrapper
    assert "TRADINGAGENT_HK_SIM_ENABLED" not in wrapper


def test_hk_cannot_reactivate_retired_mixed_sim_runner() -> None:
    run_sim = _read("shared/wrappers/run_sim.py")

    assert 'retired_cli("shared.wrappers.run_sim")' in run_sim
