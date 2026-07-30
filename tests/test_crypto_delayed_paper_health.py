from __future__ import annotations

from pathlib import Path

from Crypto.delayed_paper_exit_shadow import (
    project_crypto_delayed_paper_exit_shadow,
)
from Crypto.delayed_paper_health import build_crypto_delayed_paper_health
from Crypto.delayed_paper_runner import run_crypto_delayed_paper_once
from Crypto.five_minute_data import TradingDatasCryptoFiveMinuteDataPort
from tests.test_crypto_5m_support import (
    FixtureTradingDatasTransport,
    client,
    profile,
    window_request,
)


def _completed(root: Path) -> None:
    transport = FixtureTradingDatasTransport()
    tradingdatas_client = client(transport)
    result = run_crypto_delayed_paper_once(
        port=TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client),
        profile=profile(tradingdatas_client),
        request=window_request(),
        output_root=root,
    )
    assert result["status"] == "completed"


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_health_snapshot_is_read_only_and_separates_projection_state(
    tmp_path: Path,
) -> None:
    _completed(tmp_path)
    before = _tree_bytes(tmp_path)

    first = build_crypto_delayed_paper_health(output_root=tmp_path)

    assert _tree_bytes(tmp_path) == before
    assert first["status"] == "healthy"
    assert first["core"]["observation_count"] == 1
    assert first["core"]["completion_count"] == 1
    assert first["core"]["pending"] is False
    assert first["capital"]["balanced"] is True
    assert first["capital"]["currency"] == "USDT"
    assert first["capital"]["position_count"] == 2
    assert first["exit_shadow"]["state"] == "absent"
    assert first["learning"]["state"] == "absent"
    assert first["execution_authority"] is False
    assert first["real_trading_enabled"] is False

    projection = project_crypto_delayed_paper_exit_shadow(output_root=tmp_path)
    after_projection = _tree_bytes(tmp_path)
    second = build_crypto_delayed_paper_health(output_root=tmp_path)

    assert _tree_bytes(tmp_path) == after_projection
    assert second["status"] == "healthy"
    assert second["exit_shadow"]["state"] == "current"
    assert second["exit_shadow"]["projection_sha256"] == projection["projection_sha256"]
