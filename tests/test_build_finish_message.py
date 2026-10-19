"""Tests that the build-failure hint names the executable the build actually ran.

``_finish_build`` renders the failure line every build path shares. Host is the
structural default (``resolve()`` keeps ``host_mode`` on unless ``--container``,
``BAKAR_CONTAINER``, or a configured container opts in), so a hardcoded
``kas-container`` in that line tells a host-mode user their build ran in a
container it never touched - and sends them debugging the wrong layer.

Every other call site picks the name with ``"kas" if cfg.host_mode else
"kas-container"`` (``steps/kas_build.py``, ``commands/show.py``,
``commands/diff.py``, ``commands/lock.py``); these tests hold the failure hint
to the same rule.
"""

from __future__ import annotations

import time
from io import StringIO
from unittest.mock import MagicMock

import pytest
import typer
from rich.console import Console

import bakar.commands._build_common as build_common
import bakar.commands._hints as hints
import bakar.commands.build as build

pytestmark = pytest.mark.unit


def _failure_output(*, host_mode: bool, monkeypatch) -> str:
    """Drive ``_finish_build`` down the rc != 0 path and return what it printed."""
    buf = StringIO()
    monkeypatch.setattr(build_common, "console", Console(file=buf, width=200, no_color=True))

    cfg = MagicMock()
    cfg.host_mode = host_mode
    log = MagicMock()
    log.run_id = "20260805-125352"

    with pytest.raises(typer.Exit) as excinfo:
        build._finish_build(cfg, log, 2, "qemuarm64")

    assert excinfo.value.exit_code == 2
    return buf.getvalue()


def test_host_mode_failure_names_kas(monkeypatch) -> None:
    """A host-mode build must attribute the failure to ``kas``, not ``kas-container``."""
    out = _failure_output(host_mode=True, monkeypatch=monkeypatch)

    assert "kas build failed (exit 2, run 20260805-125352)." in out
    assert "kas-container" not in out


def test_container_mode_failure_names_kas_container(monkeypatch) -> None:
    """A container build must still attribute the failure to ``kas-container``."""
    out = _failure_output(host_mode=False, monkeypatch=monkeypatch)

    assert "kas-container build failed (exit 2, run 20260805-125352)." in out


def test_failure_keeps_the_triage_hint(monkeypatch) -> None:
    """The run-id triage hint survives the executable-name fix."""
    out = _failure_output(host_mode=True, monkeypatch=monkeypatch)

    assert "bakar triage 20260805-125352" in out


def _finish(*, rc: int, monkeypatch) -> tuple[str, str]:
    """Run ``_finish_build`` and return (build console output, hint stream output)."""
    out_buf, hint_buf = StringIO(), StringIO()
    monkeypatch.setattr(build_common, "console", Console(file=out_buf, width=200, no_color=True))
    monkeypatch.setattr(hints, "console", Console(file=hint_buf, width=200, no_color=True))

    cfg = MagicMock()
    cfg.host_mode = True
    log = MagicMock()
    log.run_id = "20260805-125352"
    log.start_monotonic = time.monotonic()

    if rc != 0:
        with pytest.raises(typer.Exit):
            build._finish_build(cfg, log, rc, "qemuarm64")
    else:
        build._finish_build(cfg, log, rc, "qemuarm64")
    return out_buf.getvalue(), hint_buf.getvalue()


def test_success_line_names_the_run(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    out, _ = _finish(rc=0, monkeypatch=monkeypatch)

    assert "build succeeded in " in out
    assert "(run 20260805-125352)" in out


def test_agent_failure_hints_triage(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    _, hint = _finish(rc=1, monkeypatch=monkeypatch)

    assert "bakar triage 20260805-125352" in hint
    assert "bakar report" not in hint


def test_agent_success_hints_report(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    _, hint = _finish(rc=0, monkeypatch=monkeypatch)

    assert "bakar report 20260805-125352" in hint
    assert "bakar triage" not in hint


@pytest.mark.parametrize("rc", [0, 1])
def test_no_hint_without_claudecode(rc: int, monkeypatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    _, hint = _finish(rc=rc, monkeypatch=monkeypatch)

    assert hint == ""
