"""Offline tests for the pre-lockup block-trade study."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from Ashare import event_blocktrade_prelockup_study as study


def _sessions(count: int, start: date = date(2026, 1, 5)) -> list[str]:
    out: list[str] = []
    cursor = start
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return out


DAYS = _sessions(30)
CODE = "600000.SH"


def _write_daily(cache: Path, code: str, days: list[str], amount: float = 1000.0,
                 close: float = 10.0) -> None:
    stem = f"daily_{code[:6]}{code[7:]}"
    with (cache / f"{stem}.csv").open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["ts_code", "trade_date", "close", "amount"])
        for d in days:
            w.writerow([code, d, close, amount])


def _write_blocks(out_dir: Path, rows: list[tuple[str, str, float, float]]) -> None:
    """rows: (day, code, price, amount_wan)."""
    by_day: dict[str, list[tuple[str, float, float]]] = {}
    for day, code, price, amount in rows:
        by_day.setdefault(day, []).append((code, price, amount))
    for day, prints in by_day.items():
        with (out_dir / f"{day}.csv").open("w", newline="", encoding="utf-8") as h:
            w = csv.writer(h)
            w.writerow(["ts_code", "trade_date", "price", "vol",
                        "amount", "buyer", "seller"])
            for code, price, amount in prints:
                vol = amount / price if price else 0.0
                w.writerow([code, day, price, vol, amount, "买方", "卖方"])


class ClassifyBlockStateTest(unittest.TestCase):
    def test_edge_inclusive_to_discount_deep(self) -> None:
        self.assertEqual(study.classify_block_state(-0.05), "discount_deep")
        self.assertEqual(study.classify_block_state(-0.03), "discount_deep")
        self.assertEqual(study.classify_block_state(-0.0299), "near_flat")
        self.assertEqual(study.classify_block_state(0.0), "near_flat")
        self.assertEqual(study.classify_block_state(0.07), "near_flat")


class LoadersTest(unittest.TestCase):
    def test_block_loader_filters_universe_and_skips_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            out_dir = cache / study.BLOCKTRADE_DIRNAME
            out_dir.mkdir(parents=True)
            _write_blocks(out_dir, [
                (DAYS[0], CODE, 9.31, 3593.66),
                (DAYS[1], "000001.SZ", 3.75, 1500.0),  # outside universe
            ])
            # malformed row inside a universe file
            with (out_dir / f"{DAYS[2]}.csv").open("a", newline="",
                                                   encoding="utf-8") as h:
                h.write(f"{CODE},{DAYS[2]},not-a-number,100\n")
            series = study.load_symbol_blocks(cache, {CODE})
            self.assertEqual(list(series.keys()), [CODE])
            days, amounts, prices = series[CODE]
            self.assertEqual(days, DAYS[:1])
            self.assertEqual(amounts[0], 3593.66)
            with self.assertRaisesRegex(
                study.BlocktradeStudyError, "blocktrade_dir_missing"
            ):
                study.load_symbol_blocks(cache / "nope", {CODE})

    def test_daily_meta_loader_maps_codes_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_daily(cache, CODE, DAYS[:25], amount=2000.0, close=11.0)
            meta = study.load_symbol_daily_meta(cache, {CODE})
            days, closes, amounts = meta[CODE]
            self.assertEqual(days, DAYS[:25])
            self.assertEqual(closes[DAYS[0]], 11.0)
            self.assertEqual(amounts[0], 2000.0)
            with self.assertRaisesRegex(
                study.BlocktradeStudyError, "daily_cache_missing"
            ):
                study.load_symbol_daily_meta(cache / "e", {CODE})

    def test_newest_first_daily_file_is_normalized_ascending(self) -> None:
        # Tushare daily CSVs arrive newest-first; a descending file must
        # still yield ascending session order (bisect correctness).
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            stem = f"daily_{CODE[:6]}{CODE[7:]}"
            with (cache / f"{stem}.csv").open("w", newline="",
                                              encoding="utf-8") as h:
                w = csv.writer(h)
                w.writerow(["ts_code", "trade_date", "close", "amount"])
                for d in reversed(DAYS[:25]):
                    w.writerow([CODE, d, 10.0, 1000.0])
            meta = study.load_symbol_daily_meta(cache, {CODE})
            days, _closes, amounts = meta[CODE]
            self.assertEqual(days, DAYS[:25])
            self.assertEqual(amounts, [1000.0] * 25)


class AttachBlockStatesTest(unittest.TestCase):
    def _setup(self, cache: Path, block_rows, daily_amount: float = 10000.0):
        _write_daily(cache, CODE, DAYS, amount=daily_amount, close=10.0)
        out_dir = cache / study.BLOCKTRADE_DIRNAME
        out_dir.mkdir(parents=True)
        _write_blocks(out_dir, block_rows)

    def test_none_bucket_when_no_prints_in_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            # Prints only far before the window: entry at DAYS[24], window is
            # sessions 14..23; DAYS[5] sits outside it.
            self._setup(cache, [(DAYS[5], CODE, 9.0, 500.0)])
            blocks = study.load_symbol_blocks(cache, {CODE})
            meta = study.load_symbol_daily_meta(cache, {CODE})
            signals = [{"ts_code": CODE, "entry_day": DAYS[24]}]
            stats = study.attach_block_states(signals, blocks, meta)
            self.assertEqual(stats["attached"], 1)
            self.assertEqual(signals[0]["block_bucket"], "none")
            self.assertEqual(signals[0]["block_intensity"], 0.0)

    def test_entry_day_print_excluded_and_premium_math(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            # Window sessions 14..23: one print at DAYS[20] priced -15%
            # below the same-day close (10.0 -> block price 8.5), one at
            # +0% on DAYS[21]; an entry-day print at DAYS[24] must be
            # ignored, and a print after entry too.
            self._setup(cache, [
                (DAYS[20], CODE, 8.5, 300.0),
                (DAYS[21], CODE, 10.0, 700.0),
                (DAYS[24], CODE, 9.0, 9000.0),
                (DAYS[25], CODE, 9.0, 9000.0),
            ])
            blocks = study.load_symbol_blocks(cache, {CODE})
            meta = study.load_symbol_daily_meta(cache, {CODE})
            signals = [{"ts_code": CODE, "entry_day": DAYS[24]}]
            stats = study.attach_block_states(signals, blocks, meta)
            self.assertEqual(stats["attached"], 1)
            # vw premium = (300*(-0.15) + 700*0.00)/1000 = -0.045.
            self.assertAlmostEqual(float(signals[0]["block_vw_premium"]), -0.045)
            self.assertEqual(signals[0]["block_bucket"], "discount_deep")
            # intensity = (300+700)*10 万元->千元 ... amount 1000万 = 10000千
            # ÷ avg daily amount 10000千 = 1.0 turnover-session equivalents.
            self.assertAlmostEqual(float(signals[0]["block_intensity"]), 1.0)

    def test_near_flat_and_insufficient_paths_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._setup(cache, [(DAYS[18], CODE, 10.05, 400.0)])
            blocks = study.load_symbol_blocks(cache, {CODE})
            meta = study.load_symbol_daily_meta(cache, {CODE})
            signals = [
                {"ts_code": CODE, "entry_day": DAYS[24]},   # near_flat
                {"ts_code": CODE, "entry_day": DAYS[5]},    # short history
                {"ts_code": "000002.SZ", "entry_day": DAYS[24]},  # no daily file
            ]
            stats = study.attach_block_states(signals, blocks, meta)
            self.assertEqual(signals[0]["block_bucket"], "near_flat")
            self.assertEqual(stats["insufficient_history"], 1)
            self.assertEqual(stats["missing_daily"], 1)
            # zero-turnover normalizer also counts as insufficient
            with tempfile.TemporaryDirectory() as tmp2:
                cache2 = Path(tmp2)
                self._setup(cache2, [(DAYS[18], CODE, 10.0, 400.0)],
                            daily_amount=0.0)
                blocks2 = study.load_symbol_blocks(cache2, {CODE})
                meta2 = study.load_symbol_daily_meta(cache2, {CODE})
                solo = [{"ts_code": CODE, "entry_day": DAYS[24]}]
                stats2 = study.attach_block_states(solo, blocks2, meta2)
                self.assertEqual(stats2["insufficient_history"], 1)


if __name__ == "__main__":
    unittest.main()
