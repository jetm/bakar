"""Regression guard for the ``bakar build`` exit-status contract.

A scripted caller (CI wrapper, shell loop, `bakar build ... && deploy`) detects a
failed build by the process exit status alone. It never parses the log. So the
contract asserted here is deliberately narrow and stated only in terms of
``result.exit_code``:

* a non-zero rc from the underlying kas build surfaces as a non-zero CLI exit,
  and the exit code is the rc verbatim (not a flattened 1);
* a successful build exits zero.

The seam is ``bakar.commands.build.step_kas.run_build``. ``build.py`` does
``from bakar.steps import kas_build as step_kas`` and calls
``step_kas.run_build(...)``, so there is no module-level ``run_build`` name in
``bakar.commands.build`` to patch - patching the attribute on the imported
module object is the working seam.

Scope: the byo form, which shares ``_finish_build`` with the manifest, bbsetup,
and qcom forms. The ``--on`` remote-dispatch and multi-release preset forms do
not route through ``_finish_build`` and are out of scope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bakar.cli import app
from bakar.commands import build as build_cmd

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _stub_doctor_checks():
    """Doctor gates every build; stub ``run_all`` to an all-pass list so these
    tests assert exit-status propagation rather than the host's disk-free state."""
    with patch("bakar.commands._helpers.run_all", return_value=[]):
        yield


@pytest.fixture
def byo_yaml(nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal generic kas YAML inside the workspace, with cwd switched to it.

    ``build()`` resolves the workspace from cwd when ``-w`` is absent, so the
    chdir is what makes the byo path reach ``_finish_build``.
    """
    (nxp_workspace / ".bakar.toml").write_text("")
    yaml_path = nxp_workspace / "my.yml"
    yaml_path.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    monkeypatch.chdir(nxp_workspace)
    return yaml_path


@pytest.mark.parametrize("rc", [1, 2, 137])
def test_build_exits_with_the_underlying_rc_on_failure(
    runner: CliRunner,
    byo_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
    rc: int,
) -> None:
    """A non-zero kas rc must reach the process exit status unflattened.

    Asserted on ``exit_code`` only: a caller must be able to branch on failure
    without reading a single line of output.
    """
    monkeypatch.setattr(build_cmd.step_kas, "run_build", lambda ctx, **kw: rc)

    result = runner.invoke(app, ["build", str(byo_yaml)])

    assert result.exit_code == rc


def test_build_exits_zero_on_success(
    runner: CliRunner,
    byo_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction: a succeeding build must not exit non-zero.

    Without this, a fix that made every build exit non-zero would satisfy the
    failure test above.
    """
    monkeypatch.setattr(build_cmd.step_kas, "run_build", lambda ctx, **kw: 0)

    result = runner.invoke(app, ["build", str(byo_yaml)])

    assert result.exit_code == 0
