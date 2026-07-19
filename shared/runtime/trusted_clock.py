"""Explicit non-production execution clock authority.

The paper runtime must not infer quote freshness from a timestamp frozen in the
quote itself.  Every side-effect boundary asks this port for a fresh time.  No
production clock is implemented here; the only concrete implementation is a
frozen, deterministic fixture clock for offline tests and paper fixtures.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping


class TrustedExecutionClockError(ValueError):
    """Raised when an execution clock cannot prove a valid reading."""


_ALLOWED_EFFECTS = frozenset({"sim_submit", "capital_commit"})


def _aware(value: datetime, *, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise TrustedExecutionClockError(f"{field_name}_timezone_required")
    return value


def _parse(value: str, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TrustedExecutionClockError(f"{field_name}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TrustedExecutionClockError(f"{field_name}_invalid") from exc
    return _aware(parsed, field_name=field_name)


def _utc_text(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


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


class TrustedExecutionClock(ABC):
    """Required execution-clock port; deliberately has no default behavior."""

    identity_sha256: str
    production_eligible: bool

    @abstractmethod
    def now(self, *, effect: str, order_id: str) -> datetime:
        """Return the current trusted time for one exact side-effect boundary."""


class NonProductionFixtureExecutionClock(TrustedExecutionClock):
    """Frozen deterministic clock for offline paper fixtures only."""

    clock_id = "tradingagent-non-production-fixture-execution-clock"
    clock_version = "1"
    authority_tier = "non_production_fixture"
    production_eligible = False

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("fixture_execution_clock_is_final")

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"_default_instant", "_effect_overrides", "identity_sha256"}:
            if hasattr(self, name):
                raise AttributeError("fixture_execution_clock_is_frozen")
            object.__setattr__(self, name, value)
            return
        raise AttributeError("fixture_execution_clock_is_frozen")

    def __init__(
        self,
        *,
        default_instant: datetime,
        effect_overrides: Mapping[str, datetime],
    ) -> None:
        default = _aware(default_instant, field_name="default_instant")
        if not isinstance(effect_overrides, Mapping):
            raise TrustedExecutionClockError("effect_overrides_invalid")
        normalized: dict[str, datetime] = {}
        for key, instant in dict(effect_overrides).items():
            if not isinstance(key, str) or ":" not in key or key != key.strip():
                raise TrustedExecutionClockError("effect_override_key_invalid")
            effect, order_id = key.split(":", 1)
            if effect not in _ALLOWED_EFFECTS or not order_id.strip():
                raise TrustedExecutionClockError("effect_override_key_invalid")
            normalized[key] = _aware(
                instant,
                field_name="effect_override_instant",
            )
        self._default_instant = default
        self._effect_overrides = MappingProxyType(normalized)
        self.identity_sha256 = _sha256(
            {
                "contract_id": "tradingagent.trusted_execution_clock.v1",
                "clock_id": self.clock_id,
                "clock_version": self.clock_version,
                "authority_tier": self.authority_tier,
                "production_eligible": False,
                "default_instant": _utc_text(default),
                "effect_overrides": {
                    key: _utc_text(value) for key, value in sorted(normalized.items())
                },
            }
        )

    @classmethod
    def from_isoformat(
        cls,
        *,
        default_instant: str,
        effect_overrides: Mapping[str, str],
    ) -> "NonProductionFixtureExecutionClock":
        if not isinstance(effect_overrides, Mapping):
            raise TrustedExecutionClockError("effect_overrides_invalid")
        return cls(
            default_instant=_parse(
                default_instant,
                field_name="default_instant",
            ),
            effect_overrides={
                key: _parse(value, field_name="effect_override_instant")
                for key, value in dict(effect_overrides).items()
            },
        )

    def now(self, *, effect: str, order_id: str) -> datetime:
        if effect not in _ALLOWED_EFFECTS:
            raise TrustedExecutionClockError("effect_invalid")
        if (
            not isinstance(order_id, str)
            or not order_id
            or order_id != order_id.strip()
        ):
            raise TrustedExecutionClockError("order_id_invalid")
        return self._effect_overrides.get(
            f"{effect}:{order_id}",
            self._default_instant,
        )
