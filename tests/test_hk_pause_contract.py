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


def test_hk_sim_wrapper_requires_explicit_enable_flag() -> None:
    wrapper = _read("shared/wrappers/job_hk_sim.sh")

    assert "TRADINGAGENT_HK_SIM_ENABLED" in wrapper
    assert "SKIP hk_sim disabled" in wrapper
    assert "exit 0" in wrapper


def test_hk_run_sim_and_proxy_are_fail_closed_by_default() -> None:
    run_sim = _read("shared/wrappers/run_sim.py")

    assert 'market == "hk" and not _env_enabled("TRADINGAGENT_HK_SIM_ENABLED")' in run_sim
    assert '"status": "disabled"' in run_sim
    assert 'and _env_enabled("SIM_HK_PROXY_ENABLED")' in run_sim
