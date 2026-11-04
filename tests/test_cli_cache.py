"""CLI tests for ``bakar cache``."""

from __future__ import annotations

import types
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

import bakar.commands._app as _state
from bakar.cli import app
from bakar.steps import cache_capture, cache_push

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("DL_DIR", raising=False)
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    monkeypatch.setattr(_state, "_USER_CONFIG", None)


def test_help_lists_verbs() -> None:
    result = runner.invoke(app, ["cache", "--help"])
    assert result.exit_code == 0
    for verb in ("push", "status", "capture"):
        assert verb in result.output


def test_push_requires_watch() -> None:
    result = runner.invoke(app, ["cache", "push"])
    assert result.exit_code == 2
    assert "--watch" in result.output


def test_push_watch_without_target_fails() -> None:
    result = runner.invoke(app, ["cache", "push", "--watch"])
    assert result.exit_code != 0
    assert "cache_push_target" in result.output


@pytest.mark.parametrize(
    ("env", "configured", "want"),
    [
        ("http://env.test", "http://cfg.test", "http://env.test"),
        (None, "http://cfg.test", "http://cfg.test"),
        ("", "http://cfg.test", "http://cfg.test"),
        (None, None, None),
    ],
)
def test_push_watch_passes_mirror_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str | None, configured: str | None, want: str | None
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(cache_push, "check_prerequisites", lambda *a, **k: None)
    monkeypatch.setattr(cache_push, "watch", lambda *a, **k: seen.update(k))
    monkeypatch.setenv("DL_DIR", str(tmp_path / "dl"))
    if env is None:
        monkeypatch.delenv("BAKAR_DL_MIRROR_URL", raising=False)
    else:
        monkeypatch.setenv("BAKAR_DL_MIRROR_URL", env)
    cfg = types.SimpleNamespace(dl_dir=None, cache_push_target="hub:/dl", dl_mirror_url=configured)
    monkeypatch.setattr(_state, "_load_user_config_safe", lambda: cfg)
    result = runner.invoke(app, ["cache", "push", "--watch"])
    assert result.exit_code == 0, result.output
    assert seen["mirror_url"] == want


def test_status_empty_queue() -> None:
    result = runner.invoke(app, ["cache", "status"])
    assert result.exit_code == 0
    assert "queue: 0" in result.output
    assert "oldest: -" in result.output
    assert "paused: no" in result.output
    assert "last reconcile: never" in result.output


def test_capture_without_bitbake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_capture, "find_bitbake_pids", lambda *a, **k: [])
    result = runner.invoke(app, ["cache", "capture"])
    assert result.exit_code == 0
    assert "no running BitBake process" in result.output
