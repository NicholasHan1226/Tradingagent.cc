from pathlib import Path
import json
import os
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/run-crypto-forty-symbol-rolling-eval.sh"


def _entry(tmp_path):
    store, output, release = (tmp_path / name for name in ("store", "output", "release"))
    for path in (store, output, release):
        path.mkdir()
    (store / "head.json").write_text("frozen-input")
    module = release / "Crypto"
    module.mkdir()
    (module / "forty_symbol_rolling_evaluation.py").write_text('''import argparse,json,os
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--store-root', required=True)
p.add_argument('--out-json', required=True)
p.add_argument('--report', required=True)
a=p.parse_args()
assert os.environ['REAL_TRADING_ENABLED']=='false'
assert os.environ['PYTHONDONTWRITEBYTECODE']=='1'
assert Path(a.store_root, 'head.json').read_text()=='frozen-input'
if os.environ.get('ENTRY_TEST_FAIL')=='1': raise SystemExit(7)
Path(a.out_json).write_text(json.dumps({'store':a.store_root,'authority':'none'}))
Path(a.report).write_text('research only')
''')
    text = SCRIPT.read_text().replace(
        "/var/lib/tradingagent/crypto-40-symbol-observation", str(store)
    ).replace(
        "/var/lib/tradingagent/crypto-40-symbol-rolling-eval", str(output)
    ).replace(
        "/opt/investment/releases/tradingagent/current", str(release)
    ).replace(
        "/opt/investment/tools/venvs/tradingagent-observation-py312-pyyaml603-v1/bin/python3", sys.executable
    )
    script = tmp_path / "entry.sh"
    script.write_text(text)
    return script, store, output


def test_existing_daily_entrypoint_uses_store_root_and_unique_outputs(tmp_path):
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    script, store, output = _entry(tmp_path)
    for _ in range(2):
        subprocess.run(["bash", str(script)], check=True, capture_output=True)
    reports = list(output.glob("entry-*/entry.json"))
    assert len(reports) == 2
    assert all(json.loads(path.read_text())["store"] == str(store) for path in reports)
    assert len((output / "run.log").read_text().splitlines()) == 2
    assert list(store.iterdir()) == [store / "head.json"]
    assert (store / "head.json").read_text() == "frozen-input"
    assert "--events" not in SCRIPT.read_text()
    assert "--bars-dir" not in SCRIPT.read_text()
    assert "rm " not in SCRIPT.read_text()


def test_daily_entrypoint_failure_is_not_logged_as_success(tmp_path):
    script, store, output = _entry(tmp_path)
    result = subprocess.run(["bash", str(script)], env={**os.environ, "ENTRY_TEST_FAIL": "1"}, capture_output=True)
    assert result.returncode == 7
    assert not (output / "run.log").exists()
    assert len(list(output.iterdir())) == 1  # retain isolated failed attempt
    assert (store / "head.json").read_text() == "frozen-input"
    assert subprocess.run(["bash", str(script), "--events", "tail"], capture_output=True).returncode == 2
