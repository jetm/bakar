"""Tests for the ``bakar ps`` command.

``bakar ps`` is directory-independent by design: it performs no workspace
resolution of any kind, so every test here monkeypatches the host-wide
discovery calls on ``bakar.commands.ps.build_stop`` directly rather than
building a ``.bakar.toml``-marked workspace fixture the way every other
CLI-command test suite does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bakar.commands.ps as ps_cmd
from bakar.build_stop import ContainerCandidate, RunCandidate, RunRoot
from bakar.cli import app
from tests.conftest import make_build_config

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


def _no_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every host-wide discovery call to report nothing found.

    Shared setup for tests that do not care about discovery itself - without
    this, ``bakar ps`` would scan the real host's ``/proc`` and query the real
    container runtime, which is neither hermetic nor guaranteed to find
    nothing on a developer machine that happens to have a build running.
    """
    monkeypatch.setattr(ps_cmd.build_stop, "_discover_host_cookers", dict)
    monkeypatch.setattr(ps_cmd.build_stop, "correlate_host_discoveries", lambda _discovered: [])
    monkeypatch.setattr(ps_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(ps_cmd.build_stop, "discover_running_containers_or_warn", lambda _runtime: ([], None))


def test_ps_empty_result_prints_plain_message(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """No live builds anywhere -> the documented plain-text line, not silence."""
    _no_discovery(monkeypatch)

    result = runner.invoke(app, ["ps"])

    assert result.exit_code == 0, result.output
    assert "no bakar builds running" in result.output


def test_ps_runs_outside_any_workspace(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`bakar ps` succeeds from a directory with no workspace markers at all.

    This is the required verify test: a bare tmpdir carries no
    ``.bakar.toml``, no ``nxp``/``ti``/``build-*`` subdirectory - nothing any
    workspace-resolution helper could key off. `_workspace_from_cwd` is
    patched to raise if it is ever called, so a regression that makes `ps`
    perform workspace resolution fails loudly here instead of only when run
    from a genuinely markerless directory in practice.
    """
    _no_discovery(monkeypatch)

    def _must_not_be_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("bakar ps must not perform workspace resolution")

    monkeypatch.setattr("bakar.commands._helpers._workspace_from_cwd", _must_not_be_called)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["ps"])

    assert result.exit_code == 0, result.output
    assert "no bakar builds running" in result.output


def test_ps_renders_host_mode_row(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A host-mode candidate renders its run id, mode, family, and machine.

    Family/machine come the same way group 9's correlation already resolves
    them: ``candidate.root.family`` and ``candidate.cfg.machine``.
    """
    run_dir = tmp_path / "nxp" / "build" / "runs" / "20260618-120000-111"
    run_dir.mkdir(parents=True)
    root = RunRoot(bsp_root=tmp_path / "nxp", family="nxp", resolve_workspace=tmp_path, resolve_family="nxp")
    cfg = make_build_config(workspace=tmp_path / "nxp", machine="imx8mp-var-dart")
    candidate = RunCandidate(run_dir=run_dir, root=root, cfg=cfg)

    monkeypatch.setattr(ps_cmd.build_stop, "_discover_host_cookers", lambda: {"fake": frozenset()})
    monkeypatch.setattr(ps_cmd.build_stop, "correlate_host_discoveries", lambda _discovered: [candidate])
    monkeypatch.setattr(ps_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(ps_cmd.build_stop, "discover_running_containers_or_warn", lambda _runtime: ([], None))

    result = runner.invoke(app, ["ps"])

    assert result.exit_code == 0, result.output
    assert "20260618-120000-111" in result.output
    assert "mode=host" in result.output
    assert "family=nxp" in result.output
    assert "machine=imx8mp-var-dart" in result.output


def test_ps_container_row_falls_back_to_unknown_when_mount_unrecoverable(
    runner: _CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container row whose bind-mount source cannot be recovered falls back
    to "unknown" for family and machine instead of raising."""
    candidate = ContainerCandidate(run_id="20260618-130000-222", container_id="abc123")

    monkeypatch.setattr(ps_cmd.build_stop, "_discover_host_cookers", dict)
    monkeypatch.setattr(ps_cmd.build_stop, "correlate_host_discoveries", lambda _discovered: [])
    monkeypatch.setattr(ps_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(ps_cmd.build_stop, "discover_running_containers_or_warn", lambda _runtime: ([candidate], None))
    # Simulate an unrecoverable mount path (runtime inspect failed/timed out/
    # no matching /work mount) without shelling out to a real container runtime.
    monkeypatch.setattr(ps_cmd, "_container_mount_source", lambda _runtime, _cid: None)

    result = runner.invoke(app, ["ps"])

    assert result.exit_code == 0, result.output
    assert "20260618-130000-222" in result.output
    assert "mode=container" in result.output
    assert "family=unknown" in result.output
    assert "machine=unknown" in result.output


def test_ps_container_row_falls_back_to_unknown_when_run_record_unreadable(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recovered mount path that yields no matching run record also falls
    back to "unknown" rather than raising - the run id in the container label
    has no corresponding run dir under the recovered mount source."""
    candidate = ContainerCandidate(run_id="20260618-140000-333", container_id="def456")
    # A real, empty directory: enumerate_workspace_runs succeeds but finds no
    # candidate whose run_dir.name matches this container's run id.
    empty_mount_source = tmp_path / "workspace"
    empty_mount_source.mkdir()

    monkeypatch.setattr(ps_cmd.build_stop, "_discover_host_cookers", dict)
    monkeypatch.setattr(ps_cmd.build_stop, "correlate_host_discoveries", lambda _discovered: [])
    monkeypatch.setattr(ps_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(ps_cmd.build_stop, "discover_running_containers_or_warn", lambda _runtime: ([candidate], None))
    monkeypatch.setattr(ps_cmd, "_container_mount_source", lambda _runtime, _cid: str(empty_mount_source))

    result = runner.invoke(app, ["ps"])

    assert result.exit_code == 0, result.output
    assert "family=unknown" in result.output
    assert "machine=unknown" in result.output
