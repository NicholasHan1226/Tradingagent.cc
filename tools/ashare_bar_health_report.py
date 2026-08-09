#!/usr/bin/env python3
"""Per-bar A-share delayed-paper health report for one trading date.

Usage: python3 tools/ashare_bar_health_report.py [YYYY-MM-DD]

Reads the TradingDatas provider-native read model and the TradingAgent
systemd journals for the A-share delayed-paper lanes. Read-only.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
MINUTE_DATASET_ID = "cn.dataset.rt_min"
DEFAULT_DB = "/opt/investment-data/tradingdatas/read_model/provider_native.sqlite"
JOURNAL_SINCE_FORMAT = "%Y-%m-%d 09:35:00"
JOURNAL_UNTIL_FORMAT = "%Y-%m-%d 15:10:00"


def session_bars(day: date) -> tuple[datetime, ...]:
    """Return the 48 completed five-minute bar ends for one A-share session."""

    slots: list[datetime] = []
    current = datetime.combine(day, time(9, 35), tzinfo=SHANGHAI)
    morning_end = datetime.combine(day, time(11, 30), tzinfo=SHANGHAI)
    while current <= morning_end:
        slots.append(current)
        current += timedelta(minutes=5)
    current = datetime.combine(day, time(13, 5), tzinfo=SHANGHAI)
    afternoon_end = datetime.combine(day, time(15, 0), tzinfo=SHANGHAI)
    while current <= afternoon_end:
        slots.append(current)
        current += timedelta(minutes=5)
    return tuple(slots)


def extract_failure_reason(text: str) -> str:
    marker = '"failure_reason": "'
    index = text.find(marker)
    if index < 0:
        return "fail"
    tail = text[index + len(marker) :]
    return tail.split('"', 1)[0] or "fail"


def parse_journal_line(line: str) -> tuple[datetime, str] | None:
    """Parse one short-iso journal line into (when, state)."""

    try:
        stamp, rest = line.split(" ", 1)
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if '"failure_reason"' in rest:
        return when, extract_failure_reason(rest)
    if "Finished" in rest:
        return when, "pass"
    return None


def classify(events: tuple[tuple[datetime, str], ...], bar: datetime) -> str:
    start = bar + timedelta(minutes=5, seconds=20)
    end = bar + timedelta(minutes=6, seconds=40)
    hits = [state for when, state in events if start <= when <= end]
    if "pass" in hits:
        return "pass"
    if hits:
        return hits[-1]
    return "no_run"


def td_rows(conn: sqlite3.Connection, bar: datetime) -> int:
    text = bar.strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT payload_json FROM provider_dataset_rows "
        "WHERE dataset_id=? AND json_extract(payload_json,'$.time')=?",
        (MINUTE_DATASET_ID, text),
    ).fetchall()
    symbols: set[str] = set()
    for (payload,) in rows:
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if obj.get("time") == text and isinstance(obj.get("ts_code"), str):
            symbols.add(obj["ts_code"])
    return len(symbols)


def journal_events(unit: str, trading_date: str) -> tuple[tuple[datetime, str], ...]:
    result = subprocess.run(
        [
            "journalctl",
            "-u",
            unit,
            "--since",
            trading_date + " 09:35:00",
            "--until",
            trading_date + " 15:10:00",
            "--no-pager",
            "-o",
            "short-iso",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    events: list[tuple[datetime, str]] = []
    for line in result.stdout.splitlines():
        parsed = parse_journal_line(line)
        if parsed is not None:
            events.append(parsed)
    return tuple(events)


def report_rows(trading_date: str) -> list[dict[str, object]]:
    day = date.fromisoformat(trading_date)
    conn = sqlite3.connect(DEFAULT_DB)
    paper30 = journal_events(
        "tradingagent-ashare-minute-paper.service", trading_date
    )
    scale500 = journal_events(
        "tradingagent-ashare-minute-scale500-paper.service", trading_date
    )
    rows = []
    for bar in session_bars(day):
        rows.append(
            {
                "bar": bar.strftime("%H:%M"),
                "td_rows": td_rows(conn, bar),
                "paper30": classify(paper30, bar),
                "scale500": classify(scale500, bar),
            }
        )
    conn.close()
    return rows


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    trading_date = args[0] if args else date.today().isoformat()
    print(f"{'bar':<8}{'td_rows':>8}{'paper30':>10}{'scale500':>10}")
    for row in report_rows(trading_date):
        print(
            f"{row['bar']:<8}{row['td_rows']:>8}"
            f"{str(row['paper30']):>10}{str(row['scale500']):>10}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
