from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

import shared.runtime.trusted_clock as trusted_clock_module
from shared.runtime.trusted_clock import (
    NonProductionFixtureExecutionClock,
    TrustedExecutionClockError,
)


def _manifest_bytes(
    *,
    effect_times: dict[str, str] | None = None,
    **overrides: object,
) -> bytes:
    manifest: dict[str, object] = {
        "contract_id": "tradingagent.sealed_runtime_clock_manifest.v1",
        "run_id": "ashare-paper-run-20260722",
        "effect_times": effect_times
        or {
            "sim_submit:ORDER-1": "2026-07-22T09:31:00+08:00",
            "capital_commit:ORDER-1": "2026-07-22T01:31:20Z",
        },
    }
    manifest.update(overrides)
    return json.dumps(
        manifest,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sealed_clock_type() -> type:
    clock_type = getattr(trusted_clock_module, "SealedRuntimeClock", None)
    assert clock_type is not None, "SealedRuntimeClock must be implemented"
    return clock_type


def _clock(raw: bytes | None = None) -> object:
    content = raw or _manifest_bytes()
    return _sealed_clock_type().from_run_manifest_bytes(
        manifest_bytes=content,
        expected_manifest_sha256=_digest(content),
    )


def test_sealed_clock_reads_explicit_aware_times_and_replays_exactly() -> None:
    raw = _manifest_bytes()
    clock = _clock(raw)

    submit = clock.now(effect="sim_submit", order_id="ORDER-1")
    submit_replay = clock.now(effect="sim_submit", order_id="ORDER-1")
    commit = clock.now(effect="capital_commit", order_id="ORDER-1")

    assert submit.isoformat() == "2026-07-22T09:31:00+08:00"
    assert submit_replay == submit
    assert commit.isoformat() == "2026-07-22T01:31:20+00:00"
    assert commit.astimezone(timezone.utc) >= submit.astimezone(timezone.utc)
    assert clock.manifest_sha256 == _digest(raw)
    assert len(clock.identity_sha256) == 64
    assert clock.production_eligible is False

    with pytest.raises(AttributeError, match="sealed_runtime_clock_is_frozen"):
        clock.run_id = "different-run"  # type: ignore[misc]


def test_sealed_clock_requires_complete_exact_effect_order_keys() -> None:
    missing_commit = _manifest_bytes(
        effect_times={
            "sim_submit:ORDER-1": "2026-07-22T09:31:00+08:00",
        }
    )
    with pytest.raises(TrustedExecutionClockError, match="effect_times_incomplete"):
        _clock(missing_commit)

    clock = _clock()
    with pytest.raises(TrustedExecutionClockError, match="effect_time_missing"):
        clock.now(effect="sim_submit", order_id="ORDER-2")
    with pytest.raises(TrustedExecutionClockError, match="effect_time_missing"):
        clock.now(effect="sim_submit", order_id="order-1")


def test_sealed_clock_rejects_unknown_or_malformed_keys() -> None:
    unknown_effect = _manifest_bytes(
        effect_times={
            "sim_submit:ORDER-1": "2026-07-22T09:31:00+08:00",
            "capital_commit:ORDER-1": "2026-07-22T09:31:20+08:00",
            "broker_submit:ORDER-1": "2026-07-22T09:31:10+08:00",
        }
    )
    with pytest.raises(TrustedExecutionClockError, match="effect_time_key_invalid"):
        _clock(unknown_effect)

    malformed = _manifest_bytes(
        effect_times={
            "sim_submit: ORDER-1": "2026-07-22T09:31:00+08:00",
            "capital_commit:ORDER-1": "2026-07-22T09:31:20+08:00",
        }
    )
    with pytest.raises(TrustedExecutionClockError, match="effect_time_key_invalid"):
        _clock(malformed)

    clock = _clock()
    with pytest.raises(TrustedExecutionClockError, match="effect_invalid"):
        clock.now(effect="broker_submit", order_id="ORDER-1")
    with pytest.raises(TrustedExecutionClockError, match="order_id_invalid"):
        clock.now(effect="sim_submit", order_id=" ORDER-1")


@pytest.mark.parametrize(
    "invalid_time",
    [
        "2026-07-22T09:31:00",
        "",
        " 2026-07-22T09:31:00+08:00",
        "not-a-time",
    ],
)
def test_sealed_clock_rejects_naive_or_invalid_timestamps(
    invalid_time: str,
) -> None:
    raw = _manifest_bytes(
        effect_times={
            "sim_submit:ORDER-1": invalid_time,
            "capital_commit:ORDER-1": "2026-07-22T09:31:20+08:00",
        }
    )
    with pytest.raises(TrustedExecutionClockError, match="effect_time_"):
        _clock(raw)


def test_sealed_clock_rejects_cross_effect_time_regression() -> None:
    raw = _manifest_bytes(
        effect_times={
            "sim_submit:ORDER-1": "2026-07-22T09:31:20+08:00",
            "capital_commit:ORDER-1": "2026-07-22T01:31:19Z",
        }
    )
    with pytest.raises(TrustedExecutionClockError, match="effect_times_regressed"):
        _clock(raw)


def test_sealed_clock_rejects_tampered_or_noncanonical_manifest_contract() -> None:
    raw = _manifest_bytes()
    tampered = raw.replace(b"ORDER-1", b"ORDER-2")
    with pytest.raises(TrustedExecutionClockError, match="manifest_sha256_mismatch"):
        _sealed_clock_type().from_run_manifest_bytes(
            manifest_bytes=tampered,
            expected_manifest_sha256=_digest(raw),
        )

    with pytest.raises(TrustedExecutionClockError, match="manifest_fields_invalid"):
        _clock(_manifest_bytes(unexpected="forbidden"))
    with pytest.raises(TrustedExecutionClockError, match="manifest_sha256_invalid"):
        _sealed_clock_type().from_run_manifest_bytes(
            manifest_bytes=raw,
            expected_manifest_sha256="A" * 64,
        )
    with pytest.raises(TrustedExecutionClockError, match="manifest_bytes_invalid"):
        _sealed_clock_type().from_run_manifest_bytes(
            manifest_bytes=bytearray(raw),  # type: ignore[arg-type]
            expected_manifest_sha256=_digest(raw),
        )


def test_sealed_clock_cannot_bypass_manifest_verification_via_constructor() -> None:
    with pytest.raises(
        TrustedExecutionClockError,
        match="sealed_runtime_clock_constructor_forbidden",
    ):
        _sealed_clock_type()(
            run_id="ashare-paper-run-20260722",
            manifest_sha256="a" * 64,
            effect_times={
                "sim_submit:ORDER-1": datetime.fromisoformat(
                    "2026-07-22T09:31:00+08:00"
                ),
                "capital_commit:ORDER-1": datetime.fromisoformat(
                    "2026-07-22T09:31:20+08:00"
                ),
            },
        )


def test_sealed_clock_rejects_duplicate_json_keys() -> None:
    raw = (
        b'{"contract_id":"tradingagent.sealed_runtime_clock_manifest.v1",'
        b'"run_id":"ashare-paper-run-20260722",'
        b'"run_id":"forged-run",'
        b'"effect_times":{'
        b'"sim_submit:ORDER-1":"2026-07-22T09:31:00+08:00",'
        b'"capital_commit:ORDER-1":"2026-07-22T09:31:20+08:00"}}'
    )
    with pytest.raises(TrustedExecutionClockError, match="manifest_json_invalid"):
        _clock(raw)


def test_missing_effect_never_falls_back_to_system_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _clock()

    class SystemClockMustNotBeRead:
        @classmethod
        def now(cls, *args: object, **kwargs: object) -> datetime:
            raise AssertionError("system_clock_fallback_forbidden")

    monkeypatch.setattr(trusted_clock_module, "datetime", SystemClockMustNotBeRead)
    with pytest.raises(TrustedExecutionClockError, match="effect_time_missing"):
        clock.now(effect="capital_commit", order_id="ORDER-404")


def test_existing_fixture_clock_behavior_remains_unchanged() -> None:
    default = datetime.fromisoformat("2026-07-22T09:31:00+08:00")
    override = datetime.fromisoformat("2026-07-22T09:31:20+08:00")
    fixture = NonProductionFixtureExecutionClock(
        default_instant=default,
        effect_overrides={"capital_commit:ORDER-1": override},
    )

    assert fixture.now(effect="sim_submit", order_id="UNLISTED") == default
    assert fixture.now(effect="capital_commit", order_id="ORDER-1") == override
    assert fixture.production_eligible is False
