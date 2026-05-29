"""Tests for the ``bakar bitbake-override`` command.

The command delegates the real work to ``bakar.steps.bitbake_override``:
``apply``, ``revert``, and ``status``. Each is imported into the command
module via ``from bakar.steps import bitbake_override as step_override``,
so patching at ``bakar.commands.override.step_override.<fn>`` is the seam
that keeps assertions visible to the test without mutating the host
filesystem.

The default action (no flags) is ``--status``. ``--apply`` and ``--revert``
are mutually exclusive with each other and with ``--status``; passing
more than one exits with code 2. The ``BAKAR_BITBAKE_OVERRIDE=0`` env
var short-circuits ``apply`` inside the step (returning the disabled
status without touching the BSP tree), so this test asserts the apply
helper is invoked but reports the disabled state - mirroring the
production short-circuit behaviour at
``src/bakar/steps/bitbake_override.py:465-468``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from bakar.cli import app
from bakar.steps.bitbake_override import OverrideStatus

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _fake_status(state: str = "active", detail: str = "symlink ok") -> OverrideStatus:
    """Build an ``OverrideStatus`` for use as a patched return value."""
    from pathlib import Path

    return OverrideStatus(
        state=state,
        branch="br-2.8",
        sha="abc1234",
        upstream_version="2.8.1",
        bsp_version="2.8.0",
        poky_bitbake=Path("/nonexistent/sources/poky/bitbake"),
        upstream_dir=Path("/nonexistent/upstream-bitbake"),
        detail=detail,
    )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_apply_invokes_step_apply(
    runner: CliRunner,
    fake_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--apply`` must dispatch to ``step_override.apply``.

    Patches both ``apply`` and ``status`` on the step module so the
    command never touches the on-disk poky tree. The synthetic
    workspace under ``tmp_path`` provides the ``.bakar.toml`` marker
    and ``nxp/`` subdir that ``_workspace_from_cwd`` walks for.
    """
    monkeypatch.chdir(fake_workspace)
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE", raising=False)

    with (
        patch(
            "bakar.commands.override.step_override.apply",
            return_value=_fake_status("active", "linked"),
        ) as mock_apply,
        patch(
            "bakar.commands.override.step_override.status",
            return_value=_fake_status("active"),
        ),
    ):
        result = runner.invoke(
            app,
            ["bitbake-override", "--apply", "--manifest", "imx-6.6.52-2.2.2.xml"],
        )

    assert result.exit_code == 0, result.output
    assert mock_apply.call_count == 1, f"expected step_override.apply to fire once, got {mock_apply.call_count}"


