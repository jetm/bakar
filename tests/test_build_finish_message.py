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

from io import StringIO
from unittest.mock import MagicMock

import pytest
import typer
from rich.console import Console

import bakar.commands.build as build

pytestmark = pytest.mark.unit


def _failure_output(*, host_mode: bool, monkeypatch) -> str:
    """Drive ``_finish_build`` down the rc != 0 path and return what it printed."""
    buf = StringIO()
    monkeypatch.setattr(build, "console", Console(file=buf, width=200, no_color=True))

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

    assert "kas build failed (exit 2)." in out
    assert "kas-container" not in out


def test_container_mode_failure_names_kas_container(monkeypatch) -> None:
    """A container build must still attribute the failure to ``kas-container``."""
    out = _failure_output(host_mode=False, monkeypatch=monkeypatch)

    assert "kas-container build failed (exit 2)." in out


def test_failure_keeps_the_triage_hint(monkeypatch) -> None:
    """The run-id triage hint survives the executable-name fix."""
    out = _failure_output(host_mode=True, monkeypatch=monkeypatch)

    assert "bakar triage 20260805-125352" in out
