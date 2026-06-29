from datetime import date
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Ashare import t_plus_1


@pytest.fixture(autouse=True)
def reset_trade_calendar_cache(monkeypatch):
    monkeypatch.setattr(t_plus_1, "TRADE_CALENDAR_SEARCH_ROOTS", ())
    t_plus_1._load_trade_calendar_data.cache_clear()
    yield
    t_plus_1._load_trade_calendar_data.cache_clear()


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("20260630", date(2026, 6, 30)),
        ("2026-06-30", date(2026, 6, 30)),
        ("2026-06-30T10:00:00", date(2026, 6, 30)),
        ("2026-06-30T10:00:00+08:00", date(2026, 6, 30)),
    ],
)
def test_to_date_supports_expected_formats(raw_value, expected):
    assert t_plus_1._to_date(raw_value) == expected


def test_to_date_rejects_invalid_strings():
    with pytest.raises(ValueError, match="Unsupported date string format"):
        t_plus_1._to_date("2026/06/30")


def test_can_sell_requires_next_trading_day_after_friday_buy():
    assert t_plus_1.can_sell("2026-06-26", "2026-06-27") is False
    assert t_plus_1.can_sell("2026-06-26", "2026-06-29") is True


def test_can_sell_skips_known_holidays():
    assert t_plus_1.can_sell("2026-09-24", "2026-09-25") is False
    assert t_plus_1.can_sell("2026-09-24", "2026-09-28") is True


def test_can_sell_returns_false_when_open_date_missing():
    assert t_plus_1.can_sell(None, "2026-06-30") is False
    assert t_plus_1.can_sell("", "2026-06-30") is False


def test_filter_sellable_uses_t_plus_1_logic():
    positions = [
        {"ts_code": "600000.SH", "open_date": "2026-06-26"},
        {"ts_code": "600001.SH", "open_date": "2026-06-29"},
        {"ts_code": "600002.SH", "open_date": None},
    ]

    sellable = t_plus_1.filter_sellable(positions, "2026-06-29")

    assert [position["ts_code"] for position in sellable] == ["600000.SH"]


def test_get_trading_calendar_uses_external_trade_cal_when_available(tmp_path, monkeypatch):
    calendar_path = tmp_path / "trade_cal.csv"
    calendar_path.write_text(
        "cal_date,is_open\n"
        "2026-06-26,1\n"
        "2026-06-27,0\n"
        "2026-06-29,1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(t_plus_1, "TRADE_CALENDAR_SEARCH_ROOTS", (Path(tmp_path),))
    t_plus_1._load_trade_calendar_data.cache_clear()

    assert t_plus_1.get_trading_calendar("2026-06-26", "2026-06-29") == [
        date(2026, 6, 26),
        date(2026, 6, 29),
    ]
