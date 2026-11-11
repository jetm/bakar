"""Tests for ``build_targets``, the multi-target build helper.

``_run_task`` always ends in ``typer.Exit``, so a caller that must act on the
result (feed-first loop) cannot use it. ``build_targets`` runs one bitbake
invocation over several targets through ``run_shell_live`` and returns the exit
code and run id. The shell runner is stubbed; no container starts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bakar.commands.bitbake import BuildResult, build_targets

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

_MANIFEST = "imx-6.6.52-2.2.0.xml"


def _fake_live(rc: int, calls: list[dict]):
    def fake_live(ctx, command):
        calls.append({"command": command, "run_id": ctx.log.run_id})
        return rc

    return fake_live


def _build(workspace: Path, targets: list[str], **kwargs) -> BuildResult:
    return build_targets(targets, manifest=_MANIFEST, workspace=workspace, **kwargs)


def test_multi_target_issues_one_bitbake_with_all_targets(nxp_workspace: Path) -> None:
    calls: list[dict] = []
    with patch("bakar.commands.bitbake.run_shell_live", _fake_live(0, calls)):
        result = _build(nxp_workspace, ["htop", "busybox"])

    assert len(calls) == 1
    assert calls[0]["command"] == "bitbake htop busybox"
    assert result.exit_code == 0


def test_targets_are_shell_quoted(nxp_workspace: Path) -> None:
    calls: list[dict] = []
    with patch("bakar.commands.bitbake.run_shell_live", _fake_live(0, calls)):
        _build(nxp_workspace, ["a b", "c"], keep_going=True)

    assert calls[0]["command"] == "bitbake -k 'a b' c"


def test_successful_build_returns_zero_and_run_id(nxp_workspace: Path) -> None:
    calls: list[dict] = []
    with patch("bakar.commands.bitbake.run_shell_live", _fake_live(0, calls)):
        result = _build(nxp_workspace, ["htop"])

    assert result.exit_code == 0
    assert result.run_id == calls[0]["run_id"]
    assert result.run_id


def test_failed_build_returns_nonzero_and_run_id(nxp_workspace: Path) -> None:
    calls: list[dict] = []
    with patch("bakar.commands.bitbake.run_shell_live", _fake_live(3, calls)):
        result = _build(nxp_workspace, ["htop", "busybox"])

    assert result.exit_code == 3
    assert result.run_id == calls[0]["run_id"]
    assert result.run_id


def test_empty_targets_rejected(nxp_workspace: Path) -> None:
    with pytest.raises(ValueError, match="at least one target"):
        _build(nxp_workspace, [])
