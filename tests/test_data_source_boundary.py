from pathlib import Path


PRODUCTION_PATHS = [
    Path("Ashare"),
    Path("Crypto"),
    Path("US"),
    Path("HK"),
    Path("PM"),
    Path("shared/execution"),
]


def test_market_adapters_do_not_reach_reader_shared_fallback() -> None:
    offenders: list[str] = []
    for root in PRODUCTION_PATHS:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if 'getattr(self.reader, "shared"' in text:
                offenders.append(str(path))
    assert offenders == []

