"""Tests for the ``bakar prefetch`` command.

Drives the command through the Typer ``CliRunner``, monkeypatching
``run_shell`` so no real kas/kas-container invocation happens. The prefetch
command calls ``step_kas.run_shell(...)`` where ``step_kas`` is the imported
``bakar.steps.kas_build`` module, so the stub is installed on
``prefetch_module.step_kas`` (the attribute the command actually looks up).

The stub captures the ``command=`` kwarg and the resolved ``cfg`` so the tests
can assert on the fetch command string and the resolved machine.

The prefetch console may write to stderr; CliRunner mixes both streams into
``result.output`` (asserting against ``result.stdout`` would miss stderr).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import bakar.commands.prefetch as prefetch_module
from bakar.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


class _ShellStub:
    """Records the ``cfg`` and ``command=`` from each ``run_shell`` call."""

    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.cfg = None
        self.command: str | None = None
        self.called = False

    def __call__(self, ctx, args, *, command=None) -> int:
        self.called = True
        self.cfg = ctx.cfg
        self.command = command
        return self.rc


# Minimal valid bitbake-setup config (subset of the shape checked by
# ``is_bbsetup_workspace`` - both ``data`` and ``bitbake-config`` must be
# present as top-level keys). Mirrors test_cli_gen_kas.py's
# ``_VALID_BBSETUP_CONFIG``.
_VALID_BBSETUP_CONFIG: dict = {
    "type": "registry",
    "name": "oe-nodistro-wrynose",
    "data": {
        "sources": {
            "openembedded-core": {
                "git-remote": {
                    "uri": "https://git.openembedded.org/openembedded-core",
                    "branch": "wrynose",
                }
            }
        }
    },
    "bitbake-config": {
        "name": "nodistro",
        "bb-layers": ["openembedded-core/meta"],
    },
}


@pytest.fixture
def bbsetup_workspace(tmp_path: Path) -> Path:
    """A tmp dir laid out like an initialized ``bitbake-setup`` workspace."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config-upstream.json").write_text(json.dumps(_VALID_BBSETUP_CONFIG), encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "init-build-env").write_text("", encoding="utf-8")
    return tmp_path


@pytest.fixture
def generic_yaml(tmp_path: Path) -> Path:
    """Write a minimal generic/BYO kas YAML (qemu machine, no NXP/TI markers)."""
    yaml_path = tmp_path / "my.yml"
    yaml_path.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    return yaml_path


@pytest.mark.unit
def test_prefetch_invokes_runall_fetch_with_image(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prefetch calls run_shell with a ``bitbake --runall=fetch <image>`` command."""
    stub = _ShellStub(rc=0)
    monkeypatch.setattr(prefetch_module.step_kas, "run_shell", stub)
    result = runner.invoke(app, ["prefetch", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output
    assert stub.called
    assert stub.command is not None
    assert "bitbake --runall=fetch" in stub.command
    # The resolved image must appear in the fetch command string.
    assert stub.cfg.image in stub.command


@pytest.mark.unit
def test_prefetch_machine_override_reaches_invocation(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-m imx95-var-dart`` makes the resolved cfg.machine reach run_shell."""
    stub = _ShellStub(rc=0)
    monkeypatch.setattr(prefetch_module.step_kas, "run_shell", stub)
    result = runner.invoke(
        app,
        ["prefetch", "-m", "imx95-var-dart", "--workspace", str(nxp_workspace)],
    )
    assert result.exit_code == 0, result.output
    assert stub.cfg is not None
    assert stub.cfg.machine == "imx95-var-dart"


@pytest.mark.unit
def test_prefetch_nonzero_return_propagates(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero run_shell return makes the command exit non-zero."""
    stub = _ShellStub(rc=3)
    monkeypatch.setattr(prefetch_module.step_kas, "run_shell", stub)
    result = runner.invoke(app, ["prefetch", "--workspace", str(nxp_workspace)])
    assert result.exit_code != 0, result.output


@pytest.mark.unit
def test_prefetch_bbsetup_workspace_defaults_to_core_image_minimal(
    runner: _CliRunner, bbsetup_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bbsetup workspace, no ``--image``: fetch target is core-image-minimal, never generic."""
    stub = _ShellStub(rc=0)
    monkeypatch.setattr(prefetch_module.step_kas, "run_shell", stub)
    result = runner.invoke(
        app,
        ["prefetch", "--workspace", str(bbsetup_workspace), "-m", "qemux86-64"],
    )
    assert result.exit_code == 0, result.output
    assert stub.called
    assert stub.command is not None
    assert "bitbake --runall=fetch core-image-minimal" in stub.command
    assert "generic" not in stub.command


@pytest.mark.unit
def test_prefetch_bbsetup_workspace_image_overrides_target(
    runner: _CliRunner, bbsetup_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bbsetup workspace with an explicit ``--image`` overrides the fetch target."""
    stub = _ShellStub(rc=0)
    monkeypatch.setattr(prefetch_module.step_kas, "run_shell", stub)
    result = runner.invoke(
        app,
        [
            "prefetch",
            "--workspace",
            str(bbsetup_workspace),
            "--image",
            "my-custom-image",
            "-m",
            "qemux86-64",
        ],
    )
    assert result.exit_code == 0, result.output
    assert stub.command is not None
    assert "bitbake --runall=fetch my-custom-image" in stub.command


@pytest.mark.unit
def test_prefetch_byo_generic_yaml_defaults_to_core_image_minimal(
    runner: _CliRunner, generic_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BYO/generic kas YAML, no ``--image``: regression test for the reported bug.

    Before the fix, this path sent the literal sentinel string ``"generic"``
    to bitbake as the fetch target, producing ``Nothing PROVIDES 'generic'``.
    The existing NXP-only ``test_prefetch_invokes_runall_fetch_with_image``
    test never caught this because it only exercises ``nxp_workspace``.
    """
    stub = _ShellStub(rc=0)
    monkeypatch.setattr(prefetch_module.step_kas, "run_shell", stub)
    result = runner.invoke(app, ["prefetch", str(generic_yaml)])
    assert result.exit_code == 0, result.output
    assert stub.called
    assert stub.command is not None
    assert "bitbake --runall=fetch core-image-minimal" in stub.command
    assert "generic" not in stub.command
