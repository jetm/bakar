"""CLI tests for ``bakar cache link-mirror``."""

from __future__ import annotations

import os
import types
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

import bakar.commands._app as _state
from bakar.cli import app
from bakar.steps import hub_mount

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("DL_DIR", raising=False)
    monkeypatch.delenv("BAKAR_DL_MIRROR_PATHS", raising=False)
    monkeypatch.delenv("BAKAR_CONTAINER", raising=False)
    monkeypatch.setattr(_state, "_load_user_config_safe", types.SimpleNamespace)
    monkeypatch.setattr(hub_mount, "_PROBE_MEMO", {})


def _config(monkeypatch: pytest.MonkeyPatch, **fields: object) -> None:
    cfg = types.SimpleNamespace(**fields)
    monkeypatch.setattr(_state, "_load_user_config_safe", lambda: cfg)


@pytest.fixture
def dl_dir(tmp_path: Path) -> Path:
    d = tmp_path / "dl"
    d.mkdir()
    return d


@pytest.fixture
def mirror(tmp_path: Path) -> Path:
    m = tmp_path / "mirror"
    m.mkdir()
    (m / "pkg-1.0.tar.gz").write_bytes(b"payload")
    (m / "pkg-1.0.tar.gz.done").write_bytes(b"")
    return m


def _listing(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir())


def test_dry_run_changes_nothing(monkeypatch: pytest.MonkeyPatch, dl_dir: Path, mirror: Path) -> None:
    _config(monkeypatch, dl_dir=str(dl_dir), dl_mirror_paths=str(mirror))
    result = runner.invoke(app, ["cache", "link-mirror"])
    assert result.exit_code == 0, result.output
    assert _listing(dl_dir) == []


def test_apply_creates_links(monkeypatch: pytest.MonkeyPatch, dl_dir: Path, mirror: Path) -> None:
    _config(monkeypatch, dl_dir=str(dl_dir), dl_mirror_paths=str(mirror))
    result = runner.invoke(app, ["cache", "link-mirror", "--apply"])
    assert result.exit_code == 0, result.output
    link = dl_dir / "pkg-1.0.tar.gz"
    assert link.exists()
    assert link.read_bytes() == b"payload"


def test_no_downloads_directory_refused(monkeypatch: pytest.MonkeyPatch, mirror: Path) -> None:
    _config(monkeypatch, dl_mirror_paths=str(mirror))
    result = runner.invoke(app, ["cache", "link-mirror", "--apply"])
    assert result.exit_code == 1
    assert "DL_DIR" in result.output
    assert "build.dl_dir" in result.output


def test_downloads_path_not_a_directory_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mirror: Path) -> None:
    missing = tmp_path / "absent"
    _config(monkeypatch, dl_dir=str(missing), dl_mirror_paths=str(mirror))
    result = runner.invoke(app, ["cache", "link-mirror", "--apply"])
    assert result.exit_code == 1
    assert not missing.exists()


def test_mirror_paths_unset_refused(monkeypatch: pytest.MonkeyPatch, dl_dir: Path) -> None:
    _config(monkeypatch, dl_dir=str(dl_dir))
    result = runner.invoke(app, ["cache", "link-mirror", "--apply"])
    assert result.exit_code == 1
    assert "build.dl_mirror_paths" in result.output
    assert _listing(dl_dir) == []


def test_environment_mirror_paths_ignored(monkeypatch: pytest.MonkeyPatch, dl_dir: Path, mirror: Path) -> None:
    monkeypatch.setenv("BAKAR_DL_MIRROR_PATHS", str(mirror))
    _config(monkeypatch, dl_dir=str(dl_dir))
    result = runner.invoke(app, ["cache", "link-mirror", "--apply"])
    assert result.exit_code == 1
    assert _listing(dl_dir) == []


def test_container_config_refused(monkeypatch: pytest.MonkeyPatch, dl_dir: Path, mirror: Path) -> None:
    _config(monkeypatch, dl_dir=str(dl_dir), dl_mirror_paths=str(mirror), container=True)
    result = runner.invoke(app, ["cache", "link-mirror", "--apply"])
    assert result.exit_code == 1
    assert "container" in result.output
    assert _listing(dl_dir) == []


def test_environment_container_ignored(monkeypatch: pytest.MonkeyPatch, dl_dir: Path, mirror: Path) -> None:
    monkeypatch.setenv("BAKAR_CONTAINER", "1")
    _config(monkeypatch, dl_dir=str(dl_dir), dl_mirror_paths=str(mirror))
    result = runner.invoke(app, ["cache", "link-mirror", "--apply"])
    assert result.exit_code == 0, result.output
    assert (dl_dir / "pkg-1.0.tar.gz").exists()


