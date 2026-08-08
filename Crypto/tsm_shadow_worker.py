"""CLI boundary for the read-only Crypto TSM(1d) shadow observer.

Modes:
  --csv: build shadow rows from a local 5m OHLCV export (offline validation).
  --runtime-manifest: read closed 5m bars from the TradingDatas Crypto V1
      loopback service (18083) using the frozen runtime manifest and the
      dedicated read-only token leaf, then append the shadow ledger.

In both modes the observer never places orders, touches capital, or writes to
any runtime path.  Output is an append-only, checksummed shadow ledger.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from Crypto.delayed_paper_runtime import (
    CryptoDelayedPaperRuntimeError,
    load_crypto_delayed_paper_runtime_manifest,
)
from Crypto.fixture_sim.contracts import _assert_simulation_only
from Crypto.tsm_shadow_observer import (
    TsmShadowObserverError,
    run_tsm_shadow_once,
)
from shared.data.sharedsignals_v1 import (
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)
from shared.data.tradingdatas_pagination import (
    PaginationContractError,
    collect_query_pages,
)
from shared.data.tradingdatas_transport import build_runtime_transport


SUPPORTED_SYMBOLS = ("BTCUSDT", "ETHUSDT")
HISTORY_START = "2026-01-01T00:00:00.000Z"
MAX_PAGES = 200
MAX_ROWS = 200_000
MAX_LIMIT = 10_000


def _csv_rows(path: Path) -> Mapping[str, list[dict[str, Any]]]:
    """Parse the local export format: ###SYMBOL### header, then CSV rows."""

    out: dict[str, list[dict[str, Any]]] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("###"):
            current = line.strip("#").strip().upper()
            out.setdefault(current, [])
            continue
        if current is None or not line or line.startswith("done"):
            continue
        parts = line.split(",")
        if len(parts) != 6:
            raise TsmShadowObserverError("tsm_shadow_csv_row_invalid")
        out[current].append(
            {
                "open_time_ms": int(parts[0]),
                "open": parts[1],
                "high": parts[2],
                "low": parts[3],
                "close": parts[4],
                "volume": parts[5],
            }
        )
    return {symbol: out.get(symbol, []) for symbol in SUPPORTED_SYMBOLS}


def _runtime_bars(
    *,
    manifest_path: Path,
    token_file: Path,
) -> Mapping[str, list[dict[str, Any]]]:
    """Read all closed 5m bars for BTC/ETH through the loopback V1 service."""

    try:
        manifest = load_crypto_delayed_paper_runtime_manifest(manifest_path)
        transport = build_runtime_transport(
            "http-json-v1",
            token_file=token_file,
            base_url=manifest.base_url,
        )
        client = SharedSignalsV1Client(
            SharedSignalsV1Config(
                base_url=manifest.base_url,
                expected_catalog_version=manifest.catalog_version,
                dataset_ids=manifest.dataset_ids,
                access_policy_id=manifest.access_policy_id,
                catalog_version_policy="evidence_only",
                timeout_seconds=60.0,
                max_limit=MAX_LIMIT,
                cache_ttl_seconds=0,
            ),
            transport=transport,
        )
        catalog = client.get_catalog()
        bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for binding in manifest.profile.symbols:
            dataset = binding.bars
            run = collect_query_pages(
                client=client,
                request=QueryRequest(
                    dataset_id=dataset.dataset_id,
                    schema_major=catalog.schema_major,
                    fields=(
                        "symbol",
                        "open_time",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                    ),
                    filters={
                        "symbol": {"eq": binding.symbol},
                        "open_time": {"between": [HISTORY_START, "9999-12-31T00:00:00.000Z"]},
                    },
                    order=("open_time",),
                    limit=MAX_LIMIT,
                ),
                identity_fields=("symbol", "open_time"),
                max_pages=MAX_PAGES,
                max_rows=MAX_ROWS,
            )
            normalized: list[dict[str, Any]] = []
            for row in run.rows:
                open_time = row.get("open_time")
                if not isinstance(open_time, str):
                    raise TsmShadowObserverError("tsm_shadow_open_time_invalid")
                from datetime import datetime, timezone

                parsed = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise TsmShadowObserverError("tsm_shadow_open_time_invalid")
                normalized.append(
                    {
                        "open_time_ms": int(
                            parsed.astimezone(timezone.utc).timestamp() * 1000
                        ),
                        "open": row["open"],
                        "high": row["high"],
                        "low": row["low"],
                        "close": row["close"],
                        "volume": row["volume"],
                    }
                )
            bars_by_symbol[binding.symbol] = normalized
        return {symbol: bars_by_symbol.get(symbol, []) for symbol in SUPPORTED_SYMBOLS}
    except (
        CryptoDelayedPaperRuntimeError,
        PaginationContractError,
        TsmShadowObserverError,
    ) as exc:
        raise TsmShadowObserverError(f"tsm_shadow_runtime_failed:{exc}") from exc


def run_tsm_shadow_worker_once(
    *,
    csv_path: Path | None = None,
    manifest_path: Path | None = None,
    token_file: Path | None = None,
    ledger_root: Path,
) -> dict[str, Any]:
    """Run one read-only TSM shadow pass and return the receipt."""

    _assert_simulation_only()
    if csv_path is not None:
        bars = _csv_rows(csv_path)
    elif manifest_path is not None and token_file is not None:
        bars = _runtime_bars(manifest_path=manifest_path, token_file=token_file)
    else:
        raise TsmShadowObserverError("tsm_shadow_input_mode_required")
    if not any(bars.values()):
        raise TsmShadowObserverError("tsm_shadow_no_input_bars")
    return run_tsm_shadow_once(five_minute_bars=bars, ledger_root=ledger_root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Crypto TSM(1d) shadow observer"
    )
    parser.add_argument("--csv", type=Path, help="local 5m OHLCV export")
    parser.add_argument(
        "--runtime-manifest",
        type=Path,
        help="frozen crypto runtime manifest (18083 loopback)",
    )
    parser.add_argument("--token-file", type=Path, help="read-only TD token leaf")
    parser.add_argument("--ledger-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = run_tsm_shadow_worker_once(
            csv_path=args.csv,
            manifest_path=args.runtime_manifest,
            token_file=args.token_file,
            ledger_root=args.ledger_root,
        )
    except (TsmShadowObserverError, OSError, ValueError, TypeError) as exc:
        print(f"tsm shadow observer failed closed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "run_tsm_shadow_worker_once",
    "main",
]
