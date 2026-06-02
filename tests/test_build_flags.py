"""Unit tests for the keep-going and dry-run build ergonomics.

Two mechanics are exercised here:

- keep-going: ``run_build`` appends ``["--", "-k"]`` to the assembled
  kas-container command after the kas YAML arg so bitbake runs with
  ``-k`` (continue on error).
- dry-run: ``run_build`` prints the resolved command and exits 0
  *before* spawning any subprocess, PTY, or sampler thread.

Driving the full ``run_build`` non-dry-run path is impractical (it
allocates a PTY and blocks a pump thread on ``os.read``), so the
``["--", "-k"]`` ordering is asserted through the dry-run ``command:``
output, which emits the byte-identical assembled command (design D6).
Test (a) additionally monkeypatches ``subprocess.Popen`` to a sentinel
that fails if reached, proving the dry-run gate is in place.

The ``--help`` tests confirm the CLI options are wired so the flags are
discoverable.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from bakar.cli import app
from bakar.config import BuildConfig
from bakar.observability import RunLogger
from bakar.steps import kas_build
from bakar.steps.kas_build import dry_run_preview_lines

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI SGR escapes so help-text assertions survive colored output.

    Typer/rich colorize ``--help`` when the captured stream looks like a
    terminal (as in CI), inserting escape codes mid-token so a plain
    ``"--dry-run" in output`` check fails even though the flag is present.
    """
    return _ANSI_RE.sub("", text)


def _make_cfg(workspace: Path) -> BuildConfig:
    """Construct a minimal NXP BuildConfig rooted at ``workspace``."""
    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",
        machine="imx95-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.12.49-2.2.0.xml",
        repo_url="https://example.invalid/none.git",
        repo_branch="walnascar",
        container_image="jetm/kas-build-env:latest",
    )


def _prepare_workspace(tmp_path: Path) -> tuple[BuildConfig, Path, Path]:
    """Build a cfg plus a kas YAML inside bsp_root and an overlay source."""
    cfg = _make_cfg(tmp_path)
    cfg.bsp_root.mkdir(parents=True, exist_ok=True)
    kas_yaml = cfg.bsp_root / "build.yml"
    kas_yaml.write_text("header: {}\n", encoding="utf-8")
    overlay = tmp_path / "bakar-tuning-nxp.yml"
    overlay.write_text("header: {}\n", encoding="utf-8")
    return cfg, kas_yaml, overlay


def _run_dry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    keep_going: bool,
) -> int:
    """Run ``run_build`` in dry-run mode, failing if a subprocess spawns."""
    cfg, kas_yaml, overlay = _prepare_workspace(tmp_path)

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("subprocess.Popen must not be called on dry-run")

    monkeypatch.setattr(kas_build.subprocess, "Popen", _boom)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        ctx = kas_build.KasBuildContext(
            cfg=cfg,
            log=log,
            kas_yaml=kas_yaml,
            overlay_source=overlay,
            keep_going=keep_going,
            dry_run=True,
        )
        return kas_build.run_build(ctx)


def _command_line(captured_out: str) -> str:
    """Extract the single ``command:`` line from dry-run stdout."""
    lines = [ln for ln in captured_out.splitlines() if ln.startswith("command:")]
    assert len(lines) == 1, f"expected one command: line, got {lines!r}"
    return lines[0]


# ---------------------------------------------------------------------------
# (a) keep-going appends "-- -k" after the kas arg; Popen is never reached
# ---------------------------------------------------------------------------


def test_keep_going_appends_dash_k_after_kas_arg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = _run_dry(tmp_path, monkeypatch, keep_going=True)
    assert rc == 0

    out = capsys.readouterr().out
    command = _command_line(out)
    # "build <kas_arg> -- -k": the passthrough suffix sits after the YAML arg.
    assert command.rstrip().endswith("-- -k"), command
    build_idx = command.index(" build ")
    assert command.index("-- -k") > build_idx, command


