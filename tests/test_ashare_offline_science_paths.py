from pathlib import Path

import pytest

from shared.runtime_test.ashare_offline_science import (
    AshareOfflineScienceError,
    _external_output_root,
)


@pytest.mark.parametrize("nested", [False, True])
def test_output_root_rejects_symlink_before_resolving(
    tmp_path: Path,
    nested: bool,
) -> None:
    real_root = tmp_path / "real-output"
    real_root.mkdir()
    linked_root = tmp_path / "linked-output"
    linked_root.symlink_to(real_root, target_is_directory=True)
    requested_root = linked_root / "reports" if nested else linked_root

    with pytest.raises(
        AshareOfflineScienceError,
        match="output_root_symlink_forbidden",
    ):
        _external_output_root(requested_root)
