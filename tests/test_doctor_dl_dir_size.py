"""Doctor row dl-dir-size (spec cache-sharing-doctor-checks)."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import TYPE_CHECKING

from bakar import diagnostics
from bakar.diagnostics import Severity, Status, check_dl_dir_size
from tests.conftest import make_build_config

if TYPE_CHECKING:
    import pytest


def _cfg(dl_dir: Path | None, push: str | None = "hub:/x"):
    return make_build_config(
        workspace=Path("/nonexistent-ws"),
        dl_dir=str(dl_dir) if dl_dir else None,
        cache_push_target=push,
    )


def _limit(monkeypatch: pytest.MonkeyPatch, nbytes: int) -> None:
    monkeypatch.setattr(diagnostics, "_DL_DIR_WARN_BYTES", nbytes)


def test_over_limit_warns_with_size_and_verb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _limit(monkeypatch, 1000)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.tar").write_bytes(b"x" * 2000)
    r = check_dl_dir_size(_cfg(tmp_path))
    assert r.status == Status.FAIL
    assert r.severity == Severity.WARN
    assert str(tmp_path) in r.message
    assert "GB" in r.message
    assert "bakar cache prune-downloads" in r.message


def test_under_limit_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _limit(monkeypatch, 1000)
    (tmp_path / "a.tar").write_bytes(b"x" * 10)
    r = check_dl_dir_size(_cfg(tmp_path))
    assert r.status == Status.PASS
    assert r.severity == Severity.INFO


def test_symlink_to_large_file_not_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _limit(monkeypatch, 1000)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big").write_bytes(b"x" * 5000)
    dl = tmp_path / "dl"
    dl.mkdir()
    (dl / "link").symlink_to(outside / "big")
    (dl / "dirlink").symlink_to(outside, target_is_directory=True)
    r = check_dl_dir_size(_cfg(dl))
    assert r.status == Status.PASS


def test_directories_do_not_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _limit(monkeypatch, 1)
    for i in range(20):
        (tmp_path / f"d{i}").mkdir()
    r = check_dl_dir_size(_cfg(tmp_path))
    assert r.status == Status.PASS


def test_walk_past_bound_reports_not_measured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _limit(monkeypatch, 1)
    for i in range(5):
        (tmp_path / f"f{i}").write_bytes(b"x" * 100)
    monkeypatch.setattr(diagnostics, "_DL_DIR_WALK_BOUND_S", 0.5)
    ticks = itertools.count(0, 1)
    monkeypatch.setattr(diagnostics.time, "monotonic", lambda: float(next(ticks)))
    r = check_dl_dir_size(_cfg(tmp_path))
    assert r.status == Status.SKIP
    assert r.severity == Severity.INFO
    assert "not measured" in r.message


def test_store_host_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _limit(monkeypatch, 1)
    (tmp_path / "a").write_bytes(b"x" * 100)
    r = check_dl_dir_size(_cfg(tmp_path, push=None))
    assert r.status == Status.SKIP
    assert r.severity == Severity.INFO


def test_missing_or_unset_directory_skips(tmp_path: Path) -> None:
    for d in (tmp_path / "absent", None):
        r = check_dl_dir_size(_cfg(d))
        assert r.status == Status.SKIP
        assert r.severity == Severity.INFO


def test_never_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _limit(monkeypatch, 1)
    (tmp_path / "a").write_bytes(b"x" * 100)
    for cfg in (_cfg(tmp_path), _cfg(tmp_path, push=None), _cfg(tmp_path / "absent"), _cfg(None)):
        assert check_dl_dir_size(cfg).severity != Severity.BLOCK
