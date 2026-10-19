"""bakar's own warnings render as one line; others keep the default format."""

from __future__ import annotations

import io
import subprocess
import sys
from typing import TYPE_CHECKING

from bakar.commands import _app

if TYPE_CHECKING:
    from pathlib import Path


def test_bakar_filename_renders_one_line() -> None:
    buf = io.StringIO()
    _app._bakar_showwarning(
        UserWarning("unknown key 'x'"),
        UserWarning,
        str(_app._PACKAGE_DIR / "user_config.py"),
        7,
        buf,
    )
    assert buf.getvalue() == "bakar: warning: unknown key 'x'\n"


def test_third_party_filename_delegates(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(_app, "_ORIGINAL_SHOWWARNING", lambda *a: calls.append(a))
    _app._bakar_showwarning(DeprecationWarning("old"), DeprecationWarning, "/elsewhere/lib.py", 3)
    assert len(calls) == 1
    assert calls[0][2] == "/elsewhere/lib.py"


def test_unknown_key_is_one_line_in_subprocess(tmp_path: Path) -> None:
    cfg = tmp_path / ".config" / "bakar"
    cfg.mkdir(parents=True)
    (cfg / "config.toml").write_text("[build]\nnot_a_key = 1\n")
    env = {
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / ".config"),
        "PATH": "/usr/bin:/bin",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "bakar", "settings", "list"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    lines = [ln for ln in proc.stderr.splitlines() if "unknown key 'not_a_key'" in ln]
    assert len(lines) == 1, proc.stderr
    assert "user_config.py" not in proc.stderr
