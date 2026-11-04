"""Doctor rows for the shared-cache hub push (spec cache-sharing-doctor-checks)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from bakar import detach_records, diagnostics, mounts
from bakar.diagnostics import (
    Severity,
    Status,
    check_cache_push_service,
    check_sstate_mirror_shared_mount,
    check_store_stale_tarballs,
)
from bakar.steps import cache_queue
from tests.conftest import make_build_config


class _FakeQueue:
    def __init__(self, age: float | None) -> None:
        self._age = age

    def __enter__(self) -> _FakeQueue:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(oldest_age_s=self._age)


def _push_env(monkeypatch: pytest.MonkeyPatch, *, active: bool, age: float | None) -> None:
    monkeypatch.setattr(detach_records, "systemctl_unit_active", lambda unit: active)
    monkeypatch.setattr(cache_queue, "PushQueue", lambda: _FakeQueue(age))
    monkeypatch.setattr(cache_queue, "queue_exists", lambda: True)


def _cfg(**kw: object):
    return make_build_config(workspace=Path("/nonexistent-ws"), **kw)


# --- push service -----------------------------------------------------------


def test_push_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    _push_env(monkeypatch, active=True, age=None)
    r = check_cache_push_service(_cfg(cache_push_target="hub:/x"))
    assert r.status == Status.PASS


def test_push_unit_inactive_warns_and_names_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    _push_env(monkeypatch, active=False, age=None)
    r = check_cache_push_service(_cfg(cache_push_target="hub:/x"))
    assert r.status == Status.FAIL
    assert r.severity == Severity.WARN
    assert "bakar-cache-push.service" in r.message


def test_push_backlog_20_min_is_info(monkeypatch: pytest.MonkeyPatch) -> None:
    _push_env(monkeypatch, active=True, age=20 * 60)
    r = check_cache_push_service(_cfg(cache_push_target="hub:/x"))
    assert r.status == Status.FAIL
    assert r.severity == Severity.INFO
    assert "20" in r.message


def test_push_backlog_70_min_is_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    _push_env(monkeypatch, active=True, age=70 * 60)
    r = check_cache_push_service(_cfg(cache_push_target="hub:/x"))
    assert r.status == Status.FAIL
    assert r.severity == Severity.WARN
    assert "70" in r.message


def test_push_backlog_under_15_min_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _push_env(monkeypatch, active=True, age=5 * 60)
    r = check_cache_push_service(_cfg(cache_push_target="hub:/x"))
    assert r.status == Status.PASS
    assert "5" in r.message


def test_push_check_creates_no_files_without_a_queue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setattr(detach_records, "systemctl_unit_active", lambda unit: True)
    r = check_cache_push_service(_cfg(cache_push_target="hub:/x"))
    assert r.status == Status.PASS
    assert "no queue yet" in r.message
    assert list(state.iterdir()) == []


def test_push_check_reads_an_existing_queue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(detach_records, "systemctl_unit_active", lambda unit: True)
    with cache_queue.PushQueue(cache_queue.state_dir()) as queue:
        queue.enqueue(["a", "a.done"], now=1.0)
    r = check_cache_push_service(_cfg(cache_push_target="hub:/x"))
    assert r.status == Status.FAIL
    assert r.severity == Severity.WARN


def test_push_store_host_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(unit: str) -> bool:
        raise AssertionError("must not probe the unit")

    monkeypatch.setattr(detach_records, "systemctl_unit_active", boom)
    r = check_cache_push_service(_cfg(cache_push_target=None))
    assert r.status == Status.SKIP


# --- sstate mirror ----------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SSTATE_MIRRORS", "SSTATE_DIR", "BAKAR_SSTATE_MIRROR_URL"):
        monkeypatch.delenv(var, raising=False)


def test_http_mirror_over_nfs_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda p: True)
    r = check_sstate_mirror_shared_mount(
        _cfg(sstate_mirrors="file://.* http://hub/sstate/PATH", sstate_dir="/srv/sstate")
    )
    assert r.status == Status.FAIL
    assert r.severity == Severity.BLOCK
    assert "http://hub/sstate/PATH" in r.message
    assert "/srv/sstate" in r.message


def test_file_mirror_over_nfs_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda p: True)
    r = check_sstate_mirror_shared_mount(_cfg(sstate_mirrors="file://.* file:///mnt/PATH", sstate_dir="/srv/sstate"))
    assert r.status == Status.PASS


def test_http_mirror_local_dir_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda p: False)
    r = check_sstate_mirror_shared_mount(_cfg(sstate_mirrors="https://hub/PATH", sstate_dir="/srv/sstate"))
    assert r.status == Status.PASS


def test_http_mirror_undetermined_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda p: None)
    r = check_sstate_mirror_shared_mount(_cfg(sstate_mirrors="http://hub/PATH", sstate_dir="/srv/sstate"))
    assert r.status == Status.FAIL
    assert r.severity == Severity.WARN
    assert "could not determine" in r.message


def test_env_mirrors_and_dir_win_over_config(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []

    def probe(p: Path) -> bool:
        seen.append(p)
        return True

    monkeypatch.setattr(mounts, "is_path_on_nfs", probe)
    monkeypatch.setenv("SSTATE_MIRRORS", "http://env/PATH")
    monkeypatch.setenv("SSTATE_DIR", "/env/sstate")
    r = check_sstate_mirror_shared_mount(_cfg(sstate_mirrors="file://x", sstate_dir="/cfg/sstate"))
    assert r.severity == Severity.BLOCK
    assert seen == [Path("/env/sstate")]


def test_config_sstate_mirror_url_over_nfs_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda p: True)
    r = check_sstate_mirror_shared_mount(_cfg(sstate_mirror_url="http://hub/sstate", sstate_dir="/srv/sstate"))
    assert r.severity == Severity.BLOCK


def test_env_sstate_mirror_url_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    # The build drops a caller's BAKAR_SSTATE_MIRROR_URL and exports the config value.
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda p: True)
    monkeypatch.setenv("BAKAR_SSTATE_MIRROR_URL", "http://env-hub/sstate")
    r = check_sstate_mirror_shared_mount(_cfg(sstate_mirror_url=None, sstate_dir="/srv/sstate"))
    assert r.status == Status.PASS


def test_http_mirror_no_sstate_dir_skips() -> None:
    r = check_sstate_mirror_shared_mount(_cfg(sstate_mirrors="http://hub/PATH", sstate_dir=None))
    assert r.status == Status.SKIP


def test_no_mirror_passes() -> None:
    r = check_sstate_mirror_shared_mount(_cfg(sstate_mirrors=None, sstate_dir="/srv/sstate"))
    assert r.status == Status.PASS


# --- stale tarballs ---------------------------------------------------------


def _touch(path: Path, age_s: float) -> None:
    path.write_text("x")
    import time

    t = time.time() - age_s
    os.utime(path, (t, t))


def test_no_stale_tarballs_passes(tmp_path: Path) -> None:
    _touch(tmp_path / "git2_a.tar.gz", 7200)
    r = check_store_stale_tarballs(_cfg(dl_dir=str(tmp_path), cache_push_target=None))
    assert r.status == Status.PASS


def test_25_stale_tarballs_warn_and_list_ten(tmp_path: Path) -> None:
    for i in range(25):
        _touch(tmp_path / f"git2_{i:02d}.tar.gz.tmp", 7200)
    r = check_store_stale_tarballs(_cfg(dl_dir=str(tmp_path), cache_push_target=None))
    assert r.status == Status.FAIL
    assert r.severity == Severity.WARN
    assert "25" in r.message
    listed = [i for i in range(25) if f"git2_{i:02d}.tar.gz.tmp" in r.message]
    assert listed == list(range(10))


def test_fresh_tarball_not_counted(tmp_path: Path) -> None:
    _touch(tmp_path / "git2_new.tar.gz.tmp", 300)
    r = check_store_stale_tarballs(_cfg(dl_dir=str(tmp_path), cache_push_target=None))
    assert r.status == Status.PASS


def test_nested_tarball_ignored(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    _touch(sub / "git2_deep.tar.gz.tmp", 7200)
    r = check_store_stale_tarballs(_cfg(dl_dir=str(tmp_path), cache_push_target=None))
    assert r.status == Status.PASS


def test_pusher_host_skipped(tmp_path: Path) -> None:
    _touch(tmp_path / "git2_a.tar.gz.tmp", 7200)
    r = check_store_stale_tarballs(_cfg(dl_dir=str(tmp_path), cache_push_target="hub:/x"))
    assert r.status == Status.SKIP


def test_no_dl_dir_skipped() -> None:
    r = check_store_stale_tarballs(_cfg(dl_dir=None, cache_push_target=None))
    assert r.status == Status.SKIP


# --- registration -----------------------------------------------------------


def test_rows_registered() -> None:
    names = {"cache-push-service", "sstate-mirror-shared-mount", "store-stale-tarballs"}
    assert names <= {n for _g, ns in diagnostics.CHECK_GROUPS for n in ns}
    assert names <= set(diagnostics._CHECK_SEVERITY)
    for fn in (check_cache_push_service, check_sstate_mirror_shared_mount, check_store_stale_tarballs):
        assert fn in diagnostics.SHARED_CHECKS
    assert diagnostics._CHECK_SEVERITY["sstate-mirror-shared-mount"] == Severity.BLOCK


def test_overlay_derived_http_mirror_over_nfs_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda p: True)
    r = check_sstate_mirror_shared_mount(_cfg(sstate_mirror_url="http://hub/sstate", sstate_dir="/srv/sstate"))
    assert r.status == Status.FAIL
    assert r.severity == Severity.BLOCK


def test_overlay_derived_http_mirror_local_dir_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda p: False)
    r = check_sstate_mirror_shared_mount(_cfg(sstate_mirror_url="https://hub/sstate", sstate_dir="/srv/sstate"))
    assert r.status == Status.PASS


def test_explicit_file_mirror_beats_overlay_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda p: True)
    r = check_sstate_mirror_shared_mount(
        _cfg(
            sstate_mirrors="file://.* file:///mnt/PATH", sstate_mirror_url="http://hub/sstate", sstate_dir="/srv/sstate"
        )
    )
    assert r.status == Status.PASS
