"""Tests for the workspace pin-sync script, run through local bash."""

from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path

import pytest

from bakar.steps import pin_sync
from bakar.steps.pin_sync import (
    parse_sync,
    render_sync,
    run_local,
    sync_script,
    validate_sync_entries,
)
from bakar.steps.repo_reuse import PinnedRepo

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True, env=_ENV
    ).stdout.strip()


def _commit(up: Path, text: str) -> str:
    (up / "f.txt").write_text(f"{text}\n")
    _git(up, "add", "f.txt")
    _git(up, "commit", "-q", "-m", text)
    return _git(up, "rev-parse", "HEAD")


def _upstream(root: Path, name: str, commits: int = 2) -> tuple[Path, list[str]]:
    up = root / "upstream" / name
    up.mkdir(parents=True)
    _git(up, "init", "-q", "-b", "main")
    return up, [_commit(up, f"c{i}") for i in range(commits)]


def _clone(up: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", f"file://{up}", str(dest)], check=True, env=_ENV, capture_output=True)


class _Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.ws = tmp_path / "ws"
        self.ws.mkdir()
        self.root = tmp_path

    def entry(self, name: str, clone: bool = True) -> PinnedRepo:
        """A clone sitting on the newest commit of its branch, pinned to the first."""
        up, shas = _upstream(self.root, name)
        path = self.ws / name
        if clone:
            _clone(up, path)
        return PinnedRepo(name, f"file://{up}", shas[0], path, None)

    def run(self, entries: list[PinnedRepo]):
        rejected = validate_sync_entries(entries)
        script = sync_script(self.ws, entries, rejected)
        rc, out, err = run_local(script)
        return rc, out, err, parse_sync(out, entries, rejected, rc)


@pytest.fixture
def fx(tmp_path: Path) -> _Fixture:
    return _Fixture(tmp_path)


def test_sync_moves_clean_off_pin_clone_detached_keeps_branch(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    rc, _, _, report = fx.run([e])
    assert rc == 0
    assert report.rows[0].status == "MOVED"
    assert report.rows[0].branch == "main"
    assert report.rows[0].old == old
    assert _git(e.path, "rev-parse", "HEAD") == e.pin
    # HEAD is detached and the branch ref is untouched.
    assert (
        subprocess.run(["git", "-C", str(e.path), "symbolic-ref", "-q", "HEAD"], env=_ENV, check=False).returncode != 0
    )
    assert _git(e.path, "rev-parse", "main") == old
    lines = render_sync(report, [e])
    assert lines[0] == f"MOVED alpha main {old[:10]} -> {e.pin[:10]}"
    assert lines[-1] == "batch complete: 1/1 repos, moved=1 failed=0"


def test_sync_second_run_is_noop(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    fx.run([e])
    _, _, _, report = fx.run([e])
    assert [r.status for r in report.rows] == ["AT_PIN"]
    assert report.revert_path is None
    assert render_sync(report, [e])[0] == "AT PIN alpha"


def test_sync_leaves_dirty_clone(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    (e.path / "f.txt").write_text("local\n")
    _, _, _, report = fx.run([e])
    assert report.rows[0].status == "DIRTY"
    assert _git(e.path, "rev-parse", "HEAD") == old
    assert (e.path / "f.txt").read_text() == "local\n"
    assert render_sync(report, [e])[0] == "LEFT alpha has local changes, not moved"


def test_sync_fetches_absent_pin_from_url(fx: _Fixture) -> None:
    up, _ = _upstream(fx.root, "alpha", commits=1)
    path = fx.ws / "alpha"
    _clone(up, path)
    pin = _commit(up, "later")
    e = PinnedRepo("alpha", f"file://{up}", pin, path, None)
    probe = ["git", "-C", str(path), "cat-file", "-e", f"{pin}^{{commit}}"]
    assert subprocess.run(probe, env=_ENV, check=False).returncode != 0
    _, _, _, report = fx.run([e])
    assert report.rows[0].status == "MOVED", report.rows[0]
    assert _git(path, "rev-parse", "HEAD") == pin


def test_sync_reports_missing_clone(fx: _Fixture) -> None:
    e = fx.entry("alpha", clone=False)
    _, _, _, report = fx.run([e])
    assert report.rows[0].status == "MISSING"
    assert not e.path.exists()
    assert render_sync(report, [e])[0] == "MISSING alpha (kas clones it at build time)"


def _hold(path: Path, shared: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a")
    fcntl.flock(handle, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
    return handle


def test_sync_refuses_when_bitbake_lock_held(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    handle = _hold(fx.ws / "build-a" / "build" / "bitbake.lock")
    try:
        rc, out, _, report = fx.run([e])
    finally:
        handle.close()
    assert rc == 3
    assert report.refused and "build-a" in report.refused
    assert report.rows == []
    assert _git(e.path, "rev-parse", "HEAD") == old
    assert "BAKAR_SYNC_REFUSED" in out


def test_sync_ignores_stale_bitbake_lock_file(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    lock = fx.ws / "build-a" / "build" / "bitbake.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    rc, _, _, report = fx.run([e])
    assert rc == 0
    assert report.refused is None
    assert report.rows[0].status == "MOVED"


def test_sync_refuses_when_in_use_lock_held_shared(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    handle = _hold(fx.ws / ".bakar" / "repos-in-use.lock", shared=True)
    try:
        rc, _, _, report = fx.run([e])
    finally:
        handle.close()
    assert rc == 3
    assert report.refused and "repos-in-use.lock" in report.refused
    assert _git(e.path, "rev-parse", "HEAD") == old


def test_sync_refuses_when_repos_lock_held(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    handle = _hold(fx.ws / ".bakar" / "repos.lock")
    try:
        rc, _, _, report = fx.run([e])
    finally:
        handle.close()
    assert rc == 3
    assert report.refused and "repos.lock" in report.refused
    assert _git(e.path, "rev-parse", "HEAD") == old


def test_sync_writes_revert_record_before_move(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    rc, out, _, report = fx.run([e])
    assert rc == 0
    assert report.revert_path is not None
    assert out.count("BAKAR_SYNC_REVERT") == 1
    record = Path(report.revert_path)
    assert record.parent == fx.ws / ".bakar" / "sync-revert"
    assert record.read_text() == f"alpha\t{e.path}\t{old}\tmain\n"
    assert out.index("BAKAR_SYNC_REVERT") < out.index("\tMOVED\t")


def test_sync_revert_record_marks_detached_head(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    _git(e.path, "checkout", "-q", "--detach", old)
    _, _, _, report = fx.run([e])
    assert report.rows[0].branch is None
    assert Path(report.revert_path or "").read_text().endswith(f"{old}\t-\n")
    assert render_sync(report, [e])[0].startswith("MOVED alpha - ")


def test_sync_names_build_dirs_pinning_old_commit(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    for name, text in (("build-a", f"commit: {old}\n"), ("build-b", "commit: other\n")):
        d = fx.ws / name
        d.mkdir()
        (d / "avocado-bakar.yml").write_text(text)
    _, _, _, report = fx.run([e])
    assert report.notes == [(0, "build-a")]
    assert f"build dirs pinning alpha at {old[:12]}: build-a" in render_sync(report, [e])


def test_sync_hostile_values_never_embedded(fx: _Fixture) -> None:
    good = fx.entry("alpha")
    hostile = [
        PinnedRepo("shortpin", "file:///x", "abcd", fx.ws / "shortpin", None),
        PinnedRepo("dashurl", "-evil", "a" * 40, fx.ws / "dashurl", None),
        PinnedRepo("tab\tname", "file:///x", "a" * 40, fx.ws / "tabname", None),
        PinnedRepo("nlpath", "file:///x", "a" * 40, fx.ws / "nl\npath", None),
    ]
    entries = [good, *hostile]
    rejected = validate_sync_entries(entries)
    assert sorted(rejected) == [1, 2, 3, 4]
    script = sync_script(fx.ws, entries, rejected)
    for needle in ("shortpin", "dashurl", "-evil", "tabname", "nlpath", "nl\npath"):
        assert needle not in script
    assert "sync_one 1 " not in script
    assert "sync_one 0 " in script
    assert '"$examined" 1\n' in script
    rc, out, _ = run_local(script)
    report = parse_sync(out, entries, rejected, rc)
    assert [(r.index, r.status) for r in report.rows] == [
        (0, "MOVED"),
        (1, "FAIL"),
        (2, "FAIL"),
        (3, "FAIL"),
        (4, "FAIL"),
    ]
    assert (report.examined, report.total) == (5, 5)
    assert report.complete


def test_sync_completion_line_counts_all(fx: _Fixture) -> None:
    a = fx.entry("alpha")
    b = fx.entry("beta", clone=False)
    c = fx.entry("gamma")
    (c.path / "f.txt").write_text("dirty\n")
    entries = [a, b, c]
    rc, out, _, report = fx.run(entries)
    assert rc == 0
    assert "BAKAR_SYNC_DONE\t3\t3\n" in out
    assert report.complete
    assert (report.examined, report.total) == (3, 3)
    assert render_sync(report, entries)[-1] == "batch complete: 3/3 repos, moved=1 failed=0"


def test_parse_sync_incomplete_when_done_line_missing() -> None:
    e = PinnedRepo("alpha", "file:///x", "a" * 40, Path("/nope"), None)
    report = parse_sync("", [e], {}, 1)
    assert not report.complete
    assert (report.examined, report.total) == (0, 1)


def test_run_host_uses_module_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    from bakar.steps import remote_dispatch

    seen: dict = {}

    def fake(host, script, **kwargs):
        seen.update(host=host, kwargs=kwargs)
        return subprocess.CompletedProcess([], 0, stdout="out", stderr="err")

    monkeypatch.setattr(remote_dispatch, "_ssh_bash_capture", fake)
    assert pin_sync.run_host("h", "true") == (0, "out", "err")
    assert seen["kwargs"]["timeout"] == 3600.0
