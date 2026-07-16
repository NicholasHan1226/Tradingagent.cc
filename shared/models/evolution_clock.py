"""Explicit trusted-time port for negative-only model evolution.

The controller must not infer freshness from a timestamp embedded in the
metrics artifact.  This module deliberately provides no production clock; its
only concrete implementation is a frozen offline fixture.
"""

from __future__ import annotations

import hashlib
import json
import re
from abc import ABC, abstractmethod
from datetime import datetime


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class TrustedEvolutionClockError(ValueError):
    """Raised when the evolution clock cannot prove a safe reading."""


def _aware(value: object, *, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise TrustedEvolutionClockError(f"{field_name}_timezone_required")
    return value


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _require_sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise TrustedEvolutionClockError(f"{field_name}_invalid")
    return value


class TrustedEvolutionClock(ABC):
    """Required evolution-clock authority; no implicit wall-clock fallback."""

    identity_sha256: str
    production_eligible: bool

    @abstractmethod
    def now(
        self,
        *,
        model_manifest_sha256: str,
        evidence_sha256: str,
    ) -> datetime:
        """Return a reading bound to one model/evidence evaluation."""


class NonProductionFixtureEvolutionClock(TrustedEvolutionClock):
    """Frozen deterministic clock for local tests and paper fixtures only."""

    clock_id = "tradingagent-non-production-fixture-evolution-clock"
    clock_version = "1"
    authority_tier = "non_production_fixture"
    production_eligible = False

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("fixture_evolution_clock_is_final")

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"_default_instant", "identity_sha256"}:
            if hasattr(self, name):
                raise AttributeError("fixture_evolution_clock_is_frozen")
            object.__setattr__(self, name, value)
            return
        raise AttributeError("fixture_evolution_clock_is_frozen")

    def __init__(self, *, default_instant: datetime) -> None:
        instant = _aware(default_instant, field_name="default_instant")
        self._default_instant = instant
        self.identity_sha256 = _sha256(
            {
                "authority_tier": self.authority_tier,
                "clock_id": self.clock_id,
                "clock_version": self.clock_version,
                "contract_id": "tradingagent.trusted_evolution_clock.v1",
                "default_instant": instant.isoformat(),
                "production_eligible": False,
            }
        )

    def now(
        self,
        *,
        model_manifest_sha256: str,
        evidence_sha256: str,
    ) -> datetime:
        _require_sha256(
            model_manifest_sha256,
            field_name="model_manifest_sha256",
        )
        _require_sha256(evidence_sha256, field_name="evidence_sha256")
        return self._default_instant


__all__ = [
    "NonProductionFixtureEvolutionClock",
    "TrustedEvolutionClock",
    "TrustedEvolutionClockError",
]
