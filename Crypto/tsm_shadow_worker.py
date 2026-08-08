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
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from Crypto.delayed_paper_runtime import (
    CryptoDelayedPaperRuntimeError,
    load_crypto_delayed_paper_runtime_manifest,
)
from Crypto.five_minute_data import (
    CryptoDatasetQueryProfile,
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
LOOKBACK_DAYS = 100
MAX_PAGES = 300
MAX_ROWS = 200_000
MAX_LIMIT = 500


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
    history_start: str,
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
        history_end = (
            datetime.now(tz=timezone.utc).replace(microsecond=0)
            + timedelta(days=1)
        ).isoformat()
        bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for binding in manifest.profile.symbols:
            dataset = binding.bars
            profile = CryptoDatasetQueryProfile.from_catalog(
                catalog,
                expected_catalog_version=manifest.catalog_version,
                dataset_id=dataset.dataset_id,
                expected_schema_major=dataset.schema_major,
                selected_fields=dataset.selected_fields,
                query_order=dataset.query_order,
                identity_fields=dataset.identity_fields,
                filter_bindings=dataset.filter_bindings,
                page_limit=MAX_LIMIT,
                max_pages=MAX_PAGES,
                max_rows=MAX_ROWS,
            )
            filter_fields: dict[str, dict[str, Any]] = {}
            for fb in profile.filter_bindings:
                if fb.role == "symbol":
                    filter_fields[fb.field] = {"eq": binding.symbol}
                elif fb.role == "open_time_window":
                    filter_fields[fb.field] = {
                        "between": [history_start, history_end]
                    }
            run = collect_query_pages(
                client=client,
                request=QueryRequest(
                    dataset_id=profile.dataset_id,
                    schema_major=profile.schema_major,
                    fields=profile.selected_fields,
                    filters=filter_fields,
                    order=profile.query_order,
                    limit=profile.page_limit,
                ),
                identity_fields=profile.identity_fields,
                max_pages=profile.max_pages,
                max_rows=profile.max_rows,
            )
            normalized: list[dict[str, Any]] = []
            for row in run.envelope.data:
                open_time = row.get("open_time")
                if not isinstance(open_time, str):
                    raise TsmShadowObserverError("tsm_shadow_open_time_invalid")
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
    lookback_days: int | None = None,
) -> dict[str, Any]:
    """Run one read-only TSM shadow pass and return the receipt."""

    _assert_simulation_only()
    if csv_path is not None:
        bars = _csv_rows(csv_path)
    elif manifest_path is not None and token_file is not None:
        days = LOOKBACK_DAYS if lookback_days is None else lookback_days
        history_start = (
            datetime.now(tz=timezone.utc) - timedelta(days=days)
        ).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        bars = _runtime_bars(
            manifest_path=manifest_path,
            token_file=token_file,
            history_start=history_start,
        )
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
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="history window for runtime mode (default 100d)",
    )
    args = parser.parse_args(argv)
    try:
        receipt = run_tsm_shadow_worker_once(
            csv_path=args.csv,
            manifest_path=args.runtime_manifest,
            token_file=args.token_file,
            ledger_root=args.ledger_root,
            lookback_days=args.lookback_days,
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
