"""Explicit non-production execution clock authority.

The paper runtime must not infer quote freshness from a timestamp frozen in the
quote itself.  Every side-effect boundary asks this port for an explicitly
sealed time.  No live wall-clock authority is implemented here.  Concrete
implementations are a content-addressed run-manifest clock for production-style
simulation and a frozen deterministic fixture clock for offline tests.
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
_EFFECT_ORDER = ("sim_submit", "capital_commit")
_SEALED_MANIFEST_CONTRACT_ID = "tradingagent.sealed_runtime_clock_manifest.v1"
_SEALED_CONSTRUCTION_KEY = object()


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


class SealedRuntimeClock(TrustedExecutionClock):
    """Frozen simulation clock bound to one content-addressed run manifest.

    Every supported side-effect/order pair must be present in the immutable
    manifest.  Missing readings never fall back to the process wall clock.
    """

    clock_id = "tradingagent-sealed-runtime-clock"
    clock_version = "1"
    authority_tier = "sealed_run_manifest"
    production_eligible = False

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("sealed_runtime_clock_is_final")

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("sealed_runtime_clock_is_frozen")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        *,
        run_id: str,
        manifest_sha256: str,
        effect_times: Mapping[str, datetime],
        _verified_manifest: object = None,
    ) -> None:
        if _verified_manifest is not _SEALED_CONSTRUCTION_KEY:
            raise TrustedExecutionClockError(
                "sealed_runtime_clock_constructor_forbidden"
            )
        if not isinstance(run_id, str) or not run_id or run_id != run_id.strip():
            raise TrustedExecutionClockError("run_id_invalid")
        if (
            not isinstance(manifest_sha256, str)
            or len(manifest_sha256) != 64
            or any(character not in "0123456789abcdef" for character in manifest_sha256)
        ):
            raise TrustedExecutionClockError("manifest_sha256_invalid")
        if not isinstance(effect_times, Mapping) or not effect_times:
            raise TrustedExecutionClockError("effect_times_invalid")

        normalized: dict[str, datetime] = {}
        by_order: dict[str, dict[str, datetime]] = {}
        for key, instant in dict(effect_times).items():
            if not isinstance(key, str) or ":" not in key or key != key.strip():
                raise TrustedExecutionClockError("effect_time_key_invalid")
            effect, order_id = key.split(":", 1)
            if (
                effect not in _ALLOWED_EFFECTS
                or not order_id
                or order_id != order_id.strip()
            ):
                raise TrustedExecutionClockError("effect_time_key_invalid")
            aware_instant = _aware(instant, field_name="effect_time")
            normalized[key] = aware_instant
            by_order.setdefault(order_id, {})[effect] = aware_instant

        for order_effects in by_order.values():
            if set(order_effects) != _ALLOWED_EFFECTS:
                raise TrustedExecutionClockError("effect_times_incomplete")
            prior: datetime | None = None
            for effect in _EFFECT_ORDER:
                current = order_effects[effect]
                if prior is not None and current < prior:
                    raise TrustedExecutionClockError("effect_times_regressed")
                prior = current

        self.run_id = run_id
        self.manifest_sha256 = manifest_sha256
        self._effect_times = MappingProxyType(normalized)
        self.identity_sha256 = _sha256(
            {
                "contract_id": "tradingagent.trusted_execution_clock.v1",
                "clock_id": self.clock_id,
                "clock_version": self.clock_version,
                "authority_tier": self.authority_tier,
                "production_eligible": False,
                "run_id": run_id,
                "manifest_sha256": manifest_sha256,
                "effect_times": {
                    key: _utc_text(value) for key, value in sorted(normalized.items())
                },
            }
        )
        self._sealed = True

    @classmethod
    def from_run_manifest_bytes(
        cls,
        *,
        manifest_bytes: bytes,
        expected_manifest_sha256: str,
    ) -> "SealedRuntimeClock":
        """Verify and parse one immutable, content-addressed JSON manifest."""

        if type(manifest_bytes) is not bytes:
            raise TrustedExecutionClockError("manifest_bytes_invalid")
        if (
            not isinstance(expected_manifest_sha256, str)
            or len(expected_manifest_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected_manifest_sha256
            )
        ):
            raise TrustedExecutionClockError("manifest_sha256_invalid")
        actual_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if actual_sha256 != expected_manifest_sha256:
            raise TrustedExecutionClockError("manifest_sha256_mismatch")

        def reject_duplicate_keys(
            pairs: list[tuple[str, object]],
        ) -> dict[str, object]:
            decoded: dict[str, object] = {}
            for key, value in pairs:
                if key in decoded:
                    raise TrustedExecutionClockError("manifest_json_invalid")
                decoded[key] = value
            return decoded

        try:
            manifest = json.loads(
                manifest_bytes.decode("utf-8"),
                object_pairs_hook=reject_duplicate_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TrustedExecutionClockError("manifest_json_invalid") from exc
        expected_fields = {"contract_id", "run_id", "effect_times"}
        if not isinstance(manifest, dict) or set(manifest) != expected_fields:
            raise TrustedExecutionClockError("manifest_fields_invalid")
        if manifest.get("contract_id") != _SEALED_MANIFEST_CONTRACT_ID:
            raise TrustedExecutionClockError("manifest_contract_invalid")
        raw_effect_times = manifest.get("effect_times")
        if not isinstance(raw_effect_times, dict) or not raw_effect_times:
            raise TrustedExecutionClockError("effect_times_invalid")
        parsed_effect_times: dict[str, datetime] = {}
        for key, raw_instant in raw_effect_times.items():
            if not isinstance(key, str):
                raise TrustedExecutionClockError("effect_time_key_invalid")
            parsed_effect_times[key] = _parse(
                raw_instant,
                field_name="effect_time",
            )
        return cls(
            run_id=manifest.get("run_id"),
            manifest_sha256=actual_sha256,
            effect_times=parsed_effect_times,
            _verified_manifest=_SEALED_CONSTRUCTION_KEY,
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
        key = f"{effect}:{order_id}"
        try:
            return self._effect_times[key]
        except KeyError as exc:
            raise TrustedExecutionClockError("effect_time_missing") from exc


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
