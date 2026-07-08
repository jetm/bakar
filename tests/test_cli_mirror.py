"""Tests for the ``bakar mirror`` command.

Covers the two pure helpers (filename normalization, output-dir precedence)
and drives the command through the Typer ``CliRunner`` with ``subprocess.run``
monkeypatched so no real ``git clone`` or ``tar`` runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import bakar.commands.mirror  # noqa: F401 - registers the command on app
from bakar.cli import app
from bakar.commands.mirror import mirror_tarball_name, resolve_output_dir

if TYPE_CHECKING:
    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

_URL = "https://github.com/openembedded/meta-openembedded.git"
_EXPECTED_NAME = "git2_github.com.openembedded.meta-openembedded.git.tar.gz"


# ---------------------------------------------------------------------------
# mirror_tarball_name
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_filename_matches_bitbake_convention() -> None:
    """The canonical example maps to BitBake's git2_ tarball name."""
    assert mirror_tarball_name(_URL) == _EXPECTED_NAME


@pytest.mark.unit
def test_filename_normalizes_slash_and_colon() -> None:
    """``/`` and ``:`` are normalized to ``.`` in the produced name."""
    name = mirror_tarball_name("https://git.example.org:8443/group/repo.git")
    assert "/" not in name
    assert ":" not in name
    assert name == "git2_git.example.org.8443.group.repo.git.tar.gz"


@pytest.mark.unit
def test_filename_has_prefix_and_suffix() -> None:
    """The name always carries the ``git2_`` prefix and ``.tar.gz`` suffix."""
    name = mirror_tarball_name(_URL)
    assert name.startswith("git2_")
    assert name.endswith(".tar.gz")


# ---------------------------------------------------------------------------
# resolve_output_dir precedence
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_output_dir_flag_wins(tmp_path: Path) -> None:
    """``--output-dir`` takes precedence over DL_DIR."""
    out = tmp_path / "flag"
    assert resolve_output_dir(out, "/some/dl") == out


@pytest.mark.unit
def test_dl_dir_used_when_no_flag(tmp_path: Path) -> None:
    """DL_DIR is used when no flag is supplied and it is non-empty."""
    assert resolve_output_dir(None, "/some/dl") == Path("/some/dl")


@pytest.mark.unit
def test_dl_dir_none_falls_back_to_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``dl_dir is None`` falls through to the current directory."""
    monkeypatch.chdir(tmp_path)
    assert resolve_output_dir(None, None) == tmp_path


@pytest.mark.unit
def test_dl_dir_empty_falls_back_to_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An empty-string DL_DIR is treated as unset and falls back to cwd."""
    monkeypatch.chdir(tmp_path)
    assert resolve_output_dir(None, "") == tmp_path


# ---------------------------------------------------------------------------
# Command body: subprocess.run monkeypatched
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_fake_run(calls: list[list[str]], committer_date: str = "Mon, 1 Jan 2024 00:00:00 +0000"):
    """Return a fake ``subprocess.run`` recording argv lists and faking output."""

    def fake_run(argv, *args, **kwargs):
        calls.append(list(argv))
        if argv[:2] == ["git", "-C"] or "log" in argv:
            return _FakeProc(returncode=0, stdout=committer_date)
        return _FakeProc(returncode=0)

    return fake_run


@pytest.mark.unit
def test_clone_and_tar_invoked(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A successful run issues git clone, git log, and tar without real subprocesses."""
    monkeypatch.chdir(tmp_path)
    calls: list[list[str]] = []
    with patch("bakar.commands.mirror.subprocess.run", _make_fake_run(calls)):
        result = runner.invoke(app, ["mirror", _URL, "--output-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert any(c[:4] == ["git", "clone", "--bare", "--mirror"] for c in calls)
    assert any("log" in c and "--format=%cD" in c for c in calls)
    assert any(c and c[0] == "tar" for c in calls)


@pytest.mark.unit
def test_tarball_named_with_git2_convention(
    runner: _CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tar destination carries the git2_ tarball name in the output dir."""
    monkeypatch.chdir(tmp_path)
    calls: list[list[str]] = []
    with patch("bakar.commands.mirror.subprocess.run", _make_fake_run(calls)):
        result = runner.invoke(app, ["mirror", _URL, "--output-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    tar_call = next(c for c in calls if c and c[0] == "tar")
    dest = str(tmp_path / _EXPECTED_NAME)
    assert dest in tar_call


@pytest.mark.unit
def test_tar_uses_committer_date_and_oe_owner(
    runner: _CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """tar is invoked with --mtime <committer-date> and --owner/--group oe:0."""
    monkeypatch.chdir(tmp_path)
    calls: list[list[str]] = []
    date = "Tue, 2 Feb 2021 03:04:05 +0000"
    with patch("bakar.commands.mirror.subprocess.run", _make_fake_run(calls, committer_date=date)):
        result = runner.invoke(app, ["mirror", _URL, "--output-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    tar_call = next(c for c in calls if c and c[0] == "tar")
    assert "--mtime" in tar_call
    assert tar_call[tar_call.index("--mtime") + 1] == date
    assert "--owner" in tar_call and tar_call[tar_call.index("--owner") + 1] == "oe:0"
    assert "--group" in tar_call and tar_call[tar_call.index("--group") + 1] == "oe:0"


@pytest.mark.unit
def test_tempdir_removed_after_success(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No temporary clone directory remains on disk after a successful run."""
    monkeypatch.chdir(tmp_path)
    created: list[Path] = []
    import tempfile as _tf

    real_mkdtemp = _tf.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        created.append(Path(d))
        return d

    calls: list[list[str]] = []
    with (
        patch("bakar.commands.mirror.subprocess.run", _make_fake_run(calls)),
        patch("bakar.commands.mirror.tempfile.mkdtemp", tracking_mkdtemp),
    ):
        result = runner.invoke(app, ["mirror", _URL, "--output-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert created, "mkdtemp was not called"
    assert not created[0].exists()


@pytest.mark.unit
def test_clone_failure_propagates_nonzero(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A failed git clone exits non-zero rather than reporting success."""
    monkeypatch.chdir(tmp_path)

    def fail_run(argv, *args, **kwargs):
        return _FakeProc(returncode=128, stderr="fatal: repository not found")

    with patch("bakar.commands.mirror.subprocess.run", fail_run):
        result = runner.invoke(app, ["mirror", _URL, "--output-dir", str(tmp_path)])

    assert result.exit_code != 0


@pytest.mark.unit
def test_missing_binary_exits_with_clear_error(
    runner: _CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing git or tar binary exits non-zero with an install hint, not a traceback."""
    monkeypatch.chdir(tmp_path)
    with patch("bakar.commands.mirror.shutil.which", return_value=None):
        result = runner.invoke(app, ["mirror", _URL, "--output-dir", str(tmp_path)])

    assert result.exit_code != 0
    assert "not found on PATH" in result.output
