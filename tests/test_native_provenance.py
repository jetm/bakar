"""Tests for bakar.native_provenance, using real temporary git repositories."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from bakar import native_provenance as np_
from bakar.pin_state import commit_distance

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _make_repo(parent: Path, name: str) -> Path:
    repo = parent / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _commit(repo, "one")
    return repo


def _commit(repo: Path, msg: str) -> str:
    (repo / "f.txt").write_text(msg)
    _git(repo, "add", "f.txt")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    w = tmp_path / "ws"
    _make_repo(w / "sources", "bitbake")
    _make_repo(w / "sources", "openembedded-core")
    _make_repo(w / "layers", "meta-avocado")
    return w


def _snap(ws: Path) -> np_.RepoSnapshot:
    return np_.snapshot_repos(ws, ws, probe_timeout=10.0)


def _write(sstate: Path, snap: np_.RepoSnapshot, run_id: str = "r1", now: datetime = T0) -> Path | None:
    return np_.write_record(sstate, "scarthgap", snap, node="n1", run_id=run_id, outcome="ok", now=now)


def test_snapshot_discovers_and_flags_core(ws: Path) -> None:
    snap = _snap(ws)
    by = {r.name: r for r in snap.repos}
    assert set(by) == {"bitbake", "openembedded-core", "meta-avocado"}
    assert by["bitbake"].core and by["openembedded-core"].core
    assert not by["meta-avocado"].core
    assert all(r.sha and r.dirty is False and not r.unreadable for r in snap.repos)


def test_record_created_on_first_write(ws: Path, tmp_path: Path) -> None:
    sstate = tmp_path / "sstate"
    path = _write(sstate, _snap(ws))
    assert path is not None and path.is_file()
    recs, bad = np_.load_records(sstate, "scarthgap")
    assert bad == [] and len(recs) == 1
    assert recs[0].release == "scarthgap" and recs[0].last_node == "n1"
    assert recs[0].repos["bitbake"]["core"] is True


def test_identical_second_write_updates_same_file(ws: Path, tmp_path: Path) -> None:
    sstate = tmp_path / "sstate"
    snap = _snap(ws)
    p1 = _write(sstate, snap, "r1", T0)
    p2 = _write(sstate, snap, "r2", T0 + timedelta(hours=1))
    assert p1 == p2
    assert p1 is not None
    assert len(list(p1.parent.glob("*.json"))) == 1
    (rec,), _ = np_.load_records(sstate, "scarthgap")
    assert rec.last_run_id == "r2"
    assert rec.first_seen == T0.strftime("%Y-%m-%dT%H:%M:%S%z")
    assert rec.last_seen != rec.first_seen


def test_moving_a_layer_yields_two_files(ws: Path, tmp_path: Path) -> None:
    sstate = tmp_path / "sstate"
    _write(sstate, _snap(ws))
    _commit(ws / "layers" / "meta-avocado", "two")
    _write(sstate, _snap(ws), "r2")
    recs, _ = np_.load_records(sstate, "scarthgap")
    assert len(recs) == 2


def test_unknown_release_bucket(ws: Path, tmp_path: Path) -> None:
    sstate = tmp_path / "sstate"
    path = np_.write_record(sstate, None, _snap(ws), node="n", run_id="r", outcome="ok", now=T0)
    assert path is not None and path.parent.name == "_unknown"
    recs, _ = np_.load_records(sstate, None)
    assert len(recs) == 1


def test_no_readable_repo_writes_nothing(tmp_path: Path) -> None:
    empty = np_.RepoSnapshot(repos=())
    assert _write(tmp_path / "sstate", empty) is None
    assert not (tmp_path / "sstate").exists()


def test_orphan_tmp_ignored_and_malformed_reported(ws: Path, tmp_path: Path) -> None:
    sstate = tmp_path / "sstate"
    path = _write(sstate, _snap(ws))
    assert path is not None
    (path.parent / ".x.json.tmp").write_text("{}")
    (path.parent / "bad.json").write_text("{not json")
    (path.parent / "norepos.json").write_text('{"schema": 1}')
    recs, bad = np_.load_records(sstate, "scarthgap")
    assert len(recs) == 1
    assert sorted(bad) == ["bad.json", "norepos.json"]


def test_forecast_no_records(ws: Path) -> None:
    fc = np_.forecast(_snap(ws), [], distance=commit_distance)
    assert fc.kind == "no_records" and fc.record is None and not fc.core_moved


def test_forecast_exact(ws: Path, tmp_path: Path) -> None:
    sstate = tmp_path / "sstate"
    _write(sstate, _snap(ws))
    recs, _ = np_.load_records(sstate, "scarthgap")
    fc = np_.forecast(_snap(ws), recs, distance=commit_distance)
    assert fc.kind == "exact" and fc.record is not None and fc.diffs == ()


def test_forecast_only_bitbake_moved(ws: Path, tmp_path: Path) -> None:
    sstate = tmp_path / "sstate"
    _write(sstate, _snap(ws))
    recs, _ = np_.load_records(sstate, "scarthgap")
    _commit(ws / "sources" / "bitbake", "two")
    _commit(ws / "sources" / "bitbake", "three")
    fc = np_.forecast(_snap(ws), recs, distance=commit_distance)
    assert fc.kind == "nearest" and fc.core_moved is True
    (d,) = fc.diffs
    assert d.name == "bitbake" and d.core and d.ahead == 2 and d.behind == 0


def test_forecast_only_layer_moved(ws: Path, tmp_path: Path) -> None:
    sstate = tmp_path / "sstate"
    _write(sstate, _snap(ws))
    recs, _ = np_.load_records(sstate, "scarthgap")
    _commit(ws / "layers" / "meta-avocado", "two")
    fc = np_.forecast(_snap(ws), recs, distance=commit_distance)
    assert fc.core_moved is False
    assert [d.name for d in fc.diffs] == ["meta-avocado"]


def test_forecast_picks_nearest_record(ws: Path, tmp_path: Path) -> None:
    sstate = tmp_path / "sstate"
    _write(sstate, _snap(ws), "old", T0)
    _commit(ws / "layers" / "meta-avocado", "two")
    _write(sstate, _snap(ws), "mid", T0 + timedelta(hours=1))
    _commit(ws / "sources" / "bitbake", "two")
    _commit(ws / "sources" / "openembedded-core", "two")
    _write(sstate, _snap(ws), "far", T0 + timedelta(hours=2))
    _commit(ws / "layers" / "meta-avocado", "three")
    recs, _ = np_.load_records(sstate, "scarthgap")
    fc = np_.forecast(_snap(ws), recs, distance=commit_distance)
    assert fc.record is not None and fc.record.last_run_id == "far"
    assert [d.name for d in fc.diffs] == ["meta-avocado"]


def test_forecast_recorded_sha_absent_gives_none_distance(ws: Path, tmp_path: Path) -> None:
    sstate = tmp_path / "sstate"
    _write(sstate, _snap(ws))
    recs, _ = np_.load_records(sstate, "scarthgap")
    # Point the record at a sha the checkout does not contain.
    recs[0].repos["bitbake"]["sha"] = "f" * 40
    fc = np_.forecast(_snap(ws), recs, distance=commit_distance)
    (d,) = fc.diffs
    assert d.name == "bitbake" and d.ahead is None and d.behind is None


def test_forecast_lists_dirty(ws: Path) -> None:
    (ws / "layers" / "meta-avocado" / "f.txt").write_text("edited")
    snap = _snap(ws)
    fc = np_.forecast(snap, [], distance=commit_distance)
    assert fc.dirty == ("meta-avocado",)


def test_probe_timeout_marks_unreadable(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=0.1)

    monkeypatch.setattr(np_.subprocess, "run", boom)
    snap = np_.snapshot_repos(ws, ws, probe_timeout=0.1)
    assert snap.repos and all(r.unreadable and r.sha is None for r in snap.repos)
    fc = np_.forecast(snap, [], distance=commit_distance)
    assert set(fc.unreadable) == {r.name for r in snap.repos}


def test_bad_sha_record_listed_unreadable_and_never_reaches_git(ws: Path, tmp_path: Path) -> None:
    sstate = tmp_path / "sstate"
    path = _write(sstate, _snap(ws))
    assert path is not None
    victim = tmp_path / "victim"
    evil = path.parent / "evil.json"
    evil.write_text(
        json.dumps({"schema": 1, "repos": {"bitbake": {"sha": f"--output={victim}", "dirty": False, "core": True}}})
    )
    recs, bad = np_.load_records(sstate, "scarthgap")
    assert bad == ["evil.json"]
    assert [r.file for r in recs] == [path.name]
    np_.forecast(_snap(ws), recs, distance=commit_distance)
    assert not list(tmp_path.glob("victim*"))


def test_forecast_never_creates_files_from_option_like_sha(ws: Path, tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    snap = _snap(ws)
    repos = {r.name: {"sha": r.sha, "dirty": False, "core": r.core} for r in snap.repos}
    repos["bitbake"] = {"sha": f"--output={victim}", "dirty": False, "core": True}
    rec = np_.Record("scarthgap", repos, "", "", "", "", "", "x.json")
    np_.forecast(snap, [rec], distance=commit_distance)
    assert not list(tmp_path.glob("victim*"))


def test_same_basename_repos_get_unique_names_and_round_trip_exact(tmp_path: Path) -> None:
    ws_ = tmp_path / "ws"
    bsp = tmp_path / "bsp"
    _make_repo(ws_ / "layers", "meta-foo")
    _make_repo(bsp / "layers", "meta-foo")
    _make_repo(ws_ / "layers", "meta-bar")
    snap = np_.snapshot_repos(ws_, bsp, probe_timeout=10.0)
    names = [r.name for r in snap.repos]
    assert len(names) == len(set(names)) == 3
    assert "meta-bar" in names
    assert np_.snapshot_repos(ws_, bsp, probe_timeout=10.0) == snap
    sstate = tmp_path / "sstate"
    assert _write(sstate, snap) is not None
    recs, bad = np_.load_records(sstate, "scarthgap")
    assert not bad
    assert len(recs[0].repos) == 3
    assert np_.forecast(snap, recs, distance=commit_distance).kind == "exact"


def _ns_dir(sstate: Path) -> Path:
    return sstate / ".bakar" / "native-provenance"


def test_write_refuses_symlinked_release_dir(ws: Path, tmp_path: Path) -> None:
    sstate = tmp_path / "sstate"
    _ns_dir(sstate).mkdir(parents=True)
    private = tmp_path / "private"
    private.mkdir()
    private.chmod(0o700)
    (_ns_dir(sstate) / "scarthgap").symlink_to(private)
    assert _write(sstate, _snap(ws)) is None
    assert list(private.iterdir()) == []
    assert private.stat().st_mode & 0o777 == 0o700


def test_write_refuses_symlinked_native_provenance_dir(ws: Path, tmp_path: Path) -> None:
    sstate = tmp_path / "sstate"
    (sstate / ".bakar").mkdir(parents=True)
    private = tmp_path / "private"
    private.mkdir()
    private.chmod(0o700)
    _ns_dir(sstate).symlink_to(private)
    assert _write(sstate, _snap(ws)) is None
    assert list(private.iterdir()) == []
    assert private.stat().st_mode & 0o777 == 0o700


def test_write_never_narrows_group_writable_dirs(ws: Path, tmp_path: Path) -> None:
    sstate = tmp_path / "sstate"
    root = _ns_dir(sstate) / "scarthgap"
    root.mkdir(parents=True)
    for d in (sstate / ".bakar", _ns_dir(sstate), root):
        d.chmod(0o775)
    assert _write(sstate, _snap(ws)) is not None
    for d in (sstate / ".bakar", _ns_dir(sstate), root):
        assert d.stat().st_mode & 0o7777 == 0o775


def test_write_widens_restrictive_dirs_to_readable(ws: Path, tmp_path: Path) -> None:
    sstate = tmp_path / "sstate"
    root = _ns_dir(sstate) / "scarthgap"
    root.mkdir(parents=True)
    root.chmod(0o700)
    assert _write(sstate, _snap(ws)) is not None
    assert root.stat().st_mode & 0o755 == 0o755


def test_snapshot_budget_exhausted_marks_rest_unreadable_without_git(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter(range(0, 1000, 40))
    monkeypatch.setattr(np_.time, "monotonic", lambda: float(next(ticks)))
    real_run = subprocess.run
    calls: list[list[str]] = []

    def counting_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return real_run(cmd, **kw)

    monkeypatch.setattr(np_.subprocess, "run", counting_run)
    snap = np_.snapshot_repos(ws, ws, probe_timeout=10.0, total_budget=60.0)
    assert len(snap.repos) == 3
    assert [r.unreadable for r in snap.repos].count(True) == 2
    assert len(calls) == 2
    assert all(r.sha is None for r in snap.repos if r.unreadable)


def test_write_failure_leaves_no_tmp_orphan(ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sstate = tmp_path / "sstate"

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(np_.os, "replace", boom)
    assert _write(sstate, _snap(ws)) is None
    root = _ns_dir(sstate) / "scarthgap"
    assert list(root.glob(".*tmp")) == []
    assert list(root.iterdir()) == []


def test_planted_orphan_tmp_not_loaded(ws: Path, tmp_path: Path) -> None:
    sstate = tmp_path / "sstate"
    path = _write(sstate, _snap(ws))
    assert path is not None
    (path.parent / f".{path.stem}.abc.json.tmp").write_text("{}")
    recs, bad = np_.load_records(sstate, "scarthgap")
    assert len(recs) == 1 and bad == []


@pytest.mark.parametrize(
    "body",
    [
        {"repos": {"bitbake": {"sha": "a" * 40, "dirty": False, "core": True}}},
        {"schema": 2, "last_seen": "t", "repos": {"bitbake": {"sha": "a" * 40, "dirty": False, "core": True}}},
        {"schema": 1, "last_seen": "t", "repos": {"bitbake": {"sha": "a" * 40, "dirty": "no", "core": True}}},
        {"schema": 1, "last_seen": "t", "repos": {"bitbake": {"sha": "a" * 40, "core": True}}},
        {"schema": 1, "last_seen": "", "repos": {"bitbake": {"sha": "a" * 40, "dirty": False, "core": True}}},
    ],
)
def test_record_missing_schema_or_flags_is_unreadable(ws: Path, tmp_path: Path, body: dict[str, Any]) -> None:
    sstate = tmp_path / "sstate"
    path = _write(sstate, _snap(ws))
    assert path is not None
    (path.parent / "partial.json").write_text(json.dumps(body))
    recs, bad = np_.load_records(sstate, "scarthgap")
    assert bad == ["partial.json"]
    assert [r.file for r in recs] == [path.name]


def test_deeply_nested_record_is_unreadable_not_a_crash(ws: Path, tmp_path: Path) -> None:
    sstate = tmp_path / "sstate"
    path = _write(sstate, _snap(ws))
    assert path is not None
    (path.parent / "deep.json").write_text("[" * 200_000)
    recs, bad = np_.load_records(sstate, "scarthgap")
    assert bad == ["deep.json"]
    assert len(recs) == 1


@pytest.mark.parametrize("key", ["../../outside", "/abs", "a/b", "..", ".hidden", "x" * 65])
def test_release_key_that_is_not_one_component_maps_to_unknown(ws: Path, tmp_path: Path, key: str) -> None:
    sstate = tmp_path / "sstate"
    path = np_.write_record(sstate, key, _snap(ws), node="n1", run_id="r1", outcome="ok", now=T0)
    assert path is not None
    assert path.parent == _ns_dir(sstate) / "_unknown"
    recs, bad = np_.load_records(sstate, key)
    assert bad == [] and len(recs) == 1
    assert not (tmp_path / "outside").exists()