# ---------------------------------------------------------------------------
# (b) dry-run returns 0, never calls Popen, and prints a command: line
# ---------------------------------------------------------------------------


def test_dry_run_skips_popen_and_prints_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = _run_dry(tmp_path, monkeypatch, keep_going=False)
    assert rc == 0

    out = capsys.readouterr().out
    command = _command_line(out)
    assert " build " in command, command
    # keep_going was False, so no bitbake passthrough.
    assert "-- -k" not in command, command


# ---------------------------------------------------------------------------
# (c) keep_going + dry_run: the printed command carries the -- -k suffix
# ---------------------------------------------------------------------------


def test_keep_going_dry_run_output_contains_dash_k(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = _run_dry(tmp_path, monkeypatch, keep_going=True)
    assert rc == 0

    out = capsys.readouterr().out
    assert "-- -k" in out, out


# ---------------------------------------------------------------------------
# (d) bakar build --help exposes --keep-going and -k
# ---------------------------------------------------------------------------


def test_build_help_lists_keep_going_flag() -> None:
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["build", "--help"])
    assert result.exit_code == 0, result.output
    # Strip ANSI: CI renders the help colored, splitting the flag token with
    # escape codes so a raw substring check fails.
    out = _plain(result.output)
    assert "--keep-going" in out, out
    # Typer renders "  --keep-going   -k   <help>"; match the two together so
    # a lone "-k" substring (e.g. "gen-kas", "Pass -k to bitbake") can't fool us.
    assert re.search(r"--keep-going\s+-k", out), out


# ---------------------------------------------------------------------------
# (d2) dry_run_preview_lines directly verifies keep-going cmd assembly
# ---------------------------------------------------------------------------


def test_dry_run_preview_lines_keep_going_appends_dash_k(tmp_path: Path) -> None:
    """dry_run_preview_lines includes '-- -k' after the kas arg when keep_going=True.

    This covers the non-dry-run cmd assembly path indirectly: the same
    cmd-building code runs for both paths; a regression that dropped '-- -k'
    would fail here without needing to mock the PTY/sampler infrastructure.
    """
    cfg, kas_yaml, overlay = _prepare_workspace(tmp_path)
    lines = dry_run_preview_lines(cfg, kas_yaml, overlay, keep_going=True)
    command_line = next(ln for ln in lines if ln.startswith("command:"))
    assert "-- -k" in command_line, command_line
    # -- -k must come after the kas build arg, not before it
    assert command_line.index("build ") < command_line.index("-- -k"), command_line


def test_dry_run_preview_lines_no_keep_going(tmp_path: Path) -> None:
    """dry_run_preview_lines omits '-- -k' when keep_going=False."""
    cfg, kas_yaml, overlay = _prepare_workspace(tmp_path)
    lines = dry_run_preview_lines(cfg, kas_yaml, overlay, keep_going=False)
    command_line = next(ln for ln in lines if ln.startswith("command:"))
    assert "-- -k" not in command_line, command_line


def test_dry_run_preview_lines_extra_overlays_included(tmp_path: Path) -> None:
    """extra_overlays appear in the kas_arg of the preview command."""
    cfg, kas_yaml, overlay = _prepare_workspace(tmp_path)
    extra = tmp_path / "extra.yml"
    extra.write_text("header: {}\n", encoding="utf-8")
    lines = dry_run_preview_lines(cfg, kas_yaml, overlay, [extra])
    command_line = next(ln for ln in lines if ln.startswith("command:"))
    assert "extra.yml" in command_line, command_line


# ---------------------------------------------------------------------------
# (e) sync and gen-kas --help each expose --dry-run and -n
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["sync", "gen-kas"])
def test_command_help_lists_dry_run_flag(command: str) -> None:
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output
    # Strip ANSI: CI colorizes help output, splitting "--dry-run" with escapes.
    out = _plain(result.output)
    assert "--dry-run" in out, out
    assert re.search(r"--dry-run\s+-n", out), out
