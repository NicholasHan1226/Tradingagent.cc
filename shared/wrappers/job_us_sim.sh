#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${ROOT}"
PYTHONPATH="${ROOT}" /opt/marketgraph/venv/bin/python3 -c "
from US.simulator import USSimulator
from US.common import USConfig
from shared.markets.style_runner import StyleRunner
r = StyleRunner('us', USSimulator(config=USConfig()))
print(r.run_all())
"
