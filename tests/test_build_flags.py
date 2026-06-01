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

from typing import TYPE_CHECKING

import pytest

from bakar.cli import app
from bakar.config import BuildConfig
from bakar.observability import RunLogger
from bakar.steps import kas_build

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


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
    """Build a cfg plus a kas YAML inside bsp_root and an overlay source.

    ``run_build`` resolves the kas YAML relative to ``cfg.bsp_root`` and
    copies the overlay into ``bsp_root/.bakar/overlays/``, so both files
    must exist on disk for the dry-run path to reach cmd assembly.
    """
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
    assert "--keep-going" in result.output
    assert "-k" in result.output


# ---------------------------------------------------------------------------
# (e) sync and gen-kas --help each expose --dry-run and -n
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["sync", "gen-kas"])
def test_command_help_lists_dry_run_flag(command: str) -> None:
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output
    assert "--dry-run" in result.output
    assert "-n" in result.output
