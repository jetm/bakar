"""Tests for the ``bakar sync`` command.

The sync command does not call ``subprocess.run`` directly; it dispatches to
``bsp.sync_step`` which is bound at ``BspModel`` construction time to the
function references in ``bakar.steps.repo`` (NXP, ``init_and_sync``) and
``bakar.steps.ti_layertool`` (TI, ``populate``). Patching ``subprocess.run``
on those two step modules is the seam that keeps the dispatched argv
visible to the test.

The doctor pre-flight always runs now; an autouse fixture stubs ``run_all`` to
an empty pass list so these tests stay host-independent.
``workspace.detect`` is patched to a state that needs a sync (so the step
runs) but does not need a setup_env regenerate (so the second-half of
sync.py is skipped, avoiding the need to patch setup_env too).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from bakar.commands import app
from bakar.workspace import WorkspaceState

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _stub_doctor_checks():
    """Doctor always runs now; stub ``run_all`` to an empty (all-pass) list so these
    tests stay host-independent - real checks BLOCK on disk-free / git config."""
    with patch("bakar.commands._helpers.run_all", return_value=[]):
        yield


def _state_needing_sync(family: str) -> WorkspaceState:
    """Build a WorkspaceState that forces sync but skips setup_env.

    ``needs_repo_sync`` requires ``not (repo_initialized and sources_populated)``;
    leaving both False fires the sync. ``needs_setup_env`` is
    ``needs_full_reinit or not bblayers_present``; with the manifest/branch
    fields aligned and bblayers_present=True the second-half setup_env path
    in ``sync.py`` is skipped.
    """
    return WorkspaceState(
        bsp_family=family,  # type: ignore[arg-type]
        repo_initialized=False,
        sources_populated=False,
        build_dir_exists=False,
        bblayers_present=True,
        kas_yaml_present=False,
        forks_linux_imx=False,
        cache_dirs_ok=True,
        repo_manifest_include=None,
        repo_manifests_branch=None,
        requested_manifest="",
        requested_branch="",
        sha_drift=(),
    )


def _fake_completed(returncode: int = 0) -> MagicMock:
    """A stand-in for a ``subprocess.CompletedProcess`` with the given rc."""
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = ""
    cp.stderr = ""
    return cp


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_sync_nxp_dispatches_to_repo(
    runner: CliRunner,
    fake_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NXP sync must invoke the ``repo`` tool via ``bakar.steps.repo``.

    Asserts that the patched ``subprocess.run`` in ``bakar.steps.repo`` was
    called and that at least one call's argv begins with ``"repo"``. The
    ``check=True`` keyword means ``subprocess.run`` would raise on a non-zero
    return; the fake returns 0 so the step completes normally.
    """
    monkeypatch.chdir(fake_workspace)
    # Patch detect so it forces sync but skips setup_env. Patch at the
    # sync.py import site so other tests are unaffected.
    monkeypatch.setattr(
        "bakar.commands.sync.detect",
        lambda cfg: _state_needing_sync("nxp"),
    )

    with patch(
        "bakar.steps.repo.subprocess.run",
        return_value=_fake_completed(0),
    ) as mock_run:
        result = runner.invoke(
            app,
            ["sync", "--manifest", "imx-6.6.52-2.2.2.xml"],
        )

    assert result.exit_code == 0, result.output
    assert mock_run.call_count >= 1, "expected bakar.steps.repo.subprocess.run to fire"
    argv_first_tokens = [call.args[0][0] for call in mock_run.call_args_list if call.args]
    assert "repo" in argv_first_tokens, f"expected at least one `repo` invocation, got {argv_first_tokens!r}"


