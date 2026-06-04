"""Integration tests for the preset load-to-build-dispatch path.

Exercises the full pipeline from loading a ``config.toml`` fixture into
``PresetEntry`` objects, through the ``_state._PRESETS`` startup wiring,
to ``build --preset <name> --dry-run`` dispatch - without invoking a
kas-container.

Run explicitly with:

    uv run pytest tests/integration/test_preset_integration.py -v --no-cov
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import bakar.commands._app as _state
from bakar.cli import app
from bakar.commands import build as build_cmd
from bakar.preset_config import PresetEntry, load_presets
from bakar.user_config import UserConfig

if TYPE_CHECKING:
    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Minimal workspace with a ``.bakar.toml`` marker; chdir into it."""
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def kas_yaml(tmp_path: Path) -> Path:
    """A minimal kas YAML on disk that the bbsetup preset can reference."""
    p = tmp_path / "qemux86-64.yml"
    p.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    return p


@pytest.fixture
def config_toml(tmp_path: Path, kas_yaml: Path) -> Path:
    """Write a ``config.toml`` with one nxp and one bbsetup preset."""
    content = f"""\
[[presets]]
name = "imx8mp-scarthgap"
family = "nxp"
machine = "imx8mp-var-dart"
distro = "fsl-imx-xwayland"
image = "core-image-minimal"
manifest = "imx-6.6.52-2.2.2.xml"
branch = "scarthgap"

[[presets]]
name = "avocado-qemux86-64"
family = "bbsetup"
machine = "qemux86-64"
image = "avocado-os"
kas_yaml = "{kas_yaml}"
"""
    path = tmp_path / "config.toml"
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_user_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent real user config from being loaded."""
    monkeypatch.setattr(_state, "_load_user_config_safe", lambda: UserConfig(hashserv=False))


def _plant_presets(monkeypatch: pytest.MonkeyPatch, presets: list[PresetEntry]) -> None:
    """Plant a preset list into _state and prevent the startup loader from overwriting it."""

    def fake_load_presets_safe() -> None:
        _state._PRESETS = presets  # type: ignore[assignment]

    monkeypatch.setattr(_state, "_load_presets_safe", fake_load_presets_safe)
    monkeypatch.setattr(_state, "_PRESETS", presets, raising=False)


def _stub_build_infra(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub dispatchers, detect, and run_build to prevent container calls.

    Returns a capture dict with keys ``bsp_dispatch``, ``yaml_dispatch``,
    and ``run_build`` populated by the fakes.
    """
    from bakar.bsp_model import get_model
    from bakar.workspace import WorkspaceState

    captured: dict = {"bsp_dispatch": [], "yaml_dispatch": [], "run_build": []}

    def fake_dispatch_bsp(manifest):  # type: ignore[no-untyped-def]
        captured["bsp_dispatch"].append(manifest)
        return ("nxp", get_model("nxp"))

    def fake_dispatch_yaml(yaml_path):  # type: ignore[no-untyped-def]
        captured["yaml_dispatch"].append(yaml_path)
        return ("generic", None)

    def fake_run_build(ctx, *, extra_overlays=None):  # type: ignore[no-untyped-def]
        captured["run_build"].append(ctx)
        return 0

    def fake_detect(cfg):  # type: ignore[no-untyped-def]
        return WorkspaceState(
            bsp_family="nxp",
            repo_initialized=True,
            sources_populated=True,
            build_dir_exists=True,
            bblayers_present=True,
            kas_yaml_present=True,
            forks_linux_imx=False,
            cache_dirs_ok=True,
            repo_manifest_include=cfg.manifest,
            repo_manifests_branch=cfg.repo_branch,
            requested_manifest=cfg.manifest,
            requested_branch=cfg.repo_branch,
        )

    monkeypatch.setattr(build_cmd, "_dispatch_bsp", fake_dispatch_bsp)
    monkeypatch.setattr(build_cmd, "_dispatch_from_yaml", fake_dispatch_yaml)
    monkeypatch.setattr(build_cmd.step_kas, "run_build", fake_run_build)
    monkeypatch.setattr("bakar.commands.build.detect", fake_detect)
    return captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_nxp_preset_dispatches_via_bsp(
    runner: _CliRunner,
    workspace: Path,
    config_toml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full path: load config.toml -> nxp PresetEntry -> build --preset -> _dispatch_bsp.

    Loads a real PresetEntry from the fixture config.toml (no monkeypatching
    of load_presets itself), plants the result in _state, and asserts that
    ``_dispatch_bsp`` is called with the preset's manifest and that the
    resolved workspace path embeds the manifest version string.
    """
    _stub_user_config(monkeypatch)

    # Load from the real fixture TOML - this is the "real PresetEntry" assertion.
    presets = load_presets(config_path=config_toml)
    nxp_presets = [p for p in presets if p.family == "nxp"]
    assert nxp_presets, f"expected at least one nxp preset in fixture, got {presets!r}"

    _plant_presets(monkeypatch, presets)
    captured = _stub_build_infra(monkeypatch)

    # Capture workspace passed to resolve() to verify the output path prefix.
    resolved_workspaces: list = []
    original_resolve = build_cmd.resolve

    def capturing_resolve(**kwargs):  # type: ignore[no-untyped-def]
        resolved_workspaces.append(kwargs.get("workspace"))
        return original_resolve(**kwargs)

    monkeypatch.setattr(build_cmd, "resolve", capturing_resolve)

    result = runner.invoke(app, ["build", "--preset", "imx8mp-scarthgap", "--skip-doctor", "--dry-run"])

    assert result.exit_code == 0, f"unexpected exit {result.exit_code}; output:\n{result.output}"

    # Correct dispatch function called.
    assert captured["bsp_dispatch"] == ["imx-6.6.52-2.2.2.xml"], (
        f"expected _dispatch_bsp with preset manifest, got {captured['bsp_dispatch']!r}"
    )
    assert captured["yaml_dispatch"] == [], (
        f"_dispatch_from_yaml must not be called for an nxp preset, got {captured['yaml_dispatch']!r}"
    )

    # Output path contains the manifest version.
    assert resolved_workspaces, "resolve() was not called"
    ws_path = str(resolved_workspaces[0])
    assert "6.6.52-2.2.2" in ws_path, f"expected manifest version '6.6.52-2.2.2' in workspace path, got {ws_path!r}"


def test_bbsetup_preset_dispatches_via_yaml(
    runner: _CliRunner,
    workspace: Path,
    config_toml: Path,
    monkeypatch: pytest.MonkeyPatch,
    kas_yaml: Path,
) -> None:
    """Full path: load config.toml -> bbsetup PresetEntry -> build --preset -> _dispatch_from_yaml.

    Loads a real PresetEntry from the fixture config.toml, plants it in
    _state, and asserts that ``_dispatch_from_yaml`` is called with the
    preset's kas_yaml path while ``_dispatch_bsp`` is never called.
    """
    _stub_user_config(monkeypatch)

    presets = load_presets(config_path=config_toml)
    bbsetup_presets = [p for p in presets if p.family == "bbsetup"]
    assert bbsetup_presets, f"expected at least one bbsetup preset in fixture, got {presets!r}"

    _plant_presets(monkeypatch, presets)
    captured = _stub_build_infra(monkeypatch)

    result = runner.invoke(app, ["build", "--preset", "avocado-qemux86-64", "--skip-doctor", "--dry-run"])

    assert result.exit_code == 0, f"unexpected exit {result.exit_code}; output:\n{result.output}"

    # Correct dispatch function called.
    assert captured["yaml_dispatch"], "_dispatch_from_yaml was not called for a bbsetup preset"
    assert captured["bsp_dispatch"] == [], (
        f"_dispatch_bsp must not be called for a bbsetup preset, got {captured['bsp_dispatch']!r}"
    )

    # The dispatched YAML path matches the preset's kas_yaml.
    dispatched_path = captured["yaml_dispatch"][0]
    assert Path(dispatched_path) == kas_yaml, f"expected dispatched YAML {kas_yaml}, got {dispatched_path!r}"
