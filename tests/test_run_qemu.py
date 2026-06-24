"""Tests for bakar.steps.run_qemu and the `bakar run` CLI command."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from bakar.config import BuildConfig
from bakar.steps.run_qemu import _find_meta_avocado_dir, resolve_run_script

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meta_avocado_cfg(sources: Path) -> BuildConfig:
    yaml_path = sources / "meta-avocado" / "kas" / "machine" / "qemux86-64.yml"
    return BuildConfig(
        workspace=sources,
        bsp_family="generic",
        machine="generic",
        distro="generic",
        image="generic",
        manifest="",
        repo_url="",
        repo_branch="",
        kas_container_image="kasproject/kas:latest",
        kas_yaml_override=yaml_path.resolve(),
    )


def _make_kas_yaml(sources: Path, stem: str = "qemux86-64") -> Path:
    kas_dir = sources / "meta-avocado" / "kas" / "machine"
    kas_dir.mkdir(parents=True, exist_ok=True)
    p = kas_dir / f"{stem}.yml"
    p.write_text(f"machine: avocado-{stem}\n")
    return p


# ---------------------------------------------------------------------------
# Unit: _find_meta_avocado_dir
# ---------------------------------------------------------------------------


def test_find_meta_avocado_dir(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    kas_yaml = _make_kas_yaml(sources)
    result = _find_meta_avocado_dir(kas_yaml)
    assert result == (sources / "meta-avocado").resolve()


def test_find_meta_avocado_dir_raises_outside_repo(tmp_path: Path) -> None:
    yaml = tmp_path / "not-avocado" / "kas" / "machine" / "qemux86-64.yml"
    yaml.parent.mkdir(parents=True)
    yaml.write_text("machine: avocado-qemux86-64\n")
    with pytest.raises(ValueError, match="not inside a meta-avocado checkout"):
        _find_meta_avocado_dir(yaml)


# ---------------------------------------------------------------------------
# Unit: resolve_run_script
# ---------------------------------------------------------------------------


def test_resolve_run_script_with_swtpm(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    kas_yaml = _make_kas_yaml(sources)
    script = resolve_run_script(kas_yaml, swtpm=True)
    expected = (sources / "meta-avocado").resolve() / "meta-avocado-qemu" / "scripts" / "run-qemux86-64-swtpm"
    assert script == expected


def test_resolve_run_script_without_swtpm(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    kas_yaml = _make_kas_yaml(sources)
    script = resolve_run_script(kas_yaml, swtpm=False)
    expected = (sources / "meta-avocado").resolve() / "meta-avocado-qemu" / "scripts" / "run-qemux86-64"
    assert script == expected


def test_resolve_run_script_arm64(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    kas_yaml = _make_kas_yaml(sources, stem="qemuarm64")
    script = resolve_run_script(kas_yaml, swtpm=False)
    assert script.name == "run-qemuarm64"


# ---------------------------------------------------------------------------
# Unit: run_qemu step
# ---------------------------------------------------------------------------


def test_run_qemu_raises_when_script_missing(tmp_path: Path) -> None:
    from bakar.steps.run_qemu import run_qemu

    sources = tmp_path / "sources"
    kas_yaml = _make_kas_yaml(sources)
    cfg = _meta_avocado_cfg(sources)
    (cfg.bsp_root / "build").mkdir(parents=True)

    log = MagicMock()
    with pytest.raises(FileNotFoundError, match="Run script not found"):
        run_qemu(cfg, log, swtpm=True, kas_yaml=kas_yaml)


def test_run_qemu_raises_when_build_missing(tmp_path: Path) -> None:
    from bakar.steps.run_qemu import run_qemu

    sources = tmp_path / "sources"
    kas_yaml = _make_kas_yaml(sources)
    cfg = _meta_avocado_cfg(sources)
    # Create script but no build dir
    script_dir = sources / "meta-avocado" / "meta-avocado-qemu" / "scripts"
    script_dir.mkdir(parents=True)
    (script_dir / "run-qemux86-64-swtpm").write_text("#!/bin/bash\n")

    log = MagicMock()
    with pytest.raises(RuntimeError, match="Build output not found"):
        run_qemu(cfg, log, swtpm=True, kas_yaml=kas_yaml)


def test_run_qemu_restores_termios_on_tty(tmp_path: Path) -> None:
    from bakar.steps.run_qemu import run_qemu

    sources = tmp_path / "sources"
    kas_yaml = _make_kas_yaml(sources)
    cfg = _meta_avocado_cfg(sources)

    script_dir = sources / "meta-avocado" / "meta-avocado-qemu" / "scripts"
    script_dir.mkdir(parents=True, exist_ok=True)
    (script_dir / "run-qemux86-64-swtpm").write_text("#!/bin/bash\n")
    (cfg.bsp_root / "build").mkdir(parents=True)

    log = MagicMock()
    mock_proc = MagicMock()
    mock_proc.wait.return_value = 0
    sentinel_attrs = object()

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("sys.stdin") as mock_stdin,
        patch("bakar.steps.run_qemu.termios") as mock_termios,
    ):
        mock_stdin.isatty.return_value = True
        mock_stdin.fileno.return_value = 0
        mock_termios.tcgetattr.return_value = sentinel_attrs

        run_qemu(cfg, log, swtpm=True, kas_yaml=kas_yaml)

    mock_termios.tcgetattr.assert_called_once_with(0)
    mock_termios.tcsetattr.assert_called_once_with(0, mock_termios.TCSADRAIN, sentinel_attrs)


def test_run_qemu_invokes_script_with_bsp_root_cwd(tmp_path: Path) -> None:
    from bakar.steps.run_qemu import run_qemu

    sources = tmp_path / "sources"
    kas_yaml = _make_kas_yaml(sources)
    cfg = _meta_avocado_cfg(sources)

    script_dir = sources / "meta-avocado" / "meta-avocado-qemu" / "scripts"
    script_dir.mkdir(parents=True)
    script = script_dir / "run-qemux86-64-swtpm"
    script.write_text("#!/bin/bash\n")
    (cfg.bsp_root / "build").mkdir(parents=True)

    log = MagicMock()
    mock_proc = MagicMock()
    mock_proc.wait.return_value = 0

    with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
        rc = run_qemu(cfg, log, swtpm=True, kas_yaml=kas_yaml)

    assert rc == 0
    mock_popen.assert_called_once_with(
        ["bash", str(script)],
        cwd=cfg.bsp_root,
    )


# ---------------------------------------------------------------------------
# CLI integration: bakar run
# ---------------------------------------------------------------------------


def test_cli_run_non_meta_avocado_exits_2(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    import bakar.commands._app as cli_module
    from bakar.cli import app

    cli_module._VENDORS = None
    runner = CliRunner()

    # Create a generic (non-meta-avocado) YAML
    yaml_dir = tmp_path / "bsp" / "kas" / "machine"
    yaml_dir.mkdir(parents=True)
    kas_yaml = yaml_dir / "imx8.yml"
    kas_yaml.write_text("machine: imx8mp-var-dart\n")

    with patch("bakar.commands._app.load_vendors", return_value=[]):
        result = runner.invoke(app, ["run", str(kas_yaml)])

    assert result.exit_code == 2


def test_cli_run_missing_script_exits_2(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    import bakar.commands._app as cli_module
    from bakar.cli import app

    cli_module._VENDORS = None
    runner = CliRunner()

    sources = tmp_path / "sources"
    kas_yaml = _make_kas_yaml(sources)
    # Provide build dir but no run script
    cfg = _meta_avocado_cfg(sources)
    (cfg.bsp_root / "build").mkdir(parents=True)

    with patch("bakar.commands._app.load_vendors", return_value=[]):
        result = runner.invoke(app, ["run", str(kas_yaml)])

    assert result.exit_code == 2
    assert "Run script not found" in result.output