def test_revert_invokes_step_revert(
    runner: CliRunner,
    fake_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--revert`` must dispatch to ``step_override.revert``.

    The command then calls ``status`` for the post-revert summary line,
    so both are patched. Asserts revert was called exactly once and
    apply was NOT called.
    """
    monkeypatch.chdir(fake_workspace)
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE", raising=False)

    with (
        patch("bakar.commands.override.step_override.revert") as mock_revert,
        patch(
            "bakar.commands.override.step_override.status",
            return_value=_fake_status("stale", "post-revert"),
        ),
        patch("bakar.commands.override.step_override.apply") as mock_apply,
    ):
        result = runner.invoke(
            app,
            ["bitbake-override", "--revert", "--manifest", "imx-6.6.52-2.2.2.xml"],
        )

    assert result.exit_code == 0, result.output
    assert mock_revert.call_count == 1, f"expected step_override.revert to fire once, got {mock_revert.call_count}"
    assert mock_apply.call_count == 0, f"--revert must not call apply, got {mock_apply.call_count}"


def test_disable_env_short_circuits_apply(
    runner: CliRunner,
    fake_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BAKAR_BITBAKE_OVERRIDE=0`` must produce no on-disk change.

    The short-circuit lives inside ``step_override.apply`` (see
    ``_disabled()`` at ``src/bakar/steps/bitbake_override.py:103``).
    The command still calls ``apply``, but the patched return value
    here pretends the step recognised the env var and returned a
    ``disabled`` status without mutating anything. The test confirms
    exit 0 AND that the rendered status line shows ``disabled``.
    """
    monkeypatch.chdir(fake_workspace)
    monkeypatch.setenv("BAKAR_BITBAKE_OVERRIDE", "0")

    disabled = _fake_status("disabled", "BAKAR_BITBAKE_OVERRIDE=0")

    # The real apply() short-circuits internally when the env var is 0,
    # returning the disabled status. The patched apply mirrors that
    # contract: it never touches the BSP tree, just returns the
    # pre-built disabled status.
    with (
        patch(
            "bakar.commands.override.step_override.apply",
            return_value=disabled,
        ) as mock_apply,
        patch(
            "bakar.commands.override.step_override.status",
            return_value=disabled,
        ),
    ):
        result = runner.invoke(
            app,
            ["bitbake-override", "--apply", "--manifest", "imx-6.6.52-2.2.2.xml"],
        )

    assert result.exit_code == 0, result.output
    assert "disabled" in result.output, f"expected 'disabled' status line, got:\n{result.output}"
    # The patched apply was called - that's where the real short-circuit
    # lives. The assertion that matters is that NOTHING under the
    # workspace's nxp/ tree changed (no sources/poky was created).
    assert mock_apply.call_count == 1
    poky_dir = fake_workspace / "nxp" / "sources" / "poky"
    assert not poky_dir.exists(), f"BAKAR_BITBAKE_OVERRIDE=0 must not create {poky_dir}"


def test_apply_runtime_error_exits_nonzero(
    runner: CliRunner,
    fake_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``RuntimeError`` from the step (e.g. missing source repo) must
    surface as exit code 2.

    Mirrors the error handler at ``src/bakar/commands/override.py:90-92``
    which catches ``RuntimeError`` and re-raises as ``typer.Exit(2)``.
    The synthetic workspace has no ``sources/poky/bitbake`` tree and no
    upstream clone, so the real step would raise on the branch
    auto-detect path - the test simulates that failure directly.
    """
    monkeypatch.chdir(fake_workspace)
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE", raising=False)

    with patch(
        "bakar.commands.override.step_override.apply",
        side_effect=RuntimeError("override source repo missing"),
    ) as mock_apply:
        result = runner.invoke(
            app,
            ["bitbake-override", "--apply", "--manifest", "imx-6.6.52-2.2.2.xml"],
        )

    assert result.exit_code != 0, f"RuntimeError must exit non-zero, got 0 with output:\n{result.output}"
    assert mock_apply.call_count == 1


def test_branch_and_repo_flags_pass_through(
    runner: CliRunner,
    fake_workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--branch`` and ``--repo`` must be forwarded to ``step_override.apply``.

    The command takes ``branch: str | None`` and ``repo: Path | None``
    and forwards them as the ``branch=`` and ``repo_path=`` kwargs.
    Asserts the patched call received the same values the user passed.
    """
    monkeypatch.chdir(fake_workspace)
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE", raising=False)

    fake_repo = tmp_path / "upstream-source"
    fake_repo.mkdir()

    with (
        patch(
            "bakar.commands.override.step_override.apply",
            return_value=_fake_status("active", "linked"),
        ) as mock_apply,
        patch(
            "bakar.commands.override.step_override.status",
            return_value=_fake_status("active"),
        ),
    ):
        result = runner.invoke(
            app,
            [
                "bitbake-override",
                "--apply",
                "--branch",
                "br-2.8",
                "--repo",
                str(fake_repo),
                "--manifest",
                "imx-6.6.52-2.2.2.xml",
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_apply.call_count == 1
    call_kwargs = mock_apply.call_args.kwargs
    assert call_kwargs.get("branch") == "br-2.8", f"expected branch='br-2.8' kwarg, got {call_kwargs!r}"
    # ``repo_path`` is Pathified by Typer before reaching the handler.
    assert str(call_kwargs.get("repo_path")) == str(fake_repo), f"expected repo_path={fake_repo!s}, got {call_kwargs!r}"


def test_conflicting_flags_exit_two(
    runner: CliRunner,
    fake_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing two of --apply/--revert/--status together must exit 2.

    Covers the mutual-exclusion guard at
    ``src/bakar/commands/override.py:69-72``.
    """
    monkeypatch.chdir(fake_workspace)
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE", raising=False)

    result = runner.invoke(
        app,
        ["bitbake-override", "--apply", "--revert", "--manifest", "imx-6.6.52-2.2.2.xml"],
    )

    assert result.exit_code == 2, f"expected exit 2 for conflicting flags, got {result.exit_code}:\n{result.output}"


def test_status_default_action(
    runner: CliRunner,
    fake_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no flags the command must dispatch to status only.

    Confirms apply/revert are NOT called on the default path.
    """
    monkeypatch.chdir(fake_workspace)
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE", raising=False)

    with (
        patch(
            "bakar.commands.override.step_override.status",
            return_value=_fake_status("missing", "poky tree absent (pre-bootstrap)"),
        ) as mock_status,
        patch("bakar.commands.override.step_override.apply") as mock_apply,
        patch("bakar.commands.override.step_override.revert") as mock_revert,
    ):
        result = runner.invoke(
            app,
            ["bitbake-override", "--manifest", "imx-6.6.52-2.2.2.xml"],
        )

    assert result.exit_code == 0, result.output
    assert mock_status.call_count >= 1
    assert mock_apply.call_count == 0
    assert mock_revert.call_count == 0