def test_sync_ti_dispatches_to_oe_layertool(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TI sync must invoke oe-layertool, not ``repo``.

    Builds a TI-shaped workspace with the layertool script and config file
    that ``populate`` checks before calling ``subprocess.run``. Both step
    modules' ``subprocess.run`` symbols are the same stdlib reference, so
    a single ``patch("subprocess.run", ...)`` is enough; the assertion
    distinguishes the dispatched argv (``bash ./oe-layertool-setup.sh ...``
    for TI vs ``repo ...`` for NXP).
    """
    # TI workspace layout: marker + ti/ + oe-layertool script + config file.
    (tmp_path / ".bakar.toml").write_text("")
    ti_root = tmp_path / "ti"
    layertool_dir = ti_root / "oe-layertool"
    layertool_dir.mkdir(parents=True)
    (layertool_dir / "oe-layertool-setup.sh").write_text("#!/bin/sh\n")
    manifest_name = "processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt"
    cfg_dir = layertool_dir / "configs" / "variscite"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / manifest_name).write_text("")
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(
        "bakar.commands.sync.detect",
        lambda cfg: _state_needing_sync("ti"),
    )

    def _side(*args: object, **kwargs: object) -> MagicMock:
        # populate() verifies sources/oe-core/oe-init-build-env exists after
        # the script returns and raises otherwise; create it as a side effect
        # so the post-subprocess check passes.
        marker = ti_root / "sources" / "oe-core" / "oe-init-build-env"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("")
        return _fake_completed(0)

    with patch("bakar.steps.ti_layertool.subprocess.run", side_effect=_side) as mock_run:
        result = runner.invoke(
            app,
            ["sync", "--manifest", manifest_name],
        )

    assert result.exit_code == 0, result.output
    assert mock_run.call_count >= 1, "expected the TI subprocess.run to fire"
    # Confirm the dispatched argv is the layertool invocation, NOT `repo`.
    argv0_list = [call.args[0][0] for call in mock_run.call_args_list if call.args]
    assert "repo" not in argv0_list, f"TI subprocess argv must not start with `repo`, got {argv0_list!r}"
    # And confirm the layertool script was actually invoked.
    all_argv0_or_1 = [tok for call in mock_run.call_args_list if call.args for tok in call.args[0][:2]]
    assert any("oe-layertool-setup.sh" in tok for tok in all_argv0_or_1), (
        f"expected the TI layertool script in argv, got {all_argv0_or_1!r}"
    )


def test_sync_nxp_subprocess_failure_propagates(
    runner: CliRunner,
    fake_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ``repo`` call must bubble a non-zero exit out of ``sync``.

    ``init_and_sync`` calls ``subprocess.run(..., check=True)``. With check=True
    a non-zero rc raises ``CalledProcessError``; the test simulates this by
    making the patched function raise directly. The sync command does not
    catch the exception, so Typer surfaces it as a non-zero exit.
    """
    import subprocess

    monkeypatch.chdir(fake_workspace)
    monkeypatch.setattr(
        "bakar.commands.sync.detect",
        lambda cfg: _state_needing_sync("nxp"),
    )

    def _boom(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(returncode=1, cmd=args[0] if args else "repo")

    with patch("bakar.steps.repo.subprocess.run", side_effect=_boom):
        result = runner.invoke(
            app,
            ["sync", "--manifest", "imx-6.6.52-2.2.2.xml"],
        )

    assert result.exit_code != 0, f"expected non-zero exit on subprocess failure, got 0 with output:\n{result.output}"


def test_sync_workspace_not_found_exits_nonzero(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running ``sync`` outside a workspace must exit non-zero.

    ``_workspace_from_cwd`` walks parents looking for a marker; a bare
    ``tmp_path`` has none, so the helper raises ``typer.Exit(2)``. The
    error message names the missing marker so the user knows how to fix.
    """
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["sync", "--manifest", "imx-6.6.52-2.2.2.xml"],
    )

    assert result.exit_code != 0, f"expected non-zero exit when no workspace marker exists, got 0:\n{result.output}"


def test_sync_dry_run_script_stdout(
    runner: CliRunner,
    fake_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run-script -`` writes a bash script to stdout and exits 0.

    The script must start with the shebang line and contain a provenance
    comment naming the bsp_family. No subprocess calls are made; the
    command exits before sync.
    """
    monkeypatch.chdir(fake_workspace)

    result = runner.invoke(
        app,
        ["sync", "--manifest", "imx-6.6.52-2.2.2.xml", "--dry-run-script", "-"],
    )

    assert result.exit_code == 0, result.output
    assert result.output.startswith("#!/usr/bin/env bash"), (
        f"expected shebang as first line, got:\n{result.output[:200]}"
    )
    assert "# bsp_family: nxp" in result.output, f"expected bsp_family comment in script, got:\n{result.output[:400]}"


def test_sync_dry_run_script_file(
    runner: CliRunner,
    fake_workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run-script PATH`` writes the script to a file and exits 0.

    The command must not invoke any sync subprocess and the written file
    must contain the shebang header.
    """
    monkeypatch.chdir(fake_workspace)
    script_path = tmp_path / "sync.sh"

    result = runner.invoke(
        app,
        ["sync", "--manifest", "imx-6.6.52-2.2.2.xml", "--dry-run-script", str(script_path)],
    )

    assert result.exit_code == 0, result.output
    assert script_path.exists(), "expected script file to be written"
    content = script_path.read_text()
    assert content.startswith("#!/usr/bin/env bash"), f"expected shebang, got:\n{content[:200]}"


def test_sync_dry_run_script_nxp_contains_repo(
    runner: CliRunner,
    fake_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NXP ``--dry-run-script`` output must contain ``repo init``/``repo sync``.

    The sync step in the generated script branches on ``cfg.bsp_family``;
    for the NXP family the correct tool is ``repo``.
    """
    monkeypatch.chdir(fake_workspace)

    result = runner.invoke(
        app,
        ["sync", "--manifest", "imx-6.6.52-2.2.2.xml", "--dry-run-script", "-"],
    )

    assert result.exit_code == 0, result.output
    assert "repo init" in result.output, f"expected 'repo init' in script sync step, got:\n{result.output}"
    assert "repo sync" in result.output, f"expected 'repo sync' in script sync step, got:\n{result.output}"


def test_sync_dry_run_without_script_writes_no_file(
    runner: CliRunner,
    fake_workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` (without ``--dry-run-script``) must not create a script file.

    The plain ``--dry-run`` flag calls ``_print_dry_run`` and exits; it must
    never write any file to the workspace.
    """
    monkeypatch.chdir(fake_workspace)
    before = set((fake_workspace).rglob("*.sh"))

    result = runner.invoke(
        app,
        ["sync", "--manifest", "imx-6.6.52-2.2.2.xml", "--dry-run"],
    )

    after = set((fake_workspace).rglob("*.sh"))
    assert result.exit_code == 0, result.output
    assert after == before, f"unexpected script file(s) written: {after - before}"
