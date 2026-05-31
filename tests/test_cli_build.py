"""Tests for the ``bakar build`` auto-overlay behavior.

Cover ``_hashequiv_extra_overlays`` as a unit and the build CLI's
deduplication of the hashequiv overlay against a user-supplied
``main.yml:overlay.yml`` argument. The dedup case is load-bearing:
without it, kas would receive the overlay twice and emit duplicate
``BB_SIGNATURE_HANDLER`` assignments.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bakar.commands._app as _state
from bakar.cli import app
from bakar.commands import build as build_cmd
from bakar.commands._helpers import _hashequiv_extra_overlays, _overlay_dir
from bakar.config import BuildConfig
from bakar.user_config import UserConfig

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


def _make_cfg(workspace: Path, *, use_hashequiv: bool = False) -> BuildConfig:
    return BuildConfig(
        workspace=workspace,
        bsp_family="generic",
        machine="generic",
        distro="generic",
        image="generic",
        manifest="",
        repo_url="https://example.invalid/repo.git",
        repo_branch="",
        container_image="jetm/kas-build-env:latest",
        use_hashequiv=use_hashequiv,
    )


# ---------------------------------------------------------------------------
# Helper-only tests (no CLI)
# ---------------------------------------------------------------------------


def test_hashequiv_overlay_auto_appended_when_use_hashequiv_true(tmp_path: Path) -> None:
    """Helper returns the hashequiv overlay path when use_hashequiv=True.

    Pins the filename exactly so a rename of the shipped overlay (or a
    bug that returns the generic overlay) fails this test loudly.
    """
    cfg = _make_cfg(tmp_path, use_hashequiv=True)

    overlays = _hashequiv_extra_overlays(cfg)

    assert len(overlays) == 1
    assert overlays[0].name == "bakar-tuning-hashequiv.yml"


def test_hashequiv_overlay_empty_when_use_hashequiv_false(tmp_path: Path) -> None:
    """Helper returns an empty list when use_hashequiv=False (default opt-out)."""
    cfg = _make_cfg(tmp_path, use_hashequiv=False)

    assert _hashequiv_extra_overlays(cfg) == []


# ---------------------------------------------------------------------------
# CLI dedup tests
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp workspace with a ``.bakar.toml`` marker; chdir into it."""
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def generic_yaml(tmp_path: Path) -> Path:
    """Write a minimal generic kas YAML (qemu machine, no NXP/TI markers)."""
    yaml_path = tmp_path / "my.yml"
    yaml_path.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    return yaml_path


def _stub_user_config_loader(monkeypatch: pytest.MonkeyPatch, *, hashserv: bool) -> None:
    """Force the Typer callback to return a UserConfig with the chosen hashserv flag.

    The build callback writes ``_state._USER_CONFIG = _load_user_config_safe()``
    on every invocation, so monkeypatching the loader is the only stable way
    to plant a fixed value before the CLI reaches the build subcommand.
    """
    monkeypatch.setattr(_state, "_load_user_config_safe", lambda: UserConfig(hashserv=hashserv))


def test_hashequiv_overlay_deduped_when_user_passes_it(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-passed hashequiv overlay must not be duplicated when hashserv=true.

    Asserts the recorded ``extra_overlays`` contains the hashequiv overlay
    EXACTLY ONCE - a >=1 assertion would let the dedup-regression bug pass.
    """
    _stub_user_config_loader(monkeypatch, hashserv=True)

    recorded: list[list] = []

    def fake_run_build(ctx, *, extra_overlays=None):  # type: ignore[no-untyped-def]
        recorded.append(list(extra_overlays or []))
        return 0

    monkeypatch.setattr(build_cmd.step_kas, "run_build", fake_run_build)

    overlay_path = _overlay_dir() / "bakar-tuning-hashequiv.yml"
    arg = f"{generic_yaml}:{overlay_path}"

    result = runner.invoke(app, ["build", arg, "--skip-doctor"])

    assert result.exit_code == 0, result.output
    assert len(recorded) == 1, f"expected exactly one run_build call, got {recorded!r}"

    hashequiv_entries = [p for p in recorded[0] if p.name == "bakar-tuning-hashequiv.yml"]
    assert len(hashequiv_entries) == 1, f"expected hashequiv overlay to appear EXACTLY once, got {hashequiv_entries!r}"


def test_hashequiv_overlay_not_appended_when_use_hashequiv_false(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When hashserv=false, the auto-append branch is skipped entirely.

    With no user-supplied overlay suffix the recorded ``extra_overlays``
    list must be empty - confirms the helper is the sole source of the
    hashequiv overlay path.
    """
    _stub_user_config_loader(monkeypatch, hashserv=False)

    recorded: list[list] = []

    def fake_run_build(ctx, *, extra_overlays=None):  # type: ignore[no-untyped-def]
        recorded.append(list(extra_overlays or []))
        return 0

    monkeypatch.setattr(build_cmd.step_kas, "run_build", fake_run_build)

    result = runner.invoke(app, ["build", str(generic_yaml), "--skip-doctor"])

    assert result.exit_code == 0, result.output
    assert len(recorded) == 1, f"expected exactly one run_build call, got {recorded!r}"

    hashequiv_entries = [p for p in recorded[0] if p.name == "bakar-tuning-hashequiv.yml"]
    assert hashequiv_entries == [], f"expected no hashequiv overlay when use_hashequiv=False, got {hashequiv_entries!r}"
