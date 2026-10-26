"""Tests for the workspace pin-sync script, run through local bash."""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bakar.steps import pin_probe, pin_sync
from bakar.steps.pin_sync import (
    SyncReport,
    SyncRow,
    parse_revert,
    parse_sync,
    render_revert,
    render_sync,
    revert_script,
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
    lines = render_sync(report, [e], workspace=fx.ws)
    assert lines[0] == f"MOVED alpha main {old[:10]} -> {e.pin[:10]}"
    assert lines[-1] == "batch complete: 1/1 repos, moved=1 failed=0"


def test_sync_second_run_is_noop(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    fx.run([e])
    _, _, _, report = fx.run([e])
    assert [r.status for r in report.rows] == ["AT_PIN"]
    assert report.revert_path is None
    assert render_sync(report, [e], workspace=fx.ws)[0] == "AT PIN alpha"


def test_sync_treats_an_uppercase_pin_like_its_lowercase_form(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    shouted = PinnedRepo(e.name, e.url, e.pin.upper(), e.path, None)
    _, _, _, first = fx.run([shouted])
    _, _, _, second = fx.run([shouted])
    assert [r.status for r in first.rows] == ["MOVED"]
    assert [r.status for r in second.rows] == ["AT_PIN"]
    assert _git(e.path, "rev-parse", "HEAD") == e.pin


def test_sync_reports_local_changes_on_a_clone_already_at_its_pin(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    fx.run([e])
    (e.path / "f.txt").write_text("local\n")
    _, _, _, report = fx.run([e])
    assert report.rows[0].status == "AT_PIN"
    assert render_sync(report, [e], workspace=fx.ws)[0] == "AT PIN alpha (has local changes)"
    assert (e.path / "f.txt").read_text() == "local\n"


def test_sync_leaves_dirty_clone(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    (e.path / "f.txt").write_text("local\n")
    _, _, _, report = fx.run([e])
    assert report.rows[0].status == "DIRTY"
    assert _git(e.path, "rev-parse", "HEAD") == old
    assert (e.path / "f.txt").read_text() == "local\n"
    assert render_sync(report, [e], workspace=fx.ws)[0] == "LEFT alpha has local changes, not moved"


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
    assert render_sync(report, [e], workspace=fx.ws)[0] == "MISSING alpha (kas clones it at build time)"


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
    assert report.refused == f"a bakar build or shell session holds {fx.ws}/.bakar/repos-in-use.lock"
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
    # The fifth field is the pin the sync set, which revert checks the clone still sits at.
    assert record.read_text() == f"alpha\t{e.path}\t{old}\tmain\t{e.pin}\n"
    assert out.index("BAKAR_SYNC_REVERT") < out.index("\tMOVED\t")


def test_sync_revert_record_marks_detached_head(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    _git(e.path, "checkout", "-q", "--detach", old)
    _, _, _, report = fx.run([e])
    assert report.rows[0].branch is None
    assert Path(report.revert_path or "").read_text().endswith(f"{old}\t-\t{e.pin}\n")
    assert render_sync(report, [e], workspace=fx.ws)[0].startswith("MOVED alpha - ")


def test_sync_names_build_dirs_pinning_old_commit_next_to_its_row(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    # Three commits, so beta's HEAD differs from alpha's even when both upstreams hash alike.
    up, shas = _upstream(fx.root, "beta", commits=3)
    _clone(up, fx.ws / "beta")
    later = PinnedRepo("beta", f"file://{up}", shas[0], fx.ws / "beta", None)
    old = _git(e.path, "rev-parse", "HEAD")
    for name, text in (("build-a", f"commit: {old}\n"), ("build-b", "commit: other\n")):
        d = fx.ws / name
        d.mkdir()
        (d / "avocado-bakar.yml").write_text(text)
    _, _, _, report = fx.run([e, later])
    assert report.notes == [(0, "build-a")]
    lines = render_sync(report, [e, later], workspace=fx.ws)
    assert lines[0].startswith("MOVED alpha ")
    assert lines[1] == f"build dirs pinning alpha at {old[:12]}: build-a"
    assert lines[2].startswith("MOVED beta ")


def test_sync_hostile_values_never_embedded(fx: _Fixture) -> None:
    good = fx.entry("alpha")
    hostile = [
        PinnedRepo("shortpin", "file:///x", "abcz", fx.ws / "shortpin", None),
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
    assert render_sync(report, entries, workspace=fx.ws)[-1] == "batch complete: 3/3 repos, moved=1 failed=0"


def test_parse_sync_incomplete_when_done_line_missing() -> None:
    e = PinnedRepo("alpha", "file:///x", "a" * 40, Path("/nope"), None)
    report = parse_sync("", [e], {}, 1)
    assert not report.complete
    assert (report.examined, report.total) == (0, 1)


@pytest.mark.parametrize("bad", ["7", "-1", "1"])
def test_parse_sync_drops_rows_and_notes_with_an_index_outside_the_entries(bad: str, tmp_path: Path) -> None:
    # A garbled or hostile host line must not index the caller's list: 7 raises, -1 silently
    # picks the last entry, and 1 names an entry that was rejected up front and never sent.
    entries = [
        PinnedRepo("alpha", "file:///x", "a" * 40, tmp_path / "alpha", None),
        PinnedRepo("tabby", "file:///x", "b" * 40, tmp_path / "tabby", None),
    ]
    out = (
        f"BAKAR_SYNC\t0\tAT_PIN\t{'a' * 40}\t-\t-\n"
        f"BAKAR_SYNC\t{bad}\tMOVED\t{'c' * 40}\t{'d' * 40}\t-\n"
        f"BAKAR_SYNC_NOTE\t{bad}\t{tmp_path / 'build'}\n"
        "BAKAR_SYNC_DONE\t1\t1\n"
    )
    report = parse_sync(out, entries, {1: "name or path contains a tab or newline"}, 0)
    assert [r.index for r in report.rows] == [0, 1]
    assert [r.status for r in report.rows] == ["AT_PIN", "FAIL"]
    assert report.notes == []
    assert render_sync(report, entries, workspace=tmp_path)[-1].startswith("batch complete")


def test_run_host_uses_module_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    from bakar.steps import remote_dispatch

    seen: dict = {}

    def fake(host, script, **kwargs):
        seen.update(host=host, kwargs=kwargs)
        return subprocess.CompletedProcess([], 0, stdout="out", stderr="err")

    monkeypatch.setattr(remote_dispatch, "_ssh_bash_capture", fake)
    assert pin_sync.run_host("h", "true") == (0, "out", "err")
    assert seen["kwargs"]["timeout"] == 3600.0


def _revert(fx: _Fixture, lines: list[str]):
    record = fx.root / "record.tsv"
    record.write_text("".join(f"{line}\n" for line in lines))
    rc, out, _ = run_local(revert_script(fx.ws, str(record)))
    return rc, out, parse_revert(out, rc)


def test_revert_restores_branches(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    fx.run([e])
    assert _git(e.path, "rev-parse", "HEAD") == e.pin
    rc, _, report = _revert(fx, [f"alpha\t{e.path}\t{old}\tmain"])
    assert rc == 0
    assert report.rows == [(1, "RESTORED", "on the recorded branch")]
    assert report.complete
    assert _git(e.path, "symbolic-ref", "--short", "HEAD") == "main"
    assert _git(e.path, "rev-parse", "HEAD") == old
    assert render_revert(report) == [
        "RESTORED line 1 on the recorded branch",
        "batch complete: 1/1 repos, restored=1 failed=0",
    ]


def test_revert_detaches_when_branch_moved(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    detached = _git(e.path, "rev-parse", "HEAD~1")
    _git(e.path, "checkout", "-q", "--detach", detached)
    rc, _, report = _revert(fx, [f"alpha\t{e.path}\t{detached}\tmain", f"alpha\t{e.path}\t{old}\t-"])
    assert rc == 0
    assert [(n, s) for n, s, _ in report.rows] == [(1, "DETACHED"), (2, "DETACHED")]
    assert _git(e.path, "rev-parse", "HEAD") == old
    assert _git(e.path, "rev-parse", "main") == old
    assert (
        subprocess.run(["git", "-C", str(e.path), "symbolic-ref", "-q", "HEAD"], env=_ENV, check=False).returncode != 0
    )
    assert render_revert(report)[-1] == "batch complete: 2/2 repos, restored=2 failed=0"


def test_revert_refuses_path_outside_workspace(fx: _Fixture) -> None:
    outside = fx.entry("alpha")
    inside_path = fx.ws / "beta"
    _clone(outside.path.parent.parent / "upstream" / "alpha", inside_path)
    head = _git(inside_path, "rev-parse", "HEAD")
    elsewhere = fx.root / "elsewhere"
    _clone(outside.path.parent.parent / "upstream" / "alpha", elsewhere)
    _git(elsewhere, "checkout", "-q", "--detach", "HEAD~1")
    moved = _git(elsewhere, "rev-parse", "HEAD")
    traversal = f"{fx.ws}/beta/../../elsewhere"
    rc, _, report = _revert(
        fx,
        [
            f"x\t{elsewhere}\t{head}\tmain",
            f"x\t{traversal}\t{head}\tmain",
            f"x\t{inside_path}\t{'z' * 40}\tmain",
            f"x\t{inside_path}\t{head}\tbad..branch",
        ],
    )
    assert rc == 0
    assert [(n, s) for n, s, _ in report.rows] == [(1, "OUTSIDE"), (2, "OUTSIDE"), (3, "INVALID"), (4, "INVALID")]
    assert _git(elsewhere, "rev-parse", "HEAD") == moved
    assert render_revert(report)[-1] == "batch complete: 4/4 repos, restored=0 failed=4"


def test_revert_leaves_dirty_clone(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    fx.run([e])
    (e.path / "f.txt").write_text("local\n")
    _, _, report = _revert(fx, [f"alpha\t{e.path}\t{old}\tmain"])
    assert [(n, s) for n, s, _ in report.rows] == [(1, "DIRTY")]
    assert _git(e.path, "rev-parse", "HEAD") == e.pin
    assert (e.path / "f.txt").read_text() == "local\n"
    assert render_revert(report)[-1] == "batch complete: 1/1 repos, restored=0 failed=1"


def test_revert_leaves_a_clone_that_moved_on_since_the_sync(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    _, _, _, synced = fx.run([e])
    assert synced.revert_path is not None
    (e.path / "f.txt").write_text("kept\n")
    _git(e.path, "add", "f.txt")
    _git(e.path, "commit", "-q", "-m", "work on the detached head")
    commit = _git(e.path, "rev-parse", "HEAD")

    rc, out, _ = run_local(revert_script(fx.ws, synced.revert_path))
    report = parse_revert(out, rc)

    assert [(n, s) for n, s, _ in report.rows] == [(1, "MOVED_ON")]
    assert _git(e.path, "rev-parse", "HEAD") == commit
    assert old != commit
    assert render_revert(report)[-1] == "batch complete: 1/1 repos, restored=0 failed=1"


def test_revert_restores_from_a_sync_record_with_the_pin(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    _, _, _, synced = fx.run([e])
    assert synced.revert_path is not None
    rc, out, _ = run_local(revert_script(fx.ws, synced.revert_path))
    report = parse_revert(out, rc)
    assert [(n, s) for n, s, _ in report.rows] == [(1, "RESTORED")]
    assert _git(e.path, "rev-parse", "HEAD") == old


def test_revert_refuses_while_bitbake_lock_held(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    fx.run([e])
    handle = _hold(fx.ws / "build-a" / "build" / "bitbake.lock")
    try:
        rc, _, report = _revert(fx, [f"alpha\t{e.path}\t{old}\tmain"])
    finally:
        handle.close()
    assert rc == 3
    assert report.refused and "build-a" in report.refused
    assert not report.complete
    assert _git(e.path, "rev-parse", "HEAD") == e.pin


def test_revert_missing_record_is_refused(fx: _Fixture) -> None:
    rc, out, _ = run_local(revert_script(fx.ws, str(fx.root / "nope.tsv")))
    report = parse_revert(out, rc)
    assert rc == 3
    assert report.refused == f"revert record {fx.root / 'nope.tsv'} not found"


_NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


def test_sync_refuses_a_path_nested_below_another_clone(fx: _Fixture) -> None:
    outer = fx.entry("alpha")
    before = _git(outer.path, "rev-parse", "HEAD")
    (outer.path / "sub").mkdir()
    nested = PinnedRepo("nested", outer.url, outer.pin, outer.path / "sub", None)
    _, _, _, report = fx.run([nested])
    assert [(r.status, r.detail) for r in report.rows] == [("FAIL", "path is not the top of a clone")]
    assert _git(outer.path, "rev-parse", "HEAD") == before
    assert report.revert_path is None


def test_sync_refuses_path_outside_workspace(fx: _Fixture) -> None:
    up, shas = _upstream(fx.root, "alpha")
    elsewhere = fx.root / "elsewhere"
    _clone(up, elsewhere)
    before = _git(elsewhere, "rev-parse", "HEAD")
    entries = [
        PinnedRepo("abs", f"file://{up}", shas[0], elsewhere, None),
        PinnedRepo("dots", f"file://{up}", shas[0], fx.ws / ".." / "elsewhere", None),
    ]
    _, _, _, report = fx.run(entries)
    assert [(r.status, r.detail) for r in report.rows] == [("FAIL", "path is not under the workspace")] * 2
    assert _git(elsewhere, "rev-parse", "HEAD") == before
    assert report.revert_path is None


def _ignored_conflict(fx: _Fixture) -> PinnedRepo:
    """A clone on a commit that ignores gen.txt, holding a local gen.txt, pinned to a commit tracking it."""
    up = fx.root / "upstream" / "alpha"
    up.mkdir(parents=True)
    _git(up, "init", "-q", "-b", "main")
    (up / "gen.txt").write_text("tracked\n")
    _git(up, "add", "gen.txt")
    _git(up, "commit", "-q", "-m", "track gen")
    pin = _git(up, "rev-parse", "HEAD")
    _git(up, "rm", "-q", "gen.txt")
    (up / ".gitignore").write_text("gen.txt\n")
    _git(up, "add", ".gitignore")
    _git(up, "commit", "-q", "-m", "ignore gen")
    path = fx.ws / "alpha"
    _clone(up, path)
    (path / "gen.txt").write_text("local build output\n")
    return PinnedRepo("alpha", f"file://{up}", pin, path, None)


def test_sync_does_not_overwrite_an_ignored_file_the_pin_tracks(fx: _Fixture) -> None:
    e = _ignored_conflict(fx)
    old = _git(e.path, "rev-parse", "HEAD")
    _, _, _, report = fx.run([e])
    assert report.rows[0].status == "FAIL"
    assert report.rows[0].detail.startswith("checkout failed:")
    assert (e.path / "gen.txt").read_text() == "local build output\n"
    assert _git(e.path, "rev-parse", "HEAD") == old


def test_revert_restores_a_clone_whose_sync_checkout_failed(fx: _Fixture) -> None:
    e = _ignored_conflict(fx)
    old = _git(e.path, "rev-parse", "HEAD")
    _, _, _, synced = fx.run([e])
    assert synced.rows[0].status == "FAIL"
    assert synced.revert_path is not None
    rc, out, _ = run_local(revert_script(fx.ws, synced.revert_path))
    report = parse_revert(out, rc)
    assert [(n, s) for n, s, _ in report.rows] == [(1, "RESTORED")]
    assert _git(e.path, "rev-parse", "HEAD") == old


def test_sync_ignores_git_status_stderr(fx: _Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_git = shutil.which("git")
    assert real_git is not None
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    stub = stub_dir / "git"
    stub.write_text(
        f'#!/bin/bash\nfor a in "$@"; do [ "$a" = status ] && echo "warning: noise" >&2; done\nexec {real_git} "$@"\n'
    )
    stub.chmod(0o755)
    e = fx.entry("alpha")
    monkeypatch.setenv("PATH", f"{stub_dir}:{os.environ['PATH']}")
    _, _, _, report = fx.run([e])
    assert report.rows[0].status == "MOVED"
    assert _git(e.path, "rev-parse", "HEAD") == e.pin


def test_sync_records_the_branch_when_a_tag_shares_its_name(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    _git(e.path, "tag", "main", e.pin)
    _, _, _, synced = fx.run([e])
    assert synced.rows[0].branch == "main"
    assert synced.revert_path is not None
    assert Path(synced.revert_path).read_text() == f"alpha\t{e.path}\t{old}\tmain\t{e.pin}\n"
    rc, out, _ = run_local(revert_script(fx.ws, synced.revert_path))
    report = parse_revert(out, rc)
    assert [(n, s) for n, s, _ in report.rows] == [(1, "RESTORED")]
    assert _git(e.path, "symbolic-ref", "HEAD") == "refs/heads/main"


def test_revert_accepts_an_uppercase_recorded_head(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    fx.run([e])
    _, _, report = _revert(fx, [f"alpha\t{e.path}\t{old.upper()}\tmain\t{e.pin.upper()}"])
    assert [(n, s) for n, s, _ in report.rows] == [(1, "RESTORED")]
    assert _git(e.path, "rev-parse", "HEAD") == old


def test_revert_checks_the_record_before_creating_locks(fx: _Fixture) -> None:
    ws = fx.root / "nows"
    record = ws / ".bakar" / "sync-revert" / "x.tsv"
    rc, out, _ = run_local(revert_script(ws, str(record)))
    assert rc == 3
    assert parse_revert(out, rc).refused == f"revert record {record} not found"
    assert not ws.exists()


@_NOT_ROOT
def test_revert_refuses_an_unreadable_record(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    _, _, _, synced = fx.run([e])
    assert synced.revert_path is not None
    record = Path(synced.revert_path)
    record.chmod(0)
    try:
        rc, out, _ = run_local(revert_script(fx.ws, str(record)))
    finally:
        record.chmod(0o644)
    report = parse_revert(out, rc)
    assert rc == 3
    assert report.refused == f"revert record {record} is not readable"
    assert not report.complete
    assert _git(e.path, "rev-parse", "HEAD") == e.pin != old


@_NOT_ROOT
def test_sync_reports_a_lock_file_it_cannot_open(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    bakar = fx.ws / ".bakar"
    bakar.mkdir()
    bakar.chmod(0o500)
    try:
        rc, _, _, report = fx.run([e])
    finally:
        bakar.chmod(0o755)
    assert rc == 3
    assert report.refused == f"cannot open {bakar}/repos-in-use.lock"
    assert _git(e.path, "rev-parse", "HEAD") == old


def test_sync_timeout_keeps_partial_rows_and_revert_record(monkeypatch: pytest.MonkeyPatch) -> None:
    old = "b" * 40
    entries = [
        PinnedRepo("alpha", "file:///x", "a" * 40, Path("/ws/alpha"), None),
        PinnedRepo("beta", "file:///x", "c" * 40, Path("/ws/beta"), None),
    ]
    partial = (
        f"BAKAR_SYNC_REVERT\t/ws/.bakar/sync-revert/r.tsv\nBAKAR_SYNC\t0\tMOVED\t{old}\tmain\t-\nBAKAR_SYNC\t1\tMO"
    )
    seen: list[float] = []

    def slow(script: str, *, timeout: float) -> tuple[int, str, str, bool]:
        seen.append(timeout)
        return 255, partial, "", True

    monkeypatch.setattr(pin_probe, "_run_local_bash", slow)
    rc, out, _ = run_local("true")
    report = parse_sync(out, entries, {}, rc)
    assert seen == [3600.0]
    assert not report.complete
    assert [(r.index, r.status) for r in report.rows] == [(0, "MOVED")]
    assert render_sync(report, entries, workspace=Path("/ws")) == [
        f"MOVED alpha main {old[:10]} -> {'a' * 10}",
        "revert record: /ws/.bakar/sync-revert/r.tsv",
        "sync did not finish after 3600s and may still be running; check before re-running",
        "batch complete: 1/2 repos, moved=1 failed=0",
    ]


def _stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, body: str) -> None:
    """Put an executable ``name`` running ``body`` ahead of everything else on PATH."""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / name
    stub.write_text(f"#!/bin/bash\n{body}\n")
    stub.chmod(0o755)
    path = os.environ["PATH"]
    if not path.startswith(f"{stub_dir}:"):
        monkeypatch.setenv("PATH", f"{stub_dir}:{path}")


def _stub_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prelude: str) -> None:
    real_git = shutil.which("git")
    assert real_git is not None
    _stub(tmp_path, monkeypatch, "git", f'{prelude}\nexec {real_git} "$@"')


@_NOT_ROOT
def test_sync_reports_an_unreadable_bitbake_lock_as_unopenable(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    lock = fx.ws / "build-a" / "build" / "bitbake.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    lock.chmod(0)
    try:
        rc, _, _, report = fx.run([e])
    finally:
        lock.chmod(0o644)
    assert rc == 3
    assert report.refused == f"cannot open {lock}"
    assert _git(e.path, "rev-parse", "HEAD") == old
    assert lock.read_text() == ""


def test_sync_reports_an_unreadable_status_at_the_pin_as_a_failure(
    fx: _Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = fx.entry("alpha")
    fx.run([e])
    _stub_git(tmp_path, monkeypatch, 'for a in "$@"; do [ "$a" = status ] && exit 128; done')
    _, _, _, report = fx.run([e])
    assert [(r.status, r.old, r.detail) for r in report.rows] == [("FAIL", e.pin, "cannot read status")]


def test_revert_reports_an_unreadable_head_as_a_failure(
    fx: _Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = fx.entry("alpha")
    _, _, _, synced = fx.run([e])
    assert synced.revert_path is not None
    _stub_git(tmp_path, monkeypatch, '[ "${*: -2}" = "rev-parse HEAD" ] && exit 128')
    rc, out, _ = run_local(revert_script(fx.ws, synced.revert_path))
    report = parse_revert(out, rc)
    assert report.rows == [(1, "FAIL", "cannot read HEAD")]
    assert _git(e.path, "rev-parse", "HEAD") == e.pin


def test_sync_reports_an_abbreviated_pin_already_at_its_commit_as_at_pin(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    fx.run([e])
    assert e.pin is not None
    short = PinnedRepo(e.name, e.url, e.pin[:12], e.path, None)
    assert validate_sync_entries([short]) == {}
    _, _, _, report = fx.run([short])
    assert [r.status for r in report.rows] == ["AT_PIN"]


def test_sync_never_moves_a_clone_to_an_abbreviated_pin(fx: _Fixture) -> None:
    e = fx.entry("alpha")
    old = _git(e.path, "rev-parse", "HEAD")
    assert e.pin is not None
    short = PinnedRepo(e.name, e.url, e.pin[:12], e.path, None)
    _, _, _, report = fx.run([short])
    assert [(r.status, r.detail) for r in report.rows] == [("FAIL", "pin is not a full 40-character commit id")]
    assert _git(e.path, "rev-parse", "HEAD") == old
    assert report.revert_path is None


def _colliding_clone(path: Path) -> tuple[str, list[str]]:
    """A clean clone at ``path`` holding two commits whose ids share a 4-hex prefix.

    Returns ``(prefix, [the commit HEAD sits at, the other one])``.
    """
    _git(path.parent, "init", "-q", "-b", "main", str(path))
    stream = [
        f"commit refs/heads/main\ncommitter t <t@example.com> {1700000000 + i} +0000\ndata 1\n{i % 10}\n"
        for i in range(1500)
    ]
    subprocess.run(
        ["git", "-C", str(path), "fast-import", "--quiet"],
        input="".join(stream),
        check=True,
        capture_output=True,
        text=True,
        env=_ENV,
    )
    by_prefix: dict[str, list[str]] = {}
    for sha in _git(path, "rev-list", "main").split():
        by_prefix.setdefault(sha[:4], []).append(sha)
    prefix, shas = next((p, s) for p, s in by_prefix.items() if len(s) > 1)
    _git(path, "checkout", "-q", "--detach", shas[0])
    return prefix, shas[:2]


def test_sync_does_not_report_an_ambiguous_abbreviated_pin_as_at_pin(fx: _Fixture) -> None:
    path = fx.ws / "alpha"
    prefix, (head, _other) = _colliding_clone(path)
    short = PinnedRepo("alpha", f"file://{path}", prefix, path, None)

    _, _, _, report = fx.run([short])

    # git cannot resolve the prefix to one commit, so neither can kas: HEAD merely starting with it proves nothing.
    assert [(r.status, r.detail) for r in report.rows] == [("FAIL", "pin is not a full 40-character commit id")]
    assert _git(path, "rev-parse", "HEAD") == head


def test_sync_never_follows_a_planted_revert_record(
    fx: _Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink waiting at the record's name is neither followed nor appended to."""
    e = fx.entry("alpha")
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched\n")
    revert_dir = fx.ws / ".bakar" / "sync-revert"
    # date runs in the script's command substitution, so its parent is the script and $PPID its $$.
    _stub(
        tmp_path,
        monkeypatch,
        "date",
        f'mkdir -p {revert_dir}\nln -s {victim} "{revert_dir}/20260101T000000-$PPID.tsv"\necho 20260101T000000',
    )
    _, _, _, report = fx.run([e])
    assert any(p.is_symlink() for p in revert_dir.iterdir()), "the plant did not land"
    assert victim.read_text() == "untouched\n"
    assert report.revert_path is not None
    record = Path(report.revert_path)
    assert not record.is_symlink()
    assert record.read_text().startswith(f"alpha\t{e.path}\t")


def test_sync_holds_both_workspace_locks_exclusively_while_moving(
    fx: _Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = fx.entry("alpha")
    probe = tmp_path / "probe.txt"
    bakar_dir = fx.ws / ".bakar"
    checks = " ".join(f"{bakar_dir}/{name}" for name in ("repos-in-use.lock", "repos.lock"))
    _stub_git(
        tmp_path,
        monkeypatch,
        'for a in "$@"; do [ "$a" = checkout ] || continue\n'
        f"    for l in {checks}; do\n"
        f'        if flock -n -s "$l" true; then echo "free $l"; else echo "held $l"; fi >> {probe}\n'
        "    done\ndone",
    )
    _, _, _, report = fx.run([e])
    assert [r.status for r in report.rows] == ["MOVED"]
    assert probe.read_text().splitlines() == [
        f"held {bakar_dir}/repos-in-use.lock",
        f"held {bakar_dir}/repos.lock",
    ]


def test_render_sync_names_the_workspace_in_the_restart_advice() -> None:
    entries = [PinnedRepo("bitbake", "file:///x", "a" * 40, Path("/w s/bitbake"), None)]
    report = SyncReport(refused=None, rows=[SyncRow(0, "MOVED", "b" * 40, "main", "-")], examined=1, total=1)
    local = render_sync(report, entries, workspace=Path("/w s"))
    hosted = render_sync(report, entries, workspace=Path("/w s"), host="pc2")
    advice = [line for line in local if line.startswith("bitbake moved")]
    assert advice == [
        "bitbake moved: restart the servers that loaded the old one with "
        "`bakar hashserv stop -w '/w s'` then `bakar hashserv start -w '/w s'`, and "
        "`bakar prserv stop -w '/w s'` then `bakar prserv start -w '/w s'`"
    ]
    assert [line for line in hosted if line.startswith("bitbake moved")] == [
        advice[0].replace("old one with", "old one, on pc2, with")
    ]


def test_sync_timeout_on_a_host_says_it_may_still_run_there(monkeypatch: pytest.MonkeyPatch) -> None:
    from bakar.steps import remote_dispatch

    timed_out = remote_dispatch.CaptureTimedOut(["ssh"], 255, stdout="BAKAR_SYNC_REVERT\t/r.tsv\n", stderr="x")
    monkeypatch.setattr(remote_dispatch, "_ssh_bash_capture", lambda host, script, **kw: timed_out)
    rc, out, err = pin_sync.run_host("pc2", "true")
    report = parse_sync(out, [], {}, rc)
    assert report.revert_path == "/r.tsv"
    assert (
        report.timed_out == "did not finish after 3600s on pc2 and may still be running there; check before re-running"
    )
    assert err == ""
