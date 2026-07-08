"""Extended tests for ``bakar build`` covering pipeline ordering, flag
propagation, and pre-flight gating.

The build flow dispatches to four user-visible step seams:

* ``bsp.sync_step``    -> ``bakar.steps.repo.init_and_sync`` (NXP)
* ``bsp.setup_env_step`` -> ``bakar.steps.setup_env.run`` (NXP)
* ``step_override.apply`` -> ``bakar.steps.bitbake_override.apply``
* ``step_kas.regenerate_yaml`` / ``step_kas.run_build`` -> ``bakar.steps.kas_build.*``

Sync and setup_env both call ``subprocess.run`` from their respective step
modules. Because every step module's ``subprocess`` attribute points at the
same stdlib module object, ``patch("bakar.steps.repo.subprocess.run", ...)``
and ``patch("bakar.steps.setup_env.subprocess.run", ...)`` mutate the SAME
function reference - the later patch silently overwrites the earlier one. A
single ``patch("subprocess.run", router)`` that dispatches on ``argv[0]`` is
the correct seam: it captures every subprocess call across step modules and
lets the test assert the dispatched argv directly.

``workspace.detect`` is monkeypatched to a ``WorkspaceState`` that needs
both a sync and a setup_env regenerate, so the pipeline runs both step
seams unconditionally. Doctor always runs now; an autouse fixture stubs
``run_all`` to an empty pass list for the ordering and flag tests, while the
doctor-gating tests below patch ``bakar.commands._helpers.run_all`` directly
to inject a BLOCK finding.
"""

from __future__ import annotations

