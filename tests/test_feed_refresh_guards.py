"""Refresh guards: every refusal lands before the first write to feed content."""

from __future__ import annotations

import fcntl
from pathlib import Path
from types import SimpleNamespace

import pytest

from bakar import feed, run_discovery
from bakar.commands import _feed_refresh as refresh
from bakar.diagnostics import Status

pytestmark = pytest.mark.unit


def _snapshot(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def _guards(tmp_path: Path, **over: object) -> None:
    feed_dir = tmp_path / "feed"
    kwargs: dict[str, object] = {
        "workspace": tmp_path / "ws",
        "feed_root": feed_dir,
        "sync_stage_root": tmp_path / "feed-stage",
        "delta_stage_root": feed_dir / "delta",
        "release": "2026",
        "channel": "edge",
    }
    kwargs.update(over)
    refresh.check_guards(**kwargs)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_live_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_discovery, "live_workspace_runs", lambda *_a, **_k: [])


def test_stage_root_equal_to_synced_root_is_refused(tmp_path: Path) -> None:
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    before = _snapshot(tmp_path)
    with pytest.raises(refresh.RefreshRefusedError, match="synced feed root"):
        _guards(tmp_path, delta_stage_root=feed_dir)
    assert _snapshot(tmp_path) == before


def test_stage_root_equal_to_sync_stage_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(refresh.RefreshRefusedError, match="synced"):
        _guards(tmp_path, delta_stage_root=tmp_path / "feed-stage")


def test_stage_root_reaching_synced_root_through_symlink_is_refused(tmp_path: Path) -> None:
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(feed_dir)
    with pytest.raises(refresh.RefreshRefusedError, match="synced feed root"):
        _guards(tmp_path, delta_stage_root=link)


def test_default_delta_stage_root_passes(tmp_path: Path) -> None:
    _guards(tmp_path)


def test_mirror_marker_in_any_subrepo_is_refused(tmp_path: Path) -> None:
    sub = tmp_path / "feed" / "delta" / "2026" / "edge" / "target" / "qemux86-64"
    sub.mkdir(parents=True)
    (sub / feed.MIRROR_MARKER).write_text("{}", encoding="utf-8")
    before = _snapshot(tmp_path)
    with pytest.raises(refresh.RefreshRefusedError, match="feed mirror"):
        _guards(tmp_path)
    assert _snapshot(tmp_path) == before


def test_delta_without_marker_passes(tmp_path: Path) -> None:
    sub = tmp_path / "feed" / "delta" / "2026" / "edge" / "target" / "qemux86-64"
    sub.mkdir(parents=True)
    _guards(tmp_path)


def test_live_build_is_refused_and_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []

    def live(path: Path, **_k: object) -> list[object]:
        seen.append(path)
        return [SimpleNamespace(run_dir=Path("/runs/abc"))]

    monkeypatch.setattr(run_discovery, "live_workspace_runs", live)
    with pytest.raises(refresh.RefreshRefusedError, match="abc"):
        _guards(tmp_path)
    assert seen == [tmp_path / "ws"]


def test_lock_held_by_another_process_is_refused_without_writing(tmp_path: Path) -> None:
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    with refresh.refresh_lock(feed_dir):
        before = _snapshot(tmp_path)
        with pytest.raises(refresh.RefreshRefusedError, match="another refresh"), refresh.refresh_lock(feed_dir):
            pytest.fail("second holder must not enter")
        assert _snapshot(tmp_path) == before


def test_lock_is_real_flock_and_released_on_exit(tmp_path: Path) -> None:
    feed_dir = tmp_path / "feed"
    with refresh.refresh_lock(feed_dir):
        probe = refresh.lock_path(feed_dir).open("a+")
        with pytest.raises(BlockingIOError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        probe.close()
    with refresh.refresh_lock(feed_dir):
        pass


def test_lock_lives_beside_the_feed_root_and_never_writes_into_it(tmp_path: Path) -> None:
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    with refresh.refresh_lock(feed_dir):
        assert _snapshot(feed_dir) == []
        assert refresh.lock_path(feed_dir) == tmp_path / "feed-delta.lock"
        assert refresh.lock_path(feed_dir).is_file()


def test_lock_does_not_create_an_absent_feed_root(tmp_path: Path) -> None:
    feed_dir = tmp_path / "feed"
    with refresh.refresh_lock(feed_dir):
        assert not feed_dir.exists()


def test_refused_lock_leaves_holder_released_after_guard_failure(tmp_path: Path) -> None:
    feed_dir = tmp_path / "feed"
    with pytest.raises(refresh.RefreshRefusedError), refresh.refresh_lock(feed_dir):
        _guards(tmp_path, delta_stage_root=feed_dir)
    with refresh.refresh_lock(feed_dir):
        pass


def _scripts(tmp_path: Path) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("render-pool-local.py", "repo-stage-rpms.sh"):
        (scripts / name).write_text("#!/bin/sh\n", encoding="utf-8")
        (scripts / name).chmod(0o755)
    return scripts


def test_preflight_before_first_build_needs_no_deploy_dir(tmp_path: Path) -> None:
    results = refresh.preflight_results(
        scripts=_scripts(tmp_path),
        deploy_dir=None,
        roots=feed.FeedRoots(feed_root=tmp_path / "feed", stage_root=tmp_path / "feed" / "delta"),
        release="2026",
        channel="edge",
    )
    names = {r.name for r in results}
    assert "build output" not in names
    assert "avocado-repo.map" not in names
    assert "render-pool-local.py" in names
    failed = [
        r.name for r in results if r.status is Status.FAIL and r.name in {"render-pool-local.py", "repo-stage-rpms.sh"}
    ]
    assert failed == []


def test_preflight_before_first_build_still_reports_missing_scripts(tmp_path: Path) -> None:
    results = refresh.preflight_results(scripts=None, deploy_dir=None, roots=None, release="2026", channel="edge")
    assert any(r.name == "meta-avocado" and r.status is Status.FAIL for r in results)


def test_preflight_after_build_checks_build_output(tmp_path: Path) -> None:
    results = refresh.preflight_results(
        scripts=_scripts(tmp_path),
        deploy_dir=tmp_path / "deploy" / "rpm",
        roots=None,
        release="2026",
        channel="edge",
    )
    bad = [r for r in results if r.name == "build output"]
    assert len(bad) == 1
    assert bad[0].status is Status.FAIL