def test_no_ready_mirror_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dl_dir: Path) -> None:
    _config(monkeypatch, dl_dir=str(dl_dir), dl_mirror_paths=str(tmp_path / "gone"))
    result = runner.invoke(app, ["cache", "link-mirror", "--apply"])
    assert result.exit_code == 1
    assert "missing" in result.output
    assert _listing(dl_dir) == []


def test_environment_wins_for_downloads_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dl_dir: Path, mirror: Path
) -> None:
    other = tmp_path / "configured-dl"
    other.mkdir()
    monkeypatch.setenv("DL_DIR", str(dl_dir))
    _config(monkeypatch, dl_dir=str(other), dl_mirror_paths=str(mirror))
    result = runner.invoke(app, ["cache", "link-mirror", "--apply"])
    assert result.exit_code == 0, result.output
    assert (dl_dir / "pkg-1.0.tar.gz").exists()
    assert _listing(other) == []


def test_dropped_directory_warned_once_and_other_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dl_dir: Path, mirror: Path
) -> None:
    gone = tmp_path / "gone"
    _config(monkeypatch, dl_dir=str(dl_dir), dl_mirror_paths=f"{gone} {mirror}")
    result = runner.invoke(app, ["cache", "link-mirror", "--apply"])
    assert result.exit_code == 0, result.output
    assert result.output.count("skipping mirror directory") == 1
    assert (dl_dir / "pkg-1.0.tar.gz").exists()


def test_help_lists_verb() -> None:
    result = runner.invoke(app, ["cache", "--help"])
    assert result.exit_code == 0
    assert "link-mirror" in result.output


def test_unreadable_downloads_directory_exits_1(monkeypatch: pytest.MonkeyPatch, dl_dir: Path, mirror: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads any directory")
    _config(monkeypatch, dl_dir=str(dl_dir), dl_mirror_paths=str(mirror))
    dl_dir.chmod(0o311)
    try:
        result = runner.invoke(app, ["cache", "link-mirror", "--apply"])
    finally:
        dl_dir.chmod(0o755)
    assert result.exit_code == 1
    assert "cannot read the downloads directory" in result.output
    assert _listing(dl_dir) == []


def test_container_config_refused_before_the_directory_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    _config(monkeypatch, container=True)
    result = runner.invoke(app, ["cache", "link-mirror"])
    assert result.exit_code == 1
    assert "container" in result.output
    assert "downloads" not in result.output


def _empty_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("empty:")]


def test_empty_directory_reported_and_nothing_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dl_dir: Path
) -> None:
    hollow = tmp_path / "hollow"
    hollow.mkdir()
    _config(monkeypatch, dl_dir=str(dl_dir), dl_mirror_paths=str(hollow))
    result = runner.invoke(app, ["cache", "link-mirror"])
    assert result.exit_code == 0, result.output
    lines = _empty_lines(result.output)
    assert len(lines) == 1
    assert str(hollow) in lines[0]
    assert "not mounted" in lines[0]
    assert "1 mirror directories listed, 0 payloads examined" in result.output
    assert "would link 0 file(s)" in result.output
    applied = runner.invoke(app, ["cache", "link-mirror", "--apply"])
    assert applied.exit_code == 0, applied.output
    assert _listing(dl_dir) == []


def test_empty_first_directory_does_not_stop_the_second(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dl_dir: Path, mirror: Path
) -> None:
    hollow = tmp_path / "hollow"
    hollow.mkdir()
    _config(monkeypatch, dl_dir=str(dl_dir), dl_mirror_paths=f"{hollow} {mirror}")
    result = runner.invoke(app, ["cache", "link-mirror", "--apply"])
    assert result.exit_code == 0, result.output
    assert (dl_dir / "pkg-1.0.tar.gz").read_bytes() == b"payload"
    lines = _empty_lines(result.output)
    assert len(lines) == 1
    assert str(hollow) in lines[0]
    assert str(mirror) not in lines[0]


def test_empty_directory_holding_only_a_subdirectory_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dl_dir: Path
) -> None:
    nested = tmp_path / "nested"
    (nested / "sub").mkdir(parents=True)
    _config(monkeypatch, dl_dir=str(dl_dir), dl_mirror_paths=str(nested))
    result = runner.invoke(app, ["cache", "link-mirror"])
    assert result.exit_code == 0, result.output
    lines = _empty_lines(result.output)
    assert len(lines) == 1
    assert str(nested) in lines[0]
