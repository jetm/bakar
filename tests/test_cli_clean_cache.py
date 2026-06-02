"""Tests for the ``bakar clean-cache`` command.

Hermetic tests for the sstate-prune and ccache-evict paths. Each sstate test
sets up a synthetic ``sstate-cache`` directory under ``tmp_path`` with files
aged via ``os.utime``; SSTATE_DIR is resolved through ``monkeypatch.setenv`` so
no real config or env is read. sstate-focused tests pass ``--no-ccache`` to keep
them isolated from the ccache path; ccache tests pass ``--no-sstate`` and mock
``subprocess.run``/``shutil.which`` so no real ccache binary is invoked.

The age threshold flag is ``--older-than N`` (default 30 days). Dry-run is opt-in
via ``--dry-run``. Without ``--yes`` and without ``--dry-run`` the command
prompts via ``typer.confirm``; deletion-path tests pass ``--yes``.

``_atime_tracked`` reads ``/proc/mounts``; it is patched to a deterministic
value so tests do not depend on the host's mount options.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from bakar.cli import app

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _age_file(path: Path, days_old: float) -> None:
    """Backdate *path* atime and mtime by *days_old* days."""
    ts = time.time() - days_old * 86400
    os.utime(path, (ts, ts))


def _make_sstate(
    root: Path,
    old_files: int = 3,
    new_files: int = 2,
    old_days: float = 60.0,
) -> tuple[list[Path], list[Path]]:
    """Create a synthetic sstate-cache tree.

    Returns ``(old_paths, new_paths)``. Old files are backdated *old_days*
    days; new files keep their current atime/mtime (just-created).
    """
    root.mkdir(parents=True, exist_ok=True)
    bucket = root / "sstate" / "ab"
    bucket.mkdir(parents=True, exist_ok=True)
    old_paths: list[Path] = []
    new_paths: list[Path] = []
    for i in range(old_files):
        p = bucket / f"old_{i}.tar.zst"
        p.write_bytes(b"old payload " + str(i).encode())
        _age_file(p, old_days)
        old_paths.append(p)
    for i in range(new_files):
        p = bucket / f"new_{i}.tar.zst"
        p.write_bytes(b"new payload " + str(i).encode())
        new_paths.append(p)
    return old_paths, new_paths


@pytest.fixture
def sstate_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An sstate-cache directory pointed at by the ``SSTATE_DIR`` env var.

    Patches ``_atime_tracked`` to True so tests don't depend on the host's
    mount options - the file ages set via ``os.utime`` then drive the
    delete decision deterministically.
    """
    d = tmp_path / "sstate-cache"
    d.mkdir()
    monkeypatch.setenv("SSTATE_DIR", str(d))
    monkeypatch.setattr("bakar.commands.clean_cache._atime_tracked", lambda _p: True)
    return d


# ---------------------------------------------------------------------------
# sstate happy paths: dry-run vs --yes
# ---------------------------------------------------------------------------


