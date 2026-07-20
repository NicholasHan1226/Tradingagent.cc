from __future__ import annotations

import pytest

from shared.runtime_test import sharedsignals_evidence_contract as contract
from shared.governance.retirement import RETIRED_RUNTIME_EXIT_CODE, RetiredRuntimeError


def test_retired_contract_library_fails_closed() -> None:
    with pytest.raises(RetiredRuntimeError, match="legacy_runtime_retired"):
        contract.run_contract_check(api_url="http://legacy.invalid")


def test_retired_contract_cli_is_tombstoned() -> None:
    assert contract.main(["--api-url", "http://legacy.invalid"]) == (
        RETIRED_RUNTIME_EXIT_CODE
    )
