"""Tests for the ``bakar dump`` command.

Drives the command through the Typer ``CliRunner``, monkeypatching
``run_kas_subcommand`` on the dump module so no real kas invocation happens
(mock pattern from ``tests/test_cli_layers.py``). ``run_kas_subcommand`` is
imported into the command module, so it is patched on
``bakar.commands.dump`` - where the ``dump`` function looks it up.

Importing ``bakar.cli`` registers every command (including ``dump``) on the
shared ``app``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import bakar.commands.dump as dump_module
from bakar.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def nxp_workspace(tmp_path: Path) -> Path:
    """A workspace with an ``nxp/`` subdir so workspace detection picks nxp."""
    (tmp_path / "nxp").mkdir()
    return tmp_path


class _Stub:
    """Records the kwargs ``run_kas_subcommand`` was called with."""

    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        ctx: Any,
        subcommand: str,
        extra_args: list[str],
        *,
        capture_to: Any = None,
    ) -> int:
        self.calls.append(
            {
                "subcommand": subcommand,
                "extra_args": extra_args,
                "kas_yaml": ctx.kas_yaml,
                "overlay_source": ctx.overlay_source,
                "extra_overlays": ctx.extra_overlays,
                "capture_to": capture_to,
            }
        )
        return self.rc


@pytest.mark.unit
def test_dump_no_output_streams_to_stdout(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dump without ``--output`` calls the stub with ``capture_to=None`` and exits 0."""
    stub = _Stub(rc=0)
    monkeypatch.setattr(dump_module.step_kas, "run_kas_subcommand", stub)
    result = runner.invoke(app, ["dump", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output
    assert len(stub.calls) == 1
    assert stub.calls[0]["subcommand"] == "dump"
    assert stub.calls[0]["capture_to"] is None


@pytest.mark.unit
def test_dump_output_passes_capture_to_path(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--output resolved.yml`` passes ``capture_to`` equal to that path."""
    stub = _Stub(rc=0)
    monkeypatch.setattr(dump_module.step_kas, "run_kas_subcommand", stub)
    result = runner.invoke(app, ["dump", "--workspace", str(nxp_workspace), "--output", "resolved.yml"])
    assert result.exit_code == 0, result.output
    assert len(stub.calls) == 1
    capture_to = stub.calls[0]["capture_to"]
    assert capture_to is not None
    assert str(capture_to) == "resolved.yml"


@pytest.mark.unit
def test_dump_nonzero_return_propagates(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero return from the stub makes the command exit non-zero."""
    stub = _Stub(rc=3)
    monkeypatch.setattr(dump_module.step_kas, "run_kas_subcommand", stub)
    result = runner.invoke(app, ["dump", "--workspace", str(nxp_workspace)])
    assert result.exit_code != 0, result.output


@pytest.mark.unit
def test_yaml_and_manifest_mutually_exclusive(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing both a positional kas YAML and ``--manifest`` exits code 2."""
    stub = _Stub(rc=0)
    monkeypatch.setattr(dump_module.step_kas, "run_kas_subcommand", stub)
    result = runner.invoke(
        app,
        [
            "dump",
            "my.yml",
            "--manifest",
            "imx-6.12.49-2.2.0.xml",
            "--workspace",
            str(nxp_workspace),
        ],
    )
    assert result.exit_code == 2, result.output
    assert len(stub.calls) == 0


@pytest.mark.unit
def test_dump_applies_sccache_tuning_overlay_when_enabled(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dump must apply the sccache tuning overlay so the flattened YAML matches the build.

    ``dump`` exists to show the resolved YAML a build would run. If it omits the
    opt-in tuning overlays, ``CCACHE = "sccache "`` never appears and the runbook's
    A1 check cannot observe the launcher swap. Mirrors the overlay set the build
    path assembles via ``_tuning_extra_overlays``.
    """
    from bakar.user_config import UserConfig

    stub = _Stub(rc=0)
    monkeypatch.setattr(dump_module.step_kas, "run_kas_subcommand", stub)
    uc = UserConfig(sccache_dist=True, sccache_scheduler_url="http://localhost:10600")
    monkeypatch.setattr("bakar.commands._app._load_user_config_safe", lambda: uc)

    result = runner.invoke(app, ["dump", "--workspace", str(nxp_workspace)])

    assert result.exit_code == 0, result.output
    assert len(stub.calls) == 1
    names = [p.name for p in stub.calls[0]["extra_overlays"]]
    assert "bakar-tuning-sccache.yml" in names, names


@pytest.mark.unit
def test_dump_sccache_dist_flag_applies_overlay(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``--sccache-dist`` flag enables the overlay without a UserConfig entry.

    --sccache-dist is a global flag, not build-only: ``bakar dump --sccache-dist``
    must flatten the same overlay set a --sccache-dist build runs. No UserConfig
    is set here, proving the flag alone enables it.
    """
    stub = _Stub(rc=0)
    monkeypatch.setattr(dump_module.step_kas, "run_kas_subcommand", stub)

    result = runner.invoke(app, ["--sccache-dist", "dump", "--workspace", str(nxp_workspace)])

    assert result.exit_code == 0, result.output
    assert len(stub.calls) == 1
    names = [p.name for p in stub.calls[0]["extra_overlays"]]
    assert "bakar-tuning-sccache.yml" in names, names
