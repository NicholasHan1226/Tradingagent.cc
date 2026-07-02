#!/usr/bin/env python3
"""PM Phase D workflow — unified probability-market shadow cycle orchestrator.

Orchestrates the full PM shadow cycle:
  1. Load PMConfig and validate safety (no real execution, no live CLOB).
  2. Initialize PMMarketData, PMSimulator, PMShadowRunner.
  3. Collect universe + signals.
  4. Score + filter candidates.
  5. Simulate orders (local mock, probability settlement).
  6. Write shadow records.
  7. Return structured summary.

All operations are shadow/simulated only. Probability values clamped [0, 1].
"""

from __future__ import annotations

import traceback
from datetime import datetime, timezone
from typing import Any

from PM.common import PMConfig, load_pm_config
from PM.market_data import PMMarketData
from PM.shadow_runner import PMShadowRunner
from PM.simulator import PMSimulator


class PMWorkflow:
    """Unified PM shadow-cycle orchestrator.

    Usage:
        wf = PMWorkflow()
        result = wf.run_pm_shadow_cycle(as_of="2026-07-02")
    """

    def __init__(self, config: PMConfig | None = None) -> None:
        self.config = config or load_pm_config()
        self.config.validate()

        self._market_data: PMMarketData | None = None
        self._simulator: PMSimulator | None = None
        self._shadow_runner: PMShadowRunner | None = None

    @property
    def market_data(self) -> PMMarketData:
        if self._market_data is None:
            self._market_data = PMMarketData(self.config.to_market_tool_config())
        return self._market_data

    @property
    def simulator(self) -> PMSimulator:
        if self._simulator is None:
            self._simulator = PMSimulator(
                config=self.config.to_market_tool_config(),
                market_data=self.market_data,
            )
        return self._simulator

    @property
    def shadow_runner(self) -> PMShadowRunner:
        if self._shadow_runner is None:
            self._shadow_runner = PMShadowRunner(
                config=self.config.to_market_tool_config(),
                market_data=self.market_data,
                simulator=self.simulator,
            )
        return self._shadow_runner

    # --- Public API -----------------------------------------------------------

    def run_pm_shadow_cycle(
        self,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Run a complete PM shadow cycle for the given date.

        Parameters
        ----------
        as_of : str | None
            Date string in YYYY-MM-DD format. Defaults to today UTC.

        Returns
        -------
        dict[str, Any]
            Structured cycle summary including results from all phases.
        """
        date = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        started_at = datetime.now(timezone.utc).isoformat()

        result: dict[str, Any] = {
            "workflow": "run_pm_shadow_cycle",
            "market": "pm",
            "date": date,
            "started_at": started_at,
            "mode": "shadow",
            "live_clob": False,
        }

        # Phase 1: Validate environment
        try:
            self._validate_phase()
            result["phase_validate"] = {"status": "ok"}
        except Exception as exc:
            result["phase_validate"] = {"status": "error", "error": str(exc)}
            result["status"] = "failed_validation"
            result["finished_at"] = datetime.now(timezone.utc).isoformat()
            return result

        # Phase 2: Health check
        try:
            health = self.market_data.health_check()
            result["phase_health"] = health
        except Exception as exc:
            result["phase_health"] = {"status": "error", "error": str(exc)}

        # Phase 3: Run shadow cycle
        try:
            shadow_result = self.shadow_runner.run_shadow(date)
            result["phase_shadow"] = shadow_result
            result["status"] = shadow_result.get("status", "completed")
        except Exception as exc:
            result["phase_shadow"] = {
                "status": "error",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
            result["status"] = "failed_shadow"

        # Phase 4: Compile summary
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["summary"] = self._compile_summary(result)

        return result

    def health_check(self) -> dict[str, Any]:
        """Run a standalone health check for the PM tool chain."""
        return self.market_data.health_check()

    # --- Internal methods -----------------------------------------------------

    def _validate_phase(self) -> None:
        """Validate that all safety invariants hold.

        Raises if real execution, live CLOB, or live broker is enabled.
        """
        # Ensure config is safe
        self.config.validate()

        # Double-check safety flags
        safety = self.config.safety
        if safety.real_money_enabled:
            raise RuntimeError(
                "PMWorkflow: real_money_enabled is True — aborting"
            )
        if safety.direct_execution_enabled:
            raise RuntimeError(
                "PMWorkflow: direct_execution_enabled is True — aborting"
            )
        if safety.live_broker_enabled:
            raise RuntimeError(
                "PMWorkflow: live_broker_enabled is True — no live broker access"
            )

        # Verify market is pm
        if self.config.market != "pm":
            raise ValueError(
                f"PMWorkflow market must be 'pm', got {self.config.market!r}"
            )

    @staticmethod
    def _compile_summary(result: dict[str, Any]) -> dict[str, Any]:
        """Compile a human-readable summary from cycle results."""
        shadow = result.get("phase_shadow", {})
        positions = shadow.get("positions", [])

        total_positions = len(positions)
        filled = sum(1 for p in positions if p.get("status") == "filled")
        errors = sum(1 for p in positions if p.get("status") == "error")

        scores = [p.get("score", 0.0) for p in positions
                  if p.get("status") == "filled" and "score" in p]
        avg_score = sum(scores) / len(scores) if scores else 0.0

        return {
            "total_positions": total_positions,
            "filled": filled,
            "errors": errors,
            "average_score": round(avg_score, 4),
            "universe_size": shadow.get("universe_size", 0),
            "signals_count": shadow.get("signals_count", 0),
            "candidates_count": shadow.get("candidates_count", 0),
            "mode": "shadow",
            "live_clob": False,
        }


# -- Module-level convenience ------------------------------------------------


def run_pm_shadow_cycle(
    as_of: str | None = None,
    config: PMConfig | None = None,
) -> dict[str, Any]:
    """Convenience function to run a single PM shadow cycle.

    Equivalent to:
        PMWorkflow(config).run_pm_shadow_cycle(as_of=as_of)
    """
    wf = PMWorkflow(config)
    return wf.run_pm_shadow_cycle(as_of=as_of)


__all__ = ["PMWorkflow", "run_pm_shadow_cycle"]
