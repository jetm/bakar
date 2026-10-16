"""The multi-release fan-out honours the global --host / --container flags.

Every other multi-release test replaces ``_run_single_preset_release``
outright, so nothing pinned that it resolves its config through
``resolve_global_config``. These tests run the real per-release path and stub
only the kas build runner, recording the BuildConfig each release receives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

import bakar.steps.kas_build as step_kas
from bakar.cli import app
from tests.test_cli_build import (
    _make_multi_release_bbsetup_preset,
    _stub_preset_loader,
    _stub_user_config_loader,
)

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate HOME, run from a bakar workspace and skip the real doctor checks."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    ws = tmp_path / "ws"
    (ws / "nxp").mkdir(parents=True)
    (ws / ".bakar.toml").write_text("")
    monkeypatch.chdir(ws)
    with patch("bakar.commands._helpers.run_all", return_value=[]):
        yield


def _host_modes(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]) -> list[bool]:
    _stub_user_config_loader(monkeypatch, hashserv=False)
    _stub_preset_loader(monkeypatch, [_make_multi_release_bbsetup_preset(tmp_path)])
    seen: list[Any] = []

    def fake_run_build(ctx: Any, **kwargs: Any) -> int:
        seen.append(ctx.cfg)
        return 0

    monkeypatch.setattr(step_kas, "run_build", fake_run_build)

    result = runner.invoke(app, [*argv, "build", "--preset", "avocado-all-machines"])

    assert result.exit_code == 0, result.output
    assert len(seen) == 2, "expected one kas build per release"
    return [cfg.host_mode for cfg in seen]


def test_multi_release_global_host_reaches_every_release(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Host is the default, so only a container default makes --host observable.
    monkeypatch.setenv("BAKAR_CONTAINER", "1")
    assert _host_modes(runner, monkeypatch, tmp_path, ["--host"]) == [True, True]


def test_multi_release_global_container_reaches_every_release(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _host_modes(runner, monkeypatch, tmp_path, ["--container"]) == [False, False]


def test_multi_release_default_without_flag_is_host(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("BAKAR_CONTAINER", raising=False)
    assert _host_modes(runner, monkeypatch, tmp_path, []) == [True, True]