import subprocess as _subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from bakar.cli import app
from bakar.diagnostics import CheckResult, Severity, Status
from bakar.workspace import WorkspaceState

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _stub_doctor_checks():
    """Doctor always runs now; stub ``run_all`` to an empty pass list so the
    ordering/flag tests stay host-independent. The doctor-gating tests below
    re-patch ``run_all`` inside their own ``with`` block, which takes precedence."""
    with patch("bakar.commands._helpers.run_all", return_value=[]):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state_needing_sync_and_setup() -> WorkspaceState:
    """A WorkspaceState that forces both the sync and the setup_env steps.

    ``needs_repo_sync`` requires ``not (repo_initialized and sources_populated)``
    so both flags are False. ``needs_setup_env`` is true when bblayers.conf is
    absent OR a full reinit is required - we set ``bblayers_present=False`` so
    the setup_env path runs even on the happy path (no reinit).
    """
    return WorkspaceState(
        bsp_family="nxp",
        repo_initialized=False,
        sources_populated=False,
        build_dir_exists=False,
        bblayers_present=False,
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
    """Stand-in for a ``subprocess.CompletedProcess`` with the given rc."""
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = ""
    cp.stderr = ""
    return cp


def _make_subprocess_router(
    *,
    fake_workspace: Path,
    record: list[str] | None = None,
    repo_rc: int = 0,
    setup_env_rc: int = 0,
) -> object:
    """Build a ``subprocess.run`` replacement that dispatches by ``argv[0]``.

    All step modules share the same stdlib ``subprocess`` module reference, so
    a single patch on ``subprocess.run`` covers every call. The router:

    * tags ``repo`` calls (NXP sync) as ``"sync"`` and returns ``repo_rc``
    * tags ``bash`` calls (NXP setup_env) as ``"setup_env"``, writes the
      bblayers.conf marker that ``setup_env.run`` post-checks, and returns
      ``setup_env_rc``
    * falls through to the real ``subprocess.run`` for anything else

    ``record`` (if supplied) collects the dispatched tags in order so callers
    can assert sync precedes setup_env.
    """
    real_run = _subprocess.run

    def router(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        first = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else None
        if first == "repo":
            if record is not None:
                record.append("sync")
            return _fake_completed(repo_rc)
        if first == "bash":
            # var-setup-release.sh writes build/conf/bblayers.conf as a side
            # effect; the post-subprocess check raises if it is missing.
            bblayers = fake_workspace / "nxp" / "build" / "conf" / "bblayers.conf"
            bblayers.parent.mkdir(parents=True, exist_ok=True)
            bblayers.write_text("BBLAYERS = ''\n")
            if record is not None:
                record.append("setup_env")
            return _fake_completed(setup_env_rc)
        return real_run(cmd, *args, **kwargs)

    return router


@pytest.fixture
def patched_detect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``workspace.detect`` to a state that runs sync and setup_env."""
    monkeypatch.setattr(
        "bakar.commands.build.detect",
        lambda cfg: _state_needing_sync_and_setup(),
    )


@pytest.fixture
def nxp_workspace(fake_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``fake_workspace`` + the var-setup-release.sh script + cwd switch.

    ``setup_env.run`` checks for ``var-setup-release.sh`` BEFORE invoking
    subprocess, so the file must exist or the step raises FileNotFoundError
    before our patch ever fires.
    """
    (fake_workspace / "nxp" / "var-setup-release.sh").write_text("#!/bin/sh\n")
    monkeypatch.chdir(fake_workspace)
    return fake_workspace


# ---------------------------------------------------------------------------
# Pipeline ordering
# ---------------------------------------------------------------------------


def test_build_pipeline_runs_sync_then_setup_env_then_kas(
    runner: CliRunner,
    nxp_workspace: Path,
    patched_detect: None,
) -> None:
    """Build pipeline must dispatch sync -> setup_env -> kas-container in order.

    A single ``subprocess.run`` router records sync vs setup_env via argv[0]
    (repo vs bash); the kas build seam is patched separately. The recorded
    order is asserted via ``.index()`` rather than equality so an additional
    repo call (e.g. ``repo init`` plus ``repo sync``) does not break the test.
    """
    call_order: list[str] = []

    def record_run_build(*_args: object, **_kwargs: object) -> int:
        call_order.append("kas_build")
        return 0

    router = _make_subprocess_router(fake_workspace=nxp_workspace, record=call_order)

    with (
        patch("subprocess.run", side_effect=router),
        patch("bakar.commands.build.step_override.apply", return_value=MagicMock()),
        patch("bakar.commands.build.step_kas.regenerate_yaml"),
        patch("bakar.commands.build.step_kas.run_build", side_effect=record_run_build),
    ):
        result = runner.invoke(
            app,
            ["build", "--manifest", "imx-6.6.52-2.2.2.xml"],
        )

    assert result.exit_code == 0, result.output
    # sync must come before setup_env, and both before kas_build.
    assert "sync" in call_order, f"expected a sync call, got {call_order!r}"
    assert "setup_env" in call_order, f"expected a setup_env call, got {call_order!r}"
    assert "kas_build" in call_order, f"expected a kas_build call, got {call_order!r}"
    assert call_order.index("sync") < call_order.index("setup_env"), call_order
    assert call_order.index("setup_env") < call_order.index("kas_build"), call_order
    assert call_order.count("kas_build") == 1, call_order


# ---------------------------------------------------------------------------
# Flag overrides
# ---------------------------------------------------------------------------


def _invoke_build_with_overrides(
    runner: CliRunner,
    nxp_workspace: Path,
    extra_args: list[str],
) -> tuple[int, list[object]]:
    """Drive ``bakar build`` with the step seams short-circuited.

    Returns ``(exit_code, captured_cfgs)``. Each captured cfg is the
    ``BuildConfig`` passed to ``step_kas.run_build`` - the assertion is then
    ``cfg.machine`` / ``cfg.distro`` / ``cfg.image`` matches the flag value.
    """
    captured: list[object] = []

    def record_run_build(ctx, *_args: object, **_kwargs: object) -> int:  # type: ignore[no-untyped-def]
        captured.append(ctx.cfg)
        return 0

    router = _make_subprocess_router(fake_workspace=nxp_workspace)

    with (
        patch("subprocess.run", side_effect=router),
        patch("bakar.commands.build.step_override.apply", return_value=MagicMock()),
        patch("bakar.commands.build.step_kas.regenerate_yaml"),
        patch("bakar.commands.build.step_kas.run_build", side_effect=record_run_build),
    ):
        result = runner.invoke(
            app,
            ["build", "--manifest", "imx-6.6.52-2.2.2.xml", *extra_args],
        )

    return result.exit_code, captured


def test_build_machine_flag_overrides_default(
    runner: CliRunner,
    nxp_workspace: Path,
    patched_detect: None,
) -> None:
    """``--machine imx95-var-dart`` reaches the BuildConfig handed to run_build."""
    exit_code, captured = _invoke_build_with_overrides(runner, nxp_workspace, ["--machine", "imx95-var-dart"])

    assert exit_code == 0
    assert len(captured) == 1
    assert captured[0].machine == "imx95-var-dart"  # type: ignore[attr-defined]


def test_build_distro_flag_overrides_default(
    runner: CliRunner,
    nxp_workspace: Path,
    patched_detect: None,
) -> None:
    """``--distro fsl-imx-wayland`` reaches the BuildConfig handed to run_build."""
    exit_code, captured = _invoke_build_with_overrides(runner, nxp_workspace, ["--distro", "fsl-imx-wayland"])

    assert exit_code == 0
    assert len(captured) == 1
    assert captured[0].distro == "fsl-imx-wayland"  # type: ignore[attr-defined]


def test_build_image_flag_overrides_default(
    runner: CliRunner,
    nxp_workspace: Path,
    patched_detect: None,
) -> None:
    """``--image core-image-base`` reaches the BuildConfig handed to run_build."""
    exit_code, captured = _invoke_build_with_overrides(runner, nxp_workspace, ["--image", "core-image-base"])

    assert exit_code == 0
    assert len(captured) == 1
    assert captured[0].image == "core-image-base"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Failure propagation
# ---------------------------------------------------------------------------


def test_build_failing_kas_step_exits_nonzero(
    runner: CliRunner,
    nxp_workspace: Path,
    patched_detect: None,
) -> None:
    """A non-zero ``step_kas.run_build`` return propagates to the CLI exit code.

    The build wrapper catches the rc and raises ``typer.Exit(code=rc)``; without
    that bridge a failing kas-container would still report success.
    """
    router = _make_subprocess_router(fake_workspace=nxp_workspace)

    with (
        patch("subprocess.run", side_effect=router),
        patch("bakar.commands.build.step_override.apply", return_value=MagicMock()),
        patch("bakar.commands.build.step_kas.regenerate_yaml"),
        patch("bakar.commands.build.step_kas.run_build", return_value=1),
    ):
        result = runner.invoke(
            app,
            ["build", "--manifest", "imx-6.6.52-2.2.2.xml"],
        )

    assert result.exit_code != 0, f"expected non-zero exit on kas build failure, got 0 with output:\n{result.output}"


# ---------------------------------------------------------------------------
# Doctor pre-flight gating
# ---------------------------------------------------------------------------


def _make_block_fail() -> list[CheckResult]:
    return [
        CheckResult(
            name="fake-blocker",
            severity=Severity.BLOCK,
            status=Status.FAIL,
            message="synthetic BLOCK failure for the test",
            fix_hint=None,
        ),
    ]


def test_build_hide_doctor_report_does_not_bypass_block_finding(
    runner: CliRunner,
    nxp_workspace: Path,
    patched_detect: None,
) -> None:
    """``--hide-doctor-report`` hides the report but never skips the checks.

    Doctor always runs; a BLOCK finding must still abort the build even when
    the report is hidden. Patches ``run_all`` to return a BLOCK and asserts the
    pipeline exits non-zero, that ``run_all`` WAS consulted, and that
    ``step_kas.run_build`` is never reached.
    """
    run_all_mock = MagicMock(return_value=_make_block_fail())
    run_build_mock = MagicMock(return_value=0)

    with (
        patch("bakar.commands._helpers.run_all", run_all_mock),
        patch("bakar.commands.build.step_kas.run_build", run_build_mock),
    ):
        result = runner.invoke(
            app,
            ["--hide-doctor-report", "build", "--manifest", "imx-6.6.52-2.2.2.xml"],
        )

    assert result.exit_code != 0, (
        f"a BLOCK finding must abort even with --hide-doctor-report, got 0 with output:\n{result.output}"
    )
    assert run_all_mock.call_count >= 1, "doctor checks must run even when the report is hidden"
    assert run_build_mock.call_count == 0, "step_kas.run_build must not run when the doctor gate blocks the build"


def test_build_without_skip_doctor_aborts_on_block_finding(
    runner: CliRunner,
    nxp_workspace: Path,
    patched_detect: None,
) -> None:
    """Without ``--skip-doctor``, a BLOCK-level FAIL must halt the pipeline.

    The doctor result returned by ``run_all`` contains a BLOCK FAIL; the build
    must raise ``typer.Exit(2)`` before reaching ``step_kas.run_build``.
    Asserting ``run_build.call_count == 0`` proves the gate fired, not just
    that something later exited non-zero.
    """
    run_build_mock = MagicMock(return_value=0)

    with (
        patch("bakar.commands._helpers.run_all", return_value=_make_block_fail()),
        patch("bakar.commands.build.step_kas.run_build", run_build_mock),
    ):
        result = runner.invoke(
            app,
            ["build", "--manifest", "imx-6.6.52-2.2.2.xml"],
        )

    assert result.exit_code != 0, (
        f"expected non-zero exit when a BLOCK doctor finding is present, got 0 with output:\n{result.output}"
    )
    assert run_build_mock.call_count == 0, (
        "expected step_kas.run_build NOT to be called when the doctor gate "
        f"blocks the build, got {run_build_mock.call_count} call(s)"
    )