def test_dry_run_lists_candidates_and_deletes_nothing(sstate_dir: Path) -> None:
    """``--dry-run`` reports prune candidates without mutating the tree."""
    old, new = _make_sstate(sstate_dir, old_files=3, new_files=2, old_days=60.0)

    result = runner.invoke(app, ["clean-cache", "--no-ccache", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output, result.output
    assert "3" in result.output  # file count surfaced
    for p in old + new:
        assert p.exists(), f"{p} was deleted on a dry-run"


def test_default_without_yes_or_dry_run_does_not_delete(sstate_dir: Path) -> None:
    """No ``--yes`` and no ``--dry-run``: the prompt aborts and nothing is deleted."""
    old, new = _make_sstate(sstate_dir, old_files=3, new_files=2, old_days=60.0)

    result = runner.invoke(app, ["clean-cache", "--no-ccache"])

    assert result.exit_code in (0, 1), result.output
    for p in old + new:
        assert p.exists(), f"{p} was deleted without --yes confirmation"


def test_yes_deletes_old_files_keeps_new(sstate_dir: Path) -> None:
    """``--yes`` deletes files older than 30 days, keeps newer ones."""
    old, new = _make_sstate(sstate_dir, old_files=3, new_files=2, old_days=60.0)

    result = runner.invoke(app, ["clean-cache", "--no-ccache", "--yes"])

    assert result.exit_code == 0, result.output
    assert "deleted" in result.output, result.output
    for p in old:
        assert not p.exists(), f"{p} should have been deleted (60 days old, threshold 30)"
    for p in new:
        assert p.exists(), f"{p} should have been kept (newly created, threshold 30 days)"


# ---------------------------------------------------------------------------
# Threshold tuning: --older-than
# ---------------------------------------------------------------------------


def test_older_than_custom_threshold_keeps_borderline_files(sstate_dir: Path) -> None:
    """``--older-than 90`` keeps 60-day-old files; only >90-day-old ones go."""
    sixty_day, _ = _make_sstate(sstate_dir, old_files=3, new_files=0, old_days=60.0)
    bucket = sstate_dir / "sstate" / "ab"
    hundred_day = []
    for i in range(2):
        p = bucket / f"ancient_{i}.tar.zst"
        p.write_bytes(b"ancient payload " + str(i).encode())
        _age_file(p, 100.0)
        hundred_day.append(p)

    result = runner.invoke(app, ["clean-cache", "--no-ccache", "--older-than", "90", "--yes"])

    assert result.exit_code == 0, result.output
    for p in sixty_day:
        assert p.exists(), f"{p} (60d) should be kept under --older-than 90"
    for p in hundred_day:
        assert not p.exists(), f"{p} (100d) should be deleted under --older-than 90"


def test_older_than_only_old_files_present(sstate_dir: Path) -> None:
    """``--older-than 1`` with files just-aged 2 days deletes them; just-created stay."""
    old, new = _make_sstate(sstate_dir, old_files=2, new_files=2, old_days=2.0)

    result = runner.invoke(app, ["clean-cache", "--no-ccache", "--older-than", "1", "--yes"])

    assert result.exit_code == 0, result.output
    for p in old:
        assert not p.exists(), f"{p} (2d old) should be deleted under --older-than 1"
    for p in new:
        assert p.exists(), f"{p} (just-created) should be kept under --older-than 1"


# ---------------------------------------------------------------------------
# Empty / nothing-to-prune
# ---------------------------------------------------------------------------


def test_empty_sstate_dir_reports_nothing_to_remove(sstate_dir: Path) -> None:
    """An empty sstate-cache exits 0 with the 'Nothing to remove' message."""
    result = runner.invoke(app, ["clean-cache", "--no-ccache", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Nothing to remove" in result.output, result.output


def test_all_new_files_nothing_to_prune(sstate_dir: Path) -> None:
    """Only fresh files present: exits 0 and reports nothing to remove."""
    _make_sstate(sstate_dir, old_files=0, new_files=3, old_days=60.0)

    result = runner.invoke(app, ["clean-cache", "--no-ccache", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Nothing to remove" in result.output, result.output


# ---------------------------------------------------------------------------
# Error paths: SSTATE_DIR not set / missing
# ---------------------------------------------------------------------------


def test_no_sstate_dir_set_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SSTATE_DIR unset (env + no user config), ccache off: exits 2 with a message."""
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    monkeypatch.setattr("bakar.commands.clean_cache._state._USER_CONFIG", None)

    result = runner.invoke(app, ["clean-cache", "--no-ccache"])

    assert result.exit_code == 2, result.output
    assert "SSTATE_DIR not set" in result.output, result.output


def test_sstate_dir_missing_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--sstate-dir`` pointing at a non-existent path (ccache off) exits 2."""
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("SSTATE_DIR", str(missing))

    result = runner.invoke(app, ["clean-cache", "--no-ccache"])

    assert result.exit_code == 2, result.output
    assert "does not exist" in result.output, result.output


def test_sstate_dir_cli_flag_overrides_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--sstate-dir`` wins over ``SSTATE_DIR`` env var."""
    env_path = tmp_path / "env-sstate"
    env_path.mkdir()
    monkeypatch.setenv("SSTATE_DIR", str(env_path))
    monkeypatch.setattr("bakar.commands.clean_cache._atime_tracked", lambda _p: True)

    cli_path = tmp_path / "cli-sstate"
    cli_path.mkdir()

    result = runner.invoke(app, ["clean-cache", "--no-ccache", "--sstate-dir", str(cli_path), "--dry-run"])

    assert result.exit_code == 0, result.output
    flat = "".join(result.output.split())
    assert "".join(str(cli_path).split()) in flat
    assert "".join(str(env_path).split()) not in flat


# ---------------------------------------------------------------------------
# noatime fallback path
# ---------------------------------------------------------------------------


def test_noatime_mount_emits_warning_and_uses_mtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a noatime mount the handler prints a warning and falls back to mtime."""
    d = tmp_path / "sstate-cache"
    d.mkdir()
    monkeypatch.setenv("SSTATE_DIR", str(d))
    monkeypatch.setattr("bakar.commands.clean_cache._atime_tracked", lambda _p: False)

    old, new = _make_sstate(d, old_files=2, new_files=1, old_days=60.0)

    result = runner.invoke(app, ["clean-cache", "--no-ccache", "--yes"])

    assert result.exit_code == 0, result.output
    assert "noatime" in result.output, result.output
    assert "mtime" in result.output, result.output
    for p in old:
        assert not p.exists(), f"{p} should still be deleted (mtime-based)"
    for p in new:
        assert p.exists(), f"{p} should be kept (mtime newer than threshold)"


def test_atime_tracked_reads_proc_mounts(tmp_path: Path) -> None:
    """``_atime_tracked`` returns False when noatime appears in /proc/mounts."""
    from bakar.commands.clean_cache import _atime_tracked

    target = tmp_path / "build" / "sstate"
    target.mkdir(parents=True)

    fake_mounts = f"proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\ntmpfs {tmp_path} tmpfs rw,noatime 0 0\n"

    real_read_text = type(target).read_text

    def fake_read_text(self, *args, **kwargs):
        if str(self) == "/proc/mounts":
            return fake_mounts
        return real_read_text(self, *args, **kwargs)

    with patch("bakar.commands.clean_cache.Path.read_text", fake_read_text):
        assert _atime_tracked(target) is False


def test_atime_tracked_returns_false_on_oserror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``_atime_tracked`` defensively returns False when /proc/mounts is unreadable."""
    from bakar.commands.clean_cache import _atime_tracked

    target = tmp_path / "x"
    target.mkdir()

    def boom(self, *args, **kwargs):
        if str(self) == "/proc/mounts":
            raise OSError("no proc")
        return ""

    monkeypatch.setattr("bakar.commands.clean_cache.Path.read_text", boom)

    assert _atime_tracked(target) is False


# ---------------------------------------------------------------------------
# ccache path
# ---------------------------------------------------------------------------


def _fake_ccache_run(calls: list[list[str]]):
    """Return a subprocess.run stand-in that records calls and fakes ccache."""

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "--print-stats" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="cache_size_kibibyte 1000\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return fake_run


def test_ccache_only_evicts_via_ccache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--no-sstate`` runs ``ccache --evict-older-than`` against --ccache-dir."""
    cc = tmp_path / "cc"
    cc.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr("bakar.commands.clean_cache.subprocess.run", _fake_ccache_run(calls))
    monkeypatch.setattr("bakar.commands.clean_cache.shutil.which", lambda _n: "/usr/bin/ccache")

    result = runner.invoke(app, ["clean-cache", "--no-sstate", "--ccache-dir", str(cc), "--yes"])

    assert result.exit_code == 0, result.output
    assert any("--evict-older-than" in c for c in calls), calls
    assert any("30d" in c for c in calls), calls
    assert "evicted" in result.output, result.output


def test_ccache_custom_age_passed_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--older-than 7`` becomes ``7d`` in the ccache eviction call."""
    cc = tmp_path / "cc"
    cc.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr("bakar.commands.clean_cache.subprocess.run", _fake_ccache_run(calls))
    monkeypatch.setattr("bakar.commands.clean_cache.shutil.which", lambda _n: "/usr/bin/ccache")

    result = runner.invoke(app, ["clean-cache", "--no-sstate", "--ccache-dir", str(cc), "--older-than", "7", "--yes"])

    assert result.exit_code == 0, result.output
    assert any(c == ["ccache", "--evict-older-than", "7d"] for c in calls), calls


def test_ccache_dry_run_does_not_evict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--dry-run`` reports but does not invoke eviction."""
    cc = tmp_path / "cc"
    cc.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr("bakar.commands.clean_cache.subprocess.run", _fake_ccache_run(calls))
    monkeypatch.setattr("bakar.commands.clean_cache.shutil.which", lambda _n: "/usr/bin/ccache")

    result = runner.invoke(app, ["clean-cache", "--no-sstate", "--ccache-dir", str(cc), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output, result.output
    assert not any("--evict-older-than" in c for c in calls), calls


def test_ccache_binary_missing_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ccache binary absent: ccache is skipped; with --no-sstate nothing is actionable -> exit 2."""
    cc = tmp_path / "cc"
    cc.mkdir()
    monkeypatch.setattr("bakar.commands.clean_cache.shutil.which", lambda _n: None)

    result = runner.invoke(app, ["clean-cache", "--no-sstate", "--ccache-dir", str(cc)])

    assert result.exit_code == 2, result.output
    assert "binary not on PATH" in result.output, result.output


def test_no_sstate_no_ccache_is_noop(tmp_path: Path) -> None:
    """Both caches disabled: a friendly no-op, no error."""
    result = runner.invoke(app, ["clean-cache", "--no-sstate", "--no-ccache"])

    assert "Nothing to do" in result.output, result.output


# ---------------------------------------------------------------------------
# Format helper
# ---------------------------------------------------------------------------


def test_fmt_size_scales_units() -> None:
    """``_fmt_size`` walks B->KiB->MiB->GiB and labels correctly."""
    from bakar.commands.clean_cache import _fmt_size

    assert "B" in _fmt_size(500)
    assert "KiB" in _fmt_size(2048)
    assert "MiB" in _fmt_size(5 * 1024 * 1024)
    assert "GiB" in _fmt_size(3 * 1024 * 1024 * 1024)
