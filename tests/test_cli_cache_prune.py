"""CLI tests for ``bakar cache prune-downloads``."""

from __future__ import annotations

import types
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

import bakar.commands._app as _state
from bakar import probes
from bakar.cli import app
from bakar.steps import cache_prune

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

PAYLOAD = b"x" * 10


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("DL_DIR", raising=False)
    monkeypatch.delenv("BAKAR_DL_MIRROR_URL", raising=False)
    monkeypatch.setattr(_state, "_USER_CONFIG", None)


@pytest.fixture
def dl_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "dl"
    d.mkdir()
    (d / "pkg-1.0.tar.gz").write_bytes(PAYLOAD)
    (d / "pkg-1.0.tar.gz.done").write_bytes(b"")
    monkeypatch.setenv("DL_DIR", str(d))
    monkeypatch.setenv("BAKAR_DL_MIRROR_URL", "http://hub.test")
    monkeypatch.setattr(cache_prune.probes, "head_info", lambda url, timeout: probes.HeadInfo(200, len(PAYLOAD)))
    return d


def test_dry_run_prints_blocks_and_removes_nothing(dl_dir: Path) -> None:
    result = runner.invoke(app, ["cache", "prune-downloads"])
    assert result.exit_code == 0, result.output
    for kind in ("hub:", "tarball:", "clone:"):
        assert kind in result.output
    assert "would remove 1 item(s)" in result.output
    assert "dry run" in result.output
    assert (dl_dir / "pkg-1.0.tar.gz").exists()
    assert (dl_dir / "pkg-1.0.tar.gz.done").exists()


def test_apply_removes_verified_items(dl_dir: Path) -> None:
    result = runner.invoke(app, ["cache", "prune-downloads", "--apply"])
    assert result.exit_code == 0, result.output
    assert "removed 1 item(s)" in result.output
    assert not (dl_dir / "pkg-1.0.tar.gz").exists()
    assert not (dl_dir / "pkg-1.0.tar.gz.done").exists()


def test_no_directory_configured_exits_1_naming_both_settings() -> None:
    result = runner.invoke(app, ["cache", "prune-downloads", "--apply"])
    assert result.exit_code == 1
    assert "DL_DIR" in result.output
    assert "build.dl_dir" in result.output


def test_configured_dir_not_a_directory_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_state, "_USER_CONFIG", types.SimpleNamespace(dl_dir=str(tmp_path / "missing")))
    result = runner.invoke(app, ["cache", "prune-downloads"])
    assert result.exit_code == 1
    assert "build.dl_dir" in result.output


def test_clone_older_than_zero_rejected(dl_dir: Path) -> None:
    result = runner.invoke(app, ["cache", "prune-downloads", "--clone-older-than", "0"])
    assert result.exit_code == 2
    assert (dl_dir / "pkg-1.0.tar.gz").exists()


def test_clone_class_not_requested_without_option(dl_dir: Path) -> None:
    result = runner.invoke(app, ["cache", "prune-downloads"])
    assert "clone: not requested" in result.output


def test_clone_class_examined_with_option(dl_dir: Path) -> None:
    result = runner.invoke(app, ["cache", "prune-downloads", "--clone-older-than", "30"])
    assert result.exit_code == 0, result.output
    assert "clone: examined" in result.output


def test_help_lists_verb() -> None:
    result = runner.invoke(app, ["cache", "--help"])
    assert result.exit_code == 0
    assert "prune-downloads" in result.output
