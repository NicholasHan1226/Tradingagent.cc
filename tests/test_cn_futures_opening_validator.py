from __future__ import annotations

from datetime import datetime
from pathlib import Path

from CNFutures import opening_validator
from shared.governance.retirement import RETIRED_RUNTIME_EXIT_CODE


def test_opening_validator_cli_is_a_code_level_tombstone(capsys) -> None:
    assert opening_validator.main(["--sqlite-db", "/tmp/legacy.sqlite"]) == (
        RETIRED_RUNTIME_EXIT_CODE
    )
    captured = capsys.readouterr()
    assert '"state": "retired"' in captured.err
    assert "legacy_runtime_retired" in captured.err


def test_pre_open_never_reads_legacy_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite"
    db_path.write_bytes(b"this is not sqlite")

    result = opening_validator.validate_pre_open(
        sqlite_db=db_path,
        now=datetime.fromisoformat("2026-07-20T08:30:00+08:00"),
    )

    assert result["status"] == "fail"
    assert result["state"] == "retired"
    assert result["reason"] == "legacy_opening_validator_retired"
    assert result["data_source"] == "none"
    assert db_path.read_bytes() == b"this is not sqlite"


def test_opening_and_first_sample_are_non_authoritative() -> None:
    now = datetime.fromisoformat("2026-07-20T09:15:00+08:00")
    for report in (
        opening_validator.validate_opening(now=now),
        opening_validator.first_sample_alerts(now=now),
    ):
        assert report["status"] == "fail"
        assert report["production_verified"] is False
        assert report["real_trading_enabled"] is False
        assert report["replacement"].startswith("explicit_tradingdatas_")


def test_source_contains_no_legacy_data_access() -> None:
    source = Path(opening_validator.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "import sqlite3",
        "sqlite3.connect",
        "shared.data.reader",
        "SharedSignalsAPIClient",
        "127.0.0.1:8082",
        '"/tushare',
        '"/source_status',
    ):
        assert forbidden not in source
