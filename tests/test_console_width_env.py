"""The suite must not inherit a CI terminal size into the shared console.

Rich reads COLUMNS and LINES when a Console is built and then pins the width
and height, so a runner exporting them would make console-output tests wrap at
the runner's size. ``tests/conftest.py`` drops both variables before
``bakar.commands`` is imported; this test proves it in a fresh interpreter
whose environment is set here, never by the parent shell.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_CHILD = """
import os
import tests.conftest  # noqa: F401
from bakar.commands import console

print(console._width, console._height)
os.environ["COLUMNS"] = "500"
print(console.size.width)
"""


def test_conftest_scrubs_terminal_size_before_console_is_built(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("COLUMNS", "LINES")}
    env["COLUMNS"] = "80"
    env["LINES"] = "24"
    out_file = tmp_path / "child.out"
    with out_file.open("w") as out:
        child = subprocess.Popen(
            [sys.executable, "-c", _CHILD],
            cwd=REPO_ROOT,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            returncode = child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            raise
    text = out_file.read_text()
    assert returncode == 0, text
    fixed, resized = text.splitlines()[-2:]
    assert fixed == "None None"
    assert resized == "500"
