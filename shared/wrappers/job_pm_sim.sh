#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${ROOT}"
PYTHONPATH="${ROOT}" /opt/marketgraph/venv/bin/python3 -c "
from PM.simulator import PMSimulator
from PM.common import PMConfig
from shared.markets.style_runner import StyleRunner
r = StyleRunner('pm', PMSimulator(config=PMConfig(real=False)))
print(r.run_all())
"
