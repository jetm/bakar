"""The build-UI state module must stay importable without the rendering library."""

from __future__ import annotations

import subprocess
import sys

_BLOCK_RICH = """
import importlib.abc, sys

class _Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "rich" or name.startswith("rich."):
            raise ImportError("rich blocked: " + name)
        return None

sys.meta_path.insert(0, _Block())
"""


def _run(body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-c", _BLOCK_RICH + body],
        capture_output=True,
        text=True,
        check=False,
    )


def test_build_ui_state_module_imports_without_rich() -> None:
    result = _run(
        "import bakar.steps.build_ui\nassert not [m for m in sys.modules if m == 'rich' or m.startswith('rich.')]\n"
    )
    assert result.returncode == 0, result.stderr


def test_fmt_duration_works_without_rich() -> None:
    result = _run("from bakar.fmt import fmt_duration\nfmt_duration(65)\n")
    assert result.returncode == 0, result.stderr
