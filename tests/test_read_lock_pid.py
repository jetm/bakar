"""proc_tree.read_lock_pid: the single bitbake.lock pid parser."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar import proc_tree

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("424242\n", 424242),
        ("424242 extra tokens\n", 424242),
        ("", None),
        ("   \n", None),
        ("not-a-pid\n", None),
    ],
)
def test_read_lock_pid_parses_first_token(tmp_path: Path, content: str, expected: int | None) -> None:
    lock = tmp_path / "bitbake.lock"
    lock.write_text(content)

    assert proc_tree.read_lock_pid(lock) == expected


def test_read_lock_pid_missing_file_returns_none(tmp_path: Path) -> None:
    assert proc_tree.read_lock_pid(tmp_path / "bitbake.lock") is None
