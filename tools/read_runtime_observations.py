#!/usr/bin/env python3
"""Read existing independent simulation stores for display, never account authority.

No network, writer, repair, bootstrap, or scheduler is invoked.  The frontend
starts this bounded child in the background and isolates failure from its other
domains.  File identities are diagnostic content anchors, not signatures.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Ashare.minute_loop import MinuteFixtureClosedLoop  # noqa: E402
from Crypto.delayed_paper_round_trip_health import (  # noqa: E402
    ROUND_TRIP_HEALTH_CONTRACT,
    run_crypto_delayed_paper_round_trip_health_once,
)

CONTRACT = "tradingagent.runtime_observations.v1"
ASHARE_ROOT = Path("/var/lib/tradingagent/ashare-minute-paper-scale500")
CRYPTO_MANIFEST = Path(
    "/etc/tradingagent/crypto-delayed-paper-round-trip-epochs/"
    "crypto-delayed-paper-round-trip-epoch-g5-20260801.json"
)
MAX_BUNDLE_BYTES = 64 * 1024 * 1024


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_required")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("count_invalid")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ValueError("digest_invalid")
    return value


def _decimal(value: object) -> str:
    if not isinstance(value, str) or not Decimal(value).is_finite():
        raise ValueError("amount_invalid")
    return value


def _entry(identity: str, market: str, status: str, reason: str) -> dict[str, Any]:
    return {
        "id": identity,
        "market": market,
        "sourceClass": "delayed_research",
        "status": status,
        "observedAt": None,
        "sourceSha256": None,
        "canonicalAccountConnected": False,
        "reason": reason,
    }


def _read_bundle(path: Path) -> tuple[dict[str, Any], str]:
    # Explicit existing paths only: never resolve an alias into another store.
    for member in (path, *path.parents):
        if member.is_symlink():
            raise ValueError("source_alias")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("source_not_regular")
        encoded = stream.read(MAX_BUNDLE_BYTES + 1)
        after = os.fstat(stream.fileno())
        if len(encoded) > MAX_BUNDLE_BYTES or (
            before.st_size, before.st_mtime_ns, before.st_ctime_ns
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("source_changed_or_oversized")
    value = json.loads(encoded)
    if not isinstance(value, dict):
        raise ValueError("source_shape")
    return value, hashlib.sha256(encoded).hexdigest()


def read_ashare(root: Path, now: datetime) -> dict[str, Any]:
    entry = _entry("ashare-minute-scale", "A-share", "unavailable", "source_missing")
    try:
        # At most eight explicit daily paths. A damaged latest bundle is not
        # replaced by an older success. A new pre-open directory without a
        # bundle may still show the last run, labelled with its original date.
        today = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
        path = None
        for offset in range(8):
            candidate = root / (today - timedelta(days=offset)).strftime("%Y%m%d") / "state-bundle.json"
            if candidate.exists() or candidate.is_symlink():
                path = candidate
                break
        if path is None:
            return entry
        raw, digest = _read_bundle(path)
        if (
            raw.get("schema") != "tradingagent.ashare.delayed_minute_paper_bundle.v1"
            or raw.get("authority_tier") != "non_production_fixture"
            or raw.get("real_trading_enabled") is not False
        ):
            raise ValueError("source_contract")
        # Reuse the producer's complete state/hash reconstruction. Do not trust
        # the top-level last_receipt's unsealed counters or timestamps.
        loop = MinuteFixtureClosedLoop.restore(raw["loop_state"])
        all_bars = loop.feature_engine.current_bars
        symbols = {item.symbol for item in loop.universe.instruments.values() if not item.context_only}
        if not all_bars or not set(all_bars).issubset(symbols) or not loop.accepted_bar_ends:
            raise ValueError("bar_scope")
        # The feature engine retains prior bars for missing stocks. Count only
        # the last accepted slot; one stale stock must not invalidate or inflate
        # independently valid current coverage.
        bar_end = datetime.fromisoformat(loop.accepted_bar_ends[-1])
        if bar_end.tzinfo is None:
            bar_end = bar_end.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        if any(bar.bar_end > bar_end for bar in all_bars.values()):
            raise ValueError("bar_window")
        bars = {symbol: bar for symbol, bar in all_bars.items() if bar.bar_end == bar_end}
        if not bars:
            raise ValueError("bar_window")
        observed_at = max(bar.observed_at for bar in bars.values())
        if bar_end > now or observed_at > now or bar_end.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d") != path.parent.name:
            raise ValueError("source_time")
        lag = now - observed_at
        entry.update(
            status="ready" if lag <= timedelta(minutes=30) else "dated",
            observedAt=_iso(observed_at),
            sourceSha256=digest,
            coverage={"universe": len(symbols), "accepted": len(bars), "missing": len(symbols) - len(bars)},
            reason="independent_research_account_not_connected",
        )
        # Four counterfactual books are NOT one spendable CNY account. No money
        # or combined fills from those books enters this display projection.
        return entry
    except Exception:
        return _entry("ashare-minute-scale", "A-share", "invalid", "source_validation_failed")


def read_crypto(manifest: Path, now: datetime) -> dict[str, Any]:
    entry = _entry("crypto-g5", "Crypto", "unavailable", "source_missing")
    try:
        if not manifest.exists():
            return entry
        health = run_crypto_delayed_paper_round_trip_health_once(epoch_manifest=manifest, now=now)
        if (
            health.get("contract") != ROUND_TRIP_HEALTH_CONTRACT
            or health.get("read_only") is not True
            or health.get("authority") != "none"
            or any(health.get(key) is not False for key in (
                "real_trading_enabled", "execution_authority", "production_eligible",
                "network_used", "live_broker_used", "promotion_authorized",
                "automatic_risk_expansion_enabled",
            ))
        ):
            raise ValueError("health_contract")
        if health.get("status") == "pending":
            return _entry("crypto-g5", "Crypto", "pending", "writer_in_progress")
        if health.get("status") not in {"healthy", "stale"}:
            raise ValueError("health_status")
        core, capital = health["core"], health["capital"]
        observed_at = datetime.fromisoformat(core["latest_market_slot"].replace("Z", "+00:00"))
        if observed_at > now or capital.get("balanced") is not True or capital.get("currency") != "USDT":
            raise ValueError("health_capital")
        entry.update(
            status="ready" if health["status"] == "healthy" else "dated",
            observedAt=_iso(observed_at),
            sourceSha256=_sha(capital["head_checksum"]),
            simulation={
                "currency": "USDT",
                "cash": _decimal(capital["cash"]),
                "equity": _decimal(capital["equity"]),
                "fees": _decimal(capital["fees"]),
                "realizedPnl": _decimal(capital["realized_pnl"]),
                "positions": _count(capital["position_count"]),
                "orders": _count(capital["order_count"]),
            },
            counts={"completed": _count(core["completion_count"]), "rejected": _count(health["failure_count"])},
            reason="dated_simulated_ledger_not_live_account",
        )
        return entry
    except Exception:
        return _entry("crypto-g5", "Crypto", "invalid", "source_validation_failed")


def build_snapshot(*, now: datetime | None = None, ashare_root: Path = ASHARE_ROOT, crypto_manifest: Path = CRYPTO_MANIFEST) -> dict[str, Any]:
    checked = now or datetime.now(timezone.utc)
    generated_at = _iso(checked)
    if os.environ.get("REAL_TRADING_ENABLED", "").lower() != "false":
        raise ValueError("simulation_only_required")
    return {
        "contract": CONTRACT,
        "readOnly": True,
        "realTradingEnabled": False,
        "generatedAt": generated_at,
        "entries": [read_ashare(ashare_root, checked), read_crypto(crypto_manifest, checked)],
    }


def main() -> int:
    # No arbitrary request-controlled roots or command arguments.
    if len(sys.argv) != 1:
        return 2
    try:
        result = build_snapshot()
    except Exception:
        print("runtime observation read unavailable", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
