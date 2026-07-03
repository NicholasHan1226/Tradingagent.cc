#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${ROOT}"
PYTHONPATH="${ROOT}" /opt/marketgraph/venv/bin/python3 -c "
from Crypto.simulator import CryptoSimulator
from Crypto.common import CryptoConfig
from shared.markets.style_runner import StyleRunner
r = StyleRunner('crypto', CryptoSimulator(config=CryptoConfig()))
print(r.run_all())
"
